"""TEST_AUDIT P1.6 — adapter compute_output_paths contract matrix.

Pins the EXACT on-disk path each adapter writes to, mirroring the format
the corresponding media server reads from. Closes the D38 layout-mismatch
incident class (commit 8409952): the Jellyfin trickplay adapter wrote to
``<dir>/trickplay/<basename>-<width>/`` but Jellyfin 10.10+ reads from
``<media_dir>/<basename>.trickplay/<width> - 10x10/`` — every preview was
silently invisible until we matched the exact layout.

The tests in this file pin:
  Plex bundle:   {plex_config}/Media/localhost/{h0}/{h[1:]}.bundle/Contents/Indexes/index-sd.bif
  Emby sidecar:  {media_dir}/{basename}-{width}-{interval}.bif
  Jellyfin trick: {media_dir}/{basename}.trickplay/{width} - 10x10/0.jpg

A regression in any adapter's path layout fails here LOUDLY (file:line
identifies the exact byte mismatch) instead of silently in production
where users only notice "previews still not showing in Jellyfin".
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from media_preview_generator.output.base import BifBundle
from media_preview_generator.output.emby_sidecar import EmbyBifAdapter
from media_preview_generator.output.jellyfin_trickplay import JellyfinTrickplayAdapter
from media_preview_generator.output.plex_bundle import PlexBundleAdapter


def _bundle(canonical_path: str, *, prefetched=None) -> BifBundle:
    """Minimal BifBundle for path-derivation tests (no real frames needed)."""
    return BifBundle(
        canonical_path=canonical_path,
        frame_dir=Path("/dev/null"),
        bif_path=None,
        frame_interval=10,
        width=320,
        height=180,
        frame_count=0,
        prefetched_bundle_metadata=prefetched or (),
    )


# ---------------------------------------------------------------------------
# Plex bundle adapter
# ---------------------------------------------------------------------------


class TestPlexBundleAdapterPathLayout:
    """Plex's expected path:
    ``{plex}/Media/localhost/{h0}/{h[1:]}.bundle/Contents/Indexes/index-sd.bif``

    Pins the exact filesystem layout Plex Media Server reads. A drift in
    ANY component (folder name, filename, hash split) silently breaks every
    Plex install.
    """

    def test_static_helper_produces_canonical_layout(self):
        """``PlexBundleAdapter.bundle_bif_path`` is public so other code
        (the orchestrator's enumeration prefetch) can compute paths
        without an adapter instance. Pin the exact byte string.
        """
        path = PlexBundleAdapter.bundle_bif_path(
            plex_config_folder="/config/plex",
            bundle_hash="abcdef0123456789",
        )
        # Exact layout: first hash char becomes the leading subfolder; the
        # remaining 15 chars form the bundle directory's prefix.
        assert str(path) == "/config/plex/Media/localhost/a/bcdef0123456789.bundle/Contents/Indexes/index-sd.bif", (
            f"Plex bundle layout drifted from what Plex Media Server reads: {path}"
        )

    def test_compute_output_paths_uses_prefetched_hash_when_present(self):
        """When ``bundle.prefetched_bundle_metadata`` carries the hash,
        ``compute_output_paths`` must use it (skipping the /tree call).

        Pin the EXACT path layout so a regression that mangles the hash
        split or the bundle directory name is caught.
        """
        adapter = PlexBundleAdapter(plex_config_folder="/config/plex", frame_interval=10)
        # Hash matches the canonical_path basename; second tuple field is
        # the file path Plex reported (used by the hash-selector).
        bundle = _bundle(
            "/data/movies/Test (2024).mkv",
            prefetched=(("deadbeef" * 5, "/data/movies/Test (2024).mkv"),),
        )

        # Need a PlexServer instance to satisfy the isinstance check, but
        # the prefetched metadata short-circuits the API call.
        from media_preview_generator.servers.plex import PlexServer

        server = MagicMock(spec=PlexServer)

        paths = adapter.compute_output_paths(bundle, server, item_id="rk-12345")
        assert len(paths) == 1
        # 8*5 = 40-char hash; first char "d", remainder "eadbeef..." — the
        # split must split exactly at index 1, NOT at any other point.
        expected = "/config/plex/Media/localhost/d/eadbeefdeadbeefdeadbeefdeadbeefdeadbeef.bundle/Contents/Indexes/index-sd.bif"
        assert str(paths[0]) == expected, f"Plex path layout drift: {paths[0]}"

    def test_compute_output_paths_raises_when_item_id_missing(self):
        """The Plex bundle path requires the bundle hash, which requires
        an item_id. Missing item_id → ValueError so the caller hits the
        SKIPPED_NOT_IN_LIBRARY branch instead of cryptic downstream
        attribute errors.
        """
        adapter = PlexBundleAdapter(plex_config_folder="/config/plex", frame_interval=10)
        with pytest.raises(ValueError, match="item_id"):
            adapter.compute_output_paths(_bundle("/data/x.mkv"), server=MagicMock(), item_id=None)

    def test_compute_output_paths_raises_when_server_missing(self):
        adapter = PlexBundleAdapter(plex_config_folder="/config/plex", frame_interval=10)
        with pytest.raises(ValueError, match="PlexServer"):
            adapter.compute_output_paths(_bundle("/data/x.mkv"), server=None, item_id="rk-1")

    def test_compute_output_paths_raises_when_server_wrong_type(self):
        adapter = PlexBundleAdapter(plex_config_folder="/config/plex", frame_interval=10)
        # An EmbyServer-shaped object (not PlexServer) → TypeError.
        not_plex = MagicMock()  # NOT spec=PlexServer
        with pytest.raises(TypeError, match="PlexServer"):
            adapter.compute_output_paths(_bundle("/data/x.mkv"), server=not_plex, item_id="rk-1")


# ---------------------------------------------------------------------------
# Emby sidecar adapter
# ---------------------------------------------------------------------------


class TestEmbySidecarAdapterPathLayout:
    """Emby's "Save preview video thumbnails into media folders" reads:
    ``{media_dir}/{basename}-{width}-{interval}.bif``

    Pure path derivation — no server metadata. The format and naming
    convention came from Emby community discussion; matches what Emby's
    own generation produces.
    """

    def test_basic_layout(self):
        adapter = EmbyBifAdapter(width=320, frame_interval=10)
        bundle = _bundle("/data/movies/Test Movie.mkv")
        paths = adapter.compute_output_paths(bundle, server=None, item_id=None)
        assert len(paths) == 1
        assert str(paths[0]) == "/data/movies/Test Movie-320-10.bif", (
            f"Emby sidecar layout drift: {paths[0]}. Emby reads ``{{basename}}-{{width}}-{{interval}}.bif`` "
            f"alongside the source — any deviation makes Emby ignore the file."
        )

    def test_custom_width_and_interval_in_filename(self):
        """Width + interval must appear in filename so Emby can store
        multiple resolutions side-by-side.
        """
        adapter = EmbyBifAdapter(width=480, frame_interval=5)
        bundle = _bundle("/m/Foo (1999).mkv")
        paths = adapter.compute_output_paths(bundle, server=None, item_id=None)
        assert str(paths[0]) == "/m/Foo (1999)-480-5.bif"

    def test_episode_path_with_subdirs(self):
        """TV episode in nested folders — sidecar lives in the SAME folder
        as the source, not in the library root.
        """
        adapter = EmbyBifAdapter(width=320, frame_interval=10)
        bundle = _bundle("/tv/Show/Season 02/Show - S02E01.mkv")
        paths = adapter.compute_output_paths(bundle, server=None, item_id=None)
        assert str(paths[0]) == "/tv/Show/Season 02/Show - S02E01-320-10.bif"

    def test_no_server_metadata_required(self):
        """``needs_server_metadata`` must report False so the dispatcher
        skips the per-item lookup. Sidecar layout depends only on the
        canonical path + adapter init.
        """
        adapter = EmbyBifAdapter(width=320, frame_interval=10)
        assert adapter.needs_server_metadata() is False


# ---------------------------------------------------------------------------
# Jellyfin trickplay adapter (D38 layout)
# ---------------------------------------------------------------------------


class TestJellyfinTrickplayAdapterPathLayout:
    """Jellyfin reads trickplay tiles from:
    ``{media_dir}/{basename}.trickplay/{width} - {tile_w}x{tile_h}/``

    The D38 incident (commit 8409952): the adapter previously wrote to
    ``<dir>/trickplay/<basename>-<width>/``. Jellyfin 10.10+ reads from
    ``<media_dir>/<basename>.trickplay/<width> - 10x10/`` (note the
    SPACES around the dash, the dot before "trickplay", and the explicit
    tile dimensions). Every preview was silently invisible until the
    adapter was rewritten to emit the exact layout Jellyfin expects.

    These tests pin EVERY component of that layout. A future refactor
    that drops the spaces, the dot, or the tile dimensions would fail
    here loudly.
    """

    def test_trickplay_dir_uses_basename_dot_trickplay(self):
        """Jellyfin's PathManager.GetTrickplayDirectory(item, saveWithMedia=true)
        returns ``Path.ChangeExtension(item.Path, ".trickplay")`` — that's
        ``{stem}.trickplay`` next to the source.
        """
        path = JellyfinTrickplayAdapter.trickplay_dir("/data/movies/Foo (2024).mkv")
        assert str(path) == "/data/movies/Foo (2024).trickplay", (
            f"Jellyfin trickplay dir must be '{{stem}}.trickplay' (with the dot); got {path}. "
            f"Pre-D38 layout '/data/movies/trickplay/Foo (2024)' is invisible to Jellyfin."
        )

    def test_sheet_dir_uses_width_space_dash_space_tilesxtiles(self):
        """Jellyfin's TrickplayManager.GetTrickplayDirectory appends
        ``"{width} - {tileW}x{tileH}"`` — note the SPACES around the dash
        and the lowercase 'x'. Every character matters.
        """
        path = JellyfinTrickplayAdapter.sheet_dir("/data/movies/Foo.mkv", width=320)
        assert str(path) == "/data/movies/Foo.trickplay/320 - 10x10", (
            f"Jellyfin sheet dir layout drift: {path}. "
            f"Pre-D38 used '320-10x10' (no spaces) — Jellyfin couldn't find it."
        )

    def test_compute_output_paths_returns_sheet_zero_jpg(self):
        """``compute_output_paths`` returns the sheet-0 path as the
        freshness proxy (sheet 0 is always present whenever any trickplay
        output exists, so its mtime represents the whole output).
        """
        adapter = JellyfinTrickplayAdapter(width=320, frame_interval=10)
        bundle = _bundle("/data/movies/Foo (2024).mkv")
        paths = adapter.compute_output_paths(bundle, server=None, item_id="jelly-id-99")
        assert len(paths) == 1
        assert str(paths[0]) == "/data/movies/Foo (2024).trickplay/320 - 10x10/0.jpg", (
            f"Jellyfin sheet-0 path drift: {paths[0]}"
        )

    def test_custom_width_propagates(self):
        """Width customisation must change the per-resolution sheet dir,
        not the trickplay dir.
        """
        adapter = JellyfinTrickplayAdapter(width=480, frame_interval=10)
        bundle = _bundle("/m/Foo.mkv")
        paths = adapter.compute_output_paths(bundle, server=None, item_id="x")
        assert str(paths[0]) == "/m/Foo.trickplay/480 - 10x10/0.jpg"

    def test_compute_output_paths_raises_when_item_id_missing(self):
        """item_id is required for the publish-time write_meta call (the
        only API-needing field; the path itself is pure-derivable).
        Missing item_id → ValueError.
        """
        adapter = JellyfinTrickplayAdapter(width=320, frame_interval=10)
        with pytest.raises(ValueError, match="item_id"):
            adapter.compute_output_paths(_bundle("/d/x.mkv"), server=None, item_id=None)


# ---------------------------------------------------------------------------
# Cross-adapter parametrized matrix — single concentrated contract pin
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "adapter_factory,canonical_path,item_id,server_factory,expected_path",
    [
        # (factory, canonical, item_id, server, expected)
        pytest.param(
            lambda: EmbyBifAdapter(width=320, frame_interval=10),
            "/data/movies/Foo.mkv",
            None,
            lambda: None,
            "/data/movies/Foo-320-10.bif",
            id="emby_basic",
        ),
        pytest.param(
            lambda: JellyfinTrickplayAdapter(width=320, frame_interval=10),
            "/data/movies/Foo.mkv",
            "jelly-id",
            lambda: None,
            "/data/movies/Foo.trickplay/320 - 10x10/0.jpg",
            id="jellyfin_basic",
        ),
    ],
)
def test_pure_adapter_path_matrix(adapter_factory, canonical_path, item_id, server_factory, expected_path):
    """Single parametrized sweep over the pure (no-server-metadata) adapters'
    layouts. The Plex variant lives in TestPlexBundleAdapterPathLayout
    above because it requires PlexServer mocking.

    Catches a class of bugs where adding a NEW adapter accidentally
    breaks the path layout of an existing one (same dispatch path).
    """
    adapter = adapter_factory()
    bundle = _bundle(canonical_path)
    paths = adapter.compute_output_paths(bundle, server=server_factory(), item_id=item_id)
    assert len(paths) == 1
    assert str(paths[0]) == expected_path, f"{adapter.name} path layout drift: {paths[0]}"
