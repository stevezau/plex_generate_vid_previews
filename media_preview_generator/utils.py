"""Utility functions for Plex Video Preview Generator.

Contains general-purpose utility functions that can be reused across
different modules in the application.
"""

import json
import os
import shutil
import tempfile
import time
import uuid
from typing import Any


def calculate_title_width():
    """Calculate optimal title width based on terminal size.

    Calculates the maximum number of characters that can be used for
    displaying media titles in the progress bars, accounting for all
    other UI elements.

    Returns:
        int: Maximum characters for title display (20-50 range)

    """
    terminal_width = shutil.get_terminal_size().columns

    worker_prefix = 7  # "GPU 0: " or "CPU 0: "
    percentage = 6  # " 100% "
    time_elapsed = 8  # " 00:00:00 "
    count_display = 12  # " (1/10) "
    speed_display = 8  # " 2.5x "
    progress_bar = 20  # Approximate progress bar width

    reserved_space = worker_prefix + percentage + time_elapsed + count_display + speed_display + progress_bar
    available_width = terminal_width - reserved_space

    # Set reasonable limits: minimum 20 chars, maximum 50 chars
    return max(min(available_width, 50), 20)


def format_display_title(title: str, media_type: str, title_max_width: int) -> str:
    """Format and truncate display title based on media type.

    Args:
        title: The media title to format
        media_type: 'episode' or 'movie'
        title_max_width: Maximum width for the title

    Returns:
        str: Formatted and truncated title

    """
    if media_type == "episode":
        # For episodes, ensure S01E01 format is always visible
        if len(title) > title_max_width:
            # Simple truncation: keep last 6 chars (S01E01) + show title
            season_episode = title[-6:]  # Last 6 characters (S01E01)
            available_space = title_max_width - 6 - 3  # 6 for S01E01, 3 for "..."
            if available_space > 0:
                show_title = title[:-6].strip()  # Everything except last 6 chars
                if len(show_title) > available_space:
                    show_title = show_title[:available_space]
                display_title = f"{show_title}...{season_episode}"
            else:
                # Not enough space, just show the season/episode
                display_title = f"...{season_episode}"
        else:
            display_title = title
    else:
        # For movies, use the title as-is
        display_title = title

        # Regular truncation for movies
        if len(display_title) > title_max_width:
            display_title = display_title[: title_max_width - 3] + "..."  # Leave room for "..."

    # Add padding to prevent progress bar jumping (only if not already truncated)
    if len(display_title) <= title_max_width:
        padding_needed = title_max_width - len(display_title)
        display_title = display_title + " " * padding_needed

    return display_title


def is_docker_environment() -> bool:
    """Check if running inside a Docker container."""
    return (
        os.path.exists("/.dockerenv")
        or os.environ.get("container") == "docker"
        or os.environ.get("DOCKER_CONTAINER") == "true"
        or "docker" in os.environ.get("HOSTNAME", "").lower()
    )


def is_windows() -> bool:
    """Check if running on Windows operating system."""
    return os.name == "nt"


def is_macos() -> bool:
    """Check if running on macOS operating system."""
    import platform

    return platform.system() == "Darwin"


def sanitize_path(path: str) -> str:
    """Sanitize file path for cross-platform compatibility.

    On Windows:
    - Converts forward slashes to backslashes
    - Handles UNC paths (\\\\server\\share)
    - Normalizes path separators

    On Linux/macOS:
    - Returns path as-is with normalization

    Args:
        path: The file path to sanitize

    Returns:
        str: Sanitized file path

    """
    if os.name == "nt":
        # Handle UNC paths: //server/share -> \\server\share
        if path.startswith("//"):
            path = "\\\\" + path[2:].replace("/", "\\")
        else:
            path = path.replace("/", "\\")

    # Normalize path (removes redundant separators and up-level references)
    return os.path.normpath(path)


