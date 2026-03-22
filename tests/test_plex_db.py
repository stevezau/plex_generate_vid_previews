"""Tests for the Plex SQLite database access layer.

Validates safe read/write access to marker data in the Plex database,
connection discipline (busy_timeout, immediate close), and environment
safety checks.
"""

import os
import sqlite3
from unittest.mock import patch

import pytest

from plex_generate_previews.plex_db import (
    _EXTRA_DATA_INTRO,
    _MARKER_TAG_TYPE,
    check_db_write_safety,
    delete_markers,
    get_existing_markers,
    get_marker_tag_id,
    get_plex_db_path,
    write_marker,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _create_mock_plex_db(db_path: str, marker_tag_id: int = 30569) -> None:
    """Create a minimal Plex database with tags and taggings tables."""
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tags (
            id INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
            metadata_item_id INTEGER,
            tag VARCHAR(255),
            tag_type INTEGER,
            user_thumb_url VARCHAR(255),
            user_art_url VARCHAR(255),
            user_music_url VARCHAR(255),
            created_at INTEGER,
            updated_at INTEGER,
            tag_value INTEGER,
            extra_data VARCHAR(255),
            key VARCHAR(255),
            parent_id INTEGER
        )
    """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS taggings (
            id INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
            metadata_item_id INTEGER,
            tag_id INTEGER,
            "index" INTEGER,
            text VARCHAR(255),
            time_offset INTEGER,
            end_time_offset INTEGER,
            thumb_url VARCHAR(255),
            created_at INTEGER,
            extra_data VARCHAR(255)
        )
    """
    )
    # Insert the marker tag (tag_type=12)
    conn.execute(
        "INSERT INTO tags (id, tag_type) VALUES (?, ?)",
        (marker_tag_id, _MARKER_TAG_TYPE),
    )
    conn.commit()
    conn.close()


@pytest.fixture()
def plex_db(tmp_path):
    """Create a mock Plex database and return its path."""
    db_path = str(tmp_path / "com.plexapp.plugins.library.db")
    _create_mock_plex_db(db_path)
    return db_path


@pytest.fixture()
def plex_config_folder(tmp_path):
    """Create a mock Plex config folder structure with a database."""
    db_dir = tmp_path / "Plug-in Support" / "Databases"
    db_dir.mkdir(parents=True)
    db_path = db_dir / "com.plexapp.plugins.library.db"
    _create_mock_plex_db(str(db_path))
    return str(tmp_path)


# ---------------------------------------------------------------------------
# get_plex_db_path
# ---------------------------------------------------------------------------


class TestGetPlexDbPath:
    def test_finds_database_direct_mount(self, plex_config_folder):
        result = get_plex_db_path(plex_config_folder)
        assert result.endswith("com.plexapp.plugins.library.db")
        assert os.path.isfile(result)

    def test_finds_database_nested_layout(self, tmp_path):
        """Find DB in Library/Application Support/Plex Media Server layout."""
        nested = (
            tmp_path
            / "Library"
            / "Application Support"
            / "Plex Media Server"
            / "Plug-in Support"
            / "Databases"
        )
        nested.mkdir(parents=True)
        db_path = nested / "com.plexapp.plugins.library.db"
        _create_mock_plex_db(str(db_path))

        result = get_plex_db_path(str(tmp_path))
        assert os.path.isfile(result)

    def test_raises_when_not_found(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="Plex database not found"):
            get_plex_db_path(str(tmp_path))


# ---------------------------------------------------------------------------
# check_db_write_safety
# ---------------------------------------------------------------------------


class TestCheckDbWriteSafety:
    def test_safe_on_local_writable_file(self, plex_db):
        safe, reason = check_db_write_safety(plex_db)
        assert safe is True
        assert reason == ""

    def test_unsafe_when_file_missing(self, tmp_path):
        safe, reason = check_db_write_safety(str(tmp_path / "nonexistent.db"))
        assert safe is False
        assert "not found" in reason

    def test_unsafe_when_not_writable(self, plex_db):
        os.chmod(plex_db, 0o444)
        try:
            safe, reason = check_db_write_safety(plex_db)
            assert safe is False
            assert "not writable" in reason
        finally:
            os.chmod(plex_db, 0o644)

    def test_unsafe_when_flock_fails(self, plex_db):
        """Simulate Docker-on-Windows by making flock raise OSError."""
        import fcntl as real_fcntl

        def flock_raises(*args, **kwargs):
            raise OSError("Operation not supported")

        with patch.object(real_fcntl, "flock", side_effect=flock_raises):
            safe, reason = check_db_write_safety(plex_db)
            assert safe is False
            assert "locking" in reason.lower()


# ---------------------------------------------------------------------------
# get_marker_tag_id
# ---------------------------------------------------------------------------


class TestGetMarkerTagId:
    def test_returns_correct_tag_id(self, plex_db):
        tag_id = get_marker_tag_id(plex_db)
        assert tag_id == 30569

    def test_raises_when_no_marker_tag(self, tmp_path):
        db_path = str(tmp_path / "empty.db")
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE tags (id INTEGER PRIMARY KEY, tag_type INTEGER)")
        conn.commit()
        conn.close()

        with pytest.raises(RuntimeError, match="No marker tag"):
            get_marker_tag_id(db_path)


