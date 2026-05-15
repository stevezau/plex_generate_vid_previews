"""Tests for BIF reader module and BIF viewer API endpoints."""

import array
import json
import os
import struct
from unittest.mock import MagicMock, patch

import pytest

from media_preview_generator.bif_reader import (
    read_bif_frame,
    read_bif_metadata,
    unpack_bif_to_jpegs,
)
from media_preview_generator.web.app import create_app
from media_preview_generator.web.settings_manager import reset_settings_manager

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FRAME_DATA = [b"\xff\xd8\xff" + bytes([i] * 50) for i in range(5)]


def _write_test_bif(path: str, frames: list[bytes] = _FRAME_DATA, interval_ms: int = 2000) -> str:
    """Write a minimal valid BIF file and return its path."""
    magic = [0x89, 0x42, 0x49, 0x46, 0x0D, 0x0A, 0x1A, 0x0A]
    version = 0
    count = len(frames)

    with open(path, "wb") as f:
        array.array("B", magic).tofile(f)
        f.write(struct.pack("<I", version))
        f.write(struct.pack("<I", count))
        f.write(struct.pack("<I", interval_ms))
        array.array("B", [0x00] * 44).tofile(f)

        table_size = 8 + (8 * count)
        image_offset = 64 + table_size

        for i, frame in enumerate(frames):
            f.write(struct.pack("<I", i))
            f.write(struct.pack("<I", image_offset))
            image_offset += len(frame)

        f.write(struct.pack("<I", 0xFFFFFFFF))
        f.write(struct.pack("<I", image_offset))

        for frame in frames:
            f.write(frame)

    return path


# ---------------------------------------------------------------------------
# BIF reader unit tests
# ---------------------------------------------------------------------------


class TestReadBifMetadata:
    """Test read_bif_metadata()."""

    def test_valid_bif(self, tmp_path):
        bif = _write_test_bif(str(tmp_path / "test.bif"))
        meta = read_bif_metadata(bif)

        assert meta.version == 0
        assert meta.frame_count == 5
        assert meta.frame_interval_ms == 2000
        assert len(meta.frame_offsets) == 5
        assert len(meta.frame_sizes) == 5
        assert all(s == 53 for s in meta.frame_sizes)
        assert meta.file_size > 0

    def test_single_frame(self, tmp_path):
        data = [b"\xff\xd8\xff\xe0"]
        bif = _write_test_bif(str(tmp_path / "one.bif"), frames=data)
        meta = read_bif_metadata(bif)
        assert meta.frame_count == 1
        assert meta.frame_sizes == [4]

    def test_file_not_found(self):
        with pytest.raises(FileNotFoundError):
            read_bif_metadata("/nonexistent/path.bif")

    def test_bad_magic(self, tmp_path):
        bad = tmp_path / "bad.bif"
        bad.write_bytes(b"\x00" * 128)
        with pytest.raises(ValueError, match="bad magic"):
            read_bif_metadata(str(bad))

    def test_truncated_header_raises(self, tmp_path):
        """A file shorter than the 64-byte BIF header must raise loudly.

        Without an explicit short-read guard, struct.unpack("<I", b"") on
        a partial read would raise struct.error — surfaced to the user
        as a confusing low-level exception. Either way, the production
        contract is "raise on truncated header"; a regression that
        silently returns garbage metadata would fool the viewer into
        rendering corruption.
        """
        bad = tmp_path / "truncated.bif"
        # Valid magic, but only 8 bytes total — every subsequent uint32
        # read should fail.
        bad.write_bytes(b"\x89BIF\r\n\x1a\n")
        with pytest.raises((ValueError, struct.error)):
            read_bif_metadata(str(bad))

    def test_missing_sentinel_in_index_table_raises(self, tmp_path):
        """When the index table doesn't end with the 0xFFFFFFFF sentinel,
        the parser must raise. Otherwise a corrupt BIF would silently be
        treated as truncated and the last frame's size would be wrong
        (off-by-one against the rest of the file).
        """
        magic = bytes([0x89, 0x42, 0x49, 0x46, 0x0D, 0x0A, 0x1A, 0x0A])
        version = 0
        count = 1
        interval_ms = 1000
        bad = tmp_path / "no_sentinel.bif"
        with open(bad, "wb") as f:
            f.write(magic)
            f.write(struct.pack("<I", version))
            f.write(struct.pack("<I", count))
            f.write(struct.pack("<I", interval_ms))
            f.write(b"\x00" * 44)
            # Index entry: timestamp=0, offset=80
            f.write(struct.pack("<I", 0))
            f.write(struct.pack("<I", 80))
            # Sentinel slot is filled with WRONG values (not 0xFFFFFFFF).
            f.write(struct.pack("<I", 0))  # should be 0xFFFFFFFF
            f.write(struct.pack("<I", 84))
            # Frame data
            f.write(b"\xff\xd8\xff\xe0")
        with pytest.raises(ValueError, match="sentinel"):
            read_bif_metadata(str(bad))

    def test_reference_bif(self, reference_bif):
        """Validate parsing against the checked-in reference fixture."""
        if not os.path.isfile(reference_bif):
            pytest.skip("reference.bif fixture not present")
        meta = read_bif_metadata(reference_bif)
        assert meta.frame_count > 0
        assert meta.frame_interval_ms > 0


class TestReadBifFrame:
    """Test read_bif_frame()."""

    def test_extract_each_frame(self, tmp_path):
        bif = _write_test_bif(str(tmp_path / "test.bif"))
        meta = read_bif_metadata(bif)
        for i, expected in enumerate(_FRAME_DATA):
            assert read_bif_frame(bif, i, meta) == expected

    def test_without_preloaded_metadata(self, tmp_path):
        bif = _write_test_bif(str(tmp_path / "test.bif"))
        assert read_bif_frame(bif, 0) == _FRAME_DATA[0]

    def test_index_out_of_range(self, tmp_path):
        bif = _write_test_bif(str(tmp_path / "test.bif"))
        with pytest.raises(IndexError):
            read_bif_frame(bif, 999)

    def test_negative_index(self, tmp_path):
        bif = _write_test_bif(str(tmp_path / "test.bif"))
        with pytest.raises(IndexError):
            read_bif_frame(bif, -1)


