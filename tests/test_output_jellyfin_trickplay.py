"""Tests for the Jellyfin native trickplay output adapter.

Verifies that:

- the on-disk layout matches what Jellyfin 10.9+ scans for natively,
- frames are packed into 10x10 JPG tile sheets (the Jellyfin native
  format — *not* BIF, which is Jellyscrub-plugin territory),
- the manifest.json structure matches the @jellyfin/sdk typings,
- the manifest's top-level key is the supplied item id.
"""

from __future__ import annotations

import json
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
    def test_returns_true_because_manifest_keyed_by_item_id(self):
        # The manifest's top-level key is Jellyfin's item id; the
        # dispatcher must surface that before publish.
        assert JellyfinTrickplayAdapter().needs_server_metadata() is True

    def test_name(self):
        assert JellyfinTrickplayAdapter().name == "jellyfin_trickplay"


class TestComputeOutputPaths:
    def test_manifest_path_uses_basename_and_width(self, tmp_path):
        adapter = JellyfinTrickplayAdapter(width=320)
        bundle = _make_bundle(
            "/m/Foo (2024)/Foo (2024).mkv",
            tmp_path,
            frame_count=0,
        )
        paths = adapter.compute_output_paths(bundle, MagicMock(), item_id="42")

        assert len(paths) == 1
        assert paths[0] == Path("/m/Foo (2024)/trickplay/Foo (2024)-320.json")

    def test_respects_custom_width(self, tmp_path):
        adapter = JellyfinTrickplayAdapter(width=480)
        bundle = _make_bundle("/m/Foo.mkv", tmp_path, frame_count=0)
        paths = adapter.compute_output_paths(bundle, MagicMock(), item_id="42")
        assert paths[0] == Path("/m/trickplay/Foo-480.json")

    def test_missing_item_id_raises(self, tmp_path):
        adapter = JellyfinTrickplayAdapter()
        bundle = _make_bundle("/m/Foo.mkv", tmp_path, frame_count=0)
        with pytest.raises(ValueError, match="item_id"):
            adapter.compute_output_paths(bundle, MagicMock(), item_id=None)


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
        manifest_path = adapter.compute_output_paths(bundle, MagicMock(), item_id="abc-id")[0]

        adapter.publish(bundle, [manifest_path], item_id="abc-id")

        # Exactly one sheet for 15 frames (10x10 grid holds up to 100).
        sheets_dir = media_dir / "trickplay" / "Test (2024)-320"
        assert sheets_dir.is_dir()
        sheet_files = sorted(sheets_dir.iterdir())
        assert len(sheet_files) == 1
        assert sheet_files[0].name == "0.jpg"

        # Sheet image is 10x10 grid even when only 15 thumbnails were
        # available — empty cells are black, matching Jellyfin's behaviour.
        with Image.open(sheet_files[0]) as sheet:
            assert sheet.size == (3200, 1800)  # 10*320 x 10*180

        # Manifest path matches the schema and is keyed by the item id.
        assert manifest_path.exists()
        manifest = json.loads(manifest_path.read_text())
        assert "Trickplay" in manifest
        assert "abc-id" in manifest["Trickplay"]
        info = manifest["Trickplay"]["abc-id"]["320"]
        assert info["Width"] == 320
        assert info["Height"] == 180
        assert info["TileWidth"] == 10
        assert info["TileHeight"] == 10
        assert info["ThumbnailCount"] == 15
        assert info["Interval"] == 10000  # ms

    def test_writes_multiple_sheets_for_over_100_frames(self, tmp_path):
        frame_dir = tmp_path / "frames"
        _populate_frames(frame_dir, count=250)

        media_dir = tmp_path / "Movies" / "Long (2024)"
        media_dir.mkdir(parents=True)
        media_file = media_dir / "Long (2024).mkv"
        media_file.write_bytes(b"")

        adapter = JellyfinTrickplayAdapter(width=320, frame_interval=10)
        bundle = _make_bundle(str(media_file), frame_dir, frame_count=250)
        manifest_path = adapter.compute_output_paths(bundle, MagicMock(), item_id="long-id")[0]

        adapter.publish(bundle, [manifest_path], item_id="long-id")

        sheets_dir = media_dir / "trickplay" / "Long (2024)-320"
        sheet_files = sorted(sheets_dir.iterdir())
        # 250 frames / 100 per sheet = 3 sheets (last one partially filled).
        assert [s.name for s in sheet_files] == ["0.jpg", "1.jpg", "2.jpg"]

        manifest = json.loads(manifest_path.read_text())
        info = manifest["Trickplay"]["long-id"]["320"]
        assert info["ThumbnailCount"] == 250

    def test_creates_missing_trickplay_dir(self, tmp_path):
        frame_dir = tmp_path / "frames"
        _populate_frames(frame_dir, count=5)

        media_dir = tmp_path / "Movies" / "X"
        media_dir.mkdir(parents=True)
        media_file = media_dir / "X.mkv"
        media_file.write_bytes(b"")

        # No trickplay/ directory yet.
        assert not (media_dir / "trickplay").exists()

        adapter = JellyfinTrickplayAdapter()
        bundle = _make_bundle(str(media_file), frame_dir, frame_count=5)
        manifest_path = adapter.compute_output_paths(bundle, MagicMock(), item_id="x")[0]
        adapter.publish(bundle, [manifest_path], item_id="x")

        assert (media_dir / "trickplay").is_dir()

    def test_empty_frame_dir_raises(self, tmp_path):
        frame_dir = tmp_path / "empty_frames"
        frame_dir.mkdir()

        media_file = tmp_path / "Foo.mkv"
        media_file.write_bytes(b"")

        adapter = JellyfinTrickplayAdapter()
        bundle = _make_bundle(str(media_file), frame_dir, frame_count=0)
        manifest_path = adapter.compute_output_paths(bundle, MagicMock(), item_id="x")[0]

        with pytest.raises(RuntimeError, match="No JPG frames"):
            adapter.publish(bundle, [manifest_path], item_id="x")

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
        manifest_path = adapter.compute_output_paths(bundle, MagicMock(), item_id="x")[0]
        adapter.publish(bundle, [manifest_path], item_id="x")

        sheets = sorted((tmp_path / "trickplay" / "Foo-320").iterdir())
        with Image.open(sheets[0]) as sheet:
            # Tile size = first frame's size = 320x180.
            assert sheet.size == (3200, 1800)
