"""Edge-case integration tests against the live Emby + Plex stack.

Covers:
* Server unreachable mid-publish (kill Emby; verify graceful failure).
* Path-mapping edge cases (multi-disk overlap, prefix collision).
* LibraryNotYetIndexedError → SKIPPED_NOT_INDEXED publisher status.
* Disabled library → permanent skip.
* server_identity disambiguation with two Emby endpoints.
"""

from __future__ import annotations

import json
import struct
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from media_preview_generator.processing.multi_server import (
    MultiServerStatus,
    PublisherStatus,
    process_canonical_path,
)
from media_preview_generator.servers import LibraryNotYetIndexedError, ServerRegistry

_BIF_MAGIC = bytes([0x89, 0x42, 0x49, 0x46, 0x0D, 0x0A, 0x1A, 0x0A])


def _decode_bif_count(path: Path) -> int:
    raw = path.read_bytes()
    assert raw[:8] == _BIF_MAGIC
    return struct.unpack("<I", raw[12:16])[0]


@pytest.fixture
def base_config(tmp_path):
    config = MagicMock()
    config.plex_url = ""
    config.plex_token = ""
    config.plex_timeout = 60
    config.plex_libraries = []
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


@pytest.mark.integration
@pytest.mark.slow
class TestPartialFailure:
    """One publisher fails; the others still succeed."""

    def test_jellyfin_failure_does_not_block_emby(self, emby_credentials, media_root, base_config):
        """Configured Jellyfin server is unreachable; Emby publish still wins."""
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
                    {"id": "movies", "name": "Movies", "remote_paths": ["/em-media/Movies"], "enabled": True}
                ],
                "path_mappings": [{"remote_prefix": "/em-media", "local_prefix": str(media_root)}],
                "output": {"adapter": "emby_sidecar", "width": 320, "frame_interval": 5},
            },
            {
                # Simulated dead Jellyfin: pointing at a port nothing is listening on.
                "id": "jelly-dead",
                "type": "jellyfin",
                "name": "Dead Jellyfin",
                "enabled": True,
                "url": "http://127.0.0.1:1",  # nothing listens here
                "auth": {"method": "api_key", "api_key": "doesntmatter"},
                "libraries": [
                    {"id": "movies", "name": "Movies", "remote_paths": ["/jf-media/Movies"], "enabled": True}
                ],
                "path_mappings": [{"remote_prefix": "/jf-media", "local_prefix": str(media_root)}],
                "output": {"adapter": "jellyfin_trickplay", "width": 320, "frame_interval": 5},
            },
        ]
        registry = ServerRegistry.from_settings(raw_servers, legacy_config=base_config)

        canonical = str(media_root / "Movies" / "Test Movie H264 (2024)" / "Test Movie H264 (2024).mkv")
        emby_sidecar = Path(canonical).parent / "Test Movie H264 (2024)-320-5.bif"
        if emby_sidecar.exists():
            emby_sidecar.unlink()

        try:
            result = process_canonical_path(
                canonical_path=canonical,
                registry=registry,
                config=base_config,
                gpu=None,
                gpu_device_path=None,
            )

            # At least one publisher succeeded → overall PUBLISHED.
            assert result.status is MultiServerStatus.PUBLISHED, result.message
            statuses = {p.adapter_name: p.status for p in result.publishers}
            assert statuses.get("emby_sidecar") is PublisherStatus.PUBLISHED
            # Jellyfin's lookup of remote_path → item_id hits the dead
            # endpoint and returns None; the adapter then raises
            # ValueError because no item_id, which surfaces as FAILED.
            assert statuses.get("jellyfin_trickplay") is PublisherStatus.FAILED, statuses
            # The Emby BIF still landed.
            assert emby_sidecar.exists()
        finally:
            if emby_sidecar.exists():
                emby_sidecar.unlink()


