"""End-to-end integration tests against a live Plex Media Server container.

Verifies the multi-server stack against a real Plex (started by
``docker-compose.test.yml``):

* :class:`PlexServer` connects, lists libraries, lists items.
* The bundle-hash lookup against ``/library/metadata/{id}/tree`` works
  for a real Plex item.
* :class:`PlexBundleAdapter` writes a real, structurally valid BIF at
  the per-item bundle path.
* The Plex native multipart webhook payload routes through the
  universal endpoint correctly.
* Identity matching via the captured ``server_identity`` works against
  the live Plex's ``machineIdentifier``.

Run with::

    pytest -m integration --no-cov tests/integration/test_e2e_plex.py
"""

from __future__ import annotations

import json
import struct
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import requests as _requests

from media_preview_generator.processing.multi_server import (
    MultiServerStatus,
    PublisherStatus,
    process_canonical_path,
)
from media_preview_generator.servers import ServerRegistry

_BIF_MAGIC = bytes([0x89, 0x42, 0x49, 0x46, 0x0D, 0x0A, 0x1A, 0x0A])
_JPEG_SOI = bytes([0xFF, 0xD8, 0xFF])


def _decode_bif(path: Path) -> dict:
    """Validate magic + JPEG SOI in the BIF; return basic metadata."""
    raw = path.read_bytes()
    assert len(raw) >= 64
    assert raw[:8] == _BIF_MAGIC
    image_count = struct.unpack("<I", raw[12:16])[0]
    interval_ms = struct.unpack("<I", raw[16:20])[0]
    assert image_count > 0
    first_offset = struct.unpack("<I", raw[64 + 4 : 64 + 8])[0]
    assert raw[first_offset : first_offset + 3] == _JPEG_SOI
    return {"image_count": image_count, "interval_ms": interval_ms, "size_bytes": len(raw)}


@pytest.fixture
def plex_legacy_config(plex_credentials, tmp_path):
    """Legacy Config object with Plex creds (the PlexServer wrapper still takes one)."""
    config = MagicMock()
    config.plex_url = plex_credentials["PLEX_URL"]
    config.plex_token = plex_credentials["PLEX_ACCESS_TOKEN"]
    config.plex_timeout = 60
    config.plex_libraries = ["Movies"]
    config.plex_config_folder = str(tmp_path / "plex_config")
    Path(config.plex_config_folder).mkdir(parents=True, exist_ok=True)
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
def plex_registry(plex_credentials, plex_legacy_config, media_root):
    """Registry with the live Plex container as the only entry."""
    raw_servers = [
        {
            "id": "plex-int-1",
            "type": "plex",
            "name": "Test Plex",
            "enabled": True,
            "url": plex_credentials["PLEX_URL"],
            "auth": {"method": "token", "token": plex_credentials["PLEX_ACCESS_TOKEN"]},
            "server_identity": plex_credentials["PLEX_SERVER_ID"],
            "libraries": [
                {
                    "id": "1",
                    "name": "Movies",
                    "remote_paths": ["/media/Movies"],
                    "enabled": True,
                }
            ],
            "path_mappings": [{"remote_prefix": "/media", "local_prefix": str(media_root)}],
            "output": {
                "adapter": "plex_bundle",
                "plex_config_folder": str(plex_legacy_config.plex_config_folder),
                "frame_interval": 5,
            },
        }
    ]
    return ServerRegistry.from_settings(raw_servers, legacy_config=plex_legacy_config)


@pytest.mark.integration
@pytest.mark.real_plex_server
class TestLivePlexConnection:
    def test_connects_and_identifies(self, plex_registry, plex_credentials):
        server = plex_registry.get("plex-int-1")
        result = server.test_connection()
        assert result.ok, result.message
        # /identity gives back the same machineIdentifier we captured at setup.
        assert result.server_id == plex_credentials["PLEX_SERVER_ID"]

    def test_list_libraries_returns_movies(self, plex_registry):
        server = plex_registry.get("plex-int-1")
        libraries = server.list_libraries()
        names = [lib.name for lib in libraries]
        assert "Movies" in names, names

    def test_list_items_returns_test_movies(self, plex_registry):
        server = plex_registry.get("plex-int-1")
        libraries = server.list_libraries()
        movies = next(lib for lib in libraries if lib.name == "Movies")
        items = list(server.list_items(movies.id))
        assert len(items) >= 1, [i.title for i in items]