# ---------------------------------------------------------------------------
# write_marker
# ---------------------------------------------------------------------------


class TestWriteMarker:
    def test_write_credits_marker(self, plex_db):
        success = write_marker(
            plex_db,
            metadata_item_id=12345,
            tag_id=30569,
            marker_type="credits",
            start_ms=1200000,
            end_ms=1350000,
            is_final=True,
        )
        assert success is True

        # Verify the written data
        conn = sqlite3.connect(plex_db)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM taggings WHERE metadata_item_id = 12345"
        ).fetchone()
        conn.close()

        assert row["text"] == "credits"
        assert row["time_offset"] == 1200000
        assert row["end_time_offset"] == 1350000
        assert row["tag_id"] == 30569
        assert row["index"] == 0
        assert "pv:final" in row["extra_data"]
        assert row["created_at"] > 0

    def test_write_intro_marker(self, plex_db):
        success = write_marker(
            plex_db,
            metadata_item_id=12345,
            tag_id=30569,
            marker_type="intro",
            start_ms=5000,
            end_ms=65000,
        )
        assert success is True

        conn = sqlite3.connect(plex_db)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM taggings WHERE metadata_item_id = 12345"
        ).fetchone()
        conn.close()

        assert row["text"] == "intro"
        assert row["extra_data"] == _EXTRA_DATA_INTRO

    def test_auto_increments_index(self, plex_db):
        """Multiple markers on the same item get sequential indices."""
        write_marker(plex_db, 100, 30569, "credits", 50000, 60000)
        write_marker(plex_db, 100, 30569, "credits", 60000, 70000)

        conn = sqlite3.connect(plex_db)
        rows = conn.execute(
            'SELECT "index" FROM taggings WHERE metadata_item_id = 100 ORDER BY "index"'
        ).fetchall()
        conn.close()

        assert [r[0] for r in rows] == [0, 1]

    def test_rejects_invalid_marker_type(self, plex_db):
        with pytest.raises(ValueError, match="marker_type"):
            write_marker(plex_db, 100, 30569, "commercial", 0, 1000)

    def test_credits_non_final_extra_data(self, plex_db):
        write_marker(plex_db, 100, 30569, "credits", 50000, 60000, is_final=False)

        conn = sqlite3.connect(plex_db)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT extra_data FROM taggings WHERE metadata_item_id = 100"
        ).fetchone()
        conn.close()

        assert "pv:final" not in row["extra_data"]
        assert "pv:version" in row["extra_data"]


# ---------------------------------------------------------------------------
# delete_markers
# ---------------------------------------------------------------------------


class TestDeleteMarkers:
    def test_deletes_markers_by_type(self, plex_db):
        write_marker(plex_db, 100, 30569, "credits", 50000, 60000)
        write_marker(plex_db, 100, 30569, "intro", 5000, 35000)

        deleted = delete_markers(plex_db, 100, 30569, "credits")
        assert deleted == 1

        # Intro should still exist
        markers = get_existing_markers(plex_db, 100, 30569)
        assert len(markers) == 1
        assert markers[0]["type"] == "intro"

    def test_returns_zero_when_nothing_to_delete(self, plex_db):
        deleted = delete_markers(plex_db, 999, 30569, "credits")
        assert deleted == 0


# ---------------------------------------------------------------------------
# get_existing_markers
# ---------------------------------------------------------------------------


class TestGetExistingMarkers:
    def test_returns_markers_ordered_by_index(self, plex_db):
        write_marker(plex_db, 100, 30569, "intro", 5000, 35000)
        write_marker(plex_db, 100, 30569, "credits", 1200000, 1350000)

        markers = get_existing_markers(plex_db, 100, 30569)
        assert len(markers) == 2
        assert markers[0]["type"] == "intro"
        assert markers[0]["start_ms"] == 5000
        assert markers[1]["type"] == "credits"
        assert markers[1]["start_ms"] == 1200000

    def test_returns_empty_for_no_markers(self, plex_db):
        markers = get_existing_markers(plex_db, 999, 30569)
        assert markers == []


# ---------------------------------------------------------------------------
# Connection discipline
# ---------------------------------------------------------------------------


class TestConnectionDiscipline:
    def test_busy_timeout_is_set(self, plex_db):
        """Verify PRAGMA busy_timeout is configured on connections."""
        from plex_generate_previews.plex_db import _connect

        conn = _connect(plex_db)
        try:
            row = conn.execute("PRAGMA busy_timeout").fetchone()
            assert row[0] == 5000
        finally:
            conn.close()

    def test_no_insert_into_tags_table(self, plex_db):
        """Verify that get_marker_tag_id only reads, never writes to tags."""
        conn = sqlite3.connect(plex_db)
        count_before = conn.execute("SELECT COUNT(*) FROM tags").fetchone()[0]
        conn.close()

        get_marker_tag_id(plex_db)

        conn = sqlite3.connect(plex_db)
        count_after = conn.execute("SELECT COUNT(*) FROM tags").fetchone()[0]
        conn.close()

        assert count_after == count_before
