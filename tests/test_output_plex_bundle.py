"""Tests for the Plex bundle BIF output adapter.

Verifies that:
- the bundle-hash → on-disk path mapping matches what Plex expects,
- multi-part items pick the matching bundle hash by basename,
- "not yet indexed" cases raise the right exception so the dispatcher's
  slow-backoff queue takes over,
- ``publish`` packs frames into a real BIF on disk.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from media_preview_generator.output import BifBundle, PlexBundleAdapter
from media_preview_generator.servers import LibraryNotYetIndexedError, PlexServer


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

    def test_does_not_double_prefix_url_when_item_id_is_full_path(self, tmp_path, mock_config):
        """D31 — END-TO-END regression test that DOESN'T mock get_bundle_metadata
        (which is what hid the bug for months).

        The pre-D31 webhook router stuffed the URL form ``/library/metadata/<id>``
        into ``item_id_by_server`` and passed it to ``compute_output_paths``.
        Inside, ``get_bundle_metadata`` did ``f"/library/metadata/{item_id}/tree"``
        which doubled the prefix → ``//library/metadata/<id>/tree`` → 404 →
        silently swallowed → "no MediaPart with bundle hash yet" lie.

        Every existing TestComputeOutputPaths test mocks get_bundle_metadata,
        so they all PASSED while production was silently broken on every
        Sonarr/Radarr → Plex webhook for at least 3 days. This test covers
        that gap by exercising the real URL-construction path and capturing
        the URL that hits Plex's API."""
        adapter = PlexBundleAdapter(plex_config_folder="/cfg", frame_interval=10)
        server = PlexServer(mock_config)

        # Mock the underlying plex.query to capture the URL — this is the
        # ONLY thing we mock; get_bundle_metadata's URL construction runs
        # for real.
        from xml.etree import ElementTree as ET

        captured_urls: list[str] = []

        def fake_query(url):
            captured_urls.append(url)
            return ET.fromstring(
                "<MediaContainer><MetadataItem><MediaItem>"
                '<MediaPart hash="abcdef0123456789" file="/m/foo.mkv" />'
                "</MediaItem></MetadataItem></MediaContainer>"
            )

        plex = MagicMock()
        plex.query = fake_query
        server._plex = plex

        # Pass the URL form (the buggy webhook router shape).
        paths = adapter.compute_output_paths(
            _make_bundle("/m/foo.mkv", tmp_path),
            server,
            item_id="/library/metadata/557676",
        )

        # The query URL must NOT have a doubled prefix — every Sonarr →
        # Plex webhook for months silently failed because of exactly this.
        assert len(captured_urls) == 1
        assert captured_urls[0] == "/library/metadata/557676/tree", (
            f"URL was {captured_urls[0]!r} — doubled prefix means 404, silent skip, "
            "'not indexed yet' lie. This was the D31 bug."
        )
        assert "//library/metadata" not in captured_urls[0]
        # And the bundle path was computed (would have raised
        # LibraryNotYetIndexedError if the URL had failed).
        assert paths[0] == Path("/cfg/Media/localhost/a/bcdef0123456789.bundle/Contents/Indexes/index-sd.bif")

    def test_handles_bare_rating_key_input(self, tmp_path, mock_config):
        """The same end-to-end path with the canonical input shape (bare
        ratingKey) — must produce the SAME URL as the URL-form input. Pair
        this with the previous test: both shapes converge on one correct URL."""
        adapter = PlexBundleAdapter(plex_config_folder="/cfg", frame_interval=10)
        server = PlexServer(mock_config)

        from xml.etree import ElementTree as ET

        captured_urls: list[str] = []

        def fake_query(url):
            captured_urls.append(url)
            return ET.fromstring(
                "<MediaContainer><MetadataItem><MediaItem>"
                '<MediaPart hash="abcdef0123456789" file="/m/foo.mkv" />'
                "</MediaItem></MetadataItem></MediaContainer>"
            )

        plex = MagicMock()
        plex.query = fake_query
        server._plex = plex

        adapter.compute_output_paths(
            _make_bundle("/m/foo.mkv", tmp_path),
            server,
            item_id="557676",  # bare ratingKey (the canonical shape)
        )

        assert captured_urls == ["/library/metadata/557676/tree"]


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
