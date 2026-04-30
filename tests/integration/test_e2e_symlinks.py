"""End-to-end test for symlinked source paths.

Real-world scenario: a user mounts a media volume from a NAS, then
creates symlinks under their library directory pointing into the NAS
mount. Webhooks arrive with the symlink path; the dispatcher must:

* Resolve ownership against the symlink path (not the underlying real
  path), since that's what the user configured library_remote_paths
  to.
* Be able to read the file via the symlink (FFmpeg follows symlinks
  by default).
* Stamp the journal with the underlying source's mtime+size so a
  Sonarr quality upgrade that replaces the link target still triggers
  a regen.
"""

from __future__ import annotations

import shutil
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


@pytest.fixture
def symlink_config(tmp_path):
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
def symlinked_media(media_root: Path, tmp_path: Path):
    """Create a library directory whose movie files are symlinks into media_root.

    Returns ``(library_root, symlink_canonical_path)``.
    """
    real_source = media_root / "Movies" / "Test Movie H264 (2024)" / "Test Movie H264 (2024).mkv"
    library_root = tmp_path / "library" / "Movies" / "Test Movie H264 (2024)"
    library_root.mkdir(parents=True)
    link_path = library_root / "Test Movie H264 (2024).mkv"
    link_path.symlink_to(real_source)
    yield tmp_path / "library", link_path
    if (tmp_path / "library").exists():
        shutil.rmtree(tmp_path / "library", ignore_errors=True)


@pytest.fixture
def symlink_registry(emby_credentials, symlinked_media):
    library_root, _ = symlinked_media
    raw_servers = [
        {
            "id": "emby-symlink",
            "type": "emby",
            "name": "Test Emby (symlink)",
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
            "path_mappings": [{"remote_prefix": "/em-media", "local_prefix": str(library_root)}],
            "output": {"adapter": "emby_sidecar", "width": 320, "frame_interval": 5},
        }
    ]
    return ServerRegistry.from_settings(raw_servers, legacy_config=None)


@pytest.mark.integration
class TestSymlinkedSourcePath:
    def test_publish_via_symlink_works(self, symlinked_media, symlink_registry, symlink_config):
        """Canonical path is the symlink; FFmpeg follows it transparently
        and the sidecar lands next to the symlink (not next to the real
        target — the user's library directory layout is preserved).
        """
        _, link_path = symlinked_media
        canonical = str(link_path)
        sidecar = link_path.parent / "Test Movie H264 (2024)-320-5.bif"

        try:
            result = process_canonical_path(
                canonical_path=canonical,
                registry=symlink_registry,
                config=symlink_config,
                gpu=None,
                gpu_device_path=None,
            )
            assert result.status is MultiServerStatus.PUBLISHED, result.message
            assert all(p.status is PublisherStatus.PUBLISHED for p in result.publishers)

            # Sidecar landed next to the SYMLINK, not next to the real target.
            assert sidecar.exists()
            head = sidecar.read_bytes()[:8]
            assert head == _BIF_MAGIC

            # Decode count to confirm a real BIF.
            count = struct.unpack("<I", sidecar.read_bytes()[12:16])[0]
            assert count >= 4
        finally:
            if sidecar.exists():
                sidecar.unlink()
            for f in link_path.parent.glob("*.bif.meta"):
                f.unlink()

    def test_journal_uses_underlying_source_mtime(self, symlinked_media, symlink_registry, symlink_config):
        """The .meta journal records the real file's mtime+size, not the
        symlink's. So if the user replaces the underlying file (via
        Sonarr upgrade), the journal mismatch correctly triggers regen
        even though the symlink itself didn't change.

        ``os.stat`` follows symlinks by default which is what we want.
        """
        from media_preview_generator.output.journal import _meta_path_for, outputs_fresh_for_source

        _, link_path = symlinked_media
        canonical = str(link_path)
        sidecar = link_path.parent / "Test Movie H264 (2024)-320-5.bif"

        try:
            # Initial publish stamps the journal.
            result = process_canonical_path(
                canonical_path=canonical,
                registry=symlink_registry,
                config=symlink_config,
                gpu=None,
                gpu_device_path=None,
            )
            assert result.status is MultiServerStatus.PUBLISHED
            assert _meta_path_for(sidecar).exists()

            # Journal claims fresh.
            assert outputs_fresh_for_source([sidecar], canonical) is True

            # Replace the underlying file (simulate Sonarr swap).
            real_target = link_path.resolve()
            original = real_target.read_bytes()
            try:
                with real_target.open("ab") as f:
                    f.write(b"\x00" * 1024)  # change size + mtime
                # Source no longer matches the journal.
                assert outputs_fresh_for_source([sidecar], canonical) is False, (
                    "Journal should detect the underlying source change even via symlink"
                )
            finally:
                # Restore so the source fixture isn't corrupted for other tests.
                real_target.write_bytes(original)
        finally:
            if sidecar.exists():
                sidecar.unlink()
            for f in link_path.parent.glob("*.bif.meta"):
                f.unlink()
