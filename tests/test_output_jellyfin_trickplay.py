"""Tests for the Jellyfin native trickplay output adapter.

Verifies that:

- the on-disk layout matches Jellyfin 10.10+'s saved-with-media format
  (``<media_dir>/<basename>.trickplay/<width> - <tileW>x<tileH>/``),
- frames are packed into 10x10 JPG tile sheets (the Jellyfin native
  format — *not* BIF, which is Jellyscrub-plugin territory),
- no manifest.json is written (Jellyfin synthesises ``TrickplayInfo``
  from the directory listing + sub-dir name),
- publish is atomic — no partial tile set is ever observable in the
  final directory (closes the race where Jellyfin's 3 AM
  ``TrickplayImagesTask`` could adopt a half-written directory and
  persist a bad ``ThumbnailCount`` to its DB forever).
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

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
    def test_returns_false(self):
        """Jellyfin tile layout is derived purely from canonical_path.
        item_id is an optional plugin fast-path, not a publish
        requirement — the adapter must report this honestly so the
        dispatcher doesn't pay a 30s Pass-2 cost for nothing."""
        assert JellyfinTrickplayAdapter().needs_server_metadata() is False

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

    def test_missing_item_id_does_not_raise(self, tmp_path):
        """Item-id is no longer required — the layout is derivable from
        canonical_path alone. This pins the contract change that lets
        the dispatcher skip item-id lookups on webhook paths where no
        hint is available (Sonarr/Radarr never send Jellyfin ids)."""
        adapter = JellyfinTrickplayAdapter()
        bundle = _make_bundle("/m/Foo.mkv", tmp_path, frame_count=0)
        paths = adapter.compute_output_paths(bundle, MagicMock(), item_id=None)
        assert paths[0] == Path("/m/Foo.trickplay/320 - 10x10/0.jpg")

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

    def test_publish_succeeds_without_item_id(self, tmp_path):
        """Mirror of the item_id=None path — verifies publish writes
        tiles successfully when Sonarr/Radarr gave us no Jellyfin id
        (the 100% common case in practice)."""
        frame_dir = tmp_path / "frames"
        _populate_frames(frame_dir, count=5)

        media_file = tmp_path / "No (2024).mkv"
        media_file.write_bytes(b"")

        adapter = JellyfinTrickplayAdapter(width=320)
        bundle = _make_bundle(str(media_file), frame_dir, frame_count=5)
        sheet0 = adapter.compute_output_paths(bundle, MagicMock(), item_id=None)[0]

        adapter.publish(bundle, [sheet0], item_id=None)

        sheets_dir = tmp_path / "No (2024).trickplay" / "320 - 10x10"
        assert sheets_dir.is_dir()
        assert (sheets_dir / "0.jpg").is_file()

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

    def test_atomic_replace_of_existing_trickplay_dir(self, tmp_path):
        """A prior complete .trickplay/ from an earlier run gets atomically
        replaced by the new one — stale sheets never survive into the
        new set, and the directory is never partially visible during the
        swap (verified in test_no_partial_visibility_during_swap).

        Prior behaviour wrote in-place: Jellyfin's 3 AM task could adopt
        a mid-write dir and persist ThumbnailCount wrong forever."""
        frame_dir = tmp_path / "frames"
        _populate_frames(frame_dir, count=15)

        media_dir = tmp_path / "M"
        media_dir.mkdir()
        media_file = media_dir / "Foo.mkv"
        media_file.write_bytes(b"")

        # Pre-create a stale .trickplay/ as if a prior run with more
        # frames had left it there.
        stale_dir = media_dir / "Foo.trickplay" / "320 - 10x10"
        stale_dir.mkdir(parents=True)
        for i in range(8):
            (stale_dir / f"{i}.jpg").write_bytes(b"\xff\xd8\xff stale")

        adapter = JellyfinTrickplayAdapter(width=320)
        bundle = _make_bundle(str(media_file), frame_dir, frame_count=15)
        sheet0 = adapter.compute_output_paths(bundle, MagicMock(), item_id="x")[0]
        adapter.publish(bundle, [sheet0], item_id="x")

        # Only the new single sheet should exist. Stale sheets 1..7 are
        # gone because the atomic rename replaced the entire directory.
        sheet_files = sorted(stale_dir.iterdir())
        assert [f.name for f in sheet_files] == ["0.jpg"]

        # Staging + .old directories were cleaned up.
        parent = media_dir / "Foo.trickplay"
        siblings = {p.name for p in parent.parent.iterdir()}
        # No leftover .Foo.trickplay.staging or .Foo.trickplay.old.
        assert not any(s.startswith(".") for s in siblings)

    def test_no_partial_visibility_during_swap(self, tmp_path):
        """Simulates Jellyfin observing the final directory during publish.
        Before the swap completes, `<basename>.trickplay/` must either
        be absent, or contain a complete prior tile set — never a
        mid-write state.

        Implemented by intercepting os.rename mid-swap and asserting
        the invariants from that observation point."""
        frame_dir = tmp_path / "frames"
        _populate_frames(frame_dir, count=5)

        media_file = tmp_path / "A.mkv"
        media_file.write_bytes(b"")

        final_dir = tmp_path / "A.trickplay"
        sheets_dir = final_dir / "320 - 10x10"
        staging_dir = tmp_path / ".A.trickplay.staging"

        adapter = JellyfinTrickplayAdapter(width=320)
        bundle = _make_bundle(str(media_file), frame_dir, frame_count=5)
        sheet0 = adapter.compute_output_paths(bundle, MagicMock(), item_id=None)[0]

        observations: list[str] = []
        original_rename = os.rename

        def _spying_rename(src, dst):
            # Before any rename: the staging dir holds the new tiles;
            # the final dir either doesn't exist or is the prior set.
            # Either state is safe for Jellyfin to observe.
            if final_dir.exists():
                # If it exists here, it must still be the PRIOR complete
                # set, not the new partial staging content.
                observations.append("final_exists_before_rename")
                assert sheets_dir.is_dir()
            if staging_dir.exists():
                observations.append("staging_visible_as_dotfile")
                # Staging starts with '.' — Jellyfin's scanner ignores dotfiles.
                assert staging_dir.name.startswith(".")
            return original_rename(src, dst)

        with patch("media_preview_generator.output.jellyfin_trickplay.os.rename", side_effect=_spying_rename):
            adapter.publish(bundle, [sheet0], item_id=None)

        # Rename was observed at least once (the real one, swapping staging→final).
        assert "staging_visible_as_dotfile" in observations
        # End state: final dir exists with the new content, staging is gone.
        assert sheets_dir.is_dir()
        assert not staging_dir.exists()

    def test_fallback_on_rename_failure(self, tmp_path):
        """If os.rename raises OSError (exotic filesystems — FUSE, SMB,
        overlayfs), the adapter falls back to the legacy in-place write
        rather than losing the publish entirely. Users see a warning in
        the logs but tiles still land."""
        frame_dir = tmp_path / "frames"
        _populate_frames(frame_dir, count=5)

        media_file = tmp_path / "F.mkv"
        media_file.write_bytes(b"")

        adapter = JellyfinTrickplayAdapter(width=320)
        bundle = _make_bundle(str(media_file), frame_dir, frame_count=5)
        sheet0 = adapter.compute_output_paths(bundle, MagicMock(), item_id=None)[0]

        def _always_fail(src, dst):
            raise OSError("simulated exotic filesystem")

        with patch(
            "media_preview_generator.output.jellyfin_trickplay.os.rename",
            side_effect=_always_fail,
        ):
            adapter.publish(bundle, [sheet0], item_id=None)

        # Fallback wrote in-place — final dir exists with sheet 0.
        sheets_dir = tmp_path / "F.trickplay" / "320 - 10x10"
        assert sheets_dir.is_dir()
        assert (sheets_dir / "0.jpg").is_file()

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