class TestUnpackBifToJpegs:
    """D34 — round-trip every JPEG inside a BIF back to disk so the
    multi-server dispatcher can reuse Plex's existing BIF instead of
    re-running FFmpeg for sibling publishers (Jellyfin trickplay, etc.).
    """

    def test_writes_one_jpeg_per_frame(self, tmp_path):
        bif = _write_test_bif(str(tmp_path / "in.bif"))
        out = tmp_path / "frames"
        out.mkdir()

        count = unpack_bif_to_jpegs(bif, str(out))

        assert count == 5
        files = sorted(os.listdir(str(out)))
        assert files == ["00001.jpg", "00002.jpg", "00003.jpg", "00004.jpg", "00005.jpg"], (
            f"unpacked filenames must be 1-indexed and zero-padded so the FFmpeg-shaped consumer can walk them: {files!r}"
        )
        # Spot-check that the bytes round-trip identically — no JPEG
        # re-encoding step is involved, so byte equality is the contract.
        with open(str(out / "00001.jpg"), "rb") as f:
            assert f.read() == _FRAME_DATA[0]
        with open(str(out / "00005.jpg"), "rb") as f:
            assert f.read() == _FRAME_DATA[4]

    def test_returns_zero_when_bif_empty(self, tmp_path):
        bif = _write_test_bif(str(tmp_path / "empty.bif"), frames=[])
        out = tmp_path / "frames"
        out.mkdir()
        assert unpack_bif_to_jpegs(bif, str(out)) == 0
        assert os.listdir(str(out)) == []


# ---------------------------------------------------------------------------
# Web route / API fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_singletons():
    reset_settings_manager()
    import media_preview_generator.web.jobs as jobs_mod

    with jobs_mod._job_lock:
        jobs_mod._job_manager = None
    import media_preview_generator.web.scheduler as sched_mod

    with sched_mod._schedule_lock:
        sched_mod._schedule_manager = None
    from media_preview_generator.web.routes import clear_gpu_cache

    clear_gpu_cache()
    yield
    reset_settings_manager()
    with jobs_mod._job_lock:
        jobs_mod._job_manager = None
    with sched_mod._schedule_lock:
        if sched_mod._schedule_manager is not None:
            try:
                sched_mod._schedule_manager.stop()
            except Exception:
                pass
            sched_mod._schedule_manager = None
    clear_gpu_cache()


@pytest.fixture()
def app(tmp_path):
    config_dir = str(tmp_path / "config")
    os.makedirs(config_dir, exist_ok=True)
    auth_file = os.path.join(config_dir, "auth.json")
    with open(auth_file, "w") as f:
        json.dump({"token": "test-token-12345678"}, f)
    settings_file = os.path.join(config_dir, "settings.json")
    with open(settings_file, "w") as f:
        json.dump({"setup_complete": True, "plex_config_folder": str(tmp_path / "plex")}, f)

    with patch.dict(
        os.environ,
        {
            "CONFIG_DIR": config_dir,
            "WEB_AUTH_TOKEN": "test-token-12345678",
            "WEB_PORT": "8099",
        },
    ):
        flask_app = create_app(config_dir=config_dir)
        flask_app.config["TESTING"] = True
        flask_app.config["WTF_CSRF_ENABLED"] = False
        yield flask_app


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def authed_client(client):
    with client.session_transaction() as sess:
        sess["authenticated"] = True
    return client


def _api_headers() -> dict:
    return {"Authorization": "Bearer test-token-12345678"}


@pytest.fixture()
def sample_bif(tmp_path):
    """Write a test BIF inside the tmp_path tree (under PLEX_DATA_ROOT)."""
    bif_dir = tmp_path / "plex" / "Media" / "localhost" / "a" / "bcdef.bundle" / "Contents" / "Indexes"
    bif_dir.mkdir(parents=True)
    return _write_test_bif(str(bif_dir / "index-sd.bif"))


# ---------------------------------------------------------------------------
# BIF viewer page route tests
# ---------------------------------------------------------------------------


class TestBifViewerPage:
    def test_requires_auth(self, client):
        # @login_required uses Flask's redirect() which is 302 by default —
        # pinning the status code catches a regression that flips to a 308
        # (permanent route move) where bookmarks would behave differently.
        resp = client.get("/bif-viewer", follow_redirects=False)
        assert resp.status_code == 302
        assert "/login" in resp.headers.get("Location", "")

    def test_renders_when_authenticated(self, authed_client):
        resp = authed_client.get("/bif-viewer")
        assert resp.status_code == 200
        assert b"Preview Inspector" in resp.data


# ---------------------------------------------------------------------------
# BIF info endpoint tests
# ---------------------------------------------------------------------------


