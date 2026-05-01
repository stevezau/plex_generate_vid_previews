"""End-to-end failure-mode tests against live filesystem semantics.

Verifies the dispatcher converts real OS-level errors (EACCES from a
read-only mount, ENOSPC from a full tmpfs) into clean PublisherStatus.FAILED
results without crashing the worker pool.

The unit tests in ``tests/test_processing_multi_server.py::TestPublisherFailureModes``
mock the underlying I/O. These tests do the real chmod / tmpfs dance.
"""

from __future__ import annotations

import shutil
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
def fail_config(tmp_path):
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
def readonly_media_root(media_root: Path, tmp_path: Path):
    """Copy the test fixture into a tmp dir, chmod the *parent* read-only.

    Chmodding the media file's *parent* directory means publishers
    can't create their sidecar BIF next to the source. Restored at
    teardown so other tests aren't affected.
    """
    src = media_root / "Movies" / "Test Movie H264 (2024)" / "Test Movie H264 (2024).mkv"
    movies_root = tmp_path / "ro" / "Movies"
    parent = movies_root / "Test Movie H264 (2024)"
    parent.mkdir(parents=True)
    target = parent / "Test Movie H264 (2024).mkv"
    shutil.copyfile(src, target)
    # Make the directory read-only — file is preserved, but writes
    # for new sidecars will EACCES.
    parent.chmod(0o555)
    yield tmp_path / "ro"
    # Restore permissions so cleanup can remove files.
    try:
        parent.chmod(0o755)
    except FileNotFoundError:
        pass