@pytest.mark.integration
@pytest.mark.slow
@pytest.mark.real_plex_server
class TestLivePlexBundleBif:
    """Real FFmpeg → real Plex bundle BIF at the hash-keyed bundle path."""

    def test_bundle_bif_lands_at_per_item_bundle_path(self, plex_registry, plex_legacy_config, media_root):
        canonical = str(media_root / "Movies" / "Test Movie H264 (2024)" / "Test Movie H264 (2024).mkv")

        result = process_canonical_path(
            canonical_path=canonical,
            registry=plex_registry,
            config=plex_legacy_config,
            gpu=None,
            gpu_device_path=None,
        )

        assert result.status is MultiServerStatus.PUBLISHED, result.message
        published = next(p for p in result.publishers if p.status is PublisherStatus.PUBLISHED)
        assert published.adapter_name == "plex_bundle"
        # Plex bundle BIF lives at:
        #   {plex_config_folder}/Media/localhost/{h0}/{h[1:]}.bundle/Contents/Indexes/index-sd.bif
        bif_path = published.output_paths[0]
        assert bif_path.name == "index-sd.bif"
        assert "Indexes" in str(bif_path)
        assert ".bundle" in str(bif_path)
        # And the bytes are a real BIF.
        try:
            decoded = _decode_bif(bif_path)
            assert decoded["interval_ms"] == 5000
            assert decoded["image_count"] >= 4
        finally:
            # Clean up so re-runs start fresh.
            if bif_path.exists():
                bif_path.unlink()


@pytest.mark.integration
@pytest.mark.real_plex_server
class TestPlexNativeMultipartWebhook:
    """Plex's webhook is multipart form data with a JSON ``payload`` field.

    Verify the universal /api/webhooks/incoming endpoint correctly
    classifies it, extracts Server.uuid, and routes via server_identity.
    """

    def test_multipart_payload_classified_as_plex(
        self,
        plex_credentials,
        media_root,
        tmp_path,
        monkeypatch,
        plex_legacy_config,
    ):
        """A real Plex-shape multipart webhook drives the full pipeline."""
        from media_preview_generator.web.app import create_app
        from media_preview_generator.web.settings_manager import (
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
                    "id": "plex-int-1",
                    "type": "plex",
                    "name": "Test Plex",
                    "enabled": True,
                    "url": plex_credentials["PLEX_URL"],
                    "auth": {"method": "token", "token": plex_credentials["PLEX_ACCESS_TOKEN"]},
                    "server_identity": plex_credentials["PLEX_SERVER_ID"],
                    "libraries": [
                        {
                            "id": "1",
                            "name": "Movies",
                            "remote_paths": ["/media/Movies"],
                            "enabled": True,
                        }
                    ],
                    "path_mappings": [{"remote_prefix": "/media", "local_prefix": str(media_root)}],
                    "output": {
                        "adapter": "plex_bundle",
                        "plex_config_folder": str(plex_legacy_config.plex_config_folder),
                        "frame_interval": 5,
                    },
                }
            ],
        )
        settings.complete_setup()

        # Patch the webhook router's config loader (used by the dispatch
        # path) AND the source load_config (used by _get_registry to
        # build the legacy Plex client). Both must point at our test
        # Plex container, otherwise .env leakage takes us to a real
        # Plex on the dev machine.
        monkeypatch.setattr(
            "media_preview_generator.web.webhook_router._load_config_or_minimal",
            lambda: plex_legacy_config,
        )
        monkeypatch.setattr(
            "media_preview_generator.config.load_config",
            lambda *a, **kw: plex_legacy_config,
        )

        # Look up the real ratingKey (= Plex's item id) for the H264 fixture.
        sections_resp = _requests.get(
            f"{plex_credentials['PLEX_URL']}/library/sections/1/all",
            headers={"X-Plex-Token": plex_credentials["PLEX_ACCESS_TOKEN"], "Accept": "application/json"},
            timeout=10,
        )
        sections_resp.raise_for_status()
        videos = sections_resp.json()["MediaContainer"]["Metadata"]
        h264_item = next(v for v in videos if "H264" in v["Media"][0]["Part"][0]["file"])
        rating_key = str(h264_item["ratingKey"])

        # Build the Plex multipart payload — payload field is JSON.
        plex_payload = {
            "event": "library.new",
            "user": False,
            "owner": True,
            "Account": {"id": 1, "title": "test"},
            "Server": {
                "title": "Test Plex",
                "uuid": plex_credentials["PLEX_SERVER_ID"],
            },
            "Player": {},
            "Metadata": {
                "ratingKey": rating_key,
                "title": "Test Movie",
                "type": "movie",
                "librarySectionID": 1,
            },
        }

        client = app.test_client()
        # The Plex webhook is multipart/form-data with a "payload" field.
        response = client.post(
            "/api/webhooks/incoming",
            headers={"X-Auth-Token": "integration-secret"},
            data={"payload": json.dumps(plex_payload)},
            content_type="multipart/form-data",
        )

        assert response.status_code == 200, (
            f"expected 200, got {response.status_code}: {response.get_data(as_text=True)}"
        )
        body = response.get_json()
        assert body["kind"] == "plex", body
        # The dispatch ran — either a publish or a "skipped" (output already
        # exists from a prior run); either way the canonical path resolved.
        assert body.get("status") in ("published", "skipped"), body
        assert body.get("canonical_path") == str(
            media_root / "Movies" / "Test Movie H264 (2024)" / "Test Movie H264 (2024).mkv"
        ), body
