"""Tests for OutputAdapter.list_orphans_in_folder.

Per-adapter helpers that return basename-derived sidecar paths whose
source media file is no longer present. Pure functions, side-effect-free
— the cleanup dispatcher in multi_server.py decides when to delete.

Covers the matrix per .claude/rules/testing.md:
  - Adapter (Jellyfin / Emby / base default)
  - Live basenames (none / one / many)
  - Folder shape (empty / one orphan / one live + one orphan)
  - Boundary kwarg integrity (returned paths are exactly what's on disk)
"""

from __future__ import annotations

from pathlib import Path

from media_preview_generator.output.base import BifBundle, OutputAdapter
from media_preview_generator.output.emby_sidecar import EmbyBifAdapter
from media_preview_generator.output.jellyfin_trickplay import JellyfinTrickplayAdapter


def _touch(path: Path, *, content: bytes = b"") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _mkdir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


# ---------------------------------------------------------------------------
# Jellyfin trickplay adapter
# ---------------------------------------------------------------------------


class TestJellyfinTrickplayOrphanEnumeration:
    def test_returns_orphan_when_source_video_is_gone(self, tmp_path):
        """Folder has Old.trickplay/ but no Old.mkv → returned as orphan."""
        old_dir = _mkdir(tmp_path / "Movie-1080p-TheMrG.trickplay")
        # New file IS live; its trickplay dir is NOT an orphan.
        _touch(tmp_path / "Movie-2160p-HDSWEB.mkv")
        _mkdir(tmp_path / "Movie-2160p-HDSWEB.trickplay")

        adapter = JellyfinTrickplayAdapter()
        live = {"Movie-2160p-HDSWEB"}
        orphans = adapter.list_orphans_in_folder(tmp_path, live)

        assert orphans == [old_dir]

    def test_skips_dir_when_basename_is_live(self, tmp_path):
        """Live basename → trickplay dir is not orphaned."""
        live_dir = _mkdir(tmp_path / "Movie-2160p-HDSWEB.trickplay")
        _touch(tmp_path / "Movie-2160p-HDSWEB.mkv")

        adapter = JellyfinTrickplayAdapter()
        orphans = adapter.list_orphans_in_folder(tmp_path, {"Movie-2160p-HDSWEB"})

        assert live_dir not in orphans
        assert orphans == []

    def test_skips_atomic_swap_debris(self, tmp_path):
        """``.<name>.trickplay.staging/`` and ``.old/`` siblings are owned by
        an in-flight publish, never returned as orphans even when stems mismatch."""
        # Hidden staging dir from an in-progress write — must not be touched.
        staging = _mkdir(tmp_path / ".Movie-HDSWEB.trickplay.staging")
        old_swap = _mkdir(tmp_path / ".Movie-HDSWEB.trickplay.old")
        # Real orphan
        real_orphan = _mkdir(tmp_path / "Movie-OLD.trickplay")

        adapter = JellyfinTrickplayAdapter()
        orphans = adapter.list_orphans_in_folder(tmp_path, set())

        assert staging not in orphans
        assert old_swap not in orphans
        assert real_orphan in orphans

    def test_returns_empty_for_folder_with_no_trickplay_dirs(self, tmp_path):
        _touch(tmp_path / "Movie.mkv")
        adapter = JellyfinTrickplayAdapter()
        assert adapter.list_orphans_in_folder(tmp_path, {"Movie"}) == []

    def test_returns_empty_for_nonexistent_folder(self, tmp_path):
        adapter = JellyfinTrickplayAdapter()
        # OSError from glob on a missing dir is swallowed → []
        assert adapter.list_orphans_in_folder(tmp_path / "missing", set()) == []

    def test_multiple_orphans_returned(self, tmp_path):
        old_a = _mkdir(tmp_path / "MovieA.trickplay")
        old_b = _mkdir(tmp_path / "MovieB.trickplay")
        live = _mkdir(tmp_path / "MovieC.trickplay")
        _touch(tmp_path / "MovieC.mkv")

        adapter = JellyfinTrickplayAdapter()
        orphans = set(adapter.list_orphans_in_folder(tmp_path, {"MovieC"}))

        assert orphans == {old_a, old_b}
        assert live not in orphans

    def test_skips_files_named_trickplay(self, tmp_path):
        """If a stray *file* (not directory) ends in .trickplay, skip it."""
        _touch(tmp_path / "stray.trickplay", content=b"not a directory")
        adapter = JellyfinTrickplayAdapter()
        assert adapter.list_orphans_in_folder(tmp_path, set()) == []


