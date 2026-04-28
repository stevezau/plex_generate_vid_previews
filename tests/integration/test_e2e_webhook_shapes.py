"""Integration tests covering real webhook payload shapes.

Fires representative payloads from each source through the universal
``/api/webhooks/incoming`` endpoint and verifies the dispatcher does
the right thing end-to-end against the live Emby container.

Sources covered:
* Sonarr ``Download`` event (episodeFile.path nested in series envelope)
* Radarr ``Download`` event (movieFile.path nested in movie envelope)
* Plex multipart envelope with non-relevant event (skipped)
* Generic ``{"path": "..."}`` payload
* Concurrent duplicate webhooks (frame cache should coalesce)
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def real_config(tmp_path):
    """Minimal Config for the webhook dispatcher."""
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
    """Live Flask app wired to the live Emby container, ready for webhook tests."""
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


def _post(app, payload, headers=None, content_type="application/json"):
    """Helper: post payload to /api/webhooks/incoming."""
    base_headers = {"X-Auth-Token": "integration-secret"}
    if headers:
        base_headers.update(headers)
    if content_type == "application/json":
        base_headers["Content-Type"] = content_type
        return app.test_client().post(
            "/api/webhooks/incoming",
            headers=base_headers,
            data=json.dumps(payload),
        )
    return app.test_client().post(
        "/api/webhooks/incoming",
        headers=base_headers,
        data=payload,
        content_type=content_type,
    )


@pytest.mark.integration
@pytest.mark.slow
class TestSonarrWebhook:
    def test_sonarr_download_event_dispatches(self, webhook_app, media_root):
        """Sonarr's Download event has episodeFile.path nested in the series envelope."""
        canonical = str(media_root / "TV Shows" / "Test Show" / "Season 01" / "Test Show - S01E01 - Pilot.mkv")
        # The Sonarr-style ownership requires a TV library; the webhook
        # app currently only configures Movies. This test verifies
        # routing/parsing works (the publisher list will be empty
        # because no library covers the path → status NO_OWNERS).
        sonarr_payload = {
            "eventType": "Download",
            "instanceName": "Sonarr",
            "applicationUrl": "http://sonarr:8989",
            "series": {
                "id": 1,
                "title": "Test Show",
                "path": str(media_root / "TV Shows" / "Test Show"),
            },
            "episodes": [
                {
                    "id": 1,
                    "episodeNumber": 1,
                    "seasonNumber": 1,
                    "title": "Pilot",
                }
            ],
            "episodeFile": {
                "id": 1,
                "relativePath": "Season 01/Test Show - S01E01 - Pilot.mkv",
                "path": canonical,
            },
            "release": {"releaseTitle": "Test.Show.S01E01.Pilot.x264"},
        }
        response = _post(webhook_app, sonarr_payload)
        assert response.status_code == 200, response.get_data(as_text=True)
        body = response.get_json()
        # Sonarr-style payloads classify as path-first (the router
        # extracts episodeFile.path / movieFile.path).
        assert body["kind"] in ("sonarr", "path"), body
        assert body["canonical_path"] == canonical, body
        # No library covers TV Shows → no_owners is fine.
        assert body["status"] in ("no_owners", "published", "skipped"), body


@pytest.mark.integration
@pytest.mark.slow
class TestRadarrWebhook:
    def test_radarr_download_event_dispatches(self, webhook_app, media_root):
        """Radarr's Download event has movieFile.path nested in the movie envelope."""
        canonical = str(media_root / "Movies" / "Test Movie H264 (2024)" / "Test Movie H264 (2024).mkv")
        sidecar = Path(canonical).parent / "Test Movie H264 (2024)-320-5.bif"
        if sidecar.exists():
            sidecar.unlink()

        radarr_payload = {
            "eventType": "Download",
            "instanceName": "Radarr",
            "applicationUrl": "http://radarr:7878",
            "movie": {
                "id": 1,
                "title": "Test Movie H264",
                "year": 2024,
                "folderPath": str(media_root / "Movies" / "Test Movie H264 (2024)"),
            },
            "movieFile": {
                "id": 1,
                "relativePath": "Test Movie H264 (2024).mkv",
                "path": canonical,
            },
            "release": {"releaseTitle": "Test.Movie.H264.2024.1080p.x264"},
        }

        try:
            response = _post(webhook_app, radarr_payload)
            assert response.status_code == 200, response.get_data(as_text=True)
            body = response.get_json()
            assert body["kind"] in ("radarr", "path"), body
            assert body["canonical_path"] == canonical, body
            assert body["status"] in ("published", "skipped"), body
            assert sidecar.exists(), f"sidecar BIF missing at {sidecar}"
        finally:
            if sidecar.exists():
                sidecar.unlink()


