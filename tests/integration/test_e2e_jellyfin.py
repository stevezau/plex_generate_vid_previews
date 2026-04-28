"""End-to-end integration tests against a live Jellyfin container.

Mirrors the Emby + Plex live tests for completeness:

* :class:`JellyfinServer` connects, lists libraries, lists items,
  resolves item ids → paths.
* :class:`JellyfinTrickplayAdapter` writes valid 10×10 tile sheets
  + manifest.json against a real path.
* The Jellyfin webhook plugin's ``ItemAdded`` shape routes through
  the universal endpoint and dispatches.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from plex_generate_previews.processing.multi_server import (
    MultiServerStatus,
    PublisherStatus,
    process_canonical_path,
)
from plex_generate_previews.servers import ServerRegistry


@pytest.fixture
def jf_config(tmp_path):
    config = MagicMock()
    config.plex_url = ""
    config.plex_token = ""
    config.plex_timeout = 60
    config.plex_libraries = []
    config.plex_config_folder = ""
    config.plex_local_videos_path_mapping = ""
    config.plex_videos_path_mapping = ""
    config.path_mappings = []
    config.plex_bif_frame_interval = 5
    config.thumbnail_quality = 4
    config.regenerate_thumbnails = False
    config.gpu_threads = 0
    config.cpu_threads = 2
    config.gpu_config = []
    config.tmp_folder = str(tmp_path / "tmp")
    config.working_tmp_folder = str(tmp_path / "tmp")
    Path(config.working_tmp_folder).mkdir(parents=True, exist_ok=True)
    config.tmp_folder_created_by_us = False
    config.ffmpeg_path = "/usr/bin/ffmpeg"
    config.ffmpeg_threads = 2
    config.tonemap_algorithm = "hable"
    config.log_level = "INFO"
    config.worker_pool_timeout = 60
    config.plex_library_ids = None
    config.plex_verify_ssl = True
    return config


@pytest.fixture
def jf_registry(jellyfin_credentials, media_root):
    raw_servers = [
        {
            "id": "jf-int-1",
            "type": "jellyfin",
            "name": "Test Jellyfin",
            "enabled": True,
            "url": jellyfin_credentials["JELLYFIN_URL"],
            "auth": {
                "method": "api_key",
                "api_key": jellyfin_credentials["JELLYFIN_ACCESS_TOKEN"],
            },
            "server_identity": jellyfin_credentials["JELLYFIN_SERVER_ID"],
            "libraries": [
                {
                    "id": "movies",
                    "name": "Movies",
                    "remote_paths": ["/jf-media/Movies"],
                    "enabled": True,
                }
            ],
            "path_mappings": [{"remote_prefix": "/jf-media", "local_prefix": str(media_root)}],
            "output": {"adapter": "jellyfin_trickplay", "width": 320, "frame_interval": 5},
        }
    ]
    return ServerRegistry.from_settings(raw_servers, legacy_config=None)


@pytest.mark.integration
class TestLiveJellyfinConnection:
    def test_connects_and_identifies(self, jf_registry, jellyfin_credentials):
        server = jf_registry.get("jf-int-1")
        result = server.test_connection()
        assert result.ok, result.message
        assert result.server_id == jellyfin_credentials["JELLYFIN_SERVER_ID"]

    def test_list_libraries_returns_movies(self, jf_registry):
        server = jf_registry.get("jf-int-1")
        libraries = server.list_libraries()
        names = [lib.name for lib in libraries]
        assert "Movies" in names, names

    def test_list_items_returns_test_movies(self, jf_registry):
        server = jf_registry.get("jf-int-1")
        libraries = server.list_libraries()
        movies = next(lib for lib in libraries if lib.name == "Movies")
        items = list(server.list_items(movies.id))
        assert len(items) >= 1, [i.title for i in items]

    def test_resolve_remote_path_to_item_id(self, jf_registry, media_root):
        """The reverse-lookup helper finds the right Jellyfin item id."""
        server = jf_registry.get("jf-int-1")
        # Use the fixture's known path; the helper searches by basename
        # and verifies via parent-dir tail.
        target_path = "/jf-media/Movies/Test Movie H264 (2024)/Test Movie H264 (2024).mkv"
        item_id = server.resolve_remote_path_to_item_id(target_path)
        assert item_id, f"no item id found for {target_path}"

        # Verify by round-tripping through resolve_item_to_remote_path.
        roundtrip = server.resolve_item_to_remote_path(item_id)
        assert roundtrip == target_path, roundtrip


@pytest.mark.integration
@pytest.mark.slow
class TestLiveJellyfinTrickplay:
    """Real FFmpeg → real trickplay tile-grid + manifest.json for Jellyfin."""

    def test_trickplay_lands_with_tile_sheets_and_manifest(self, jf_registry, jf_config, media_root):
        canonical = str(media_root / "Movies" / "Test Movie H264 (2024)" / "Test Movie H264 (2024).mkv")
        trickplay_dir = Path(canonical).parent / "trickplay"

        # Clean up any leftovers so we can assert the tests created them.
        if trickplay_dir.exists():
            import shutil

            shutil.rmtree(trickplay_dir)

        try:
            result = process_canonical_path(
                canonical_path=canonical,
                registry=jf_registry,
                config=jf_config,
                gpu=None,
                gpu_device_path=None,
            )
            assert result.status is MultiServerStatus.PUBLISHED, result.message
            published = next(p for p in result.publishers if p.status is PublisherStatus.PUBLISHED)
            assert published.adapter_name == "jellyfin_trickplay"

            # Manifest exists and is valid JSON keyed by Jellyfin item id.
            manifest = trickplay_dir / "Test Movie H264 (2024)-320.json"
            assert manifest.exists(), f"manifest missing at {manifest}"
            data = json.loads(manifest.read_text())
            assert "Trickplay" in data
            # Manifest must be keyed by a real Jellyfin item id.
            item_ids = list(data["Trickplay"].keys())
            assert item_ids, data
            (item_id,) = item_ids
            info = data["Trickplay"][item_id]["320"]
            assert info["TileWidth"] == 10
            assert info["TileHeight"] == 10
            assert info["Width"] > 0
            assert info["ThumbnailCount"] > 0
            assert info["Interval"] == 5000

            # Sheets directory has 0.jpg (fewer than 100 frames → 1 sheet).
            sheets_dir = trickplay_dir / "Test Movie H264 (2024)-320"
            assert sheets_dir.is_dir()
            sheets = sorted(sheets_dir.iterdir())
            assert sheets and sheets[0].name == "0.jpg"
        finally:
            if trickplay_dir.exists():
                import shutil

                shutil.rmtree(trickplay_dir)


@pytest.mark.integration
@pytest.mark.slow
class TestJellyfinNativeWebhook:
    def test_jellyfin_itemadded_payload_dispatches(
        self, jellyfin_credentials, media_root, tmp_path, monkeypatch, jf_config
    ):
        """jellyfin-plugin-webhook stock ItemAdded → universal router → BIF."""
        from plex_generate_previews.web.app import create_app
        from plex_generate_previews.web.settings_manager import (
            get_settings_manager,
            reset_settings_manager,
        )

        config_dir = tmp_path / "config"
        config_dir.mkdir()
        reset_settings_manager()
        monkeypatch.setenv("CONFIG_DIR", str(config_dir))
        monkeypatch.setenv("WEB_AUTH_TOKEN", "integration-test-token")

        app = create_app(config_dir=str(config_dir))
        app.config["TESTING"] = True

        settings = get_settings_manager()
        settings.set("webhook_secret", "integration-secret")
        settings.set(
            "media_servers",
            [
                {
                    "id": "jf-int-1",
                    "type": "jellyfin",
                    "name": "Test Jellyfin",
                    "enabled": True,
                    "url": jellyfin_credentials["JELLYFIN_URL"],
                    "auth": {
                        "method": "api_key",
                        "api_key": jellyfin_credentials["JELLYFIN_ACCESS_TOKEN"],
                    },
                    "server_identity": jellyfin_credentials["JELLYFIN_SERVER_ID"],
                    "libraries": [
                        {
                            "id": "movies",
                            "name": "Movies",
                            "remote_paths": ["/jf-media/Movies"],
                            "enabled": True,
                        }
                    ],
                    "path_mappings": [{"remote_prefix": "/jf-media", "local_prefix": str(media_root)}],
                    "output": {"adapter": "jellyfin_trickplay", "width": 320, "frame_interval": 5},
                }
            ],
        )
        settings.complete_setup()
        monkeypatch.setattr(
            "plex_generate_previews.web.webhook_router._load_config_or_minimal",
            lambda: jf_config,
        )

        # Find a real Jellyfin item id.
        import requests

        items_resp = requests.get(
            f"{jellyfin_credentials['JELLYFIN_URL']}/Items",
            params={
                "Recursive": "true",
                "IncludeItemTypes": "Movie",
                "Fields": "Path",
                "Limit": 5,
            },
            headers={"X-Emby-Token": jellyfin_credentials["JELLYFIN_ACCESS_TOKEN"]},
            timeout=10,
        )
        items_resp.raise_for_status()
        target = next(i for i in items_resp.json()["Items"] if "H264" in (i.get("Path") or ""))
        target_id = target["Id"]
        canonical = target["Path"].replace("/jf-media", str(media_root), 1)
        trickplay_dir = Path(canonical).parent / "trickplay"
        if trickplay_dir.exists():
            import shutil

            shutil.rmtree(trickplay_dir)

        try:
            response = app.test_client().post(
                "/api/webhooks/incoming",
                headers={"X-Auth-Token": "integration-secret", "Content-Type": "application/json"},
                data=json.dumps(
                    {
                        "NotificationType": "ItemAdded",
                        "ItemId": target_id,
                        "ItemType": "Movie",
                        "ServerId": jellyfin_credentials["JELLYFIN_SERVER_ID"],
                        "ServerName": "Test Jellyfin",
                    }
                ),
            )

            assert response.status_code == 200, response.get_data(as_text=True)
            body = response.get_json()
            assert body["kind"] == "jellyfin", body
            assert body.get("status") in ("published", "skipped"), body

            # Trickplay output present.
            assert (trickplay_dir / "Test Movie H264 (2024)-320.json").exists()
            assert (trickplay_dir / "Test Movie H264 (2024)-320").is_dir()
        finally:
            if trickplay_dir.exists():
                import shutil

                shutil.rmtree(trickplay_dir)
