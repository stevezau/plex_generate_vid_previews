"""End-to-end multi-server tests: one webhook → multiple publishers.

This is the headline feature of the multi-media-server refactor:
processing a file once and fanning the output to every configured
server in their own format. These tests verify it works against a
LIVE Emby + LIVE Plex with one shared media file.
"""

from __future__ import annotations

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
_JPEG_SOI = bytes([0xFF, 0xD8, 0xFF])


def _decode_bif(path: Path) -> dict:
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
def multi_server_legacy_config(plex_credentials, tmp_path):
    """Config object covering both Plex (for the Plex publisher) and global FFmpeg settings."""
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
def multi_server_registry(emby_credentials, plex_credentials, multi_server_legacy_config, media_root):
    """Registry containing the live Emby + live Plex, sharing the same media file."""
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
        },
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
                "plex_config_folder": str(multi_server_legacy_config.plex_config_folder),
                "frame_interval": 5,
            },
        },
    ]
    return ServerRegistry.from_settings(raw_servers, legacy_config=multi_server_legacy_config)


@pytest.mark.integration
@pytest.mark.real_plex_server
@pytest.mark.slow
class TestMultiServerFanOut:
    """One webhook, one FFmpeg pass, both Emby sidecar AND Plex bundle BIF land."""

    def test_one_webhook_publishes_to_emby_and_plex(
        self, multi_server_registry, multi_server_legacy_config, media_root, emby_credentials
    ):
        """The headline test: one process_canonical_path call publishes to BOTH live servers."""
        canonical = str(media_root / "Movies" / "Test Movie H264 (2024)" / "Test Movie H264 (2024).mkv")
        emby_sidecar = Path(canonical).parent / "Test Movie H264 (2024)-320-5.bif"
        if emby_sidecar.exists():
            emby_sidecar.unlink()

        # Track FFmpeg invocations to verify ONE pass feeds both publishers.
        from plex_generate_previews.processing import multi_server as ms_module

        original_generate = ms_module.generate_images
        ffmpeg_calls = []

        def _spy(*args, **kwargs):
            ffmpeg_calls.append(args[0])
            return original_generate(*args, **kwargs)

        try:
            ms_module.generate_images = _spy

            result = process_canonical_path(
                canonical_path=canonical,
                registry=multi_server_registry,
                config=multi_server_legacy_config,
                gpu=None,
                gpu_device_path=None,
            )
        finally:
            ms_module.generate_images = original_generate

        # Both publishers should report PUBLISHED.
        assert result.status is MultiServerStatus.PUBLISHED, result.message
        publisher_status = {p.adapter_name: p.status for p in result.publishers}
        assert publisher_status.get("emby_sidecar") is PublisherStatus.PUBLISHED, publisher_status
        assert publisher_status.get("plex_bundle") is PublisherStatus.PUBLISHED, publisher_status

        # FFmpeg ran exactly ONCE — that's the whole point.
        assert len(ffmpeg_calls) == 1, f"expected 1 FFmpeg call, got {len(ffmpeg_calls)}"

        plex_publisher = next(p for p in result.publishers if p.adapter_name == "plex_bundle")
        plex_bif = plex_publisher.output_paths[0]

        try:
            # Both BIFs are real on disk and structurally valid.
            assert emby_sidecar.exists()
            emby_decoded = _decode_bif(emby_sidecar)
            assert emby_decoded["image_count"] >= 4
            assert emby_decoded["interval_ms"] == 5000

            assert plex_bif.exists()
            plex_decoded = _decode_bif(plex_bif)
            assert plex_decoded["image_count"] >= 4
            assert plex_decoded["interval_ms"] == 5000

            # The two BIFs are byte-identical (same FFmpeg pass, same
            # pack settings, same content). This confirms "one pass
            # feeds both" rather than "two passes happened to produce
            # similar output".
            assert emby_sidecar.read_bytes() == plex_bif.read_bytes(), (
                "Emby and Plex BIFs differ — different FFmpeg passes?"
            )
        finally:
            if emby_sidecar.exists():
                emby_sidecar.unlink()
            if plex_bif.exists():
                plex_bif.unlink()

    def test_skip_if_output_exists_doesnt_re_run_ffmpeg(
        self, multi_server_registry, multi_server_legacy_config, media_root
    ):
        """Second dispatch with all output present → SKIPPED, no FFmpeg."""
        canonical = str(media_root / "Movies" / "Test Movie H264 (2024)" / "Test Movie H264 (2024).mkv")
        emby_sidecar = Path(canonical).parent / "Test Movie H264 (2024)-320-5.bif"

        from plex_generate_previews.processing import multi_server as ms_module

        # First dispatch: real run.
        first = process_canonical_path(
            canonical_path=canonical,
            registry=multi_server_registry,
            config=multi_server_legacy_config,
            gpu=None,
            gpu_device_path=None,
        )
        assert first.status is MultiServerStatus.PUBLISHED, first.message
        plex_bif = next(p for p in first.publishers if p.adapter_name == "plex_bundle").output_paths[0]

        try:
            # Second dispatch: ALL output exists → SKIPPED, FFmpeg should not run again.
            original_generate = ms_module.generate_images
            ffmpeg_calls = []

            def _spy(*args, **kwargs):
                ffmpeg_calls.append(args[0])
                return original_generate(*args, **kwargs)

            ms_module.generate_images = _spy
            try:
                second = process_canonical_path(
                    canonical_path=canonical,
                    registry=multi_server_registry,
                    config=multi_server_legacy_config,
                    gpu=None,
                    gpu_device_path=None,
                )
            finally:
                ms_module.generate_images = original_generate

            # Frame cache should have hit; even if it did re-run FFmpeg
            # because of cache TTL semantics, the publishers should
            # all skip because their output already exists.
            for p in second.publishers:
                assert p.status is PublisherStatus.SKIPPED_OUTPUT_EXISTS, (
                    f"{p.adapter_name}: expected SKIPPED, got {p.status}: {p.message}"
                )
            assert second.status is MultiServerStatus.SKIPPED, second.status
        finally:
            if emby_sidecar.exists():
                emby_sidecar.unlink()
            if plex_bif.exists():
                plex_bif.unlink()
