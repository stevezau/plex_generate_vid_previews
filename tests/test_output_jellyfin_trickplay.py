"""Tests for the Jellyfin native trickplay output adapter.

Verifies that:

- the on-disk layout matches Jellyfin 10.10+'s saved-with-media format
  (``<media_dir>/<basename>.trickplay/<width> - <tileW>x<tileH>/``),
- frames are packed into 10x10 JPG tile sheets (the Jellyfin native
  format — *not* BIF, which is Jellyscrub-plugin territory),
- no manifest.json is written (Jellyfin synthesises ``TrickplayInfo``
  from the directory listing + sub-dir name).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from PIL import Image

from media_preview_generator.output import BifBundle, JellyfinTrickplayAdapter


def _write_synthetic_frame(path: Path, *, size: tuple[int, int] = (320, 180)) -> None:
    img = Image.new("RGB", size, (10, 20, 30))
    img.save(path, "JPEG", quality=80)


def _populate_frames(frame_dir: Path, *, count: int, size: tuple[int, int] = (320, 180)) -> None:
    frame_dir.mkdir(parents=True, exist_ok=True)
    for i in range(count):
        _write_synthetic_frame(frame_dir / f"{i:05d}.jpg", size=size)


def _make_bundle(canonical_path: str, frame_dir: Path, frame_count: int) -> BifBundle:
    return BifBundle(
        canonical_path=canonical_path,
        frame_dir=frame_dir,
        bif_path=None,
        frame_interval=10,
        width=320,
        height=180,
        frame_count=frame_count,
    )


class TestNeedsServerMetadata:
    def test_returns_true(self):
        assert JellyfinTrickplayAdapter().needs_server_metadata() is True

    def test_name(self):
        assert JellyfinTrickplayAdapter().name == "jellyfin_trickplay"


class TestComputeOutputPaths:
    def test_sheet0_path_matches_jellyfin_pathmanager_formula(self, tmp_path):
        """Path must match Jellyfin's ``GetTrickplayDirectory(item, saveWithMedia=true)``
        plus the ``<width> - <tileW>x<tileH>`` sub-directory — verified
        against ``release-10.11.z`` source at
        Emby.Server.Implementations/Library/PathManager.cs.
        """
        adapter = JellyfinTrickplayAdapter(width=320)
        bundle = _make_bundle(
            "/m/Foo (2024)/Foo (2024).mkv",
            tmp_path,
            frame_count=0,
        )
        paths = adapter.compute_output_paths(bundle, MagicMock(), item_id="42")

        assert len(paths) == 1
        assert paths[0] == Path("/m/Foo (2024)/Foo (2024).trickplay/320 - 10x10/0.jpg")

    def test_respects_custom_width(self, tmp_path):
        adapter = JellyfinTrickplayAdapter(width=480)
        bundle = _make_bundle("/m/Foo.mkv", tmp_path, frame_count=0)
        paths = adapter.compute_output_paths(bundle, MagicMock(), item_id="42")
        assert paths[0] == Path("/m/Foo.trickplay/480 - 10x10/0.jpg")

    def test_missing_item_id_raises(self, tmp_path):
        adapter = JellyfinTrickplayAdapter()
        bundle = _make_bundle("/m/Foo.mkv", tmp_path, frame_count=0)
        with pytest.raises(ValueError, match="item_id"):
            adapter.compute_output_paths(bundle, MagicMock(), item_id=None)

    def test_static_helpers_match_compute_output_paths(self, tmp_path):
        """``trickplay_dir`` + ``sheet_dir`` are the public path helpers
        used by the BIF Viewer + diagnostics. They MUST agree with the
        adapter's own compute_output_paths or the viewer points at a
        location the publisher never wrote to."""
        canonical = "/m/Foo (2024)/Foo (2024).mkv"
        assert JellyfinTrickplayAdapter.trickplay_dir(canonical) == Path("/m/Foo (2024)/Foo (2024).trickplay")
        assert JellyfinTrickplayAdapter.sheet_dir(canonical, width=320) == Path(
            "/m/Foo (2024)/Foo (2024).trickplay/320 - 10x10"
        )


class TestPublish:
    def test_writes_one_sheet_for_under_100_frames(self, tmp_path):
        frame_dir = tmp_path / "frames"
        _populate_frames(frame_dir, count=15)

        media_dir = tmp_path / "Movies" / "Test (2024)"
        media_dir.mkdir(parents=True)
        media_file = media_dir / "Test (2024).mkv"
        media_file.write_bytes(b"")

        adapter = JellyfinTrickplayAdapter(width=320, frame_interval=10)
        bundle = _make_bundle(str(media_file), frame_dir, frame_count=15)
        sheet0 = adapter.compute_output_paths(bundle, MagicMock(), item_id="abc-id")[0]

        adapter.publish(bundle, [sheet0], item_id="abc-id")

        # Exactly one sheet for 15 frames (10x10 grid holds up to 100).
        sheets_dir = media_dir / "Test (2024).trickplay" / "320 - 10x10"
        assert sheets_dir.is_dir()
        sheet_files = sorted(sheets_dir.iterdir())
        assert len(sheet_files) == 1
        assert sheet_files[0].name == "0.jpg"

        # Sheet image is 10x10 grid even when only 15 thumbnails were
        # available — empty cells are black, matching Jellyfin's behaviour.
        with Image.open(sheet_files[0]) as sheet:
            assert sheet.size == (3200, 1800)  # 10*320 x 10*180

        # No manifest is written — Jellyfin synthesises TrickplayInfo
        # from the directory listing + sub-dir name on import.
        assert not list(media_dir.glob("*.json"))
        assert not list((media_dir / "Test (2024).trickplay").glob("*.json"))

    def test_writes_multiple_sheets_for_over_100_frames(self, tmp_path):
        frame_dir = tmp_path / "frames"
        _populate_frames(frame_dir, count=250)

        media_dir = tmp_path / "Movies" / "Long (2024)"
        media_dir.mkdir(parents=True)
        media_file = media_dir / "Long (2024).mkv"
        media_file.write_bytes(b"")

        adapter = JellyfinTrickplayAdapter(width=320, frame_interval=10)
        bundle = _make_bundle(str(media_file), frame_dir, frame_count=250)
        sheet0 = adapter.compute_output_paths(bundle, MagicMock(), item_id="long-id")[0]

        adapter.publish(bundle, [sheet0], item_id="long-id")

        sheets_dir = media_dir / "Long (2024).trickplay" / "320 - 10x10"
        sheet_files = sorted(sheets_dir.iterdir())
        # 250 frames / 100 per sheet = 3 sheets (last one partially filled).
        assert [s.name for s in sheet_files] == ["0.jpg", "1.jpg", "2.jpg"]

    def test_creates_missing_trickplay_dir(self, tmp_path):
        frame_dir = tmp_path / "frames"
        _populate_frames(frame_dir, count=5)

        media_dir = tmp_path / "Movies" / "X"
        media_dir.mkdir(parents=True)
        media_file = media_dir / "X.mkv"
        media_file.write_bytes(b"")

        # No trickplay directory yet.
        assert not (media_dir / "X.trickplay").exists()

        adapter = JellyfinTrickplayAdapter()
        bundle = _make_bundle(str(media_file), frame_dir, frame_count=5)
        sheet0 = adapter.compute_output_paths(bundle, MagicMock(), item_id="x")[0]
        adapter.publish(bundle, [sheet0], item_id="x")

        assert (media_dir / "X.trickplay" / "320 - 10x10").is_dir()

    def test_purges_stale_tiles_from_prior_run(self, tmp_path):
        """Stale 5.jpg from a prior run with more frames must be removed —
        Jellyfin imports the directory wholesale and would otherwise set
        ThumbnailCount to include the leftover tile, causing the player
        to request a frame that doesn't exist in the new sheet."""
        frame_dir = tmp_path / "frames"
        _populate_frames(frame_dir, count=15)

        media_dir = tmp_path / "M"
        media_dir.mkdir()
        media_file = media_dir / "Foo.mkv"
        media_file.write_bytes(b"")

        # Pre-create a stale sheet 5.jpg from a "previous run".
        sheets_dir = media_dir / "Foo.trickplay" / "320 - 10x10"
        sheets_dir.mkdir(parents=True)
        (sheets_dir / "5.jpg").write_bytes(b"\xff\xd8\xff stale")

        adapter = JellyfinTrickplayAdapter(width=320)
        bundle = _make_bundle(str(media_file), frame_dir, frame_count=15)
        sheet0 = adapter.compute_output_paths(bundle, MagicMock(), item_id="x")[0]
        adapter.publish(bundle, [sheet0], item_id="x")

        # Stale tile must be gone; only 0.jpg should remain.
        sheet_files = sorted(sheets_dir.iterdir())
        assert [f.name for f in sheet_files] == ["0.jpg"]

    def test_empty_frame_dir_raises(self, tmp_path):
        frame_dir = tmp_path / "empty_frames"
        frame_dir.mkdir()

        media_file = tmp_path / "Foo.mkv"
        media_file.write_bytes(b"")

        adapter = JellyfinTrickplayAdapter()
        bundle = _make_bundle(str(media_file), frame_dir, frame_count=0)
        sheet0 = adapter.compute_output_paths(bundle, MagicMock(), item_id="x")[0]

        with pytest.raises(RuntimeError, match="No JPG frames"):
            adapter.publish(bundle, [sheet0], item_id="x")

    def test_empty_output_paths_raises(self, tmp_path):
        adapter = JellyfinTrickplayAdapter()
        bundle = _make_bundle("/m/Foo.mkv", tmp_path, frame_count=0)
        with pytest.raises(ValueError):
            adapter.publish(bundle, [])

    def test_resizes_frames_when_dimensions_differ(self, tmp_path):
        # Mixed-size frames (shouldn't normally happen but guard against
        # FFmpeg quirks). Sheet should still come out a uniform grid.
        frame_dir = tmp_path / "frames"
        frame_dir.mkdir()
        _write_synthetic_frame(frame_dir / "00000.jpg", size=(320, 180))
        _write_synthetic_frame(frame_dir / "00001.jpg", size=(640, 360))  # mismatched

        media_file = tmp_path / "Foo.mkv"
        media_file.write_bytes(b"")

        adapter = JellyfinTrickplayAdapter()
        bundle = _make_bundle(str(media_file), frame_dir, frame_count=2)
        sheet0 = adapter.compute_output_paths(bundle, MagicMock(), item_id="x")[0]
        adapter.publish(bundle, [sheet0], item_id="x")

        sheets = sorted((tmp_path / "Foo.trickplay" / "320 - 10x10").iterdir())
        with Image.open(sheets[0]) as sheet:
            # Tile size = first frame's size = 320x180.
            assert sheet.size == (3200, 1800)
