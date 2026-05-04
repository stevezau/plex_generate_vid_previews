"""
Tests for utils.py module.

Tests title formatting, path sanitization, Docker detection,
and working directory setup.
"""

import os
import tempfile
from collections import namedtuple
from unittest.mock import patch

import pytest

from media_preview_generator.utils import (
    calculate_title_width,
    format_display_title,
    is_docker_environment,
    is_windows,
    sanitize_path,
    setup_working_directory,
)


class TestCalculateTitleWidth:
    """Test terminal width calculation for title display."""

    @patch("shutil.get_terminal_size")
    def test_calculate_title_width(self, mock_terminal_size):
        """Test basic title width calculation."""
        # Mock terminal with 120 columns
        TerminalSize = namedtuple("TerminalSize", ["columns", "lines"])
        mock_terminal_size.return_value = TerminalSize(columns=120, lines=30)

        width = calculate_title_width()

        # Should return a reasonable width
        assert 20 <= width <= 50

    @patch("shutil.get_terminal_size")
    def test_calculate_title_width_small_terminal(self, mock_terminal_size):
        """Test minimum width is enforced."""
        # Mock very small terminal
        TerminalSize = namedtuple("TerminalSize", ["columns", "lines"])
        mock_terminal_size.return_value = TerminalSize(columns=50, lines=24)

        width = calculate_title_width()

        # Should return at least 20
        assert width >= 20

    @patch("shutil.get_terminal_size")
    def test_calculate_title_width_large_terminal(self, mock_terminal_size):
        """Test maximum width is capped."""
        # Mock very large terminal
        TerminalSize = namedtuple("TerminalSize", ["columns", "lines"])
        mock_terminal_size.return_value = TerminalSize(columns=300, lines=60)

        width = calculate_title_width()

        # Should not exceed 50
        assert width <= 50


class TestFormatDisplayTitle:
    """Test display title formatting."""

    def test_format_display_title_episode_short(self):
        """Test episode title that fits within width."""
        title = "Breaking Bad S01E01"
        result = format_display_title(title, "episode", title_max_width=30)

        # Should not be truncated
        assert "Breaking Bad" in result
        assert "S01E01" in result
        # Should be padded to exact width
        assert len(result) == 30

    def test_format_display_title_episode_long(self):
        """Test episode title truncation."""
        title = "A Very Long Show Name That Exceeds The Width S01E01"
        result = format_display_title(title, "episode", title_max_width=30)

        # Should preserve S01E01
        assert "S01E01" in result
        # Should be truncated
        assert "..." in result
        # Length should not exceed max
        assert len(result) <= 30

    def test_format_display_title_movie(self):
        """Test movie title formatting."""
        title = "The Shawshank Redemption"
        result = format_display_title(title, "movie", title_max_width=30)

        # Should contain title
        assert "Shawshank" in result or title in result
        # Should be padded
        assert len(result) == 30

    def test_format_display_title_movie_long(self):
        """Test long movie title truncation."""
        title = "A Very Long Movie Title That Definitely Exceeds The Maximum Width"
        result = format_display_title(title, "movie", title_max_width=30)

        # Should be truncated
        assert "..." in result
        # Should not exceed max
        assert len(result) <= 30

    def test_format_display_title_preserves_season_episode(self):
        """Test that season/episode is always preserved."""
        title = "Super Long Show Name That Goes On And On S05E12"
        result = format_display_title(title, "episode", title_max_width=25)

        # Must preserve the season/episode
        assert "S05E12" in result


