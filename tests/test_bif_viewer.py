"""Tests for BIF reader module and BIF viewer API endpoints."""

import array
import json
import os
import struct
from unittest.mock import patch

import pytest

from media_preview_generator.bif_reader import (
    read_bif_frame,
    read_bif_metadata,
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
        resp = client.get("/bif-viewer", follow_redirects=False)
        assert resp.status_code in (302, 308)

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