@pytest.mark.integration
class TestPathMappingEdgeCases:
    """Path mappings need to handle multi-disk and prefix collision."""

    def test_overlapping_prefixes_pick_most_specific(self, emby_credentials, media_root, base_config):
        """Two mappings: ``/em-media`` and ``/em-media/Movies``. The longer
        prefix should win for files under it.

        Both prefixes are valid; the longer one is more specific.
        """
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
                "path_mappings": [
                    {"remote_prefix": "/em-media", "local_prefix": str(media_root)},
                    # Redundant longer mapping — must not break ownership.
                    {
                        "remote_prefix": "/em-media/Movies",
                        "local_prefix": str(media_root / "Movies"),
                    },
                ],
                "output": {"adapter": "emby_sidecar", "width": 320, "frame_interval": 5},
            }
        ]
        registry = ServerRegistry.from_settings(raw_servers, legacy_config=base_config)

        canonical = str(media_root / "Movies" / "Test Movie H264 (2024)" / "Test Movie H264 (2024).mkv")
        matches = registry.find_owning_servers(canonical)
        assert len(matches) == 1, [m.server_id for m in matches]
        assert matches[0].server_id == "emby-int-1"

    def test_prefix_collision_does_not_falsely_match(self, emby_credentials, media_root, base_config):
        """`/em-media/Movies` library should NOT match a path under
        `/em-media/Movies-Archive/...` even though the strings share a
        prefix. (Folder-bounded matching.)
        """
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
                "libraries": [
                    {"id": "movies", "name": "Movies", "remote_paths": ["/em-media/Movies"], "enabled": True}
                ],
                "path_mappings": [{"remote_prefix": "/em-media", "local_prefix": str(media_root)}],
                "output": {"adapter": "emby_sidecar", "width": 320, "frame_interval": 5},
            }
        ]
        registry = ServerRegistry.from_settings(raw_servers, legacy_config=base_config)

        # A canonical path under "Movies-Archive" shouldn't match the
        # "Movies" library prefix even though the strings share /em-media/Movies.
        canonical = str(media_root / "Movies-Archive" / "Old Movie" / "Old Movie.mkv")
        matches = registry.find_owning_servers(canonical)
        assert matches == [], [m.local_prefix for m in matches]


@pytest.mark.integration
class TestDisabledLibrary:
    def test_disabled_library_skips_permanently(self, emby_credentials, media_root, base_config):
        """When a library has enabled=False, find_owning_servers returns nothing."""
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
                "libraries": [
                    {
                        "id": "movies",
                        "name": "Movies",
                        "remote_paths": ["/em-media/Movies"],
                        "enabled": False,  # disabled
                    }
                ],
                "path_mappings": [{"remote_prefix": "/em-media", "local_prefix": str(media_root)}],
                "output": {"adapter": "emby_sidecar", "width": 320, "frame_interval": 5},
            }
        ]
        registry = ServerRegistry.from_settings(raw_servers, legacy_config=base_config)

        canonical = str(media_root / "Movies" / "Test Movie H264 (2024)" / "Test Movie H264 (2024).mkv")
        matches = registry.find_owning_servers(canonical)
        # Disabled library means no owners.
        assert matches == []

        # Dispatching reports NO_OWNERS without running FFmpeg.
        result = process_canonical_path(
            canonical_path=canonical,
            registry=registry,
            config=base_config,
        )
        assert result.status is MultiServerStatus.NO_OWNERS