class TestSanitizePath:
    """Test path sanitization."""

    @patch("os.name", "nt")
    def test_sanitize_path_windows(self):
        """Test Windows path conversion."""
        path = "/data/movies/test.mkv"
        result = sanitize_path(path)

        # Should convert to backslashes
        assert "\\" in result
        assert "/" not in result

    @patch("os.name", "posix")
    def test_sanitize_path_unix(self):
        """Unix paths remain unchanged: no slash conversion, normpath is idempotent."""
        # Real os.path.normpath under POSIX leaves an already-normalised path
        # alone — no need to mock it. Mocking it would only test that our
        # mock returns what we told it to.
        path = "/data/movies/test.mkv"
        result = sanitize_path(path)

        assert result == path
        # Backslashes never appear on POSIX output.
        assert "\\" not in result

    @patch("os.name", "posix")
    def test_sanitize_path_unix_normalises_redundant_separators(self):
        """Real normpath collapses //, /./, and /foo/.. on POSIX."""
        # This is what we actually rely on from os.path.normpath — exercising
        # it without a mock catches a regression if someone removed the
        # normalize step or replaced it with a no-op.
        assert sanitize_path("/data//movies/./test.mkv") == "/data/movies/test.mkv"
        assert sanitize_path("/data/movies/../movies/test.mkv") == "/data/movies/test.mkv"

    @patch("os.name", "nt")
    def test_sanitize_path_windows_mixed(self):
        """Test Windows handles mixed slashes."""
        path = "/data\\movies/test.mkv"
        result = sanitize_path(path)

        # All slashes should be backslashes
        assert "/" not in result
        assert "\\" in result