class TestBifInfoEndpoint:
    def test_requires_auth(self, client, sample_bif):
        resp = client.get(f"/api/bif/info?path={sample_bif}")
        assert resp.status_code == 401

    def test_returns_metadata(self, client, sample_bif):
        resp = client.get(f"/api/bif/info?path={sample_bif}", headers=_api_headers())
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["frame_count"] == 5
        assert data["frame_interval_ms"] == 2000
        assert data["file_size"] > 0
        assert "created_at" in data
        assert "avg_frame_size" in data

    def test_invalid_path_rejected(self, client):
        resp = client.get("/api/bif/info?path=/etc/passwd", headers=_api_headers())
        assert resp.status_code == 400

    def test_missing_path(self, client):
        resp = client.get("/api/bif/info", headers=_api_headers())
        assert resp.status_code == 400

    def test_media_file_path_gets_helpful_message(self, client):
        """Pasting a .mkv path should steer the user to the search box.

        Regression guard for issue #231 — users naturally try the source
        media path; the generic "Invalid or missing BIF file path" gave
        no hint that the search box above was the right path.
        """
        from media_preview_generator.web.routes.api_bif import _MEDIA_FILE_EXTS

        # Drive the loop from the constant so adding/removing an extension
        # is automatically covered without hand-maintaining the test list.
        # Mixed-case `.MP4` separately verifies the helper's .lower() pass.
        cases = list(_MEDIA_FILE_EXTS) + [".MP4"]
        for ext in cases:
            resp = client.get(
                f"/api/bif/info?path=/data/movies/Foo{ext}",
                headers=_api_headers(),
            )
            assert resp.status_code == 400, ext
            assert "media file path" in resp.get_json()["error"], ext
            assert "search box" in resp.get_json()["error"], ext

    def test_non_media_invalid_path_keeps_generic_message(self, client):
        """Non-media extensions retain the generic rejection message."""
        resp = client.get(
            "/api/bif/info?path=/etc/passwd",
            headers=_api_headers(),
        )
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "Invalid or missing BIF file path"

    def test_suspect_frames_detected(self, client, tmp_path):
        """Frames under 500 bytes are flagged as suspect."""
        tiny_frames = [b"\xff\xd8" + bytes(10)] * 3
        bif_dir = tmp_path / "plex" / "test"
        bif_dir.mkdir(parents=True)
        bif_path = _write_test_bif(str(bif_dir / "index-sd.bif"), frames=tiny_frames)

        resp = client.get(f"/api/bif/info?path={bif_path}", headers=_api_headers())
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["suspect_frame_count"] == 3

    def test_allow_list_accepts_legacy_plex_prefix(self, client, tmp_path):
        """Allow-list builder must coalesce legacy ``plex_prefix`` like ownership.py.

        A Plex server entry written via the legacy JS form uses
        ``plex_prefix``-shaped path_mappings. Without the fallback here,
        the BIF viewer's allow-list wouldn't translate the library
        remote_path into a local root, so any sidecar BIF under it
        would be rejected as out-of-allow-list.
        """
        from media_preview_generator.web.settings_manager import get_settings_manager

        # Build a real BIF inside a tmp tree and point a server entry
        # at it via the legacy plex_prefix key.
        media_dir = tmp_path / "media"
        media_dir.mkdir()
        bif_dir = media_dir / "Movies"
        bif_dir.mkdir()
        bif_path = _write_test_bif(str(bif_dir / "test.bif"))

        get_settings_manager().set(
            "media_servers",
            [
                {
                    "id": "emby-legacy",
                    "type": "emby",
                    "name": "Emby Legacy",
                    "enabled": True,
                    "url": "http://emby:8096",
                    "auth": {"api_key": "k"},
                    "libraries": [
                        {"id": "movies", "name": "Movies", "remote_paths": ["/em-media/Movies"], "enabled": True}
                    ],
                    "path_mappings": [{"plex_prefix": "/em-media", "local_prefix": str(media_dir)}],
                    "output": {"adapter": "emby_sidecar", "width": 320, "frame_interval": 5},
                }
            ],
        )

        resp = client.get(f"/api/bif/info?path={bif_path}", headers=_api_headers())
        assert resp.status_code == 200, resp.get_data(as_text=True)


# ---------------------------------------------------------------------------
# BIF frame endpoint tests
# ---------------------------------------------------------------------------


class TestBifFrameEndpoint:
    def test_requires_auth(self, client, sample_bif):
        resp = client.get(f"/api/bif/frame?path={sample_bif}&index=0")
        assert resp.status_code == 401

    def test_returns_jpeg(self, client, sample_bif):
        resp = client.get(f"/api/bif/frame?path={sample_bif}&index=0", headers=_api_headers())
        assert resp.status_code == 200
        assert resp.content_type == "image/jpeg"
        assert resp.data == _FRAME_DATA[0]

    def test_second_frame(self, client, sample_bif):
        resp = client.get(f"/api/bif/frame?path={sample_bif}&index=1", headers=_api_headers())
        assert resp.status_code == 200
        assert resp.data == _FRAME_DATA[1]

    def test_out_of_range(self, client, sample_bif):
        resp = client.get(f"/api/bif/frame?path={sample_bif}&index=999", headers=_api_headers())
        assert resp.status_code == 400

    def test_invalid_index(self, client, sample_bif):
        resp = client.get(f"/api/bif/frame?path={sample_bif}&index=abc", headers=_api_headers())
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# BIF search endpoint tests
# ---------------------------------------------------------------------------


class TestParseSeasonEpisode:
    """Test _parse_season_episode() pattern extraction."""

    def test_full_season_episode(self):
        from media_preview_generator.web.routes.api_bif import _parse_season_episode

        base, season, ep = _parse_season_episode("Rooster Fighter S01E02")
        assert base == "Rooster Fighter"
        assert season == 1
        assert ep == 2

    def test_season_only(self):
        from media_preview_generator.web.routes.api_bif import _parse_season_episode

        base, season, ep = _parse_season_episode("Breaking Bad S03")
        assert base == "Breaking Bad"
        assert season == 3
        assert ep is None

    def test_no_pattern(self):
        from media_preview_generator.web.routes.api_bif import _parse_season_episode

        base, season, ep = _parse_season_episode("Inception")
        assert base == "Inception"
        assert season is None
        assert ep is None

    def test_case_insensitive(self):
        from media_preview_generator.web.routes.api_bif import _parse_season_episode

        base, season, ep = _parse_season_episode("The Wire s02e10")
        assert base == "The Wire"
        assert season == 2
        assert ep == 10

    def test_single_digit(self):
        from media_preview_generator.web.routes.api_bif import _parse_season_episode

        base, season, ep = _parse_season_episode("Show S1E3")
        assert base == "Show"
        assert season == 1
        assert ep == 3

    def test_pattern_only_returns_original_query(self):
        from media_preview_generator.web.routes.api_bif import _parse_season_episode

        base, season, ep = _parse_season_episode("S01E05")
        assert base == "S01E05"
        assert season == 1
        assert ep == 5


class TestBuildDisplayTitle:
    """Test _build_display_title() formatting."""

    def test_episode(self):
        from media_preview_generator.web.routes.api_bif import _build_display_title

        item = {
            "type": "episode",
            "title": "The Caged Bird",
            "grandparentTitle": "Rooster Fighter",
            "parentIndex": 1,
            "index": 2,
        }
        assert _build_display_title(item) == "Rooster Fighter S01E02 - The Caged Bird"

    def test_movie_with_year(self):
        from media_preview_generator.web.routes.api_bif import _build_display_title

        item = {"type": "movie", "title": "Inception", "year": 2010}
        assert _build_display_title(item) == "Inception (2010)"

    def test_movie_without_year(self):
        from media_preview_generator.web.routes.api_bif import _build_display_title

        item = {"type": "movie", "title": "Memento", "year": ""}
        assert _build_display_title(item) == "Memento"


class TestBifSearchEndpoint:
    def test_requires_auth(self, client):
        resp = client.get("/api/bif/search?q=test")
        assert resp.status_code == 401

    def test_short_query_rejected(self, client):
        resp = client.get("/api/bif/search?q=a", headers=_api_headers())
        assert resp.status_code == 400

    def test_no_plex_configured(self, client):
        """Search fails gracefully when Plex is not configured."""
        resp = client.get("/api/bif/search?q=test+movie", headers=_api_headers())
        assert resp.status_code == 400
        assert "Plex not configured" in resp.get_json().get("error", "")