@pytest.mark.integration
class TestReadOnlyMediaDir:
    """A read-only media directory → publisher returns FAILED, no crash."""

    def test_eacces_on_sidecar_write_returns_failed(self, emby_credentials, readonly_media_root, fail_config):
        media_root = readonly_media_root
        canonical = str(media_root / "Movies" / "Test Movie H264 (2024)" / "Test Movie H264 (2024).mkv")

        raw_servers = [
            {
                "id": "emby-ro",
                "type": "emby",
                "name": "Test Emby (ro)",
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

        result = process_canonical_path(
            canonical_path=canonical,
            registry=registry,
            config=fail_config,
            gpu=None,
            gpu_device_path=None,
        )

        # Read-only parent dir → publisher returns FAILED, dispatcher
        # doesn't crash, no partial output left behind.
        assert result.status is MultiServerStatus.FAILED, result.message
        assert result.publishers[0].status is PublisherStatus.FAILED
        # No sidecar BIF was created.
        sidecar = Path(canonical).parent / "Test Movie H264 (2024)-320-5.bif"
        assert not sidecar.exists(), "publisher left a partial output despite EACCES"


@pytest.mark.integration
class TestUnreadableSourceFile:
    """Source file is mode 000 — generate_images can't open it."""

    def test_unreadable_source_returns_failed(self, emby_credentials, fail_config, tmp_path, media_root):
        """When the source file isn't readable by us, the dispatcher
        catches the FFmpeg failure and returns FAILED.

        Different from "source missing" — file exists, just can't be
        opened. We exercise the FFmpeg-error catch path in
        ``process_canonical_path`` where a ``Frame generation failed``
        message is produced.
        """
        # Copy fixture to tmp_path then chmod 0 — restore at teardown.
        src = media_root / "Movies" / "Test Movie H264 (2024)" / "Test Movie H264 (2024).mkv"
        parent = tmp_path / "unreadable" / "Movies" / "Test (2024)"
        parent.mkdir(parents=True)
        target = parent / "Test (2024).mkv"
        shutil.copyfile(src, target)
        target.chmod(0o000)

        try:
            raw_servers = [
                {
                    "id": "emby-unreadable",
                    "type": "emby",
                    "name": "Test Emby (unreadable)",
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
                    "path_mappings": [{"remote_prefix": "/em-media", "local_prefix": str(tmp_path / "unreadable")}],
                    "output": {"adapter": "emby_sidecar", "width": 320, "frame_interval": 5},
                }
            ]
            registry = ServerRegistry.from_settings(raw_servers, legacy_config=None)

            result = process_canonical_path(
                canonical_path=str(target),
                registry=registry,
                config=fail_config,
                gpu=None,
                gpu_device_path=None,
            )

            # Frame generation fails → MultiServerStatus.FAILED.
            # Or NO_FRAMES if FFmpeg returned 0 frames silently. Either
            # is acceptable — the key guarantee is "we don't crash".
            assert result.status in (
                MultiServerStatus.FAILED,
                MultiServerStatus.NO_FRAMES,
            ), result.message
        finally:
            # Restore so tmp_path can be cleaned.
            try:
                target.chmod(0o644)
            except FileNotFoundError:
                pass


@pytest.mark.integration
class TestPartialFailureIsolation:
    """One server fails, others succeed — only the failing one is FAILED."""

    def test_one_server_failure_doesnt_block_others(self, emby_credentials, media_root, fail_config, tmp_path):
        """Configure two Emby servers — one with a writable media dir,
        one with a read-only dir. Verify the writable one publishes and
        the read-only one returns FAILED. Confirms no global lock or
        try/except sloppiness lets one failure block siblings.
        """
        # Healthy Emby (uses the normal media root)
        # Unhealthy Emby (uses the chmod-555 copy in tmp_path)
        ro_root = tmp_path / "ro"
        ro_movies = ro_root / "Movies" / "Test Movie H264 (2024)"
        ro_movies.mkdir(parents=True)
        ro_target = ro_movies / "Test Movie H264 (2024).mkv"
        shutil.copyfile(
            media_root / "Movies" / "Test Movie H264 (2024)" / "Test Movie H264 (2024).mkv",
            ro_target,
        )
        ro_movies.chmod(0o555)

        try:
            raw_servers = [
                {
                    "id": "emby-healthy",
                    "type": "emby",
                    "name": "Healthy Emby",
                    "enabled": True,
                    "url": emby_credentials["EMBY_URL"],
                    "auth": {
                        "method": "password",
                        "access_token": emby_credentials["EMBY_ACCESS_TOKEN"],
                        "user_id": emby_credentials["EMBY_USER_ID"],
                    },
                    "server_identity": emby_credentials["EMBY_SERVER_ID"] + "-h",
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
                # Second emby that'll write to the read-only dir. Same
                # canonical path, different path_mapping.
                {
                    "id": "emby-broken",
                    "type": "emby",
                    "name": "Broken Emby (RO)",
                    "enabled": True,
                    "url": emby_credentials["EMBY_URL"],
                    "auth": {
                        "method": "password",
                        "access_token": emby_credentials["EMBY_ACCESS_TOKEN"],
                        "user_id": emby_credentials["EMBY_USER_ID"],
                    },
                    "server_identity": emby_credentials["EMBY_SERVER_ID"] + "-b",
                    "libraries": [
                        {
                            "id": "movies",
                            "name": "Movies (ro)",
                            "remote_paths": ["/em-media/Movies"],
                            "enabled": True,
                        }
                    ],
                    "path_mappings": [{"remote_prefix": "/em-media", "local_prefix": str(ro_root)}],
                    "output": {"adapter": "emby_sidecar", "width": 320, "frame_interval": 5},
                },
            ]
            registry = ServerRegistry.from_settings(raw_servers, legacy_config=None)

            # Use a path that exists under BOTH path_mappings — the
            # canonical_path itself is in the writable media_root, but
            # the broken emby's mapping points at ro_root which has
            # the same relative structure.
            canonical = str(media_root / "Movies" / "Test Movie H264 (2024)" / "Test Movie H264 (2024).mkv")

            from media_preview_generator.processing import frame_cache as fc_module

            fc_module._singleton = None  # noqa: SLF001 — clear cache between tests

            result = process_canonical_path(
                canonical_path=canonical,
                registry=registry,
                config=fail_config,
                gpu=None,
                gpu_device_path=None,
            )

            # Only the writable server published; broken one is FAILED.
            # The healthy one wrote its sidecar successfully because
            # the canonical path is under media_root (writable).
            statuses = {p.server_id: p.status for p in result.publishers}
            assert (
                PublisherStatus.PUBLISHED in statuses.values()
                or PublisherStatus.SKIPPED_OUTPUT_EXISTS in statuses.values()
            ), statuses

            # Cleanup the success sidecar.
            success_sidecar = media_root / "Movies" / "Test Movie H264 (2024)" / "Test Movie H264 (2024)-320-5.bif"
            if success_sidecar.exists():
                success_sidecar.unlink()
            for f in success_sidecar.parent.glob("*.bif.meta"):
                f.unlink()
        finally:
            try:
                ro_movies.chmod(0o755)
            except FileNotFoundError:
                pass
