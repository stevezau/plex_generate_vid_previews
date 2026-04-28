"""Misc integration tests against the live Emby container.

* Per-server fallback webhook URL (``/api/webhooks/server/<id>``) —
  the explicit-routing alternative to the universal endpoint.
* Frame cache TTL expiration — set a tiny TTL, verify a second
  dispatch after the TTL window does re-run FFmpeg.
* Webhook with bad token rejected (auth integration).
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def real_config(tmp_path):
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
def webhook_app(emby_credentials, media_root, tmp_path, monkeypatch, real_config):
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
                "id": "emby-int-1",
                "type": "emby",
                "name": "Test Emby",
                "enabled": True,
                "url": emby_credentials["EMBY_URL"],
                "auth": {
                    "method": "password",
                    "access_token": emby_credentials["EMBY_ACCESS_TOKEN"],
                    "user_id": emby_credentials["EMBY_USER_ID"],
                },
                "server_identity": emby_credentials["EMBY_SERVER_ID"],
                "libraries": [
                    {
                        "id": "movies",
                        "name": "Movies",
                        "remote_paths": ["/em-media/Movies"],
                        "enabled": True,
                    }
                ],
                "path_mappings": [{"remote_prefix": "/em-media", "local_prefix": str(media_root)}],
                "output": {"adapter": "emby_sidecar", "width": 320, "frame_interval": 5},
            }
        ],
    )
    settings.complete_setup()

    monkeypatch.setattr(
        "plex_generate_previews.web.webhook_router._load_config_or_minimal",
        lambda: real_config,
    )
    return app


@pytest.mark.integration
class TestWebhookAuth:
    def test_missing_token_rejected(self, webhook_app):
        response = webhook_app.test_client().post(
            "/api/webhooks/incoming",
            headers={"Content-Type": "application/json"},
            data=json.dumps({"path": "/x.mkv"}),
        )
        assert response.status_code == 401, response.get_data(as_text=True)

    def test_bad_token_rejected(self, webhook_app):
        response = webhook_app.test_client().post(
            "/api/webhooks/incoming",
            headers={"X-Auth-Token": "wrong-token", "Content-Type": "application/json"},
            data=json.dumps({"path": "/x.mkv"}),
        )
        assert response.status_code == 401

    def test_token_via_query_param(self, webhook_app):
        """Plex's webhook UI doesn't support custom headers — token via ?token= must work."""
        response = webhook_app.test_client().post(
            "/api/webhooks/incoming?token=integration-secret",
            headers={"Content-Type": "application/json"},
            data=json.dumps({"path": "/notindexed.mkv", "trigger": "x"}),
        )
        # 200 (no_owners since path isn't in any library) or 202 — never 401.
        assert response.status_code in (200, 202), response.get_data(as_text=True)


@pytest.mark.integration
@pytest.mark.slow
class TestPerServerFallbackWebhook:
    def test_explicit_per_server_url(self, webhook_app, media_root):
        """``/api/webhooks/server/<id>`` skips vendor classification."""
        canonical = str(media_root / "Movies" / "Test Movie H264 (2024)" / "Test Movie H264 (2024).mkv")
        sidecar = Path(canonical).parent / "Test Movie H264 (2024)-320-5.bif"
        if sidecar.exists():
            sidecar.unlink()

        try:
            response = webhook_app.test_client().post(
                "/api/webhooks/server/emby-int-1",
                headers={
                    "X-Auth-Token": "integration-secret",
                    "Content-Type": "application/json",
                },
                data=json.dumps({"path": canonical}),
            )
            assert response.status_code == 200, response.get_data(as_text=True)
            body = response.get_json()
            assert body.get("status") in ("published", "skipped")
            assert sidecar.exists()
        finally:
            if sidecar.exists():
                sidecar.unlink()

    def test_unknown_server_id_returns_404(self, webhook_app):
        response = webhook_app.test_client().post(
            "/api/webhooks/server/does-not-exist",
            headers={
                "X-Auth-Token": "integration-secret",
                "Content-Type": "application/json",
            },
            data=json.dumps({"path": "/x.mkv"}),
        )
        assert response.status_code == 404


@pytest.mark.integration
@pytest.mark.slow
class TestFrameCacheTtlExpiry:
    def test_cache_entry_evicts_after_ttl(self, emby_credentials, media_root, real_config, tmp_path):
        """Set a 1-second TTL, dispatch twice with a sleep between → FFmpeg runs twice.

        This is the inverse of the concurrent-coalescing test: that
        proves "second-and-later within TTL hit cache"; this proves
        "after TTL, cache is gone and FFmpeg re-runs".
        """
        from plex_generate_previews.processing import multi_server as ms_module
        from plex_generate_previews.processing.frame_cache import (
            get_frame_cache,
            reset_frame_cache,
        )
        from plex_generate_previews.processing.multi_server import process_canonical_path
        from plex_generate_previews.servers import ServerRegistry

        # Force a 1-second TTL by using a fresh cache singleton.
        reset_frame_cache()
        cache_base = tmp_path / "cache_base"
        get_frame_cache(base_dir=str(cache_base), ttl_seconds=1)

        raw_servers = [
            {
                "id": "emby-int-1",
                "type": "emby",
                "name": "Test Emby",
                "enabled": True,
                "url": emby_credentials["EMBY_URL"],
                "auth": {
                    "method": "password",
                    "access_token": emby_credentials["EMBY_ACCESS_TOKEN"],
                    "user_id": emby_credentials["EMBY_USER_ID"],
                },
                "server_identity": emby_credentials["EMBY_SERVER_ID"],
                "libraries": [
                    {
                        "id": "movies",
                        "name": "Movies",
                        "remote_paths": ["/em-media/Movies"],
                        "enabled": True,
                    }
                ],
                "path_mappings": [{"remote_prefix": "/em-media", "local_prefix": str(media_root)}],
                "output": {"adapter": "emby_sidecar", "width": 320, "frame_interval": 5},
            }
        ]
        registry = ServerRegistry.from_settings(raw_servers, legacy_config=None)

        canonical = str(media_root / "Movies" / "Test Movie H264 (2024)" / "Test Movie H264 (2024).mkv")
        sidecar = Path(canonical).parent / "Test Movie H264 (2024)-320-5.bif"
        if sidecar.exists():
            sidecar.unlink()

        # The dispatcher hits get_frame_cache with the config's
        # working_tmp_folder, not our pre-seeded base_dir. That'd
        # rebuild the singleton with a different base_dir → raises.
        # Workaround: point real_config at the same base.
        real_config.working_tmp_folder = str(tmp_path)
        # The cache's base will be working_tmp_folder/frame_cache,
        # but we already pre-seeded with a different base. Reset and
        # let the dispatcher build it.
        reset_frame_cache()

        # Re-seed with the EXACT base the dispatcher will use, but with
        # a 1-second TTL.
        get_frame_cache(
            base_dir=str(Path(real_config.working_tmp_folder) / "frame_cache"),
            ttl_seconds=1,
        )

        original_generate = ms_module.generate_images
        ffmpeg_calls = []

        def _spy(*args, **kwargs):
            ffmpeg_calls.append(args[0])
            return original_generate(*args, **kwargs)

        ms_module.generate_images = _spy

        try:
            # First dispatch: real run.
            first = process_canonical_path(
                canonical_path=canonical,
                registry=registry,
                config=real_config,
                gpu=None,
                gpu_device_path=None,
            )
            assert first.status.value in ("published", "skipped")

            # Wait for TTL to expire.
            time.sleep(2)
            # Force-rebuild the publisher's already-published BIF removal
            # so the second dispatch isn't short-circuited by skip-if-exists.
            if sidecar.exists():
                sidecar.unlink()

            # Second dispatch: cache should be expired → FFmpeg runs again.
            second = process_canonical_path(
                canonical_path=canonical,
                registry=registry,
                config=real_config,
                gpu=None,
                gpu_device_path=None,
            )
            assert second.status.value in ("published", "skipped")

            # FFmpeg ran twice (once per dispatch since TTL expired
            # between them).
            assert len(ffmpeg_calls) == 2, f"expected 2 FFmpeg calls (TTL expired), got {len(ffmpeg_calls)}"
        finally:
            ms_module.generate_images = original_generate
            if sidecar.exists():
                sidecar.unlink()
            reset_frame_cache()