class TestMultiServerBifSearch:
    """``/api/bif/servers/<server_id>/search`` — server-aware enumeration."""

    def test_unknown_server_returns_404(self, client):
        resp = client.get("/api/bif/servers/does-not-exist/search?q=test", headers=_api_headers())
        assert resp.status_code == 404

    def test_short_query_rejected(self, client):
        resp = client.get("/api/bif/servers/anything/search?q=a", headers=_api_headers())
        assert resp.status_code == 400

    def _configure_plex_server(self, app, plex_config_dir: str):
        """Wire up a Plex server entry in settings + return its id."""
        from media_preview_generator.web.settings_manager import get_settings_manager

        with app.app_context():
            sm = get_settings_manager()
            sm.set("plex_config_folder", plex_config_dir)
            sm.set(
                "media_servers",
                [
                    {
                        "id": "plex-1",
                        "type": "plex",
                        "name": "PlexLocal",
                        "enabled": True,
                        "url": "http://plex.test:32400",
                        "auth": {"token": "tok"},
                        "verify_ssl": False,
                        "libraries": [],
                        "path_mappings": [],
                    }
                ],
            )
        return "plex-1"

    def test_plex_search_resolves_bundle_path_eagerly(self, client, app, tmp_path):
        """The 2026-05-12 user bug — pre-fix Plex returned preview_path=""
        and the frontend fell back to media_file (.mkv path), which
        /api/bif/info rejected as "Invalid or missing BIF file path".
        Now the backend resolves the BIF path up-front via /tree.

        Boundary-call assertion (.claude/rules/testing.md): we assert the
        SUT-controlled output (preview_path ending in index-sd.bif), not
        just that some path was returned.
        """
        from media_preview_generator.servers.base import MediaItem
        from media_preview_generator.servers.plex import PlexServer

        bundle_hash = "abcdef1234567890"
        # Pre-create the BIF so preview_exists==True.
        bif_dir = tmp_path / "plex" / "Media" / "localhost" / "a" / "bcdef1234567890.bundle" / "Contents" / "Indexes"
        bif_dir.mkdir(parents=True)
        bif_path = _write_test_bif(str(bif_dir / "index-sd.bif"))

        self._configure_plex_server(app, str(tmp_path / "plex"))

        item = MediaItem(
            id="42",
            library_id="1",
            title="Breaking Bad S01E01",
            remote_path="/data/TV/BB/S01E01.mkv",
        )

        tree_xml = (
            f'<?xml version="1.0"?><MediaContainer>'
            f'<MediaPart hash="{bundle_hash}" file="/data/TV/BB/S01E01.mkv"/>'
            f"</MediaContainer>"
        ).encode()

        def fake_get(url, **_kwargs):
            assert "/library/metadata/42/tree" in url
            resp = MagicMock(content=tree_xml)
            resp.raise_for_status = MagicMock()
            return resp

        with (
            patch.object(PlexServer, "search_items", return_value=[item]),
            patch("requests.get", side_effect=fake_get),
        ):
            resp = client.get("/api/bif/servers/plex-1/search?q=Breaking", headers=_api_headers())

        assert resp.status_code == 200, resp.get_data(as_text=True)
        body = resp.get_json()
        results = body["results"]
        assert len(results) == 1
        assert results[0]["preview_kind"] == "bif"
        assert results[0]["preview_path"].endswith("index-sd.bif"), (
            f"preview_path must be the resolved BIF, not the .mkv. Got {results[0]['preview_path']!r}"
        )
        assert results[0]["preview_path"] == bif_path
        assert results[0]["preview_exists"] is True

    def test_plex_search_one_bad_tree_response_does_not_poison_other_results(self, client, app, tmp_path):
        """One malformed /tree response (or per-item filesystem error)
        must NOT take down the whole search — pre-fix only RequestException
        was caught inside _resolve_bif_for_item, so a Plex 1.32-style
        truncated XML body for one item turned the whole search into
        the empty-fallback path. Now ParseError + OSError are caught
        per-row and the offending row degrades to preview_path="" while
        the others resolve normally.
        """
        from media_preview_generator.servers.base import MediaItem
        from media_preview_generator.servers.plex import PlexServer

        bundle_hash = "abcdef1234567890"
        bif_dir = tmp_path / "plex" / "Media" / "localhost" / "a" / "bcdef1234567890.bundle" / "Contents" / "Indexes"
        bif_dir.mkdir(parents=True)
        good_bif = _write_test_bif(str(bif_dir / "index-sd.bif"))

        self._configure_plex_server(app, str(tmp_path / "plex"))
        items = [
            MediaItem(id="42", library_id="1", title="Healthy", remote_path="/data/x.mkv"),
            MediaItem(id="43", library_id="1", title="Broken XML", remote_path="/data/y.mkv"),
        ]

        good_xml = (
            f'<?xml version="1.0"?><MediaContainer>'
            f'<MediaPart hash="{bundle_hash}" file="/data/x.mkv"/>'
            f"</MediaContainer>"
        ).encode()

        def fake_get(url, **_kwargs):
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            if "/library/metadata/42/tree" in url:
                resp.content = good_xml
            elif "/library/metadata/43/tree" in url:
                # Truncated body — ET.fromstring raises ParseError. Pre-fix
                # this propagated out of the per-item loop and the route's
                # top-level Exception handler turned the WHOLE search into
                # a 502.
                resp.content = b"<MediaContainer><MediaPart hash=trunc"
            else:
                raise AssertionError(f"unexpected url: {url}")
            return resp

        with (
            patch.object(PlexServer, "search_items", return_value=items),
            patch("requests.get", side_effect=fake_get),
        ):
            resp = client.get("/api/bif/servers/plex-1/search?q=anything", headers=_api_headers())

        assert resp.status_code == 200, resp.get_data(as_text=True)
        results = resp.get_json()["results"]
        assert len(results) == 2, "Both rows must be returned even when one /tree response is malformed"
        # Healthy row resolved normally.
        healthy = next(r for r in results if r["title"] == "Healthy")
        assert healthy["preview_path"] == good_bif
        assert healthy["preview_exists"] is True
        # Broken row degraded gracefully to "no preview yet" semantics.
        broken = next(r for r in results if r["title"] == "Broken XML")
        assert broken["preview_path"] == ""
        assert broken["preview_exists"] is False

    def test_disabled_library_with_remote_paths_filters_by_path_prefix(self, client, app, tmp_path):
        """Disabled libraries with configured ``remote_paths`` are
        filtered by path prefix — episode rows match because their
        ``remote_path`` lives under the disabled library's folder.
        Pre-fix this matched on ``library_id`` which Emby/Jellyfin
        episodes don't carry.
        """
        from media_preview_generator.servers.base import MediaItem
        from media_preview_generator.servers.plex import PlexServer
        from media_preview_generator.web.settings_manager import get_settings_manager

        with app.app_context():
            sm = get_settings_manager()
            sm.set("plex_config_folder", str(tmp_path / "plex"))
            sm.set(
                "media_servers",
                [
                    {
                        "id": "plex-1",
                        "type": "plex",
                        "name": "PlexLocal",
                        "enabled": True,
                        "url": "http://plex.test:32400",
                        "auth": {"token": "tok"},
                        "verify_ssl": False,
                        "libraries": [
                            {
                                "id": "lib-tv",
                                "name": "TV",
                                "remote_paths": ["/data/TV"],
                                "enabled": True,
                            },
                            {
                                "id": "lib-blocked",
                                "name": "Blocked",
                                "remote_paths": ["/data/Blocked"],
                                "enabled": False,
                            },
                        ],
                        "path_mappings": [],
                    }
                ],
            )

        items = [
            MediaItem(id="1", library_id="", title="Allowed", remote_path="/data/TV/x.mkv"),
            MediaItem(id="2", library_id="", title="Blocked", remote_path="/data/Blocked/y.mkv"),
        ]

        with (
            patch.object(PlexServer, "search_items", return_value=items),
            patch(
                "requests.get",
                side_effect=lambda *a, **kw: MagicMock(content=b"<MediaContainer/>", raise_for_status=MagicMock()),
            ),
        ):
            resp = client.get("/api/bif/servers/plex-1/search?q=any", headers=_api_headers())

        assert resp.status_code == 200
        titles = [r["title"] for r in resp.get_json()["results"]]
        assert titles == ["Allowed"], f"Expected only Allowed (Blocked filtered by path-prefix), got {titles}"

    def test_disabled_library_with_empty_remote_paths_falls_back_to_library_id(self, client, app, tmp_path):
        """Disabled libraries with NO ``remote_paths`` (user hasn't
        configured local mounts yet) fall back to ``library_id``
        matching. Pin the matrix gap explicitly: items in such a
        library MUST still be filtered out — silently leaking them
        was the architecture-review MED finding.
        """
        from media_preview_generator.servers.base import MediaItem
        from media_preview_generator.servers.plex import PlexServer
        from media_preview_generator.web.settings_manager import get_settings_manager

        with app.app_context():
            sm = get_settings_manager()
            sm.set("plex_config_folder", str(tmp_path / "plex"))
            sm.set(
                "media_servers",
                [
                    {
                        "id": "plex-1",
                        "type": "plex",
                        "name": "PlexLocal",
                        "enabled": True,
                        "url": "http://plex.test:32400",
                        "auth": {"token": "tok"},
                        "verify_ssl": False,
                        "libraries": [
                            {
                                "id": "lib-blocked-no-paths",
                                "name": "Blocked No Paths",
                                "remote_paths": [],
                                "enabled": False,
                            }
                        ],
                        "path_mappings": [],
                    }
                ],
            )

        items = [
            MediaItem(id="1", library_id="lib-other", title="Allowed", remote_path="/x.mkv"),
            MediaItem(id="2", library_id="lib-blocked-no-paths", title="Blocked", remote_path="/y.mkv"),
        ]

        with (
            patch.object(PlexServer, "search_items", return_value=items),
            patch(
                "requests.get",
                side_effect=lambda *a, **kw: MagicMock(content=b"<MediaContainer/>", raise_for_status=MagicMock()),
            ),
        ):
            resp = client.get("/api/bif/servers/plex-1/search?q=any", headers=_api_headers())

        assert resp.status_code == 200
        titles = [r["title"] for r in resp.get_json()["results"]]
        assert "Blocked" not in titles, (
            f"Item from disabled library without remote_paths leaked through (the MED). Titles: {titles}"
        )
        assert "Allowed" in titles

    def test_plex_search_unanalyzed_item_marks_no_preview(self, client, app, tmp_path):
        """When Plex hasn't analyzed the item yet, /tree returns no
        MediaPart hash → preview_path stays empty + a `note` explains
        why. The frontend must NOT mark the row as clickable.
        """
        from media_preview_generator.servers.base import MediaItem
        from media_preview_generator.servers.plex import PlexServer

        self._configure_plex_server(app, str(tmp_path / "plex"))
        item = MediaItem(id="999", library_id="1", title="Unanalyzed", remote_path="/data/x.mkv")

        tree_xml = b"<?xml version='1.0'?><MediaContainer></MediaContainer>"

        def fake_get(url, **_kwargs):
            resp = MagicMock(content=tree_xml)
            resp.raise_for_status = MagicMock()
            return resp

        with (
            patch.object(PlexServer, "search_items", return_value=[item]),
            patch("requests.get", side_effect=fake_get),
        ):
            resp = client.get("/api/bif/servers/plex-1/search?q=Unanalyzed", headers=_api_headers())

        assert resp.status_code == 200
        results = resp.get_json()["results"]
        assert len(results) == 1
        assert results[0]["preview_path"] == ""
        assert results[0]["preview_exists"] is False
        assert "note" in results[0] and "analyzed" in results[0]["note"].lower()

    def test_plex_multi_version_yields_one_row_per_mediapart(self, client, app, tmp_path):
        """Issue #231 (RedRubble, 2026-05-11): items with multiple versions
        (4K + 1080p of the same movie) collapsed to a single Preview Inspector
        row because ``_resolve_bif_for_item`` ``break``ed after the first
        MediaPart. Now each MediaPart becomes its own row with its own
        ``preview_path`` and ``media_file``.

        Boundary-call assertion (.claude/rules/testing.md): we verify
        per-row contracts (distinct preview_path, distinct media_file)
        rather than just ``len(results) == 2`` — the count alone wouldn't
        catch a regression that returned 2 rows with the same path.
        """
        from media_preview_generator.servers.base import MediaItem
        from media_preview_generator.servers.plex import PlexServer

        hash_4k = "aaaaaaaaa1234567"
        hash_1080 = "bbbbbbbbb1234567"
        bif_4k_dir = tmp_path / "plex" / "Media" / "localhost" / "a" / "aaaaaaaa1234567.bundle" / "Contents" / "Indexes"
        bif_1080_dir = (
            tmp_path / "plex" / "Media" / "localhost" / "b" / "bbbbbbbb1234567.bundle" / "Contents" / "Indexes"
        )
        bif_4k_dir.mkdir(parents=True)
        bif_1080_dir.mkdir(parents=True)
        bif_4k = _write_test_bif(str(bif_4k_dir / "index-sd.bif"))
        bif_1080 = _write_test_bif(str(bif_1080_dir / "index-sd.bif"))

        self._configure_plex_server(app, str(tmp_path / "plex"))

        item = MediaItem(
            id="42",
            library_id="1",
            title="Avengers Infinity War",
            remote_path="/data/movies4k/Avengers (2018)/Avengers.2160p.mkv",
        )

        tree_xml = (
            f'<?xml version="1.0"?><MediaContainer>'
            f'<MediaPart hash="{hash_4k}" file="/data/movies4k/Avengers (2018)/Avengers.2160p.mkv"/>'
            f'<MediaPart hash="{hash_1080}" file="/data/movies1080p/Avengers (2018)/Avengers.1080p.mkv"/>'
            f"</MediaContainer>"
        ).encode()

        def fake_get(url, **_kwargs):
            assert "/library/metadata/42/tree" in url
            resp = MagicMock(content=tree_xml)
            resp.raise_for_status = MagicMock()
            return resp

        with (
            patch.object(PlexServer, "search_items", return_value=[item]),
            patch("requests.get", side_effect=fake_get),
        ):
            resp = client.get("/api/bif/servers/plex-1/search?q=Avengers", headers=_api_headers())

        assert resp.status_code == 200, resp.get_data(as_text=True)
        results = resp.get_json()["results"]
        assert len(results) == 2, f"Expected one row per MediaPart, got: {results}"

        # Both rows share the same title and item_id — they're versions of
        # the same Plex item.
        assert {r["title"] for r in results} == {"Avengers Infinity War"}
        assert {r["item_id"] for r in results} == {"42"}

        # But preview_path and media_file are per-part.
        previews = {r["preview_path"] for r in results}
        media_files = {r["media_file"] for r in results}
        assert previews == {bif_4k, bif_1080}, f"Each version must have its own BIF path. Got: {previews}"
        assert media_files == {
            "/data/movies4k/Avengers (2018)/Avengers.2160p.mkv",
            "/data/movies1080p/Avengers (2018)/Avengers.1080p.mkv",
        }, f"Each version's media_file must reflect its own MediaPart.file. Got: {media_files}"
        for row in results:
            assert row["preview_kind"] == "bif"
            assert row["preview_exists"] is True

    def test_plex_multi_version_applies_per_part_path_mapping(self, client, app, tmp_path):
        """Per-part path mapping: a 4K mount and a 1080p mount typically
        live under different ``remote_prefix`` → ``local_prefix`` pairs.
        The Preview Inspector row's ``media_file`` must reflect the
        mapping for THAT part, not for the item-level remote_path.

        Without this, multi-mount setups would show the correct BIF but
        the wrong file path on one of the rows — confusing for users
        comparing version sizes / encodes.
        """
        from media_preview_generator.servers.base import MediaItem
        from media_preview_generator.servers.plex import PlexServer
        from media_preview_generator.web.settings_manager import get_settings_manager

        hash_a = "aaaaaaaaa1234567"
        hash_b = "bbbbbbbbb1234567"
        bif_a_dir = tmp_path / "plex" / "Media" / "localhost" / "a" / "aaaaaaaa1234567.bundle" / "Contents" / "Indexes"
        bif_b_dir = tmp_path / "plex" / "Media" / "localhost" / "b" / "bbbbbbbb1234567.bundle" / "Contents" / "Indexes"
        bif_a_dir.mkdir(parents=True)
        bif_b_dir.mkdir(parents=True)
        _write_test_bif(str(bif_a_dir / "index-sd.bif"))
        _write_test_bif(str(bif_b_dir / "index-sd.bif"))

        with app.app_context():
            sm = get_settings_manager()
            sm.set("plex_config_folder", str(tmp_path / "plex"))
            sm.set(
                "media_servers",
                [
                    {
                        "id": "plex-1",
                        "type": "plex",
                        "name": "PlexLocal",
                        "enabled": True,
                        "url": "http://plex.test:32400",
                        "auth": {"token": "tok"},
                        "verify_ssl": False,
                        "libraries": [],
                        "path_mappings": [
                            {"remote_prefix": "/remote/4k", "local_prefix": "/local/4k"},
                            {"remote_prefix": "/remote/1080p", "local_prefix": "/local/1080p"},
                        ],
                    }
                ],
            )

        item = MediaItem(
            id="42",
            library_id="1",
            title="Dune",
            remote_path="/remote/4k/Dune/Dune.2160p.mkv",
        )

        tree_xml = (
            f'<?xml version="1.0"?><MediaContainer>'
            f'<MediaPart hash="{hash_a}" file="/remote/4k/Dune/Dune.2160p.mkv"/>'
            f'<MediaPart hash="{hash_b}" file="/remote/1080p/Dune/Dune.1080p.mkv"/>'
            f"</MediaContainer>"
        ).encode()

        def fake_get(url, **_kwargs):
            resp = MagicMock(content=tree_xml)
            resp.raise_for_status = MagicMock()
            return resp

        with (
            patch.object(PlexServer, "search_items", return_value=[item]),
            patch("requests.get", side_effect=fake_get),
        ):
            resp = client.get("/api/bif/servers/plex-1/search?q=Dune", headers=_api_headers())

        assert resp.status_code == 200, resp.get_data(as_text=True)
        results = resp.get_json()["results"]
        assert len(results) == 2

        media_files = {r["media_file"] for r in results}
        assert media_files == {
            "/local/4k/Dune/Dune.2160p.mkv",
            "/local/1080p/Dune/Dune.1080p.mkv",
        }, f"Per-part mapping not applied. Got: {media_files}"

    def test_plex_multi_version_part_without_file_attr_yields_empty_media_file(self, client, app, tmp_path):
        """A MediaPart with a valid ``hash`` but no ``file=`` attribute
        must yield an empty ``media_file`` for THAT row, not the
        item-level path. Pre-fix the no-file row would borrow another
        part's ``canonical_local`` — on a 2-part response this means two
        rows with the same ``media_file`` pointing at different BIFs,
        which is the inverse of the bug #231 fix and would silently
        mislead any user comparing versions.
        """
        from media_preview_generator.servers.base import MediaItem
        from media_preview_generator.servers.plex import PlexServer

        hash_a = "aaaaaaaaa1234567"
        hash_b = "bbbbbbbbb1234567"
        bif_a_dir = tmp_path / "plex" / "Media" / "localhost" / "a" / "aaaaaaaa1234567.bundle" / "Contents" / "Indexes"
        bif_b_dir = tmp_path / "plex" / "Media" / "localhost" / "b" / "bbbbbbbb1234567.bundle" / "Contents" / "Indexes"
        bif_a_dir.mkdir(parents=True)
        bif_b_dir.mkdir(parents=True)
        bif_a = _write_test_bif(str(bif_a_dir / "index-sd.bif"))
        bif_b = _write_test_bif(str(bif_b_dir / "index-sd.bif"))

        self._configure_plex_server(app, str(tmp_path / "plex"))

        item = MediaItem(
            id="42",
            library_id="1",
            title="Mixed",
            remote_path="/data/movies/Mixed.mkv",
        )

        # Second MediaPart has hash but no file= attribute.
        tree_xml = (
            f'<?xml version="1.0"?><MediaContainer>'
            f'<MediaPart hash="{hash_a}" file="/data/movies/Mixed.2160p.mkv"/>'
            f'<MediaPart hash="{hash_b}"/>'
            f"</MediaContainer>"
        ).encode()

        def fake_get(url, **_kwargs):
            resp = MagicMock(content=tree_xml)
            resp.raise_for_status = MagicMock()
            return resp

        with (
            patch.object(PlexServer, "search_items", return_value=[item]),
            patch("requests.get", side_effect=fake_get),
        ):
            resp = client.get("/api/bif/servers/plex-1/search?q=Mixed", headers=_api_headers())

        assert resp.status_code == 200, resp.get_data(as_text=True)
        results = resp.get_json()["results"]
        assert len(results) == 2

        # Pair rows by their preview_path so we can assert per-part contracts.
        by_bif = {r["preview_path"]: r for r in results}
        assert set(by_bif) == {bif_a, bif_b}
        assert by_bif[bif_a]["media_file"] == "/data/movies/Mixed.2160p.mkv"
        assert by_bif[bif_b]["media_file"] == "", (
            "MediaPart without file= must yield empty media_file, "
            f"not the item-level path. Got: {by_bif[bif_b]['media_file']!r}"
        )