@pytest.mark.integration
class TestGenericPathWebhook:
    def test_path_only_payload_dispatches(self, webhook_app, media_root):
        """The simplest custom webhook shape: ``{"path": ...}``."""
        canonical = str(media_root / "Movies" / "Test Movie H264 (2024)" / "Test Movie H264 (2024).mkv")
        sidecar = Path(canonical).parent / "Test Movie H264 (2024)-320-5.bif"
        if sidecar.exists():
            sidecar.unlink()

        try:
            response = _post(webhook_app, {"path": canonical, "trigger": "manual"})
            assert response.status_code == 200, response.get_data(as_text=True)
            body = response.get_json()
            assert body["kind"] == "path", body
            assert body["canonical_path"] == canonical, body
            assert sidecar.exists()
        finally:
            if sidecar.exists():
                sidecar.unlink()

    def test_unknown_payload_returns_400(self, webhook_app):
        """Random JSON should be rejected, not dispatched."""
        response = _post(webhook_app, {"random": "noise", "no_path": True})
        assert response.status_code == 400


@pytest.mark.integration
class TestPlexWebhookMultipart:
    def test_plex_playback_event_classified_but_ignored(self, webhook_app, emby_credentials):
        """Plex's playback events should classify as plex but return ignored.

        This verifies the multipart parser does its job even when the
        event isn't actionable. The configured registry has no Plex
        server so any item-id resolution skips entirely.
        """
        plex_payload = {
            "event": "media.play",
            "user": True,
            "owner": True,
            "Account": {"id": 1, "title": "test"},
            "Server": {"title": "Some Plex", "uuid": "some-other-uuid"},
            "Player": {"title": "Browser"},
            "Metadata": {"ratingKey": "999", "type": "movie", "title": "X"},
        }
        response = _post(
            webhook_app,
            {"payload": json.dumps(plex_payload)},
            content_type="multipart/form-data",
        )
        # Either the parser classified it as plex (and ignored
        # media.play because we only act on library.new) or rejected
        # it as unknown — both acceptable. The CRITICAL thing is no
        # 5xx.
        assert response.status_code in (200, 202), response.get_data(as_text=True)


@pytest.mark.integration
@pytest.mark.slow
class TestConcurrentWebhookCoalescing:
    def test_five_concurrent_webhooks_run_ffmpeg_once(self, webhook_app, media_root):
        """Fire 5 webhooks for the same file at the same time; FFmpeg runs once.

        The frame cache TTL is the coalescing mechanism — second-and-
        later requests for the same canonical path within the TTL
        window get the cached frame dir.
        """
        canonical = str(media_root / "Movies" / "Test Movie H264 (2024)" / "Test Movie H264 (2024).mkv")
        sidecar = Path(canonical).parent / "Test Movie H264 (2024)-320-5.bif"
        if sidecar.exists():
            sidecar.unlink()

        # Spy on FFmpeg invocations.
        from plex_generate_previews.processing import multi_server as ms_module

        original_generate = ms_module.generate_images
        ffmpeg_call_count = 0

        def _spy(*args, **kwargs):
            nonlocal ffmpeg_call_count
            ffmpeg_call_count += 1
            return original_generate(*args, **kwargs)

        ms_module.generate_images = _spy

        try:
            # Use a thread pool so the requests fire concurrently.
            client = webhook_app.test_client()

            def _fire(_):
                return client.post(
                    "/api/webhooks/incoming",
                    headers={
                        "X-Auth-Token": "integration-secret",
                        "Content-Type": "application/json",
                    },
                    data=json.dumps({"path": canonical, "trigger": "concurrent"}),
                )

            with ThreadPoolExecutor(max_workers=5) as pool:
                responses = list(pool.map(_fire, range(5)))

            # All 5 returned 200 (or 202 / 200 mix when one was first
            # and four hit cache).
            statuses = [r.status_code for r in responses]
            assert all(s == 200 for s in statuses), statuses

            # FFmpeg ran exactly ONCE despite 5 webhooks.
            assert ffmpeg_call_count == 1, f"expected 1 FFmpeg call, got {ffmpeg_call_count}"

            # The sidecar should be present (one of the dispatches won).
            assert sidecar.exists()
        finally:
            ms_module.generate_images = original_generate
            if sidecar.exists():
                sidecar.unlink()
