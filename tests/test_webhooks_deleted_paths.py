"""Tests for Radarr/Sonarr deletedFiles[] payload extraction.

When Radarr/Sonarr's Download webhook is an upgrade event, the payload
carries a ``deletedFiles`` array listing the prior release that was
replaced. Our orphan-cleanup pass needs those paths so it can target
the precise sidecars to remove and post ``UpdateType:"Deleted"`` to
Jellyfin/Emby.

Coverage matrix per .claude/rules/testing.md:
  * absolute path / relativePath + folderPath / both / neither
  * single deletion / multiple / movieFile.previousFile (fork shape)
  * malformed entries (string, missing fields, non-list deletedFiles)
"""

from __future__ import annotations

from media_preview_generator.web.webhooks import (
    _extract_radarr_deleted_paths,
    _extract_sonarr_deleted_paths,
)

# ---------------------------------------------------------------------------
# Radarr
# ---------------------------------------------------------------------------


class TestRadarrDeletedPaths:
    def test_absolute_path_in_deletedFiles(self):
        payload = {
            "movie": {"title": "X", "folderPath": "/movies/X"},
            "movieFile": {"path": "/movies/X/X-NEW.mkv"},
            "deletedFiles": [{"path": "/movies/X/X-OLD.mkv"}],
        }
        assert _extract_radarr_deleted_paths(payload) == ["/movies/X/X-OLD.mkv"]

    def test_relative_path_combined_with_folderPath(self):
        payload = {
            "movie": {"title": "X", "folderPath": "/movies/X"},
            "movieFile": {"path": "/movies/X/X-NEW.mkv"},
            "deletedFiles": [{"relativePath": "X-OLD.mkv"}],
        }
        assert _extract_radarr_deleted_paths(payload) == ["/movies/X/X-OLD.mkv"]

    def test_absolute_takes_precedence_over_relative(self):
        """When both are present, ``path`` wins."""
        payload = {
            "movie": {"folderPath": "/movies/X"},
            "deletedFiles": [{"path": "/elsewhere/Y-OLD.mkv", "relativePath": "X-OLD.mkv"}],
        }
        assert _extract_radarr_deleted_paths(payload) == ["/elsewhere/Y-OLD.mkv"]

    def test_multiple_deleted_files(self):
        payload = {
            "movie": {"folderPath": "/movies/X"},
            "deletedFiles": [
                {"path": "/movies/X/A.mkv"},
                {"path": "/movies/X/B.mkv"},
            ],
        }
        assert _extract_radarr_deleted_paths(payload) == [
            "/movies/X/A.mkv",
            "/movies/X/B.mkv",
        ]

    def test_movieFile_previousFile_fork_shape(self):
        """Some Radarr forks surface the replaced file under previousFile."""
        payload = {
            "movie": {"folderPath": "/movies/X"},
            "movieFile": {
                "path": "/movies/X/X-NEW.mkv",
                "previousFile": {"path": "/movies/X/X-OLD.mkv"},
            },
        }
        assert _extract_radarr_deleted_paths(payload) == ["/movies/X/X-OLD.mkv"]

    def test_previousFile_uses_relative_path(self):
        payload = {
            "movie": {"folderPath": "/movies/X"},
            "movieFile": {
                "path": "/movies/X/X-NEW.mkv",
                "previousFile": {"relativePath": "X-OLD.mkv"},
            },
        }
        assert _extract_radarr_deleted_paths(payload) == ["/movies/X/X-OLD.mkv"]

    def test_dedupe_across_deletedFiles_and_previousFile(self):
        """Same path mentioned in both → returned exactly once, first-seen order."""
        payload = {
            "movie": {"folderPath": "/movies/X"},
            "movieFile": {
                "path": "/movies/X/NEW.mkv",
                "previousFile": {"path": "/movies/X/OLD.mkv"},
            },
            "deletedFiles": [{"path": "/movies/X/OLD.mkv"}],
        }
        assert _extract_radarr_deleted_paths(payload) == ["/movies/X/OLD.mkv"]

    def test_normalises_paths(self):
        """Internal duplicate slashes collapsed (POSIX preserves a leading ``//``)."""
        payload = {
            "movie": {"folderPath": "/movies/X"},
            "deletedFiles": [{"path": "/movies//X//OLD.mkv"}],
        }
        assert _extract_radarr_deleted_paths(payload) == ["/movies/X/OLD.mkv"]

    def test_normalises_dedupes_paths_that_collapse_to_same_value(self):
        """Two payload entries that normalise to the same path → returned once."""
        payload = {
            "movie": {"folderPath": "/movies/X"},
            "deletedFiles": [
                {"path": "/movies/X/OLD.mkv"},
                {"path": "/movies//X//OLD.mkv"},  # same after normpath
            ],
        }
        assert _extract_radarr_deleted_paths(payload) == ["/movies/X/OLD.mkv"]

    # --- Edge cases / malformed ------------------------------------

    def test_missing_deletedFiles_key_returns_empty(self):
        payload = {
            "movie": {"folderPath": "/movies/X"},
            "movieFile": {"path": "/movies/X/NEW.mkv"},
        }
        assert _extract_radarr_deleted_paths(payload) == []

    def test_deletedFiles_is_not_a_list(self):
        """``deletedFiles`` accidentally serialised as dict → silently skip."""
        payload = {"deletedFiles": {"path": "/movies/X/OLD.mkv"}}
        assert _extract_radarr_deleted_paths(payload) == []

    def test_deletedFiles_entry_is_not_a_dict(self):
        """Entries that are bare strings → silently skip (don't crash)."""
        payload = {"deletedFiles": ["/movies/X/OLD.mkv", {"path": "/movies/X/B.mkv"}]}
        # Only the dict entry is honoured.
        assert _extract_radarr_deleted_paths(payload) == ["/movies/X/B.mkv"]

    def test_deletedFiles_entry_with_no_path_or_relative(self):
        payload = {
            "movie": {"folderPath": "/movies/X"},
            "deletedFiles": [{"size": 12345}, {"path": "/movies/X/OLD.mkv"}],
        }
        assert _extract_radarr_deleted_paths(payload) == ["/movies/X/OLD.mkv"]

    def test_relative_path_without_folderPath_is_skipped(self):
        """Can't resolve relative without folderPath → entry skipped."""
        payload = {
            "movie": {},  # no folderPath
            "deletedFiles": [{"relativePath": "X-OLD.mkv"}],
        }
        assert _extract_radarr_deleted_paths(payload) == []