class TestBifSearchShowHubUsesRatingKey:
    """Hindsight for commit ``0a74c5b`` (use ``ratingKey`` not ``key`` for
    show-hub episode lookup).

    HINDSIGHT_90_DAYS row #79: pre-fix the show-hub branch built the
    ``/allLeaves`` URL from ``show_item.get("key")`` which returns
    ``/library/metadata/<id>/children`` for show-type items. That
    yielded ``/library/metadata/<id>/children/allLeaves`` — a malformed
    URL Plex returns 404 for, so episodes never appeared in BIF search
    results for TV shows. The fix uses ``ratingKey`` (a bare integer)
    and constructs ``/library/metadata/<id>/allLeaves`` cleanly.

    Existing test_bif_viewer fixtures masked the bug because they used
    the movie hub branch, where ``key`` happens to be the bare
    metadata path. Category C / zero coverage.
    """

    def test_show_hub_constructs_show_key_from_rating_key(self, client, app):
        """The /allLeaves URL must be built from ``ratingKey``, not
        ``key``. We assert the URL Plex receives directly — the most
        precise pin point for this regression.
        """
        from media_preview_generator.web.settings_manager import get_settings_manager

        with app.app_context():
            sm = get_settings_manager()
            sm.set("plex_url", "http://plex.test:32400")
            sm.set("plex_token", "test-plex-token")
            sm.set("plex_verify_ssl", False)

        # Show-hub payload: ``key`` is the children-suffixed path that
        # the bug used; ``ratingKey`` is the bare integer the fix uses.
        # If a regression goes back to ``key``, the URL it requests
        # becomes ``/library/metadata/9999/children/allLeaves`` (404).
        hub_payload = {
            "MediaContainer": {
                "Hub": [
                    {
                        "type": "show",
                        "Metadata": [
                            {
                                "ratingKey": "9999",
                                "key": "/library/metadata/9999/children",
                                "title": "Breaking Bad",
                                "type": "show",
                            }
                        ],
                    }
                ]
            }
        }

        # /allLeaves response — one episode with all the metadata the
        # _item_to_result/_resolve_bif_for_item path needs.
        leaves_payload = {
            "MediaContainer": {
                "Metadata": [
                    {
                        "ratingKey": "10001",
                        "key": "/library/metadata/10001",
                        "type": "episode",
                        "title": "Pilot",
                        "grandparentTitle": "Breaking Bad",
                        "parentIndex": 1,
                        "index": 1,
                        "Media": [{"Part": [{"file": "/tv/BB/S01E01.mkv"}]}],
                    }
                ]
            }
        }

        # Tree response — empty MediaPart list so _resolve_bif_for_item
        # returns ('', False, {}) without touching the filesystem.
        tree_xml = b"<?xml version='1.0'?><MediaContainer></MediaContainer>"
        tree_resp = MagicMock(content=tree_xml)
        tree_resp.raise_for_status = MagicMock()

        captured_urls: list[str] = []

        def fake_get(url, **kwargs):
            captured_urls.append(url)
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            if "/hubs/search" in url:
                resp.json.return_value = hub_payload
            elif "/allLeaves" in url:
                resp.json.return_value = leaves_payload
            elif "/tree" in url:
                resp.content = tree_xml
            else:  # pragma: no cover - guard
                raise AssertionError(f"Unexpected URL: {url}")
            return resp

        with patch("requests.get", side_effect=fake_get):
            resp = client.get("/api/bif/search?q=Breaking", headers=_api_headers())

        assert resp.status_code == 200, resp.get_data(as_text=True)
        results = resp.get_json()["results"]
        assert len(results) == 1, "Expected the one mocked episode in results"
        assert results[0]["title"] == "Breaking Bad S01E01 - Pilot"

        # Pin the EXACT /allLeaves URL: must be built from ratingKey,
        # not from the children-suffixed `key`. This is the regression
        # marker — pre-0a74c5b this URL was
        # ``http://plex.test:32400/library/metadata/9999/children/allLeaves``
        # and Plex returned 404, dropping the episode from results.
        all_leaves_calls = [u for u in captured_urls if "/allLeaves" in u]
        assert all_leaves_calls == ["http://plex.test:32400/library/metadata/9999/allLeaves"], (
            f"show_key must be derived from ratingKey, producing a clean "
            f"/library/metadata/<id>/allLeaves URL. Got: {all_leaves_calls!r}"
        )

    def test_show_hub_skips_items_with_no_rating_key(self, client, app):
        """A show-hub Metadata entry that lacks ``ratingKey`` must be
        skipped, NOT silently fall back to ``key`` (which would
        re-introduce the 0a74c5b bug). The production code uses
        ``if not rating_key: continue`` — this test pins that behaviour.
        """
        from media_preview_generator.web.settings_manager import get_settings_manager

        with app.app_context():
            sm = get_settings_manager()
            sm.set("plex_url", "http://plex.test:32400")
            sm.set("plex_token", "test-plex-token")
            sm.set("plex_verify_ssl", False)

        hub_payload = {
            "MediaContainer": {
                "Hub": [
                    {
                        "type": "show",
                        "Metadata": [
                            # Has key but NO ratingKey — must be skipped.
                            {
                                "key": "/library/metadata/9999/children",
                                "title": "No Rating Key Show",
                                "type": "show",
                            }
                        ],
                    }
                ]
            }
        }

        captured_urls: list[str] = []

        def fake_get(url, **kwargs):
            captured_urls.append(url)
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            if "/hubs/search" in url:
                resp.json.return_value = hub_payload
            else:  # pragma: no cover - guard against fallback
                raise AssertionError(f"No /allLeaves call should fire when ratingKey missing; got {url}")
            return resp

        with patch("requests.get", side_effect=fake_get):
            resp = client.get("/api/bif/search?q=NoKey", headers=_api_headers())

        assert resp.status_code == 200
        assert resp.get_json()["results"] == []
        assert all("/allLeaves" not in u for u in captured_urls), (
            "Items with no ratingKey must be skipped; no /allLeaves call expected"
        )


