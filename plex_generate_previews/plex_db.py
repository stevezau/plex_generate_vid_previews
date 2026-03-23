"""Plex SQLite database access layer for marker management.

Provides safe read/write access to the Plex Media Server SQLite database
for creating intro and credits markers.  All writes target the ``taggings``
table only — the ``tags`` table has FTS4 triggers with a custom ICU
tokenizer that would fail with standard SQLite, so it is read-only.

Safety guarantees:
- ``PRAGMA busy_timeout = 5000`` on every connection
- Single INSERT + COMMIT per write call (minimal lock time)
- Connections closed immediately after each operation
- Docker-on-Windows volume mounts detected and rejected (broken POSIX locking)
- No connection pooling — each call opens and closes its own connection
"""

import json
import os
import sqlite3
import time
from typing import Dict, List, Tuple

from loguru import logger

# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

_PLEX_DB_RELATIVE = os.path.join(
    "Plug-in Support", "Databases", "com.plexapp.plugins.library.db"
)

# Plex config folder may contain a nested "Library/Application Support/
# Plex Media Server" subtree (host-mounted) or point directly at the
# Plex data root (container mount).
_PLEX_SUBTREES = [
    "",  # direct mount (e.g. /plex)
    os.path.join("Library", "Application Support", "Plex Media Server"),
]


def get_plex_db_path(plex_config_folder: str) -> str:
    """Resolve the path to the Plex library database.

    Tries common Plex directory layouts to find
    ``com.plexapp.plugins.library.db``.

    Args:
        plex_config_folder: Value of ``PLEX_CONFIG_FOLDER`` / plex_config_folder
            setting.

    Returns:
        Absolute path to the database file.

    Raises:
        FileNotFoundError: If the database cannot be found.

    """
    for subtree in _PLEX_SUBTREES:
        candidate = os.path.join(plex_config_folder, subtree, _PLEX_DB_RELATIVE)
        if os.path.isfile(candidate):
            return os.path.realpath(candidate)

    raise FileNotFoundError(
        f"Plex database not found under {plex_config_folder}. "
        f"Checked paths: "
        + ", ".join(
            os.path.join(plex_config_folder, s, _PLEX_DB_RELATIVE)
            for s in _PLEX_SUBTREES
        )
    )


# ---------------------------------------------------------------------------
# Safety checks
# ---------------------------------------------------------------------------


def check_db_write_safety(db_path: str) -> Tuple[bool, str]:
    """Check whether it is safe to write to the Plex database.

    Detects:
    - Missing or unwritable database file
    - Docker-on-Windows volume mounts (broken POSIX advisory locking)

    Args:
        db_path: Absolute path to the Plex database.

    Returns:
        ``(True, "")`` if safe, ``(False, reason)`` otherwise.

    """
    if not os.path.isfile(db_path):
        return False, f"Database file not found: {db_path}"

    if not os.access(db_path, os.W_OK):
        return False, f"Database file is not writable: {db_path}"

    # Detect Docker-on-Windows: POSIX advisory locking is unsupported
    # on Windows Docker volume mounts (CIFS/SMB).  We test by attempting
    # a shared flock on the file — if it raises, locking is broken.
    try:
        import fcntl

        fd = os.open(db_path, os.O_RDONLY)
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
    except ImportError:
        # fcntl not available (Windows native Python) — not in Docker
        return (
            False,
            "POSIX file locking not available (Windows). "
            "Stop Plex Media Server before writing markers.",
        )
    except OSError as exc:
        return (
            False,
            f"File locking test failed on {db_path}: {exc}. "
            "This typically indicates Docker on Windows where volume mounts "
            "do not support POSIX advisory locking.  Stop Plex Media Server "
            "before writing markers, or use a Linux/macOS host.",
        )

    return True, ""


# ---------------------------------------------------------------------------
# Connection helper
# ---------------------------------------------------------------------------


def _connect(db_path: str) -> sqlite3.Connection:
    """Open a connection with safety pragmas.

    Sets ``busy_timeout`` so concurrent access with Plex (which uses WAL
    mode) retries instead of immediately failing with SQLITE_BUSY.

    """
    conn = sqlite3.connect(db_path, timeout=10)
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Tag ID lookup
# ---------------------------------------------------------------------------

# Marker tags use tag_type = 12 in the Plex ``tags`` table.
_MARKER_TAG_TYPE = 12


def get_marker_tag_id(db_path: str) -> int:
    """Look up the tag_id for marker tags in the Plex database.

    This is a **read-only** operation.  The marker tag (``tag_type=12``)
    already exists in every Plex installation — we never INSERT into the
    ``tags`` table because it has FTS4 triggers with a custom ICU
    tokenizer that would fail with standard SQLite.

    Args:
        db_path: Path to the Plex database.

    Returns:
        The ``tag_id`` integer.

    Raises:
        RuntimeError: If no marker tag is found (corrupted Plex install).

    """
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT id FROM tags WHERE tag_type = ? ORDER BY id LIMIT 1",
            (_MARKER_TAG_TYPE,),
        ).fetchone()
        if row is None:
            raise RuntimeError(
                f"No marker tag (tag_type={_MARKER_TAG_TYPE}) found in Plex database. "
                "The database may be corrupted or from an unsupported Plex version."
            )
        return row["id"]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Marker CRUD
# ---------------------------------------------------------------------------

