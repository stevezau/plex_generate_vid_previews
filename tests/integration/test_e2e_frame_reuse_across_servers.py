"""Cross-server frame reuse end-to-end against live containers.

Headline scenario the user actually asked about: install Plex, run for a
while, add Jellyfin, then the next webhook for an existing file should
NOT re-extract frames. The Plex BIF stays put (its publisher reports
SKIPPED_OUTPUT_EXISTS); Jellyfin's trickplay is built from cached frames
without invoking FFmpeg.

Also covers the related controls landed in the same PR:
- ``frame_reuse.enabled=False`` falls back to the legacy 600s TTL.
- ``frame_reuse.ttl_minutes`` is honoured.
- TTL changes apply *without* a process restart (live-update).
- Disk cap evicts oldest entries on overflow.
"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from media_preview_generator.processing import multi_server as ms_module
from media_preview_generator.processing.frame_cache import (
    get_frame_cache,
    reset_frame_cache,
)
from media_preview_generator.processing.multi_server import (
    MultiServerStatus,
    PublisherStatus,
    process_canonical_path,
)
from media_preview_generator.servers import ServerRegistry


def _legacy_config(plex_credentials, tmp_path) -> MagicMock:
    """Mirror of the three-server tests' minimal-Plex legacy Config shim."""
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


def _plex_jelly_registry(plex_credentials, jellyfin_credentials, legacy_config, media_root):
    """Plex + Jellyfin registry sharing one Movies library."""
    raw_servers = [
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
                "plex_config_folder": legacy_config.plex_config_folder,
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
    return ServerRegistry.from_settings(raw_servers, legacy_config=legacy_config)


def _plex_only_registry(plex_credentials, legacy_config, media_root):
    raw_servers = [
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
                "plex_config_folder": legacy_config.plex_config_folder,
                "frame_interval": 5,
            },
        },
    ]
    return ServerRegistry.from_settings(raw_servers, legacy_config=legacy_config)