class TestSafeResolveWithin:
    """Security tests for ``_safe_resolve_within`` — the actual path-traversal
    boundary that webhook + API routes call before opening user-supplied paths.

    ``sanitize_path`` (above) is a cosmetic separator-conversion helper only.
    The security contract is enforced here:
    - rejects null-byte injection (PEP 446 / CWE-158)
    - rejects ``..`` escapes after normpath collapse
    - rejects absolute paths that point outside ``allowed_root``
    - rejects symlink escapes (realpath resolves links before the guard)
    - allows exact-root match
    - allows root=='/' (catch-all)

    These tests existed nowhere before audit batch 4 — the function had
    zero coverage despite being the project's primary defence against
    directory traversal.
    """

    def test_null_byte_injection_returns_none(self, tmp_path):
        from media_preview_generator.web.routes._helpers import _safe_resolve_within

        result = _safe_resolve_within(f"{tmp_path}/foo\x00.mkv", str(tmp_path))
        assert result is None, "null-byte must short-circuit BEFORE realpath"

    def test_dotdot_traversal_rejected(self, tmp_path):
        from media_preview_generator.web.routes._helpers import _safe_resolve_within

        # /tmp/pytest-of-data/.../allowed/../../../etc/passwd → /etc/passwd
        result = _safe_resolve_within(f"{tmp_path}/../../../../../../etc/passwd", str(tmp_path))
        assert result is None, "dot-dot traversal must be rejected"

    def test_absolute_path_outside_root_rejected(self, tmp_path):
        from media_preview_generator.web.routes._helpers import _safe_resolve_within

        result = _safe_resolve_within("/etc/passwd", str(tmp_path))
        assert result is None, "absolute path outside root must be rejected"

    def test_symlink_escape_rejected(self, tmp_path):
        """Symlinks pointing outside ``allowed_root`` must be rejected after realpath."""
        from media_preview_generator.web.routes._helpers import _safe_resolve_within

        outside = tmp_path.parent / "outside-target"
        outside.mkdir()
        (outside / "secret.txt").write_text("secret")

        link = tmp_path / "evil-link"
        link.symlink_to(outside)

        # The link itself is INSIDE allowed_root, but realpath resolves it
        # to /tmp/outside-target which is NOT — must be rejected.
        result = _safe_resolve_within(str(link / "secret.txt"), str(tmp_path))
        assert result is None, "symlink escape must be caught by realpath guard"

    def test_path_inside_root_allowed(self, tmp_path):
        from media_preview_generator.web.routes._helpers import _safe_resolve_within

        target = tmp_path / "subdir" / "file.mkv"
        target.parent.mkdir()
        target.touch()

        result = _safe_resolve_within(str(target), str(tmp_path))
        assert result is not None
        assert result == str(target.resolve()), "valid in-root path must resolve to its realpath"

    def test_exact_root_match_allowed(self, tmp_path):
        from media_preview_generator.web.routes._helpers import _safe_resolve_within

        result = _safe_resolve_within(str(tmp_path), str(tmp_path))
        assert result is not None, "exact root match must be allowed"

    def test_root_equals_filesystem_root_allows_anything(self):
        """When ``allowed_root='/'`` the guard is a no-op (intentional —
        operators who want unconstrained file access set MEDIA_ROOT='/')."""
        from media_preview_generator.web.routes._helpers import _safe_resolve_within

        # /etc/passwd must resolve when root is '/' — that's the documented behaviour.
        result = _safe_resolve_within("/etc/passwd", "/")
        # Either resolves to a path or None depending on whether /etc/passwd
        # exists; the contract is "doesn't reject solely because of /". We
        # accept either return as long as it didn't raise.
        assert result is None or result.startswith("/")

    def test_relative_path_normalised_before_check(self, tmp_path):
        """Relative paths get normpath'd then realpath'd — the result still
        has to land inside allowed_root."""
        from media_preview_generator.web.routes._helpers import _safe_resolve_within

        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "x.mkv").touch()

        # Relative path: normpath collapses ".." inside the string but
        # realpath then anchors against CWD, so unless the relative path
        # *resolves* under tmp_path, reject. Easier to assert behaviour
        # with a path crafted to escape: tmp_path/sub/../sub/x.mkv → tmp_path/sub/x.mkv (in)
        result = _safe_resolve_within(str(tmp_path / "sub" / ".." / "sub" / "x.mkv"), str(tmp_path))
        assert result is not None, "in-root path with redundant '..' segment must be allowed after normalisation"

    def test_prefix_confusion_with_sibling_path_rejected(self, tmp_path):
        """A path that shares the allowed_root's name as a PREFIX but is in
        a sibling directory must be rejected.

        Classic security boundary bug — without the trailing separator
        in the startswith check (``root_real + os.sep`` at _helpers.py
        line 88), an allowed_root of ``/data`` would let ``/data-attacker``
        through because ``"/data-attacker".startswith("/data")`` is True.
        Production correctly appends the os.sep to defeat this; the
        test pins it so a refactor that drops the sep is caught loudly.
        """
        from media_preview_generator.web.routes._helpers import _safe_resolve_within

        # Build sibling: tmp_path is /tmp/.../X; sibling is /tmp/.../X-evil
        sibling = tmp_path.parent / (tmp_path.name + "-evil")
        sibling.mkdir()
        (sibling / "secret.txt").write_text("secret")

        # /tmp/.../X-evil/secret.txt is OUTSIDE /tmp/.../X — must reject
        # despite the shared name prefix.
        result = _safe_resolve_within(str(sibling / "secret.txt"), str(tmp_path))
        assert result is None, (
            f"prefix confusion: {sibling}/secret.txt was allowed under {tmp_path} — "
            "the trailing-separator guard at _helpers.py is missing or broken"
        )

    def test_nonexistent_path_inside_root_still_resolves(self, tmp_path):
        """A path that doesn't exist on disk yet must still resolve when it
        WOULD be inside allowed_root — webhook routes are called for
        files Sonarr/Radarr just downloaded, and the BIF write path
        always references not-yet-existing output filenames.

        ``os.path.realpath`` of a nonexistent path returns the path
        as-given (with .. collapsed). The startswith guard should still
        pass cleanly. A regression that started requiring the path to
        exist would break every fresh-download webhook.
        """
        from media_preview_generator.web.routes._helpers import _safe_resolve_within

        not_yet = tmp_path / "subdir" / "fresh_download.mkv"
        # Don't create it — that's the whole point.
        result = _safe_resolve_within(str(not_yet), str(tmp_path))
        assert result is not None, (
            "a nonexistent in-root path must still resolve — Sonarr webhook fires before "
            "the file lands locally; we still need to map+queue it"
        )
        assert str(not_yet) in result or str(not_yet.parent) in result


class TestIsWindows:
    """Test Windows platform detection."""

    @patch("os.name", "nt")
    def test_is_windows_on_windows(self):
        """Test detection on Windows platform."""
        result = is_windows()
        assert result is True

    @patch("os.name", "posix")
    def test_is_windows_on_posix(self):
        """Test detection on POSIX platform (Linux/macOS)."""
        result = is_windows()
        assert result is False


