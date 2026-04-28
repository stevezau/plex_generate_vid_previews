"""End-to-end test: settings survive an app restart.

Real-world correctness check: a user configures servers, the
container restarts (image upgrade, host reboot, scheduled redeploy),
and they expect their configuration to come back identically. This
test exercises the persistence path end-to-end:

1. Start a Flask app pointed at ``CONFIG_DIR=tmp_path``.
2. Configure 3 servers (Plex + Emby + Jellyfin) via the live API.
3. Tear down the app instance + reset the SettingsManager singleton.
4. Re-instantiate a fresh app against the same ``CONFIG_DIR``.
5. List servers — assert all 3 + their auth + libraries + path
   mappings reload identically.

Goes through the real save/load round-trip (no mocking), which catches
serialisation bugs that unit tests in test_settings_manager.py miss.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture
def fresh_config_dir(tmp_path):
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    yield config_dir


def _build_app(config_dir: Path):
    """Construct a Flask app + SettingsManager singleton tied to ``config_dir``."""
    from plex_generate_previews.web.app import create_app
    from plex_generate_previews.web.settings_manager import (
        get_settings_manager,
        reset_settings_manager,
    )

    reset_settings_manager()
    app = create_app(config_dir=str(config_dir))
    app.config["TESTING"] = True
    settings = get_settings_manager()
    return app, settings


@pytest.mark.integration
class TestSettingsPersistsAcrossRestart:
    def test_three_servers_survive_restart(self, fresh_config_dir, monkeypatch):
        """Configure 3 servers, restart, verify all 3 reload."""
        monkeypatch.setenv("CONFIG_DIR", str(fresh_config_dir))
        monkeypatch.setenv("WEB_AUTH_TOKEN", "integration-test-token")

        seed_servers = [
            {
                "id": "uuid-plex",
                "type": "plex",
                "name": "My Plex",
                "enabled": True,
                "url": "http://plex.local:32400",
                "auth": {"method": "token", "token": "plex-tok-123"},
                "verify_ssl": True,
                "timeout": 30,
                "server_identity": "plex-machine-abc",
                "libraries": [
                    {"id": "1", "name": "Movies", "remote_paths": ["/media/Movies"], "enabled": True},
                    {"id": "2", "name": "TV Shows", "remote_paths": ["/media/TV"], "enabled": False},
                ],
                "path_mappings": [{"remote_prefix": "/media", "local_prefix": "/data"}],
                "output": {
                    "adapter": "plex_bundle",
                    "plex_config_folder": "/cfg/plex",
                    "frame_interval": 10,
                },
            },
            {
                "id": "uuid-emby",
                "type": "emby",
                "name": "Office Emby",
                "enabled": True,
                "url": "http://emby.local:8096",
                "auth": {
                    "method": "password",
                    "access_token": "emby-tok-456",
                    "user_id": "emby-user",
                },
                "verify_ssl": False,
                "timeout": 30,
                "server_identity": "emby-srv-xyz",
                "libraries": [{"id": "100", "name": "Movies", "remote_paths": ["/em-media/Movies"], "enabled": True}],
                "path_mappings": [{"remote_prefix": "/em-media", "local_prefix": "/data"}],
                "output": {"adapter": "emby_sidecar", "width": 320, "frame_interval": 10},
            },
            {
                "id": "uuid-jelly",
                "type": "jellyfin",
                "name": "Family Jellyfin",
                "enabled": True,
                "url": "http://jelly.local:8096",
                "auth": {"method": "api_key", "api_key": "jf-api-789"},
                "verify_ssl": True,
                "timeout": 30,
                "server_identity": "jf-srv-pqr",
                "libraries": [{"id": "200", "name": "Movies", "remote_paths": ["/jf-media/Movies"], "enabled": True}],
                "path_mappings": [{"remote_prefix": "/jf-media", "local_prefix": "/data"}],
                "output": {"adapter": "jellyfin_trickplay", "width": 320, "frame_interval": 10},
            },
        ]

        # ----- 1st app instance: write servers -----
        app1, settings1 = _build_app(fresh_config_dir)
        settings1.set("media_servers", seed_servers)
        settings1.set("webhook_secret", "shared-secret")
        settings1.complete_setup()

        # Confirm settings.json was written (real disk persistence).
        settings_file = fresh_config_dir / "settings.json"
        assert settings_file.exists()
        on_disk = json.loads(settings_file.read_text())
        assert len(on_disk.get("media_servers", [])) == 3

        # ----- Tear down + restart -----
        # Drop the Flask app + SettingsManager singleton — equivalent
        # to a process restart from the user's perspective.
        del app1
        del settings1

        # ----- 2nd app instance: same CONFIG_DIR, re-read settings -----
        app2, settings2 = _build_app(fresh_config_dir)
        reloaded = settings2.get("media_servers")

        # Identity-by-id and field-by-field comparison.
        assert isinstance(reloaded, list)
        assert len(reloaded) == 3, [s.get("id") for s in reloaded]

        by_id = {s["id"]: s for s in reloaded}
        for original in seed_servers:
            roundtrip = by_id.get(original["id"])
            assert roundtrip is not None, f"server {original['id']} lost across restart"
            assert roundtrip["type"] == original["type"]
            assert roundtrip["name"] == original["name"]
            assert roundtrip["url"] == original["url"]
            assert roundtrip["auth"] == original["auth"], (
                f"auth lost or mutated for {original['id']}: {roundtrip['auth']!r} vs {original['auth']!r}"
            )
            assert roundtrip["server_identity"] == original["server_identity"]
            assert roundtrip["libraries"] == original["libraries"]
            assert roundtrip["path_mappings"] == original["path_mappings"]
            # Output dict shape preserved.
            for key, value in original["output"].items():
                assert roundtrip["output"].get(key) == value, f"output[{key}] lost for {original['id']}"

        # And the webhook secret survived too.
        assert settings2.get("webhook_secret") == "shared-secret"

    def test_disabled_server_stays_disabled_after_restart(self, fresh_config_dir, monkeypatch):
        """Per-server enable/disable flag is preserved across restart."""
        monkeypatch.setenv("CONFIG_DIR", str(fresh_config_dir))
        monkeypatch.setenv("WEB_AUTH_TOKEN", "integration-test-token")

        app1, settings1 = _build_app(fresh_config_dir)
        settings1.set(
            "media_servers",
            [
                {
                    "id": "uuid-disabled",
                    "type": "emby",
                    "name": "Disabled Emby",
                    "enabled": False,  # explicitly disabled
                    "url": "http://x:8096",
                    "auth": {},
                    "libraries": [],
                    "path_mappings": [],
                    "output": {"adapter": "emby_sidecar"},
                }
            ],
        )
        settings1.complete_setup()
        del app1
        del settings1

        _, settings2 = _build_app(fresh_config_dir)
        servers = settings2.get("media_servers")
        assert len(servers) == 1
        assert servers[0]["enabled"] is False, "disabled flag lost across restart"

    def test_per_library_toggles_preserved(self, fresh_config_dir, monkeypatch):
        """Each library's ``enabled`` toggle survives restart — important
        because the dispatcher uses it for ownership routing."""
        monkeypatch.setenv("CONFIG_DIR", str(fresh_config_dir))
        monkeypatch.setenv("WEB_AUTH_TOKEN", "integration-test-token")

        app1, settings1 = _build_app(fresh_config_dir)
        settings1.set(
            "media_servers",
            [
                {
                    "id": "uuid-lib-toggle",
                    "type": "plex",
                    "name": "Plex with mixed libs",
                    "enabled": True,
                    "url": "http://plex:32400",
                    "auth": {"method": "token", "token": "t"},
                    "libraries": [
                        {"id": "1", "name": "Movies", "remote_paths": ["/m/movies"], "enabled": True},
                        {"id": "2", "name": "TV", "remote_paths": ["/m/tv"], "enabled": False},
                        {"id": "3", "name": "4K", "remote_paths": ["/m/4k"], "enabled": True},
                    ],
                    "path_mappings": [],
                    "output": {"adapter": "plex_bundle"},
                }
            ],
        )
        settings1.complete_setup()
        del app1
        del settings1

        _, settings2 = _build_app(fresh_config_dir)
        libs = settings2.get("media_servers")[0]["libraries"]
        toggles = {lib["id"]: lib["enabled"] for lib in libs}
        assert toggles == {"1": True, "2": False, "3": True}
