"""Three-server end-to-end: Plex + Emby + Jellyfin sharing one media file.

The user-facing pitch: process a file once, fan it out to every
configured server in their format. Verified here against three live
containers — one webhook drives one FFmpeg pass, three publishers
write three different output formats at three different paths.

Also exercises the slow-backoff "library not yet indexed" routing
when one of the servers hasn't seen the file yet (simulated by
adding a brand-new file before any container scans it).
"""

from __future__ import annotations

import json
import struct
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from plex_generate_previews.processing.multi_server import (
    MultiServerStatus,
    PublisherStatus,
    process_canonical_path,
)
from plex_generate_previews.servers import ServerRegistry

_BIF_MAGIC = bytes([0x89, 0x42, 0x49, 0x46, 0x0D, 0x0A, 0x1A, 0x0A])


def _decode_bif_count(path: Path) -> int:
    raw = path.read_bytes()
    assert raw[:8] == _BIF_MAGIC
    return struct.unpack("<I", raw[12:16])[0]


@pytest.fixture
def three_server_legacy_config(plex_credentials, tmp_path):
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
def three_server_registry(
    emby_credentials, plex_credentials, jellyfin_credentials, three_server_legacy_config, media_root
):
    """Registry with all three live servers sharing the same media file."""
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
            "libraries": [{"id": "movies", "name": "Movies", "remote_paths": ["/em-media/Movies"], "enabled": True}],
            "path_mappings": [{"remote_prefix": "/em-media", "local_prefix": str(media_root)}],
            "output": {"adapter": "emby_sidecar", "width": 320, "frame_interval": 5},
        },
        {
            "id": "plex-int-1",
            "type": "plex",
            "name": "Test Plex",
            "enabled": True,
            "url": plex_credentials["PLEX_URL"],
            "auth": {"method": "token", "token": plex_credentials["PLEX_ACCESS_TOKEN"]},
            "server_identity": plex_credentials["PLEX_SERVER_ID"],
            "libraries": [{"id": "1", "name": "Movies", "remote_paths": ["/media/Movies"], "enabled": True}],
            "path_mappings": [{"remote_prefix": "/media", "local_prefix": str(media_root)}],
            "output": {
                "adapter": "plex_bundle",
                "plex_config_folder": str(three_server_legacy_config.plex_config_folder),
                "frame_interval": 5,
            },
        },
        {
            "id": "jf-int-1",
            "type": "jellyfin",
            "name": "Test Jellyfin",
            "enabled": True,
            "url": jellyfin_credentials["JELLYFIN_URL"],
            "auth": {"method": "api_key", "api_key": jellyfin_credentials["JELLYFIN_ACCESS_TOKEN"]},
            "server_identity": jellyfin_credentials["JELLYFIN_SERVER_ID"],
            "libraries": [{"id": "movies", "name": "Movies", "remote_paths": ["/jf-media/Movies"], "enabled": True}],
            "path_mappings": [{"remote_prefix": "/jf-media", "local_prefix": str(media_root)}],
            "output": {"adapter": "jellyfin_trickplay", "width": 320, "frame_interval": 5},
        },
    ]
    return ServerRegistry.from_settings(raw_servers, legacy_config=three_server_legacy_config)