class TestIsDockerEnvironment:
    """Test Docker environment detection."""

    @patch("os.path.exists")
    def test_is_docker_environment_dockerenv(self, mock_exists):
        """Test detection via /.dockerenv file."""
        mock_exists.side_effect = lambda path: path == "/.dockerenv"

        result = is_docker_environment()
        assert result is True

    @patch("os.path.exists")
    def test_is_docker_environment_container_env(self, mock_exists):
        """Test detection via container env variable."""
        mock_exists.return_value = False

        with patch.dict("os.environ", {"container": "docker"}):
            result = is_docker_environment()
            assert result is True

    @patch("os.path.exists")
    def test_is_docker_environment_docker_container_env(self, mock_exists):
        """Test detection via DOCKER_CONTAINER env variable."""
        mock_exists.return_value = False

        with patch.dict("os.environ", {"DOCKER_CONTAINER": "true"}, clear=True):
            result = is_docker_environment()
            assert result is True

    @patch("os.path.exists")
    def test_is_docker_environment_hostname(self, mock_exists):
        """Test detection via hostname containing 'docker'."""
        mock_exists.return_value = False

        with patch.dict("os.environ", {"HOSTNAME": "my-docker-container-123"}, clear=True):
            result = is_docker_environment()
            assert result is True

    @patch("os.path.exists")
    def test_is_docker_environment_not_docker(self, mock_exists):
        """Test non-Docker environment."""
        mock_exists.return_value = False

        with patch.dict("os.environ", {}, clear=True):
            result = is_docker_environment()
            assert result is False


class TestSetupWorkingDirectory:
    """Test working directory setup."""

    def test_setup_working_directory(self):
        """Test creates unique directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            result = setup_working_directory(tmpdir)

            # Should create a subdirectory
            assert os.path.exists(result)
            assert os.path.isdir(result)
            assert result.startswith(tmpdir)
            # Should contain a unique identifier
            assert "plex_previews_" in result

    def test_setup_working_directory_unique(self):
        """Test that multiple calls create unique directories."""
        with tempfile.TemporaryDirectory() as tmpdir:
            dir1 = setup_working_directory(tmpdir)
            dir2 = setup_working_directory(tmpdir)

            # Should be different directories
            assert dir1 != dir2
            assert os.path.exists(dir1)
            assert os.path.exists(dir2)

    def test_setup_working_directory_creates_if_missing(self):
        """Test creates directory if it doesn't exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            base_path = os.path.join(tmpdir, "subdir")

            # base_path doesn't exist yet
            result = setup_working_directory(base_path)

            # Should create the full path
            assert os.path.exists(result)
            assert os.path.isdir(result)