# extra_data JSON templates matching Plex's own format (confirmed from
# a live database backup).
_EXTRA_DATA_INTRO = json.dumps(
    {"pv:version": "5", "url": "pv%3Aversion=5"},
    separators=(",", ":"),
)
_EXTRA_DATA_CREDITS_FINAL = json.dumps(
    {"pv:final": "1", "pv:version": "4", "url": "pv%3Afinal=1&pv%3Aversion=4"},
    separators=(",", ":"),
)
_EXTRA_DATA_CREDITS = json.dumps(
    {"pv:version": "4", "url": "pv%3Aversion=4"},
    separators=(",", ":"),
)


def _extra_data_for(marker_type: str, is_final: bool) -> str:
    """Return the ``extra_data`` JSON string for a marker type."""
    if marker_type == "intro":
        return _EXTRA_DATA_INTRO
    if is_final:
        return _EXTRA_DATA_CREDITS_FINAL
    return _EXTRA_DATA_CREDITS


def write_marker(
    db_path: str,
    metadata_item_id: int,
    tag_id: int,
    marker_type: str,
    start_ms: int,
    end_ms: int,
    is_final: bool = True,
) -> bool:
    """Write a single marker to the Plex ``taggings`` table.

    Uses an extremely short transaction: single INSERT + COMMIT.
    The connection is closed immediately after.

    Args:
        db_path: Path to the Plex database.
        metadata_item_id: The ``ratingKey`` of the media item.
        tag_id: The marker tag_id (from :func:`get_marker_tag_id`).
        marker_type: ``'intro'`` or ``'credits'``.
        start_ms: Marker start time in milliseconds.
        end_ms: Marker end time in milliseconds.
        is_final: Whether this is the final credits segment (sets
            ``pv:final`` in extra_data).

    Returns:
        ``True`` if the marker was written successfully.

    """
    if marker_type not in ("intro", "credits"):
        raise ValueError(
            f"marker_type must be 'intro' or 'credits', got {marker_type!r}"
        )

    conn = _connect(db_path)
    try:
        # Determine next index for this item
        row = conn.execute(
            'SELECT COALESCE(MAX("index"), -1) + 1 AS next_idx '
            "FROM taggings WHERE metadata_item_id = ? AND tag_id = ?",
            (metadata_item_id, tag_id),
        ).fetchone()
        next_index = row["next_idx"] if row else 0

        extra_data = _extra_data_for(marker_type, is_final)
        created_at = int(time.time())

        conn.execute(
            "INSERT INTO taggings "
            '(metadata_item_id, tag_id, "index", text, time_offset, '
            "end_time_offset, thumb_url, created_at, extra_data) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                metadata_item_id,
                tag_id,
                next_index,
                marker_type,
                start_ms,
                end_ms,
                "",  # thumb_url — not used for markers
                created_at,
                extra_data,
            ),
        )
        conn.commit()
        logger.debug(
            f"Wrote {marker_type} marker for item {metadata_item_id}: "
            f"{start_ms}ms–{end_ms}ms (index={next_index})"
        )
        return True
    except sqlite3.Error as exc:
        logger.warning(
            f"Failed to write {marker_type} marker for item {metadata_item_id}: {exc}"
        )
        return False
    finally:
        conn.close()


def delete_markers(
    db_path: str,
    metadata_item_id: int,
    tag_id: int,
    marker_type: str,
) -> int:
    """Remove existing markers of a given type for a media item.

    Args:
        db_path: Path to the Plex database.
        metadata_item_id: The ``ratingKey`` of the media item.
        tag_id: The marker tag_id.
        marker_type: ``'intro'`` or ``'credits'``.

    Returns:
        Number of markers deleted.

    """
    conn = _connect(db_path)
    try:
        cursor = conn.execute(
            "DELETE FROM taggings "
            "WHERE metadata_item_id = ? AND tag_id = ? AND text = ?",
            (metadata_item_id, tag_id, marker_type),
        )
        conn.commit()
        deleted = cursor.rowcount
        if deleted:
            logger.debug(
                f"Deleted {deleted} {marker_type} marker(s) for item {metadata_item_id}"
            )
        return deleted
    except sqlite3.Error as exc:
        logger.warning(
            f"Failed to delete {marker_type} markers for item {metadata_item_id}: {exc}"
        )
        return 0
    finally:
        conn.close()


def get_existing_markers(
    db_path: str,
    metadata_item_id: int,
    tag_id: int,
) -> List[Dict]:
    """Read existing markers for a media item.

    Args:
        db_path: Path to the Plex database.
        metadata_item_id: The ``ratingKey`` of the media item.
        tag_id: The marker tag_id.

    Returns:
        List of marker dicts with keys: ``type``, ``start_ms``,
        ``end_ms``, ``index``, ``extra_data``.

    """
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT text, time_offset, end_time_offset, "
            '"index", extra_data '
            "FROM taggings "
            "WHERE metadata_item_id = ? AND tag_id = ? "
            'ORDER BY "index"',
            (metadata_item_id, tag_id),
        ).fetchall()
        return [
            {
                "type": row["text"],
                "start_ms": row["time_offset"],
                "end_ms": row["end_time_offset"],
                "index": row["index"],
                "extra_data": row["extra_data"],
            }
            for row in rows
        ]
    finally:
        conn.close()