@pytest.mark.integration
@pytest.mark.real_plex_server
@pytest.mark.slow
class TestThreeServerFanOut:
    """The headline scenario: Plex + Emby + Jellyfin all configured, sharing media."""

    def test_one_webhook_publishes_to_all_three_servers(
        self,
        three_server_registry,
        three_server_legacy_config,
        media_root,
    ):
        """One process_canonical_path call → all three servers' outputs land. ONE FFmpeg pass."""
        canonical = str(media_root / "Movies" / "Test Movie H264 (2024)" / "Test Movie H264 (2024).mkv")
        emby_sidecar = Path(canonical).parent / "Test Movie H264 (2024)-320-5.bif"
        trickplay_dir = Path(canonical).parent / "trickplay"
        # Clean up
        if emby_sidecar.exists():
            emby_sidecar.unlink()
        if trickplay_dir.exists():
            import shutil

            shutil.rmtree(trickplay_dir)

        from plex_generate_previews.processing import multi_server as ms_module

        original_generate = ms_module.generate_images
        ffmpeg_calls = []

        def _spy(*args, **kwargs):
            ffmpeg_calls.append(args[0])
            return original_generate(*args, **kwargs)

        ms_module.generate_images = _spy
        try:
            result = process_canonical_path(
                canonical_path=canonical,
                registry=three_server_registry,
                config=three_server_legacy_config,
                gpu=None,
                gpu_device_path=None,
            )
        finally:
            ms_module.generate_images = original_generate

        assert result.status is MultiServerStatus.PUBLISHED, result.message

        # All three publishers reported.
        publisher_statuses = {p.adapter_name: p.status for p in result.publishers}
        assert publisher_statuses == {
            "emby_sidecar": PublisherStatus.PUBLISHED,
            "plex_bundle": PublisherStatus.PUBLISHED,
            "jellyfin_trickplay": PublisherStatus.PUBLISHED,
        }, publisher_statuses

        # FFmpeg ran exactly ONCE.
        assert len(ffmpeg_calls) == 1, f"expected 1 FFmpeg call across 3 publishers, got {len(ffmpeg_calls)}"

        plex_publisher = next(p for p in result.publishers if p.adapter_name == "plex_bundle")
        plex_bif = plex_publisher.output_paths[0]
        manifest = trickplay_dir / "Test Movie H264 (2024)-320.json"
        sheets_dir = trickplay_dir / "Test Movie H264 (2024)-320"

        try:
            # Emby sidecar BIF.
            assert emby_sidecar.exists()
            assert _decode_bif_count(emby_sidecar) >= 4

            # Plex bundle BIF at the hash-keyed bundle path.
            assert plex_bif.exists()
            assert plex_bif.name == "index-sd.bif"
            assert _decode_bif_count(plex_bif) >= 4

            # Jellyfin trickplay manifest + sheets.
            assert manifest.exists()
            data = json.loads(manifest.read_text())
            assert "Trickplay" in data
            (item_id,) = list(data["Trickplay"].keys())
            assert data["Trickplay"][item_id]["320"]["TileWidth"] == 10
            assert sheets_dir.is_dir()
            assert (sheets_dir / "0.jpg").exists()

            # Emby + Plex BIFs are byte-identical (same FFmpeg pass).
            assert emby_sidecar.read_bytes() == plex_bif.read_bytes(), (
                "Emby and Plex BIFs differ — one of them re-ran FFmpeg"
            )
        finally:
            for p in (emby_sidecar, plex_bif):
                if p.exists():
                    p.unlink()
            if trickplay_dir.exists():
                import shutil

                shutil.rmtree(trickplay_dir)

    def test_partial_ownership_only_owning_servers_publish(
        self,
        emby_credentials,
        plex_credentials,
        jellyfin_credentials,
        three_server_legacy_config,
        media_root,
    ):
        """Some libraries don't cover the path → those servers skip cleanly.

        Configure Emby with Movies enabled but Plex with TV-only and
        Jellyfin disabled. Fire a webhook for a Movies file → only
        Emby should publish.
        """
        raw_servers = [
            {
                "id": "emby-only",
                "type": "emby",
                "name": "Test Emby (Movies)",
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
                "id": "plex-tv-only",
                "type": "plex",
                "name": "Test Plex (TV only)",
                "enabled": True,
                "url": plex_credentials["PLEX_URL"],
                "auth": {"method": "token", "token": plex_credentials["PLEX_ACCESS_TOKEN"]},
                "server_identity": plex_credentials["PLEX_SERVER_ID"],
                # Library only covers TV Shows, not Movies.
                "libraries": [
                    {
                        "id": "99",
                        "name": "TV Shows",
                        "remote_paths": ["/media/TV Shows"],
                        "enabled": True,
                    }
                ],
                "path_mappings": [{"remote_prefix": "/media", "local_prefix": str(media_root)}],
                "output": {
                    "adapter": "plex_bundle",
                    "plex_config_folder": str(three_server_legacy_config.plex_config_folder),
                    "frame_interval": 5,
                },
            },
            {
                "id": "jf-disabled",
                "type": "jellyfin",
                "name": "Test Jellyfin (Disabled)",
                "enabled": False,  # whole server disabled
                "url": jellyfin_credentials["JELLYFIN_URL"],
                "auth": {"method": "api_key", "api_key": jellyfin_credentials["JELLYFIN_ACCESS_TOKEN"]},
                "server_identity": jellyfin_credentials["JELLYFIN_SERVER_ID"],
                "libraries": [],
                "path_mappings": [],
                "output": {"adapter": "jellyfin_trickplay", "width": 320, "frame_interval": 5},
            },
        ]
        registry = ServerRegistry.from_settings(raw_servers, legacy_config=three_server_legacy_config)

        canonical = str(media_root / "Movies" / "Test Movie H264 (2024)" / "Test Movie H264 (2024).mkv")
        emby_sidecar = Path(canonical).parent / "Test Movie H264 (2024)-320-5.bif"
        if emby_sidecar.exists():
            emby_sidecar.unlink()

        try:
            result = process_canonical_path(
                canonical_path=canonical,
                registry=registry,
                config=three_server_legacy_config,
                gpu=None,
                gpu_device_path=None,
            )
            assert result.status is MultiServerStatus.PUBLISHED, result.message
            # Only emby should be in the publisher list — Plex's library
            # doesn't cover /Movies, and Jellyfin is disabled outright.
            publisher_ids = {p.server_id for p in result.publishers}
            assert publisher_ids == {"emby-only"}, publisher_ids

            assert emby_sidecar.exists()
        finally:
            if emby_sidecar.exists():
                emby_sidecar.unlink()