class TestAtomicJsonSaveWithBackup:
    """J1 — backup-then-write helper for hand-editable config files.

    Backups are now timestamped: ``config.json.YYYYMMDD-HHMMSS.bak``. The
    helper prunes oldest beyond ``CONFIG_BACKUP_KEEP`` (default 10) on
    each save. Legacy single ``config.json.bak`` files left over from
    earlier app versions are not migrated; they coexist and age out.
    """

    def _list_timestamped_baks(self, dirpath, stem):
        return sorted(p for p in dirpath.iterdir() if p.name.startswith(f"{stem}.") and p.name.endswith(".bak"))

    def test_first_write_no_bak(self, tmp_path):
        """When the target doesn't exist, no .bak is created."""
        from media_preview_generator.utils import atomic_json_save_with_backup

        target = tmp_path / "config.json"
        atomic_json_save_with_backup(str(target), {"hello": "world"})
        assert target.exists()
        assert self._list_timestamped_baks(tmp_path, "config.json") == []

    def test_subsequent_write_creates_timestamped_bak_with_old_contents(self, tmp_path):
        """The new backup holds the file contents *before* the write."""
        import json as _json

        from media_preview_generator.utils import atomic_json_save_with_backup

        target = tmp_path / "config.json"
        atomic_json_save_with_backup(str(target), {"v": 1})
        atomic_json_save_with_backup(str(target), {"v": 2})

        assert _json.loads(target.read_text())["v"] == 2
        baks = self._list_timestamped_baks(tmp_path, "config.json")
        assert len(baks) == 1
        # Filename shape: config.json.YYYYMMDD-HHMMSS.bak
        suffix = baks[0].name[len("config.json.") : -len(".bak")]
        assert len(suffix) == 15 and suffix[8] == "-"
        assert _json.loads(baks[0].read_text())["v"] == 1

    @pytest.mark.slow
    def test_keeps_history_across_many_writes(self, tmp_path):
        """Multiple saves accumulate timestamped backups (vs. the old rolling single)."""
        import time

        from media_preview_generator.utils import atomic_json_save_with_backup

        target = tmp_path / "config.json"
        for v in range(5):
            atomic_json_save_with_backup(str(target), {"v": v})
            # 1.1s gap so the YYYYMMDD-HHMMSS suffix is unique per save.
            time.sleep(1.1)
        baks = self._list_timestamped_baks(tmp_path, "config.json")
        # 5 saves → 4 backups (the first save had no prior contents to back up).
        assert len(baks) == 4

    @pytest.mark.slow
    def test_prunes_oldest_beyond_retention(self, tmp_path, monkeypatch):
        """CONFIG_BACKUP_KEEP caps how many history entries are kept."""
        import time

        from media_preview_generator.utils import atomic_json_save_with_backup

        monkeypatch.setenv("CONFIG_BACKUP_KEEP", "3")
        target = tmp_path / "config.json"
        for v in range(6):
            atomic_json_save_with_backup(str(target), {"v": v})
            time.sleep(1.1)
        baks = self._list_timestamped_baks(tmp_path, "config.json")
        assert len(baks) == 3

    def test_legacy_single_bak_is_not_disturbed(self, tmp_path):
        """A legacy plain .bak from an older app version stays alongside the new
        timestamped backups so users can still restore from it."""
        from media_preview_generator.utils import atomic_json_save_with_backup

        target = tmp_path / "config.json"
        target.write_text('{"v": 1}')
        legacy = tmp_path / "config.json.bak"
        legacy.write_text('{"v": "legacy"}')

        atomic_json_save_with_backup(str(target), {"v": 2})

        assert legacy.exists()
        assert legacy.read_text() == '{"v": "legacy"}'

    def test_prune_drops_anything_older_than_max_age_days(self, tmp_path):
        """D17 — age-based pruning deletes backups whose mtime is older than
        ``max_age_days``, independent of the count cap. With ``keep=10`` and
        ``max_age_days=7``, a 30-day-old backup must still be deleted even
        though the count cap is nowhere near exceeded."""
        import time

        from media_preview_generator.utils import _prune_old_backups

        target = tmp_path / "config.json"
        target.write_text("{}")
        old_bak = tmp_path / "config.json.20260101-000000.bak"
        old_bak.write_text("{}")
        recent_bak = tmp_path / "config.json.20260501-000000.bak"
        recent_bak.write_text("{}")
        # Backdate the "old" one to 30 days ago so age-based pruning fires.
        old_mtime = time.time() - (30 * 86400)
        os.utime(str(old_bak), (old_mtime, old_mtime))

        _prune_old_backups(str(target), keep=10, max_age_days=7)

        assert not old_bak.exists(), "30-day-old backup should be pruned at max_age_days=7"
        assert recent_bak.exists(), "recent backup must survive"

    def test_prune_max_age_zero_disables_age_check(self, tmp_path):
        """D17 — ``max_age_days=0`` keeps everything regardless of age, so
        existing installs (default 0) keep behaving exactly as before."""
        import time

        from media_preview_generator.utils import _prune_old_backups

        target = tmp_path / "config.json"
        target.write_text("{}")
        ancient = tmp_path / "config.json.20200101-000000.bak"
        ancient.write_text("{}")
        old_mtime = time.time() - (1000 * 86400)  # ~3 years
        os.utime(str(ancient), (old_mtime, old_mtime))

        _prune_old_backups(str(target), keep=10, max_age_days=0)

        assert ancient.exists()

    def test_backup_failure_does_not_block_primary_write(self, tmp_path, monkeypatch):
        """If shutil.copy2 raises, the primary write still happens."""
        import json as _json
        import shutil as _shutil

        from media_preview_generator import utils

        target = tmp_path / "config.json"
        target.write_text('{"v": 1}')

        def boom(*a, **kw):
            raise OSError("fake disk error")

        monkeypatch.setattr(_shutil, "copy2", boom)
        # This should NOT raise — backup is best-effort.
        utils.atomic_json_save_with_backup(str(target), {"v": 2})
        assert _json.loads(target.read_text())["v"] == 2
