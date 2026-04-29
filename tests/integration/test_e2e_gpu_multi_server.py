"""End-to-end test: GPU FFmpeg + multi-publisher fan-out.

The whole headline of this tool is GPU-accelerated thumbnail generation.
Every existing integration test runs CPU FFmpeg. This test exercises
the real GPU pipeline (NVENC/CUDA on the SSH host's NVIDIA TITAN RTX)
combined with the multi-server fan-out.

Marker-gated (``@pytest.mark.gpu``) so CI workers without NVIDIA hardware
skip cleanly. Run locally with::

    pytest -m gpu --no-cov -o addopts='' tests/integration/test_e2e_gpu_multi_server.py

What the test verifies:

* Real CUDA/NVENC FFmpeg invocation — captured via the same ``_spy``
  pattern used in ``test_e2e_three_server.py``. The spy inspects the
  stored generate_images call args; we assert ``gpu`` was non-None
  (CPU path passes None).
* All three publishers (Emby + Plex + Jellyfin) land their format.
* Emby + Plex BIFs are byte-identical — confirms 1 FFmpeg pass fed
  both, even on the GPU path.
"""

from __future__ import annotations

import struct
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from media_preview_generator.processing.multi_server import (
    MultiServerStatus,
    PublisherStatus,
    process_canonical_path,
)
from media_preview_generator.servers import ServerRegistry

_BIF_MAGIC = bytes([0x89, 0x42, 0x49, 0x46, 0x0D, 0x0A, 0x1A, 0x0A])


def _has_nvidia_gpu() -> tuple[str, str] | None:
    """Return ``(gpu_type, gpu_device)`` for the first NVIDIA GPU, or None."""
    try:
        from media_preview_generator.gpu.detect import detect_all_gpus

        for gpu_type, gpu_device, _info in detect_all_gpus():
            if gpu_type == "NVIDIA":
                return gpu_type, gpu_device
    except Exception:
        return None
    return None


@pytest.fixture
def gpu_config(tmp_path, plex_credentials):
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
    config.gpu_threads = 1
    config.cpu_threads = 0
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
def gpu_three_server_registry(emby_credentials, plex_credentials, jellyfin_credentials, gpu_config, media_root):
    raw_servers = [
        {
            "id": "emby-gpu",
            "type": "emby",
            "name": "Test Emby (GPU)",
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
            "id": "plex-gpu",
            "type": "plex",
            "name": "Test Plex (GPU)",
            "enabled": True,
            "url": plex_credentials["PLEX_URL"],
            "auth": {"method": "token", "token": plex_credentials["PLEX_ACCESS_TOKEN"]},
            "server_identity": plex_credentials["PLEX_SERVER_ID"],
            "libraries": [{"id": "1", "name": "Movies", "remote_paths": ["/media/Movies"], "enabled": True}],
            "path_mappings": [{"remote_prefix": "/media", "local_prefix": str(media_root)}],
            "output": {
                "adapter": "plex_bundle",
                "plex_config_folder": str(gpu_config.plex_config_folder),
                "frame_interval": 5,
            },
        },
        {
            "id": "jf-gpu",
            "type": "jellyfin",
            "name": "Test Jellyfin (GPU)",
            "enabled": True,
            "url": jellyfin_credentials["JELLYFIN_URL"],
            "auth": {"method": "api_key", "api_key": jellyfin_credentials["JELLYFIN_ACCESS_TOKEN"]},
            "server_identity": jellyfin_credentials["JELLYFIN_SERVER_ID"],
            "libraries": [{"id": "movies", "name": "Movies", "remote_paths": ["/jf-media/Movies"], "enabled": True}],
            "path_mappings": [{"remote_prefix": "/jf-media", "local_prefix": str(media_root)}],
            "output": {"adapter": "jellyfin_trickplay", "width": 320, "frame_interval": 5},
        },
    ]
    return ServerRegistry.from_settings(raw_servers, legacy_config=gpu_config)