# ---------------------------------------------------------------------------
# Emby sidecar adapter
# ---------------------------------------------------------------------------


class TestEmbySidecarOrphanEnumeration:
    def test_returns_orphan_bif_and_meta_pair(self, tmp_path):
        """Old basename's .bif AND its .bif.meta sidecar are both returned."""
        old_bif = _touch(tmp_path / "Movie-OLD-320-10.bif", content=b"BIF")
        old_meta = _touch(tmp_path / "Movie-OLD-320-10.bif.meta", content=b"{}")
        # Live file has its own bif — must NOT be returned.
        _touch(tmp_path / "Movie-NEW.mkv")
        live_bif = _touch(tmp_path / "Movie-NEW-320-10.bif", content=b"BIF")

        adapter = EmbyBifAdapter()
        orphans = adapter.list_orphans_in_folder(tmp_path, {"Movie-NEW"})

        # Both old artifacts returned, in adjacent order.
        assert old_bif in orphans
        assert old_meta in orphans
        assert live_bif not in orphans

    def test_returns_only_bif_when_meta_missing(self, tmp_path):
        old_bif = _touch(tmp_path / "Movie-OLD-320-10.bif")

        adapter = EmbyBifAdapter()
        orphans = adapter.list_orphans_in_folder(tmp_path, set())

        assert orphans == [old_bif]

    def test_ignores_bif_with_non_managed_pattern(self, tmp_path):
        """A .bif without ``-<W>-<I>`` suffix isn't ours — leave it alone."""
        foreign = _touch(tmp_path / "RandomThing.bif", content=b"x")
        adapter = EmbyBifAdapter()
        assert adapter.list_orphans_in_folder(tmp_path, set()) == []
        assert foreign.exists()  # caller didn't return it; we never touch it

    def test_skips_live_basename(self, tmp_path):
        live_bif = _touch(tmp_path / "Movie-NEW-320-10.bif")
        _touch(tmp_path / "Movie-NEW.mkv")

        adapter = EmbyBifAdapter()
        assert adapter.list_orphans_in_folder(tmp_path, {"Movie-NEW"}) == []
        assert live_bif.exists()

    def test_returns_empty_for_nonexistent_folder(self, tmp_path):
        adapter = EmbyBifAdapter()
        assert adapter.list_orphans_in_folder(tmp_path / "missing", set()) == []

    def test_dash_only_basename_no_W_I_suffix_is_ignored(self, tmp_path):
        """Filename like ``Movie-Title.bif`` (no numeric W-I suffix) is foreign."""
        foreign = _touch(tmp_path / "Movie-Title.bif")
        adapter = EmbyBifAdapter()
        assert adapter.list_orphans_in_folder(tmp_path, set()) == []
        assert foreign.exists()

    def test_basename_with_dashes_is_recovered(self, tmp_path):
        """``Movie-Title-Sub-320-10.bif`` → basename = ``Movie-Title-Sub``."""
        old = _touch(tmp_path / "Movie-Title-Sub-320-10.bif")
        adapter = EmbyBifAdapter()
        # ``Movie-Title-Sub`` is not in live basenames → returned as orphan
        orphans = adapter.list_orphans_in_folder(tmp_path, set())
        assert orphans == [old]
        # And IS protected when live
        adapter2 = EmbyBifAdapter()
        assert adapter2.list_orphans_in_folder(tmp_path, {"Movie-Title-Sub"}) == []


# ---------------------------------------------------------------------------
# Base class default
# ---------------------------------------------------------------------------


class _BareAdapter(OutputAdapter):
    """Minimal concrete adapter that doesn't override list_orphans_in_folder."""

    @property
    def name(self) -> str:
        return "bare"

    def needs_server_metadata(self) -> bool:
        return False

    def compute_output_paths(self, bundle, server, item_id):
        return []

    def publish(self, bundle: BifBundle, output_paths, item_id=None) -> None:
        return None


class TestBaseAdapterDefault:
    def test_default_returns_empty(self, tmp_path):
        """Adapters that don't override get a no-op sweep — never returns orphans."""
        # Even with .trickplay dirs / .bif files in the folder, the base
        # default ignores them (only adapter-specific code knows what
        # constitutes "this adapter's artifacts").
        _mkdir(tmp_path / "Movie.trickplay")
        _touch(tmp_path / "Movie-320-10.bif")

        adapter = _BareAdapter()
        assert adapter.list_orphans_in_folder(tmp_path, set()) == []
