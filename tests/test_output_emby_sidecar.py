"""Tests for the Emby sidecar BIF output adapter."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from plex_generate_previews.output import BifBundle, EmbyBifAdapter


def _make_bundle(canonical_path: str, frame_dir: Path) -> BifBundle:
    return BifBundle(
        canonical_path=canonical_path,
        frame_dir=frame_dir,
        bif_path=None,
        frame_interval=10,
        width=320,
        height=180,
        frame_count=0,
    )


class TestNeedsServerMetadata:
    def test_returns_false(self):
        adapter = EmbyBifAdapter()
        # Emby sidecar paths are derived purely from the media path; no
        # API roundtrip needed.
        assert adapter.needs_server_metadata() is False

    def test_name(self):
        assert EmbyBifAdapter().name == "emby_sidecar"


class TestComputeOutputPaths:
    def test_default_naming(self, tmp_path):
        adapter = EmbyBifAdapter()
        paths = adapter.compute_output_paths(
            _make_bundle("/m/Foo (2024)/Foo (2024).mkv", tmp_path),
            MagicMock(),
            item_id=None,
        )
        assert paths == [Path("/m/Foo (2024)/Foo (2024)-320-10.bif")]

    def test_respects_width_and_interval(self, tmp_path):
        adapter = EmbyBifAdapter(width=480, frame_interval=5)
        paths = adapter.compute_output_paths(
            _make_bundle("/m/Foo.mkv", tmp_path),
            MagicMock(),
            item_id=None,
        )
        assert paths == [Path("/m/Foo-480-5.bif")]

    def test_handles_episode_paths_with_dashes(self, tmp_path):
        adapter = EmbyBifAdapter()
        paths = adapter.compute_output_paths(
            _make_bundle("/m/Show/S01/Show - S01E01 - Pilot.mkv", tmp_path),
            MagicMock(),
            item_id=None,
        )
        assert paths == [Path("/m/Show/S01/Show - S01E01 - Pilot-320-10.bif")]


class TestPublish:
    def test_writes_bif_with_emby_filename(self, tmp_path):
        # Arrange: a frame dir + an "existing" media file whose sibling
        # we expect the BIF to land beside.
        frame_dir = tmp_path / "frames"
        frame_dir.mkdir()
        for i in range(3):
            (frame_dir / f"{i:05d}.jpg").write_bytes(b"\xff\xd8\xff" + b"\x00" * 100)

        media_dir = tmp_path / "Movies" / "Test (2024)"
        media_dir.mkdir(parents=True)
        media_file = media_dir / "Test (2024).mkv"
        media_file.write_bytes(b"")  # placeholder; publish doesn't touch it

        adapter = EmbyBifAdapter(width=320, frame_interval=10)
        bundle = BifBundle(
            canonical_path=str(media_file),
            frame_dir=frame_dir,
            bif_path=None,
            frame_interval=10,
            width=320,
            height=180,
            frame_count=3,
        )
        out_path = adapter.compute_output_paths(bundle, MagicMock(), item_id=None)[0]

        # Act
        adapter.publish(bundle, [out_path])

        # Assert: exact Emby filename + valid BIF magic.
        assert out_path == media_dir / "Test (2024)-320-10.bif"
        assert out_path.exists()
        magic = out_path.read_bytes()[:8]
        assert magic == bytes([0x89, 0x42, 0x49, 0x46, 0x0D, 0x0A, 0x1A, 0x0A])

    def test_creates_missing_parent_dir(self, tmp_path):
        # Arrange: frame dir exists but the target *parent* doesn't yet.
        frame_dir = tmp_path / "frames"
        frame_dir.mkdir()
        (frame_dir / "00000.jpg").write_bytes(b"\xff\xd8\xff")

        adapter = EmbyBifAdapter()
        out_path = tmp_path / "new_dir" / "Foo-320-10.bif"

        bundle = BifBundle(
            canonical_path=str(tmp_path / "new_dir" / "Foo.mkv"),
            frame_dir=frame_dir,
            bif_path=None,
            frame_interval=10,
            width=320,
            height=180,
            frame_count=1,
        )

        adapter.publish(bundle, [out_path])

        assert out_path.exists()

    def test_empty_output_paths_raises(self, tmp_path):
        adapter = EmbyBifAdapter()
        bundle = _make_bundle("/m/Foo.mkv", tmp_path)
        with pytest.raises(ValueError):
            adapter.publish(bundle, [])


class TestStaticHelpers:
    def test_sidecar_path(self):
        assert EmbyBifAdapter.sidecar_path(
            "/m/Foo (2024)/Foo (2024).mkv",
            width=320,
            frame_interval=10,
        ) == Path("/m/Foo (2024)/Foo (2024)-320-10.bif")