# ---------------------------------------------------------------------------
# Sonarr
# ---------------------------------------------------------------------------


class TestSonarrDeletedPaths:
    def test_absolute_path(self):
        payload = {
            "series": {"path": "/tv/Show"},
            "episodeFile": {"path": "/tv/Show/Season 01/E01-NEW.mkv"},
            "deletedFiles": [{"path": "/tv/Show/Season 01/E01-OLD.mkv"}],
        }
        assert _extract_sonarr_deleted_paths(payload) == ["/tv/Show/Season 01/E01-OLD.mkv"]

    def test_relative_path_combined_with_series_path(self):
        payload = {
            "series": {"path": "/tv/Show"},
            "deletedFiles": [{"relativePath": "Season 01/E01-OLD.mkv"}],
        }
        assert _extract_sonarr_deleted_paths(payload) == ["/tv/Show/Season 01/E01-OLD.mkv"]

    def test_multiple_episodes_replaced(self):
        payload = {
            "series": {"path": "/tv/Show"},
            "deletedFiles": [
                {"path": "/tv/Show/S01/E01-OLD.mkv"},
                {"path": "/tv/Show/S01/E02-OLD.mkv"},
            ],
        }
        result = _extract_sonarr_deleted_paths(payload)
        assert result == [
            "/tv/Show/S01/E01-OLD.mkv",
            "/tv/Show/S01/E02-OLD.mkv",
        ]

    def test_missing_deletedFiles_returns_empty(self):
        payload = {"series": {"path": "/tv/Show"}}
        assert _extract_sonarr_deleted_paths(payload) == []

    def test_malformed_entries_skipped(self):
        payload = {
            "series": {"path": "/tv/Show"},
            "deletedFiles": [
                "string-instead-of-dict",
                {"size": 1},
                {"path": "/tv/Show/E.mkv"},
            ],
        }
        assert _extract_sonarr_deleted_paths(payload) == ["/tv/Show/E.mkv"]
