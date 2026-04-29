"""End-to-end smart-dedup tests against live containers.

The journal feature exists for one specific real-world scenario:

* Sonarr finishes importing a file → fires its webhook ~immediately →
  we run FFmpeg, fan out to all servers.
* Plex's own scheduled library scan picks up the file ~30 minutes
  later → its webhook fires → we get the SAME canonical path again.
* The frame cache (10-min TTL) has expired by then.
* But the outputs are still on disk.

Without the journal: we'd extract frames again (wasted FFmpeg work)
and the per-publisher skip-if-exists would then catch the no-op.

With the journal: the pre-FFmpeg short-circuit notices outputs exist
AND match the source's mtime+size, and skips frame extraction
entirely. Whole call takes milliseconds.

If the source file is REPLACED (Sonarr quality upgrade, manual swap),
the mtime+size mismatch forces a regenerate even though the outputs
still exist.
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from media_preview_generator.processing.multi_server import (
    MultiServerStatus,
    PublisherStatus,
    process_canonical_path,
)
from media_preview_generator.servers import ServerRegistry


@pytest.fixture
def dedup_config(tmp_path, plex_credentials):
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
def dedup_registry(emby_credentials, plex_credentials, dedup_config, media_root):
    raw_servers = [
        {
            "id": "emby-dedup",
            "type": "emby",
            "name": "Test Emby (dedup)",
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
            "id": "plex-dedup",
            "type": "plex",
            "name": "Test Plex (dedup)",
            "enabled": True,
            "url": plex_credentials["PLEX_URL"],
            "auth": {"method": "token", "token": plex_credentials["PLEX_ACCESS_TOKEN"]},
            "server_identity": plex_credentials["PLEX_SERVER_ID"],
            "libraries": [{"id": "1", "name": "Movies", "remote_paths": ["/media/Movies"], "enabled": True}],
            "path_mappings": [{"remote_prefix": "/media", "local_prefix": str(media_root)}],
            "output": {
                "adapter": "plex_bundle",
                "plex_config_folder": str(dedup_config.plex_config_folder),
                "frame_interval": 5,
            },
        },
    ]
    return ServerRegistry.from_settings(raw_servers, legacy_config=dedup_config)


@pytest.mark.integration
@pytest.mark.real_plex_server
class TestLateWebhookFollowsSonarr:
    """Sonarr → fan-out → cache TTL expires → Plex's late webhook arrives → no FFmpeg."""

    def test_late_webhook_skips_ffmpeg_when_outputs_match_source(self, dedup_registry, dedup_config, media_root):
        """Headline test for the smart-dedup feature.

        Simulates:
        1. Sonarr webhook fires → FFmpeg runs, .meta journals stamped.
        2. Time passes — frame cache TTL expires (we force-evict it).
        3. Plex's slow-arriving webhook fires for the same file →
           journal short-circuit kicks in → 0 FFmpeg calls.
        """
        canonical = str(media_root / "Movies" / "Test Movie H264 (2024)" / "Test Movie H264 (2024).mkv")
        emby_sidecar = Path(canonical).parent / "Test Movie H264 (2024)-320-5.bif"
        if emby_sidecar.exists():
            emby_sidecar.unlink()

        from media_preview_generator.output.journal import _meta_path_for, clear_meta
        from media_preview_generator.processing import frame_cache as fc_module
        from media_preview_generator.processing import multi_server as ms_module

        # Reset the singleton cache so we're not reusing frames from a
        # prior test in the same session.
        fc_module._singleton = None  # noqa: SLF001 — test override

        # Track FFmpeg invocations.
        original_generate = ms_module.generate_images
        ffmpeg_calls = []

        def _spy(*args, **kwargs):
            ffmpeg_calls.append(args[0])
            return original_generate(*args, **kwargs)

        ms_module.generate_images = _spy
        try:
            # ----- 1st webhook (Sonarr) -----
            first = process_canonical_path(
                canonical_path=canonical,
                registry=dedup_registry,
                config=dedup_config,
                gpu=None,
                gpu_device_path=None,
            )
            assert first.status is MultiServerStatus.PUBLISHED, first.message
            plex_bif = next(p for p in first.publishers if p.adapter_name == "plex_bundle").output_paths[0]
            assert _meta_path_for(emby_sidecar).exists(), "Emby BIF .meta should be stamped after publish"
            assert _meta_path_for(plex_bif).exists(), "Plex BIF .meta should be stamped after publish"
            assert len(ffmpeg_calls) == 1

            # ----- Force the frame cache to "expire" -----
            # Drop singleton so the next call rebuilds it; clears the in-memory
            # entry. Equivalent to TTL having expired in production.
            fc_module._singleton = None  # noqa: SLF001

            # ----- 2nd webhook (Plex's slow-arrival) -----
            second = process_canonical_path(
                canonical_path=canonical,
                registry=dedup_registry,
                config=dedup_config,
                gpu=None,
                gpu_device_path=None,
            )
            assert len(ffmpeg_calls) == 1, (
                f"Pre-FFmpeg short-circuit didn't fire — FFmpeg ran {len(ffmpeg_calls)} times"
            )
            assert second.status is MultiServerStatus.SKIPPED, second.message
            for p in second.publishers:
                assert p.status is PublisherStatus.SKIPPED_OUTPUT_EXISTS, (
                    f"{p.adapter_name}: expected SKIPPED_OUTPUT_EXISTS, got {p.status}"
                )
        finally:
            ms_module.generate_images = original_generate
            if emby_sidecar.exists():
                emby_sidecar.unlink()
            try:
                if plex_bif.exists():
                    plex_bif.unlink()
            except NameError:
                pass
            clear_meta([emby_sidecar, plex_bif] if "plex_bif" in locals() else [emby_sidecar])