class TestMultiServerTrickplayInfo:
    """``/api/bif/trickplay/info`` — Jellyfin manifest parser."""

    def test_unknown_server_returns_404(self, client):
        resp = client.get(
            "/api/bif/trickplay/info?server_id=missing&path=/foo.json",
            headers=_api_headers(),
        )
        assert resp.status_code == 404

    def test_invalid_path_returns_400(self, client, tmp_path):
        # Seed a Jellyfin server pointing at tmp_path; manifest path
        # doesn't exist yet.
        from media_preview_generator.web.settings_manager import get_settings_manager

        get_settings_manager().set(
            "media_servers",
            [
                {
                    "id": "jf-test",
                    "type": "jellyfin",
                    "name": "Test JF",
                    "enabled": True,
                    "url": "http://x:8096",
                    "auth": {},
                    "libraries": [],
                    "path_mappings": [{"remote_prefix": "/jf", "local_prefix": str(tmp_path)}],
                    "output": {"adapter": "jellyfin_trickplay", "width": 320, "frame_interval": 5},
                }
            ],
        )
        resp = client.get(
            "/api/bif/trickplay/info?server_id=jf-test&path=/etc/passwd",
            headers=_api_headers(),
        )
        assert resp.status_code == 400


class TestMultiServerTrickplayFrame:
    """``/api/bif/trickplay/frame`` — tile-sheet slicing."""

    def test_unknown_server_returns_404(self, client):
        resp = client.get(
            "/api/bif/trickplay/frame?server_id=missing&sheets_dir=/tmp/x&index=0",
            headers=_api_headers(),
        )
        assert resp.status_code == 404

    def test_returns_jpeg_slice_from_real_sheet(self, client, tmp_path):
        """End-to-end slice: build a known tile sheet, slice, verify pixel values."""
        from PIL import Image

        from media_preview_generator.web.settings_manager import get_settings_manager

        # Build a 2x2 grid of 4 distinct-coloured 50x50 tiles → 100x100 sheet.
        sheets_dir = tmp_path / "trickplay" / "Movie-320"
        sheets_dir.mkdir(parents=True)
        sheet = Image.new("RGB", (100, 100), (0, 0, 0))
        colours = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0)]
        for idx, c in enumerate(colours):
            row = idx // 2
            col = idx % 2
            tile = Image.new("RGB", (50, 50), c)
            sheet.paste(tile, (col * 50, row * 50))
        sheet.save(sheets_dir / "0.jpg", "JPEG", quality=95)

        get_settings_manager().set(
            "media_servers",
            [
                {
                    "id": "jf-tiles",
                    "type": "jellyfin",
                    "name": "JF Tiles",
                    "enabled": True,
                    "url": "http://x:8096",
                    "auth": {},
                    "libraries": [],
                    "path_mappings": [{"remote_prefix": "/jf", "local_prefix": str(tmp_path)}],
                    "output": {"adapter": "jellyfin_trickplay", "width": 320, "frame_interval": 5},
                }
            ],
        )

        resp = client.get(
            f"/api/bif/trickplay/frame?server_id=jf-tiles&sheets_dir={sheets_dir}&index=2&tile_width=2&tile_height=2",
            headers=_api_headers(),
        )
        assert resp.status_code == 200, resp.get_data(as_text=True)
        assert resp.mimetype == "image/jpeg"
        assert resp.data[:3] == b"\xff\xd8\xff"  # JPEG SOI

        # Decode + check the dominant colour matches tile #2 (blue).
        from io import BytesIO

        decoded = Image.open(BytesIO(resp.data)).convert("RGB")
        # Sample the centre pixel — JPEG quantisation may shift colours
        # slightly; assert blue dominates (B > R + G).
        r, g, b = decoded.getpixel((25, 25))
        assert b > r and b > g, f"Expected blue tile, got RGB=({r},{g},{b})"

    def test_path_traversal_rejected(self, client, tmp_path):
        from media_preview_generator.web.settings_manager import get_settings_manager

        get_settings_manager().set(
            "media_servers",
            [
                {
                    "id": "jf-trav",
                    "type": "jellyfin",
                    "name": "JF",
                    "enabled": True,
                    "url": "http://x:8096",
                    "auth": {},
                    "libraries": [],
                    "path_mappings": [{"remote_prefix": "/jf", "local_prefix": str(tmp_path)}],
                    "output": {"adapter": "jellyfin_trickplay", "width": 320, "frame_interval": 5},
                }
            ],
        )
        resp = client.get(
            "/api/bif/trickplay/frame?server_id=jf-trav&sheets_dir=/etc/&index=0",
            headers=_api_headers(),
        )
        assert resp.status_code == 403
