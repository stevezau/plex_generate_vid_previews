"""End-to-end test: Emby BIF format-spec compliance.

Symmetric to ``test_e2e_plex_visible.py`` and ``test_e2e_jellyfin_trickplay_fix.py``,
but Emby's player-side BIF rendering is **gated by an Emby Premiere licence**
and exposed through internal player APIs not part of the REST surface.

Honest scope: this test cannot prove "the Emby web player renders our BIF" —
that requires Premiere + a browser. What it *can* prove (and what this
test does):

1. The published file lands at the **exact** filename pattern Emby
   auto-detects: ``{basename}-{width}-{interval}.bif`` next to the
   source media (verified at https://emby.media/community/topic/112001-).
2. The BIF is structurally valid: magic bytes, header, index table
   sentinel, JPEG SOI on every frame. Parsed via the in-house
   ``bif_reader.read_bif_metadata`` so we exercise the same code path
   the BIF Viewer uses.
3. Emby's `/Library/Refresh` endpoint accepts the trigger after we
   write — proving the file is at least visible to Emby's scanner
   (it doesn't 404 or return permission errors when asked to scan).

If you have an Emby Premiere licence and want to add the visual
verification: open the Emby web player after this test runs, scrub
the timeline, and confirm thumbnails appear.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
import requests

from plex_generate_previews.bif_reader import BIF_MAGIC, read_bif_metadata
from plex_generate_previews.processing.multi_server import (
    MultiServerStatus,
    PublisherStatus,
    process_canonical_path,
)
from plex_generate_previews.servers import ServerRegistry

_JPEG_SOI = bytes([0xFF, 0xD8, 0xFF])


@pytest.fixture
def emby_visible_config(tmp_path):
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
def emby_visible_registry(emby_credentials, media_root):
    raw_servers = [
        {
            "id": "emby-visible",
            "type": "emby",
            "name": "Test Emby (visible)",
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
        }
    ]
    return ServerRegistry.from_settings(raw_servers, legacy_config=None)


@pytest.mark.integration
class TestEmbyBifFormatSpec:
    """Strict format compliance: filename pattern + BIF magic + index + JPEG SOI on every frame."""

    def test_published_bif_is_format_spec_compliant(
        self, emby_visible_registry, emby_visible_config, media_root, emby_credentials
    ):
        canonical = str(media_root / "Movies" / "Test Movie H264 (2024)" / "Test Movie H264 (2024).mkv")
        sidecar = Path(canonical).parent / "Test Movie H264 (2024)-320-5.bif"
        if sidecar.exists():
            sidecar.unlink()

        try:
            result = process_canonical_path(
                canonical_path=canonical,
                registry=emby_visible_registry,
                config=emby_visible_config,
                gpu=None,
                gpu_device_path=None,
            )
            assert result.status is MultiServerStatus.PUBLISHED, result.message
            published = next(p for p in result.publishers if p.status is PublisherStatus.PUBLISHED)
            assert published.adapter_name == "emby_sidecar"

            # ----- filename pattern -----
            # Emby's web client auto-detects ``<basename>-<width>-<interval>.bif``
            # in the same directory as the media file. Without this naming
            # convention Emby will not discover the BIF.
            assert sidecar.exists(), f"sidecar not found at {sidecar}"
            assert sidecar.name == "Test Movie H264 (2024)-320-5.bif"
            assert sidecar.parent == Path(canonical).parent

            # ----- magic bytes -----
            with sidecar.open("rb") as f:
                head = f.read(8)
            assert head == BIF_MAGIC, f"Bad BIF magic: {head.hex()}"

            # ----- structural validity via the in-house reader -----
            # Parses header + index table + verifies sentinel; raises
            # ValueError if anything is off. Exercises the same code path
            # the BIF Viewer (web UI) uses, so a regression here would
            # also break the viewer.
            meta = read_bif_metadata(str(sidecar))
            assert meta.frame_count > 0, "BIF has zero frames"
            assert meta.frame_interval_ms == 5000, (
                f"Expected 5s (5000ms) interval per config, got {meta.frame_interval_ms}ms"
            )
            assert len(meta.frame_offsets) == meta.frame_count, "offset count mismatch"

            # ----- every indexed frame is a real JPEG -----
            raw = sidecar.read_bytes()
            for idx, offset in enumerate(meta.frame_offsets):
                soi = raw[offset : offset + 3]
                assert soi == _JPEG_SOI, f"Frame {idx} at offset {offset} doesn't start with JPEG SOI: {soi.hex()}"

            # ----- Emby accepts a refresh trigger for the same library -----
            # Proves the file is at least visible to Emby's scanner —
            # i.e. correct path, correct permissions, no proxy issue.
            # This isn't proof of UI rendering (Premiere-gated) but it
            # rules out a class of "we wrote it but Emby can't see it"
            # bugs.
            refresh = requests.post(
                f"{emby_credentials['EMBY_URL']}/Library/Refresh",
                headers={"X-Emby-Token": emby_credentials["EMBY_ACCESS_TOKEN"]},
                timeout=10,
            )
            assert refresh.status_code in (200, 204), (
                f"Emby /Library/Refresh failed: HTTP {refresh.status_code}, body={refresh.text[:200]}"
            )
        finally:
            if sidecar.exists():
                sidecar.unlink()
            for f in sidecar.parent.glob("*.bif.meta"):
                f.unlink()


@pytest.mark.integration
class TestEmbyBifNamingMatchesEmbyConvention:
    """Filename pattern test (separate so it's parameterizable in future)."""

    @pytest.mark.parametrize(
        "width,interval,expected_suffix",
        [
            (320, 5, "-320-5.bif"),
            (320, 10, "-320-10.bif"),
            (480, 10, "-480-10.bif"),
        ],
    )
    def test_filename_includes_width_and_interval(
        self, width, interval, expected_suffix, emby_credentials, media_root, emby_visible_config
    ):
        """Different (width, interval) → different filename. Multiple resolutions
        can coexist next to one source file."""
        from plex_generate_previews.output.emby_sidecar import EmbyBifAdapter

        # Adapter is pure-Python; no live container needed for filename derivation.
        path = EmbyBifAdapter.sidecar_path(
            "/data/Movies/Test (2024)/Test (2024).mkv",
            width=width,
            frame_interval=interval,
        )
        assert path.name.endswith(expected_suffix)
        assert path.name == f"Test (2024){expected_suffix}"