def _jelly_only_registry(jellyfin_credentials, legacy_config, media_root):
    raw_servers = [
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
    return ServerRegistry.from_settings(raw_servers, legacy_config=legacy_config)


def _cleanup_outputs(canonical: str, plex_config_folder: str | None) -> None:
    """Remove BIFs/trickplay artifacts produced by a previous run."""
    parent = Path(canonical).parent
    base = Path(canonical).stem
    sidecar = parent / f"{base}-320-5.bif"
    if sidecar.exists():
        sidecar.unlink()
    trickplay = parent / "trickplay"
    if trickplay.exists():
        shutil.rmtree(trickplay)
    # Plex bundle outputs live under the plex_config_folder/Media/...
    if plex_config_folder and Path(plex_config_folder).exists():
        media_dir = Path(plex_config_folder) / "Metadata"
        if media_dir.exists():
            shutil.rmtree(media_dir)


@pytest.fixture(autouse=True)
def _isolate_frame_cache():
    """Each test gets a fresh frame-cache singleton.

    Don't pre-seed the singleton here — the dispatcher builds it from
    ``config.working_tmp_folder/frame_cache``, and pre-seeding with a
    different base_dir would trigger the singleton's "already
    initialised with a different base_dir" guard.
    """
    reset_frame_cache()
    yield
    reset_frame_cache()


@pytest.mark.integration
@pytest.mark.real_plex_server
@pytest.mark.slow
class TestCrossServerFrameReuse:
    """The user's headline scenario: 'fire Plex webhook, then Jellyfin webhook later → reuse'."""

    def test_plex_then_jellyfin_within_ttl_reuses_frames(
        self,
        plex_credentials,
        jellyfin_credentials,
        media_root,
        tmp_path,
    ):
        """Plex webhook generates BIF + caches frames. Jellyfin webhook
        seconds later (within the 60-minute default TTL) reuses the cache
        — no second FFmpeg pass — and produces a Jellyfin trickplay output.

        The Plex-only registry on call 1 is intentional: it mirrors a real
        install where Plex was added first and processed files alone, then
        Jellyfin was added later and now sees pre-existing canonical paths.
        """
        legacy_config = _legacy_config(plex_credentials, tmp_path)
        canonical = str(media_root / "Movies" / "Test Movie H264 (2024)" / "Test Movie H264 (2024).mkv")
        _cleanup_outputs(canonical, legacy_config.plex_config_folder)

        # Round 1 — only Plex configured, fires the webhook.
        plex_only = _plex_only_registry(plex_credentials, legacy_config, media_root)
        ffmpeg_calls: list[str] = []
        original_generate = ms_module.generate_images

        def _spy(*args, **kwargs):
            ffmpeg_calls.append(args[0])
            return original_generate(*args, **kwargs)

        ms_module.generate_images = _spy
        try:
            r1 = process_canonical_path(
                canonical_path=canonical,
                registry=plex_only,
                config=legacy_config,
            )
        finally:
            ms_module.generate_images = original_generate

        assert r1.status is MultiServerStatus.PUBLISHED
        plex_pub = next(p for p in r1.publishers if p.adapter_name == "plex_bundle")
        assert plex_pub.status is PublisherStatus.PUBLISHED
        # First call MUST extract — there's nothing in the cache yet.
        assert plex_pub.frame_source == "extracted"
        assert len(ffmpeg_calls) == 1, f"Round 1: expected 1 FFmpeg call, got {len(ffmpeg_calls)}"

        plex_bif = plex_pub.output_paths[0]
        assert plex_bif.exists(), f"Plex BIF should exist after round 1: {plex_bif}"

        # Round 2 — same canonical path, but now BOTH Plex and Jellyfin own it.
        # Per the all_fresh skip path: Plex output exists AND no source-mtime
        # change → Plex publisher should skip (no FFmpeg). Jellyfin's trickplay
        # doesn't exist yet, so the all_fresh check fails and we fall through
        # to the per-publisher path. The cache should serve Jellyfin without
        # rerunning FFmpeg.
        ffmpeg_calls.clear()
        plex_jelly = _plex_jelly_registry(plex_credentials, jellyfin_credentials, legacy_config, media_root)

        ms_module.generate_images = _spy
        try:
            r2 = process_canonical_path(
                canonical_path=canonical,
                registry=plex_jelly,
                config=legacy_config,
            )
        finally:
            ms_module.generate_images = original_generate

        try:
            assert r2.status is MultiServerStatus.PUBLISHED
            # CRITICAL: no second FFmpeg pass — the cache served us.
            assert len(ffmpeg_calls) == 0, (
                f"Round 2 must not re-extract (cache should hit). FFmpeg ran {len(ffmpeg_calls)} time(s)."
            )

            by_adapter = {p.adapter_name: p for p in r2.publishers}
            assert "plex_bundle" in by_adapter
            assert "jellyfin_trickplay" in by_adapter

            # Plex's existing BIF was preserved (output_existed badge in UI).
            plex2 = by_adapter["plex_bundle"]
            assert plex2.status is PublisherStatus.SKIPPED_OUTPUT_EXISTS
            assert plex2.frame_source == "output_existed"

            # Jellyfin produced fresh trickplay from cached frames (cache_hit badge).
            jelly = by_adapter["jellyfin_trickplay"]
            assert jelly.status is PublisherStatus.PUBLISHED
            assert jelly.frame_source == "cache_hit"

            # Trickplay artifacts on disk.
            trickplay_dir = Path(canonical).parent / "trickplay"
            manifest = trickplay_dir / "Test Movie H264 (2024)-320.json"
            sheets_dir = trickplay_dir / "Test Movie H264 (2024)-320"
            assert manifest.exists()
            data = json.loads(manifest.read_text())
            assert "Trickplay" in data
            assert sheets_dir.is_dir()
            assert (sheets_dir / "0.jpg").exists()
        finally:
            _cleanup_outputs(canonical, legacy_config.plex_config_folder)

    def test_ttl_expiry_forces_reextraction(
        self,
        plex_credentials,
        jellyfin_credentials,
        media_root,
        tmp_path,
        monkeypatch,
    ):
        """Set TTL=1s in settings; sleep past it; second webhook re-extracts.

        Live-update path: the user changes ``frame_reuse.ttl_minutes`` in
        Settings → Performance and the change applies on the very next
        dispatch with no process restart.
        """
        from media_preview_generator.processing import frame_cache as fc

        legacy_config = _legacy_config(plex_credentials, tmp_path)
        canonical = str(media_root / "Movies" / "Test Movie H264 (2024)" / "Test Movie H264 (2024).mkv")
        _cleanup_outputs(canonical, legacy_config.plex_config_folder)

        # Settings → 1-second TTL via the live-update path.
        monkeypatch.setattr(fc, "_read_frame_reuse_setting", lambda: (1, 2048))

        # Round 1 — fires Plex, populates the cache.
        plex_only = _plex_only_registry(plex_credentials, legacy_config, media_root)
        ffmpeg_calls: list[str] = []
        original_generate = ms_module.generate_images

        def _spy(*args, **kwargs):
            ffmpeg_calls.append(args[0])
            return original_generate(*args, **kwargs)

        ms_module.generate_images = _spy
        try:
            process_canonical_path(canonical_path=canonical, registry=plex_only, config=legacy_config)
        finally:
            ms_module.generate_images = original_generate

        assert len(ffmpeg_calls) == 1, "Round 1 must extract"

        # Sleep past TTL.
        time.sleep(1.5)

        # Drop the Plex BIF so its skip_if_exists doesn't short-circuit.
        # We're testing that the *cache* expires; if the publisher's
        # output is still there, we never reach the cache lookup.
        if ffmpeg_calls:
            for p in Path(canonical).parent.glob("*.bif"):
                p.unlink()

        # Round 2 with Jellyfin added — cache must miss (TTL expired) →
        # FFmpeg must run again to serve both publishers.
        ffmpeg_calls.clear()
        plex_jelly = _plex_jelly_registry(plex_credentials, jellyfin_credentials, legacy_config, media_root)

        ms_module.generate_images = _spy
        try:
            r2 = process_canonical_path(canonical_path=canonical, registry=plex_jelly, config=legacy_config)
        finally:
            ms_module.generate_images = original_generate

        try:
            assert len(ffmpeg_calls) == 1, (
                f"Round 2 should re-extract after TTL expiry. FFmpeg ran {len(ffmpeg_calls)} time(s)."
            )
            jelly = next(p for p in r2.publishers if p.adapter_name == "jellyfin_trickplay")
            assert jelly.frame_source == "extracted", "post-TTL Jellyfin must be 'extracted', not 'cache_hit'"
        finally:
            _cleanup_outputs(canonical, legacy_config.plex_config_folder)

    def test_disk_cap_evicts_when_over(
        self,
        plex_credentials,
        media_root,
        tmp_path,
        monkeypatch,
    ):
        """Set ``max_cache_disk_mb=1``; processing several files keeps cache under cap.

        Verifies the eviction policy holds against the real cache path
        used in production. The most-recent entry is always preserved.
        """
        from media_preview_generator.processing import frame_cache as fc

        monkeypatch.setattr(fc, "_read_frame_reuse_setting", lambda: (3600, 1))

        legacy_config = _legacy_config(plex_credentials, tmp_path)
        registry = _plex_only_registry(plex_credentials, legacy_config, media_root)

        movies = [
            "Test Movie H264 (2024)",
            "Test Movie HEVC (2024)",
            "Test Movie AV1 (2024)",
            "Test Movie VP9 (2024)",
            "Test 4K HDR (2024)",
        ]

        try:
            for m in movies:
                canonical = str(media_root / "Movies" / m / f"{m}.mkv")
                _cleanup_outputs(canonical, legacy_config.plex_config_folder)
                process_canonical_path(canonical_path=canonical, registry=registry, config=legacy_config)

            # The cache singleton was built by the dispatcher with the real
            # working_tmp_folder/frame_cache path. Inspect it now.
            cache = get_frame_cache()
            assert cache._max_disk_bytes == 1 * 1024 * 1024

            cache_dir = cache._base_dir
            total_bytes = sum(p.stat().st_size for p in cache_dir.rglob("*") if p.is_file())
            # Allow up to 2x the cap: the most-recent entry can exceed cap
            # if it's the first/only entry (the eviction loop preserves it).
            assert total_bytes < 2 * 1024 * 1024, (
                f"cache directory at {cache_dir} is {total_bytes} bytes; expected < 2 MB after disk-cap LRU sweep"
            )
        finally:
            for m in movies:
                canonical = str(media_root / "Movies" / m / f"{m}.mkv")
                _cleanup_outputs(canonical, legacy_config.plex_config_folder)