@pytest.mark.integration
@pytest.mark.real_plex_server
class TestSourceReplacedRegens:
    """Sonarr "quality upgrade" replaces the source → mtime+size mismatch → regen."""

    def test_replaced_source_forces_regen_even_when_outputs_exist(
        self, dedup_registry, dedup_config, media_root, tmp_path
    ):
        """The source-changed branch of the journal: fingerprint mismatch → regen."""
        # Use a copy so we can mutate without breaking other tests.
        original = media_root / "Movies" / "Test Movie H264 (2024)" / "Test Movie H264 (2024).mkv"
        # We'll mutate THIS file's mtime/size; the test cleans up after itself.
        canonical_path_obj = original
        canonical = str(canonical_path_obj)
        emby_sidecar = canonical_path_obj.parent / "Test Movie H264 (2024)-320-5.bif"
        if emby_sidecar.exists():
            emby_sidecar.unlink()

        original_size = canonical_path_obj.stat().st_size
        original_mtime = canonical_path_obj.stat().st_mtime
        original_bytes = canonical_path_obj.read_bytes()

        from media_preview_generator.output.journal import _meta_path_for, clear_meta
        from media_preview_generator.processing import frame_cache as fc_module
        from media_preview_generator.processing import multi_server as ms_module

        fc_module._singleton = None  # noqa: SLF001

        original_generate = ms_module.generate_images
        ffmpeg_calls = []

        def _spy(*args, **kwargs):
            ffmpeg_calls.append(args[0])
            return original_generate(*args, **kwargs)

        ms_module.generate_images = _spy
        try:
            # ----- 1st publish: real run, journal stamped -----
            first = process_canonical_path(
                canonical_path=canonical,
                registry=dedup_registry,
                config=dedup_config,
                gpu=None,
                gpu_device_path=None,
            )
            assert first.status is MultiServerStatus.PUBLISHED
            plex_bif = next(p for p in first.publishers if p.adapter_name == "plex_bundle").output_paths[0]
            assert _meta_path_for(emby_sidecar).exists()
            assert len(ffmpeg_calls) == 1

            # ----- Simulate Sonarr quality upgrade (replace source) -----
            # Append bytes so size differs and mtime advances. We restore
            # the original at the end of the test to avoid corrupting
            # the test fixture for other tests.
            with canonical_path_obj.open("ab") as f:
                f.write(b"\x00" * 1024)
            time.sleep(1.1)  # ensure mtime granularity catches up
            canonical_path_obj.touch()

            fc_module._singleton = None  # noqa: SLF001 — clear frame cache

            # ----- 2nd publish: source changed, must regen -----
            second = process_canonical_path(
                canonical_path=canonical,
                registry=dedup_registry,
                config=dedup_config,
                gpu=None,
                gpu_device_path=None,
            )
            assert len(ffmpeg_calls) == 2, (
                f"Source replaced — FFmpeg should have re-run; saw {len(ffmpeg_calls)} call(s)"
            )
            assert second.status is MultiServerStatus.PUBLISHED, second.message
            for p in second.publishers:
                assert p.status is PublisherStatus.PUBLISHED, (
                    f"{p.adapter_name} did not re-publish despite source change: {p.status}"
                )
        finally:
            ms_module.generate_images = original_generate
            # Restore source file to original content + mtime so
            # other tests get a stable fixture.
            canonical_path_obj.write_bytes(original_bytes)
            import os as _os

            _os.utime(canonical, (original_mtime, original_mtime))
            assert canonical_path_obj.stat().st_size == original_size, "fixture size restored"

            if emby_sidecar.exists():
                emby_sidecar.unlink()
            try:
                if plex_bif.exists():
                    plex_bif.unlink()
            except NameError:
                pass
            try:
                clear_meta([emby_sidecar, plex_bif])
            except NameError:
                clear_meta([emby_sidecar])


