"""Tests for the Plex bundle BIF output adapter.

Verifies that:
- the bundle-hash → on-disk path mapping matches what Plex expects
  (regression check for the existing `_setup_bundle_paths` behaviour),
- multi-part items pick the matching bundle hash by basename,
- "not yet indexed" cases raise the right exception so the dispatcher's
  slow-backoff queue takes over,
- ``publish`` packs frames into a real BIF on disk.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from plex_generate_previews.output import BifBundle, PlexBundleAdapter
from plex_generate_previews.servers import LibraryNotYetIndexedError, PlexServer


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
    def test_returns_true(self):
        adapter = PlexBundleAdapter(plex_config_folder="/cfg", frame_interval=10)
        assert adapter.needs_server_metadata() is True

    def test_name(self):
        adapter = PlexBundleAdapter(plex_config_folder="/cfg", frame_interval=10)
        assert adapter.name == "plex_bundle"


class TestComputeOutputPaths:
    def test_path_structure_matches_plex_bundle_layout(self, tmp_path, mock_config):
        adapter = PlexBundleAdapter(plex_config_folder="/cfg", frame_interval=10)
        server = PlexServer(mock_config)
        with patch.object(
            server,
            "get_bundle_metadata",
            return_value=[("abcdef0123456789", "/m/foo.mkv")],
        ):
            paths = adapter.compute_output_paths(
                _make_bundle("/m/foo.mkv", tmp_path),
                server,
                item_id="42",
            )

        assert len(paths) == 1
        assert paths[0] == Path("/cfg/Media/localhost/a/bcdef0123456789.bundle/Contents/Indexes/index-sd.bif")

    def test_picks_matching_part_by_basename(self, tmp_path, mock_config):
        adapter = PlexBundleAdapter(plex_config_folder="/cfg", frame_interval=10)
        server = PlexServer(mock_config)
        with patch.object(
            server,
            "get_bundle_metadata",
            return_value=[
                ("aaaaaaaaaa", "/m/disc1.mkv"),
                ("bbbbbbbbbb", "/m/disc2.mkv"),
            ],
        ):
            paths = adapter.compute_output_paths(
                _make_bundle("/m/disc2.mkv", tmp_path),
                server,
                item_id="99",
            )

        # Should pick the second part's hash, not the first.
        assert paths[0] == Path("/cfg/Media/localhost/b/bbbbbbbbb.bundle/Contents/Indexes/index-sd.bif")

    def test_falls_back_to_first_hash_when_no_basename_match(self, tmp_path, mock_config):
        adapter = PlexBundleAdapter(plex_config_folder="/cfg", frame_interval=10)
        server = PlexServer(mock_config)
        with patch.object(
            server,
            "get_bundle_metadata",
            return_value=[("zzzzzzzzzz", "/srv/different/path.mkv")],
        ):
            paths = adapter.compute_output_paths(
                _make_bundle("/m/foo.mkv", tmp_path),
                server,
                item_id="42",
            )
        assert paths[0].name == "index-sd.bif"
        assert "zzzzzzzzz.bundle" in str(paths[0])

    def test_empty_metadata_raises_not_yet_indexed(self, tmp_path, mock_config):
        adapter = PlexBundleAdapter(plex_config_folder="/cfg", frame_interval=10)
        server = PlexServer(mock_config)
        with patch.object(server, "get_bundle_metadata", return_value=[]):
            with pytest.raises(LibraryNotYetIndexedError):
                adapter.compute_output_paths(
                    _make_bundle("/m/foo.mkv", tmp_path),
                    server,
                    item_id="42",
                )

    def test_invalid_hash_for_matching_part_raises_not_yet_indexed(self, tmp_path, mock_config):
        adapter = PlexBundleAdapter(plex_config_folder="/cfg", frame_interval=10)
        server = PlexServer(mock_config)
        with patch.object(
            server,
            "get_bundle_metadata",
            return_value=[("", "/m/foo.mkv")],
        ):
            with pytest.raises(LibraryNotYetIndexedError):
                adapter.compute_output_paths(
                    _make_bundle("/m/foo.mkv", tmp_path),
                    server,
                    item_id="42",
                )

    def test_missing_item_id_raises_value_error(self, tmp_path, mock_config):
        adapter = PlexBundleAdapter(plex_config_folder="/cfg", frame_interval=10)
        server = PlexServer(mock_config)
        with pytest.raises(ValueError, match="item_id"):
            adapter.compute_output_paths(
                _make_bundle("/m/foo.mkv", tmp_path),
                server,
                item_id=None,
            )

    def test_non_plex_server_raises_type_error(self, tmp_path):
        adapter = PlexBundleAdapter(plex_config_folder="/cfg", frame_interval=10)
        with pytest.raises(TypeError, match="PlexServer"):
            adapter.compute_output_paths(
                _make_bundle("/m/foo.mkv", tmp_path),
                MagicMock(spec=object),  # not a MediaServer at all
                item_id="42",
            )


class TestPublish:
    def test_creates_parent_dirs_and_writes_bif(self, tmp_path):
        # Arrange: a frame dir with three small JPGs.
        frame_dir = tmp_path / "frames"
        frame_dir.mkdir()
        for i in range(3):
            (frame_dir / f"{i:05d}.jpg").write_bytes(b"\xff\xd8\xff" + b"\x00" * 100)

        out_dir = tmp_path / "deeply" / "nested" / "Indexes"
        out_path = out_dir / "index-sd.bif"

        adapter = PlexBundleAdapter(plex_config_folder=str(tmp_path), frame_interval=5)
        bundle = BifBundle(
            canonical_path="/m/foo.mkv",
            frame_dir=frame_dir,
            bif_path=None,
            frame_interval=5,
            width=320,
            height=180,
            frame_count=3,
        )

        # Act
        adapter.publish(bundle, [out_path])

        # Assert: directory was created and file is non-empty BIF.
        assert out_path.exists()
        data = out_path.read_bytes()
        assert data[:8] == bytes([0x89, 0x42, 0x49, 0x46, 0x0D, 0x0A, 0x1A, 0x0A])  # BIF magic

    def test_empty_output_paths_raises(self, tmp_path):
        adapter = PlexBundleAdapter(plex_config_folder="/cfg", frame_interval=10)
        bundle = _make_bundle("/m/foo.mkv", tmp_path)
        with pytest.raises(ValueError):
            adapter.publish(bundle, [])