@pytest.mark.integration
@pytest.mark.gpu
@pytest.mark.real_plex_server
@pytest.mark.real_gpu_detection
@pytest.mark.slow
class TestGpuMultiPublisher:
    """Real NVENC/CUDA FFmpeg + 3-server fan-out."""

    def test_gpu_pipeline_publishes_to_all_three_servers(
        self,
        gpu_three_server_registry,
        gpu_config,
        media_root,
    ):
        gpu = _has_nvidia_gpu()
        if gpu is None:
            pytest.skip("No NVIDIA GPU detected on this host")
        gpu_type, gpu_device = gpu

        canonical = str(media_root / "Movies" / "Test Movie H264 (2024)" / "Test Movie H264 (2024).mkv")
        emby_sidecar = Path(canonical).parent / "Test Movie H264 (2024)-320-5.bif"
        trickplay_dir = Path(canonical).parent / "trickplay"

        # Clean prior outputs so we know this run produced them.
        if emby_sidecar.exists():
            emby_sidecar.unlink()
        if trickplay_dir.exists():
            import shutil

            shutil.rmtree(trickplay_dir)

        # Reset frame cache so we don't hit cached CPU frames from earlier tests.
        from media_preview_generator.processing import frame_cache as fc_module
        from media_preview_generator.processing import multi_server as ms_module

        fc_module._singleton = None  # noqa: SLF001 — test override

        original_generate = ms_module.generate_images
        captured_calls: list[dict] = []

        def _spy(media_file, tmp_path_arg, gpu_arg, gpu_device_arg, *args, **kwargs):
            captured_calls.append(
                {
                    "media_file": media_file,
                    "gpu": gpu_arg,
                    "gpu_device": gpu_device_arg,
                }
            )
            return original_generate(media_file, tmp_path_arg, gpu_arg, gpu_device_arg, *args, **kwargs)

        ms_module.generate_images = _spy

        try:
            result = process_canonical_path(
                canonical_path=canonical,
                registry=gpu_three_server_registry,
                config=gpu_config,
                gpu=gpu_type,
                gpu_device_path=gpu_device,
            )
        finally:
            ms_module.generate_images = original_generate

        assert result.status is MultiServerStatus.PUBLISHED, result.message

        publisher_statuses = {p.adapter_name: p.status for p in result.publishers}
        assert publisher_statuses == {
            "emby_sidecar": PublisherStatus.PUBLISHED,
            "plex_bundle": PublisherStatus.PUBLISHED,
            "jellyfin_trickplay": PublisherStatus.PUBLISHED,
        }, publisher_statuses

        # GPU was actually used — generate_images was invoked with the
        # GPU type + device path we passed in (not None).
        assert len(captured_calls) == 1, (
            f"Expected exactly 1 FFmpeg pass across 3 publishers, got {len(captured_calls)}"
        )
        assert captured_calls[0]["gpu"] == gpu_type, captured_calls[0]
        assert captured_calls[0]["gpu_device"] == gpu_device, captured_calls[0]

        # All three formats land.
        plex_publisher = next(p for p in result.publishers if p.adapter_name == "plex_bundle")
        plex_bif = plex_publisher.output_paths[0]

        try:
            assert emby_sidecar.exists()
            assert emby_sidecar.read_bytes()[:8] == _BIF_MAGIC
            assert plex_bif.exists()
            assert plex_bif.read_bytes()[:8] == _BIF_MAGIC
            assert (trickplay_dir / "Test Movie H264 (2024)-320.json").exists()

            # Frame count agrees across Emby + Plex BIFs (both fed from
            # the same FFmpeg pass).
            emby_count = struct.unpack("<I", emby_sidecar.read_bytes()[12:16])[0]
            plex_count = struct.unpack("<I", plex_bif.read_bytes()[12:16])[0]
            assert emby_count == plex_count, (
                f"Frame count mismatch: emby={emby_count}, plex={plex_count} — different FFmpeg passes?"
            )
            # And in fact byte-identical.
            assert emby_sidecar.read_bytes() == plex_bif.read_bytes(), (
                "Emby and Plex BIFs differ — multiple FFmpeg passes?"
            )
        finally:
            for p in (emby_sidecar, plex_bif):
                if p.exists():
                    p.unlink()
            if trickplay_dir.exists():
                import shutil

                shutil.rmtree(trickplay_dir)
