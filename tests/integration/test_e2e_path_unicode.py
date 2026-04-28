"""End-to-end test for Unicode path mapping.

Verifies a canonical path containing non-ASCII characters (Japanese
title, accented characters, emoji) flows through the dispatcher and
publishes correctly. Regression guard for the NFC normalisation
fix in ``servers/ownership.py::_normalize`` and ``config/paths.py::
_path_matches_prefix``.

Path NFD-vs-NFC differences come up when the source filesystem is
HFS+ (macOS) — out of scope for this Linux-only test container, but
the in-memory NFC normalisation also defends against it.
"""

from __future__ import annotations

import shutil
import struct
import subprocess
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

# Unicode title with: Japanese, accented latin, emoji. Real-world worst
# case — a user with a multi-language library.
UNICODE_TITLE = "メディア café 🎬 (2024)"


@pytest.fixture
def unicode_media(media_root: Path) -> Path:
    """Generate a small test video at a canonical path with Unicode characters."""
    parent = media_root / "Movies" / UNICODE_TITLE
    parent.mkdir(parents=True, exist_ok=True)
    target = parent / f"{UNICODE_TITLE}.mkv"
    if not target.exists():
        # Use ffmpeg lavfi to create a deterministic 5-second clip. Tiny
        # fixture; takes ~0.2s. We don't reuse generate_test_media.sh
        # because that script's filenames are fixed.
        subprocess.run(
            [
                "ffmpeg",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=size=320x180:rate=30:duration=5",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                str(target),
            ],
            check=True,
        )
    yield target
    # Best-effort cleanup of unicode dir if we created it.
    if parent.exists():
        try:
            shutil.rmtree(parent)
        except OSError:
            pass


@pytest.fixture
def unicode_config(tmp_path):
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
def unicode_registry(emby_credentials, media_root):
    raw_servers = [
        {
            "id": "emby-unicode",
            "type": "emby",
            "name": "Test Emby (unicode)",
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
    return ServerRegistry.from_settings(raw_servers, legacy_config=None)


@pytest.mark.integration
class TestUnicodePathPublish:
    def test_publish_works_for_unicode_canonical_path(self, unicode_media: Path, unicode_registry, unicode_config):
        """Canonical path contains Japanese + accented + emoji chars; publish anyway."""
        canonical = str(unicode_media)
        sidecar = unicode_media.parent / f"{UNICODE_TITLE}-320-5.bif"
        if sidecar.exists():
            sidecar.unlink()

        try:
            result = process_canonical_path(
                canonical_path=canonical,
                registry=unicode_registry,
                config=unicode_config,
                gpu=None,
                gpu_device_path=None,
            )
            # Ownership matched (NFC normalisation + library remote_paths
            # all-ASCII so this just exercises the unicode-canonical path
            # arriving at an ASCII-prefix library).
            assert result.status is MultiServerStatus.PUBLISHED, result.message
            assert all(p.status is PublisherStatus.PUBLISHED for p in result.publishers)
            assert sidecar.exists()
            # File on disk is the same Unicode title byte-for-byte.
            assert UNICODE_TITLE in sidecar.name
            # Valid BIF header.
            head = sidecar.read_bytes()[:8]
            assert head == _BIF_MAGIC
        finally:
            if sidecar.exists():
                sidecar.unlink()
            for f in unicode_media.parent.glob("*.bif.meta"):
                f.unlink()


@pytest.mark.integration
class TestNFCNFDOwnership:
    """NFD canonical path matches NFC settings — the headline NFC-normalisation guarantee."""

    def test_ownership_resolves_when_canonical_is_nfd_setting_is_nfc(self, emby_credentials, media_root):
        """The canonical path arrives in NFD (HFS+ source filesystem),
        but settings library/path_mapping is NFC (typed by user).

        Without the fix, ownership would silently miss and the file
        would be NO_OWNERS. With the fix, both sides normalise to NFC
        and the match succeeds.
        """
        import unicodedata

        from plex_generate_previews.servers.ownership import server_owns_path

        # NFC: single codepoint U+00E9 'é'. NFD: 'e' + U+0301.
        nfc_setting_path = "/data/Films/café"
        nfd_canonical = unicodedata.normalize("NFD", "/data/Films/café/Movie (2024)/Movie (2024).mkv")
        # Sanity: the two byte-sequences differ before normalisation.
        assert (
            "/data/Films/café" not in nfd_canonical or unicodedata.normalize("NFC", nfd_canonical) != nfd_canonical
        ), "Test setup invalid: NFD path should byte-differ from NFC"

        # Build a minimal ServerConfig manually rather than via the
        # registry so this test stays isolated from live containers.
        from plex_generate_previews.servers.base import Library, ServerConfig, ServerType

        cfg = ServerConfig(
            id="nfd-test",
            type=ServerType.EMBY,
            name="NFD Test",
            enabled=True,
            url="http://x",
            auth={},
            libraries=[Library(id="1", name="Films", remote_paths=(nfc_setting_path,), enabled=True)],
            path_mappings=[],
        )
        match = server_owns_path(nfd_canonical, cfg)
        assert match is not None, (
            "NFD-encoded canonical path failed to match NFC-encoded setting — NFC normalisation didn't apply"
        )


# Also verify the BIF count helper for downstream tests
def _decode_bif_count(path: Path) -> int:
    raw = path.read_bytes()
    assert raw[:8] == _BIF_MAGIC
    return struct.unpack("<I", raw[12:16])[0]