@pytest.mark.integration
@pytest.mark.real_plex_server
class TestRegenerateClearsJournal:
    """Force-regenerate ignores the journal AND clears stale entries."""

    def test_regenerate_runs_ffmpeg_even_when_journal_says_fresh(self, dedup_registry, dedup_config, media_root):
        canonical = str(media_root / "Movies" / "Test Movie H264 (2024)" / "Test Movie H264 (2024).mkv")
        emby_sidecar = Path(canonical).parent / "Test Movie H264 (2024)-320-5.bif"
        if emby_sidecar.exists():
            emby_sidecar.unlink()

        from media_preview_generator.output.journal import _meta_path_for, clear_meta
        from media_preview_generator.processing import frame_cache as fc_module
        from media_preview_generator.processing import multi_server as ms_module

        fc_module._singleton = None  # noqa: SLF001

        original_generate = ms_module.generate_images
        ffmpeg_calls = []

        def _spy(*args, **kwargs):
            ffmpeg_calls.append(args[0])
            return original_generate(*args, **kwargs)

        ms_module.generate_images = _spy
        try:
            # 1st: stamps the journal.
            first = process_canonical_path(
                canonical_path=canonical,
                registry=dedup_registry,
                config=dedup_config,
                gpu=None,
                gpu_device_path=None,
            )
            assert first.status is MultiServerStatus.PUBLISHED
            plex_bif = next(p for p in first.publishers if p.adapter_name == "plex_bundle").output_paths[0]
            assert _meta_path_for(emby_sidecar).exists()
            assert len(ffmpeg_calls) == 1

            fc_module._singleton = None  # noqa: SLF001

            # 2nd with regenerate=True: ignore journal, run FFmpeg, re-stamp.
            second = process_canonical_path(
                canonical_path=canonical,
                registry=dedup_registry,
                config=dedup_config,
                gpu=None,
                gpu_device_path=None,
                regenerate=True,
            )
            assert len(ffmpeg_calls) == 2, "regenerate=True should bypass the journal short-circuit"
            assert second.status is MultiServerStatus.PUBLISHED
            assert _meta_path_for(emby_sidecar).exists()  # still stamped after re-publish
        finally:
            ms_module.generate_images = original_generate
            if emby_sidecar.exists():
                emby_sidecar.unlink()
            try:
                if plex_bif.exists():
                    plex_bif.unlink()
            except NameError:
                pass
            try:
                clear_meta([emby_sidecar, plex_bif])
            except NameError:
                clear_meta([emby_sidecar])