def atomic_json_save(filepath: str, data: Any, *, permissions: int | None = None) -> None:
    """Write JSON data to a file atomically.

    Writes to a temporary file in the same directory first, then replaces
    the target. This prevents corruption if the process is killed mid-write.

    Args:
        filepath: Destination file path.
        data: JSON-serializable data to write.
        permissions: Optional octal file permissions (e.g. 0o600).

    Raises:
        IOError: If the write or replace fails.

    """
    parent = os.path.dirname(filepath) or "."
    os.makedirs(parent, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, filepath)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    if permissions is not None:
        try:
            os.chmod(filepath, permissions)
        except OSError:
            pass


def _backup_retention() -> int:
    """How many timestamped backups to keep per file (default 10, env-overridable)."""
    raw = os.environ.get("CONFIG_BACKUP_KEEP", "10")
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return 10
    return max(1, min(n, 100))


def _prune_old_backups(filepath: str, keep: int) -> None:
    """Glob ``{filepath}.*.bak``, sort lex, delete all but the newest ``keep``.

    Best-effort — failures are swallowed (a stale backup never blocks the
    next save). Lex order matches chronological order because the timestamp
    suffix is fixed-width ``YYYYMMDD-HHMMSS``.
    """
    import glob

    pattern = filepath + ".*.bak"
    backups = sorted(glob.glob(pattern))
    excess = len(backups) - keep
    if excess <= 0:
        return
    for stale in backups[:excess]:
        try:
            os.unlink(stale)
        except OSError:
            pass


def atomic_json_save_with_backup(filepath: str, data: Any, *, permissions: int | None = None) -> None:
    """Atomic JSON write that keeps the last N timestamped backups of prior contents.

    Same atomicity guarantees as ``atomic_json_save``, plus: before writing,
    if ``filepath`` already exists, copy it to ``filepath.{YYYYMMDD-HHMMSS}.bak``
    and prune oldest beyond ``CONFIG_BACKUP_KEEP`` (default 10). Backup is
    best-effort — failures are logged but never block the primary write,
    since the caller's data is more important than the recovery copy.

    The legacy single ``filepath.bak`` (from previous app versions) is left
    in place: the inventory + restore endpoints recognise it as a
    "previous version" entry alongside the new timestamped backups, and it
    ages out as fresh saves accumulate.

    Designed for the small, hand-editable JSON files this app owns
    (settings.json, schedules.json, webhook_history.json, setup_state.json).
    For high-write-rate state, prefer SQLite (see web/jobs.py).

    Args:
        filepath: Destination file path.
        data: JSON-serializable data to write.
        permissions: Optional octal file permissions (e.g. 0o600).

    Raises:
        IOError: If the primary write or replace fails. Backup failures do not raise.
    """
    if os.path.exists(filepath):
        from datetime import datetime, timezone

        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        bak_path = f"{filepath}.{ts}.bak"
        try:
            shutil.copy2(filepath, bak_path)
            _prune_old_backups(filepath, _backup_retention())
        except OSError as exc:
            # Don't import loguru at module load — keep this dep-light. Log
            # via stderr so we don't pull in the logging stack just for a
            # backup hiccup.
            import sys

            print(
                f"[atomic_json_save_with_backup] Could not write backup {bak_path}: {exc}",
                file=sys.stderr,
            )
    atomic_json_save(filepath, data, permissions=permissions)


def setup_working_directory(tmp_folder: str) -> str:
    """Create and set up a unique working temporary directory.

    Args:
        tmp_folder: Base temporary folder path

    Returns:
        str: Path to the created working directory

    Raises:
        OSError: If directory creation fails

    """
    # Create a unique subfolder for this run to avoid conflicts
    unique_id = f"plex_previews_{int(time.time())}_{str(uuid.uuid4())[:8]}"
    working_tmp_folder = os.path.join(tmp_folder, unique_id)

    # Create our specific working directory
    os.makedirs(working_tmp_folder, exist_ok=True)

    return working_tmp_folder