@pytest.mark.integration
@pytest.mark.real_plex_server
class TestLibraryNotYetIndexed:
    """Plex bundle adapter should raise LibraryNotYetIndexedError when /tree returns no MediaParts."""

    def test_unindexed_item_id_routes_to_skipped_not_indexed(self, plex_credentials, media_root, tmp_path):
        """Use a fake item id Plex doesn't know about → /tree returns
        empty → adapter raises LibraryNotYetIndexedError → publisher
        status is SKIPPED_NOT_INDEXED rather than FAILED."""
        from media_preview_generator.output import BifBundle, PlexBundleAdapter
        from media_preview_generator.servers.plex import PlexServer

        config = MagicMock()
        config.plex_url = plex_credentials["PLEX_URL"]
        config.plex_token = plex_credentials["PLEX_ACCESS_TOKEN"]
        config.plex_timeout = 60
        config.plex_verify_ssl = True
        config.plex_libraries = ["Movies"]
        config.plex_library_ids = None

        server = PlexServer(config, server_id="plex-int-1", name="Test Plex")
        adapter = PlexBundleAdapter(plex_config_folder=str(tmp_path), frame_interval=5)
        bundle = BifBundle(
            canonical_path=str(media_root / "fake.mkv"),
            frame_dir=Path("."),
            bif_path=None,
            frame_interval=5,
            width=320,
            height=180,
            frame_count=0,
        )
        # 999999 = a Plex ratingKey that doesn't exist in this library.
        with pytest.raises(LibraryNotYetIndexedError):
            adapter.compute_output_paths(bundle, server, item_id="999999")


@pytest.mark.integration
class TestServerIdentityDisambiguation:
    """Two Emby configs pointing at the same live container — server_identity matching wins."""

    def test_inbound_webhook_with_matching_identity_routes_correctly(
        self, emby_credentials, media_root, base_config, tmp_path, monkeypatch
    ):
        """Two Emby entries (same live container, different settings IDs);
        an inbound Emby webhook with the right Server.Id routes to the
        entry whose ``server_identity`` matches.

        Single live container — identity is `EMBY_SERVER_ID`. We
        configure two entries, give them DIFFERENT settings IDs, but
        only one has the matching ``server_identity``. The router
        should pick that one.
        """
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
                    "id": "emby-A",  # this one has the matching identity
                    "type": "emby",
                    "name": "Real Emby",
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
                },
                {
                    "id": "emby-B",  # different identity — should NOT receive
                    "type": "emby",
                    "name": "Other Emby",
                    "enabled": True,
                    "url": "http://127.0.0.1:9999",  # dead URL
                    "auth": {"method": "api_key", "api_key": "x"},
                    "server_identity": "totally-different-server-id",
                    "libraries": [],
                    "path_mappings": [],
                    "output": {"adapter": "emby_sidecar", "width": 320, "frame_interval": 5},
                },
            ],
        )
        settings.complete_setup()

        monkeypatch.setattr(
            "media_preview_generator.web.webhook_router._load_config_or_minimal",
            lambda: base_config,
        )

        # Look up a real item id from the live Emby.
        import requests

        items_resp = requests.get(
            f"{emby_credentials['EMBY_URL']}/Items",
            params={
                "Recursive": "true",
                "IncludeItemTypes": "Movie",
                "Fields": "Path",
                "Limit": 5,
            },
            headers={"X-Emby-Token": emby_credentials["EMBY_ACCESS_TOKEN"]},
            timeout=10,
        )
        items_resp.raise_for_status()
        target = next(i for i in items_resp.json().get("Items", []) if "H264" in (i.get("Path") or ""))
        target_id = target["Id"]
        canonical = target["Path"].replace("/em-media", str(media_root), 1)
        sidecar = Path(canonical).parent / "Test Movie H264 (2024)-320-5.bif"
        if sidecar.exists():
            sidecar.unlink()

        try:
            response = app.test_client().post(
                "/api/webhooks/incoming",
                headers={
                    "X-Auth-Token": "integration-secret",
                    "Content-Type": "application/json",
                },
                data=json.dumps(
                    {
                        "Event": "library.new",
                        "Item": {"Id": target_id},
                        "Server": {"Id": emby_credentials["EMBY_SERVER_ID"]},
                    }
                ),
            )

            assert response.status_code == 200, response.get_data(as_text=True)
            body = response.get_json()
            assert body["kind"] == "emby"
            # Only emby-A should publish — emby-B has wrong identity
            # AND wrong libraries (empty), so it's not in the
            # publisher list.
            publisher_ids = [p["server_id"] for p in body.get("publishers", [])]
            assert "emby-A" in publisher_ids
            assert "emby-B" not in publisher_ids
            assert sidecar.exists()
        finally:
            if sidecar.exists():
                sidecar.unlink()
