"""
Tests for version_check.py module.

Tests version parsing, GitHub API interaction, and update checking.

Tests for ``check_for_updates`` capture loguru output and assert on the
specific log lines emitted, since that's the only observable side effect
of the function (it has no return value). A ``_caplog_loguru`` helper
fixture wires loguru to pytest's caplog so we can inspect the messages.
"""

import logging
import os
from unittest.mock import MagicMock, patch

import pytest
import requests
from loguru import logger

from media_preview_generator.version_check import (
    check_for_updates,
    get_current_version,
    get_latest_github_release,
    parse_version,
)


@pytest.fixture
def loguru_caplog(caplog):
    """Forward loguru records into pytest's caplog so tests can assert on them.

    ``check_for_updates`` only signals its outcome via loguru — adding a
    propagating handler at WARNING/INFO level is the boundary-respecting
    way to verify the message without monkey-patching the function under
    test or its module-level dependencies.
    """

    class _PropagateHandler(logging.Handler):
        def emit(self, record):  # pragma: no cover — handler glue
            logging.getLogger(record.name).handle(record)

    handler_id = logger.add(_PropagateHandler(), level="DEBUG", format="{message}")
    caplog.set_level(logging.DEBUG)
    try:
        yield caplog
    finally:
        logger.remove(handler_id)


class TestGetCurrentVersion:
    """Test getting current version.

    The function tries 3 sources in order:
      1. ``from . import __version__`` (set by setuptools-scm at build time)
      2. ``importlib.metadata.version("media-preview-generator")``
      3. Fallback ``"0.0.0"``

    These tests force each branch and assert the return value, instead of
    just asserting a string came back (which would pass even on a complete
    breakage of all 3 sources).
    """

    def test_returns_package_version_when_dunder_version_present(self, monkeypatch):
        """Branch 1: __version__ attribute exists ⇒ returned verbatim."""
        import media_preview_generator as pkg

        monkeypatch.setattr(pkg, "__version__", "9.9.9-test", raising=False)
        assert get_current_version() == "9.9.9-test"

    def test_falls_back_to_importlib_metadata_when_dunder_missing(self, monkeypatch):
        """Branch 2: __version__ missing ⇒ importlib.metadata.version() used."""
        import importlib.metadata

        import media_preview_generator as pkg

        monkeypatch.delattr(pkg, "__version__", raising=False)
        monkeypatch.setattr(importlib.metadata, "version", lambda name: "5.4.3")
        assert get_current_version() == "5.4.3"

    def test_falls_back_to_zero_zero_zero_when_all_sources_fail(self, monkeypatch):
        """Branch 3: both sources fail ⇒ "0.0.0" sentinel returned."""
        import importlib.metadata

        import media_preview_generator as pkg

        monkeypatch.delattr(pkg, "__version__", raising=False)

        def boom(_name):
            raise importlib.metadata.PackageNotFoundError("not installed")

        monkeypatch.setattr(importlib.metadata, "version", boom)
        assert get_current_version() == "0.0.0"


class TestParseVersion:
    """Test version string parsing."""

    def test_parse_version_valid(self):
        """Test parsing valid version string."""
        version = parse_version("2.0.0")
        assert version == (2, 0, 0)

    def test_parse_version_with_v_prefix(self):
        """Test parsing version with 'v' prefix."""
        version = parse_version("v2.0.0")
        assert version == (2, 0, 0)

    def test_parse_version_with_metadata(self):
        """Test parsing version with metadata."""
        version = parse_version("2.0.0-alpha+build123")
        assert version == (2, 0, 0)

    def test_parse_version_with_local_identifier(self):
        """Test parsing version with local identifier (PEP 440)."""
        version = parse_version("0.0.0+unknown")
        assert version == (0, 0, 0)

        version = parse_version("2.3.1.dev5+g1234abc")
        assert version == (2, 3, 1)

    def test_parse_version_invalid(self):
        """Test error on invalid version format."""
        with pytest.raises(ValueError):
            parse_version("invalid")

        with pytest.raises(ValueError):
            parse_version("2.0")

        with pytest.raises(ValueError):
            parse_version("2.0.0.1")


class TestGetLatestGitHubRelease:
    """Test GitHub API interaction."""

    @patch("requests.get")
    def test_get_latest_github_release(self, mock_get):
        """Test fetching latest release from GitHub."""
        mock_response = MagicMock()
        mock_response.json.return_value = {"tag_name": "v2.1.0"}
        mock_response.raise_for_status = MagicMock()
        mock_get.return_value = mock_response

        version = get_latest_github_release()
        assert version == "v2.1.0"

    @patch("requests.get")
    def test_get_latest_github_release_timeout(self, mock_get):
        """Test timeout handling."""
        mock_get.side_effect = requests.exceptions.Timeout("Timeout")

        version = get_latest_github_release()
        assert version is None

    @patch("requests.get")
    def test_get_latest_github_release_connection_error(self, mock_get):
        """Test connection error handling."""
        mock_get.side_effect = requests.exceptions.ConnectionError("No connection")

        version = get_latest_github_release()
        assert version is None

    @patch("requests.get")
    def test_get_latest_github_release_rate_limit(self, mock_get):
        """Test rate limit handling."""
        mock_response = MagicMock()
        mock_response.status_code = 429
        mock_response.raise_for_status.side_effect = requests.exceptions.HTTPError(response=mock_response)
        mock_get.return_value = mock_response

        version = get_latest_github_release()
        assert version is None

    @patch("requests.get")
    def test_get_latest_github_release_404(self, mock_get):
        """Test 404 error handling."""
        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_response.raise_for_status.side_effect = requests.exceptions.HTTPError(response=mock_response)
        mock_get.return_value = mock_response

        version = get_latest_github_release()
        assert version is None

    @patch("requests.get")
    def test_get_latest_github_release_empty_tag(self, mock_get):
        """Test handling of empty tag_name."""
        mock_response = MagicMock()
        mock_response.json.return_value = {"tag_name": ""}
        mock_response.raise_for_status = MagicMock()
        mock_get.return_value = mock_response

        version = get_latest_github_release()
        assert version is None


class TestCheckForUpdates:
    """Test update checking logic."""

    @patch("media_preview_generator.version_check.get_git_commit_sha", return_value=None)
    @patch("media_preview_generator.version_check.get_git_branch", return_value=None)
    @patch("media_preview_generator.version_check.get_latest_github_release")
    @patch("media_preview_generator.version_check.get_current_version")
    def test_check_for_updates_newer_available(self, mock_current, mock_latest, _mock_branch, _mock_sha, loguru_caplog):
        """Newer GitHub release is announced with both versions visible to the user."""
        mock_current.return_value = "2.0.0"
        mock_latest.return_value = "v2.1.0"

        check_for_updates()
        text = loguru_caplog.text
        assert "newer version is available" in text
        assert "v2.1.0" in text
        assert "2.0.0" in text

    @patch("media_preview_generator.version_check.get_git_commit_sha", return_value=None)
    @patch("media_preview_generator.version_check.get_git_branch", return_value=None)
    @patch("media_preview_generator.version_check.get_latest_github_release")
    @patch("media_preview_generator.version_check.get_current_version")
    def test_check_for_updates_up_to_date(self, mock_current, mock_latest, _mock_branch, _mock_sha, loguru_caplog):
        """When current == latest, no 'newer version' warning is emitted."""
        mock_current.return_value = "2.0.0"
        mock_latest.return_value = "v2.0.0"

        check_for_updates()
        # No warning to upgrade should fire on the up-to-date path.
        assert "newer version is available" not in loguru_caplog.text
        assert "Update:" not in loguru_caplog.text

    @patch("media_preview_generator.version_check.get_git_commit_sha", return_value=None)
    @patch("media_preview_generator.version_check.get_git_branch", return_value=None)
    @patch("media_preview_generator.version_check.get_latest_github_release")
    @patch("media_preview_generator.version_check.get_current_version")
    def test_check_for_updates_current_newer(self, mock_current, mock_latest, _mock_branch, _mock_sha, loguru_caplog):
        """Local version > GitHub latest (developer build) — no upgrade nag.

        Stub git_commit_sha + git_branch so the test isn't skewed by the
        real git checkout on CI runners (where get_git_branch() returns
        'dev' and the git-mode update path emits "🔄 Update: git pull
        origin dev" — leaking the "Update:" string the test asserts is
        absent).
        """
        mock_current.return_value = "2.1.0"
        mock_latest.return_value = "v2.0.0"

        check_for_updates()
        assert "newer version is available" not in loguru_caplog.text
        # Must not falsely advertise a downgrade.
        assert "Update:" not in loguru_caplog.text

    @patch("media_preview_generator.version_check.get_git_commit_sha", return_value=None)
    @patch("media_preview_generator.version_check.get_git_branch", return_value=None)
    @patch("media_preview_generator.version_check.get_latest_github_release")
    @patch("media_preview_generator.version_check.get_current_version")
    def test_check_for_updates_api_failure(self, mock_current, mock_latest, _mock_branch, _mock_sha, loguru_caplog):
        """When latest can't be fetched the function returns silently — no upgrade msg."""
        mock_current.return_value = "2.0.0"
        mock_latest.return_value = None

        check_for_updates()
        assert "newer version is available" not in loguru_caplog.text

    @patch("media_preview_generator.version_check.get_git_commit_sha", return_value=None)
    @patch("media_preview_generator.version_check.get_git_branch", return_value=None)
    @patch("media_preview_generator.version_check.is_docker_environment")
    @patch("media_preview_generator.version_check.get_latest_github_release")
    @patch("media_preview_generator.version_check.get_current_version")
    def test_check_for_updates_docker_message(
        self, mock_current, mock_latest, mock_docker, _mock_branch, _mock_sha, loguru_caplog
    ):
        """Docker installs receive the docker pull instruction."""
        mock_docker.return_value = True
        mock_current.return_value = "2.0.0"
        mock_latest.return_value = "v2.1.0"

        check_for_updates()
        text = loguru_caplog.text
        assert "docker pull" in text
        assert "stevezzau/media_preview_generator" in text

    @patch("media_preview_generator.version_check.get_git_commit_sha", return_value=None)
    @patch("media_preview_generator.version_check.get_git_branch", return_value=None)
    @patch("media_preview_generator.version_check.is_docker_environment")
    @patch("media_preview_generator.version_check.get_latest_github_release")
    @patch("media_preview_generator.version_check.get_current_version")
    def test_check_for_updates_non_docker_message(
        self, mock_current, mock_latest, mock_docker, _mock_branch, _mock_sha, loguru_caplog
    ):
        """Non-Docker installs receive the pip install instruction (not docker pull)."""
        mock_docker.return_value = False
        mock_current.return_value = "2.0.0"
        mock_latest.return_value = "v2.1.0"

        check_for_updates()
        text = loguru_caplog.text
        assert "pip install" in text
        assert "git+https://github.com/stevezau/media_preview_generator.git" in text

    @patch("media_preview_generator.version_check.get_git_commit_sha", return_value=None)
    @patch("media_preview_generator.version_check.get_git_branch", return_value=None)
    @patch("media_preview_generator.version_check.get_latest_github_release")
    @patch("media_preview_generator.version_check.get_current_version")
    def test_check_for_updates_dev_snapshot(self, mock_current, mock_latest, _mock_branch, _mock_sha, loguru_caplog):
        """0.0.0+unknown emits the dev-snapshot guidance, not the regular upgrade msg."""
        mock_current.return_value = "0.0.0+unknown"
        mock_latest.return_value = "v2.1.0"
        check_for_updates()
        text = loguru_caplog.text
        assert "development snapshot" in text
        assert "v2.1.0" in text
        assert "Latest stable release" in text

    @patch("media_preview_generator.version_check.get_latest_github_release")
    @patch("media_preview_generator.version_check.get_current_version")
    def test_check_for_updates_invalid_version_handled(self, mock_current, mock_latest, loguru_caplog):
        """Garbage current version: no nag fires.

        Pinned to the actual contract. Note: ``check_for_updates`` does
        currently swallow the parse failure silently (no log line) — that's
        a product-quality TODO (operator-debuggability gap) tracked
        separately, not a test contract to enforce here. For now this
        test pins what the production code actually does.
        """
        mock_current.return_value = "invalid"
        mock_latest.return_value = "v2.1.0"
        check_for_updates()
        assert "newer version is available" not in loguru_caplog.text

    @patch.dict(os.environ, {"GIT_BRANCH": "dev", "GIT_SHA": "abc1234567"})
    @patch("media_preview_generator.version_check.get_branch_head_sha")
    def test_check_for_updates_dev_docker_up_to_date(self, mock_head, loguru_caplog):
        """Dev Docker at latest commit logs the up-to-date message and skips upgrade nag."""
        mock_head.return_value = "abc1234567890abcdef1234567890abcdef123456"
        check_for_updates()
        text = loguru_caplog.text
        assert "Dev build up to date" in text
        assert "dev" in text
        assert "Newer dev commit" not in text

    @patch.dict(os.environ, {"GIT_BRANCH": "dev", "GIT_SHA": "abc1234"})
    @patch("media_preview_generator.version_check.get_branch_head_sha")
    def test_check_for_updates_dev_docker_behind(self, mock_head, loguru_caplog):
        """Dev Docker behind remote branch nags about a newer dev commit."""
        mock_head.return_value = "def5678567890abcdef1234567890abcdef123456"
        check_for_updates()
        text = loguru_caplog.text
        assert "Newer dev commit" in text
        # Must point users at the dev tag specifically, not :latest.
        assert ":dev" in text

    @patch.dict(os.environ, {"GIT_BRANCH": "dev", "GIT_SHA": "abc1234"})
    @patch("media_preview_generator.version_check.get_branch_head_sha")
    def test_check_for_updates_dev_docker_api_failure(self, mock_head, loguru_caplog):
        """Dev Docker + GitHub API failure: no fake "up to date" or "behind" claim."""
        mock_head.return_value = None
        check_for_updates()
        text = loguru_caplog.text
        assert "Dev build up to date" not in text
        assert "Newer dev commit" not in text

    @patch.dict(os.environ, {"GIT_BRANCH": "", "GIT_SHA": ""}, clear=False)
    @patch("media_preview_generator.version_check.get_git_branch")
    @patch("media_preview_generator.version_check.get_git_commit_sha")
    @patch("media_preview_generator.version_check.get_branch_head_sha")
    def test_check_for_updates_git_checkout_up_to_date(self, mock_head, mock_sha, mock_branch, loguru_caplog):
        """Local git checkout matches remote: emits the up-to-date message, no nag."""
        mock_sha.return_value = "abc1234567890abcdef1234567890abcdef123456"
        mock_branch.return_value = "main"
        mock_head.return_value = "abc1234567890abcdef1234567890abcdef123456"
        check_for_updates()
        text = loguru_caplog.text
        assert "Git checkout up to date" in text
        assert "main" in text
        assert "Newer commit on" not in text

    @patch.dict(os.environ, {"GIT_BRANCH": "", "GIT_SHA": ""}, clear=False)
    @patch("media_preview_generator.version_check.get_git_branch")
    @patch("media_preview_generator.version_check.get_git_commit_sha")
    @patch("media_preview_generator.version_check.get_branch_head_sha")
    def test_check_for_updates_git_checkout_behind(self, mock_head, mock_sha, mock_branch, loguru_caplog):
        """Local git checkout behind remote: emits 'git pull' instruction with branch."""
        mock_sha.return_value = "abc1234567890abcdef1234567890abcdef123456"
        mock_branch.return_value = "main"
        mock_head.return_value = "def5678567890abcdef1234567890abcdef123456"
        check_for_updates()
        text = loguru_caplog.text
        assert "Newer commit on" in text
        assert "git pull origin main" in text


class TestGetGitCommitSha:
    """Test get_git_commit_sha function."""

    @patch("subprocess.run")
    def test_returns_sha_when_in_repo(self, mock_run):
        from media_preview_generator.version_check import get_git_commit_sha

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "abc1234567890abcdef1234567890abcdef123456\n"
        mock_run.return_value = mock_result

        sha = get_git_commit_sha()
        assert sha == "abc1234567890abcdef1234567890abcdef123456"

    @patch("subprocess.run")
    def test_returns_none_when_not_in_repo(self, mock_run):
        from media_preview_generator.version_check import get_git_commit_sha

        mock_result = MagicMock()
        mock_result.returncode = 128
        mock_result.stdout = ""
        mock_run.return_value = mock_result

        sha = get_git_commit_sha()
        assert sha is None

    @patch("subprocess.run")
    def test_handles_file_not_found(self, mock_run):
        from media_preview_generator.version_check import get_git_commit_sha

        mock_run.side_effect = FileNotFoundError("git not found")
        sha = get_git_commit_sha()
        assert sha is None


class TestGetGitBranch:
    """Test get_git_branch function."""

    @patch("subprocess.run")
    def test_returns_branch_name(self, mock_run):
        from media_preview_generator.version_check import get_git_branch

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "dev\n"
        mock_run.return_value = mock_result

        branch = get_git_branch()
        assert branch == "dev"

    @patch("subprocess.run")
    def test_returns_none_for_detached_head(self, mock_run):
        from media_preview_generator.version_check import get_git_branch

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "HEAD\n"
        mock_run.return_value = mock_result

        branch = get_git_branch()
        assert branch is None

    @patch("subprocess.run")
    def test_returns_none_on_failure(self, mock_run):
        from media_preview_generator.version_check import get_git_branch

        mock_result = MagicMock()
        mock_result.returncode = 128
        mock_result.stdout = ""
        mock_run.return_value = mock_result

        branch = get_git_branch()
        assert branch is None


class TestGetBranchHeadSha:
    """Test get_branch_head_sha function."""

    @patch("requests.get")
    def test_returns_sha_on_success(self, mock_get):
        from media_preview_generator.version_check import get_branch_head_sha

        mock_response = MagicMock()
        mock_response.json.return_value = {"commit": {"sha": "abc1234567890abcdef1234567890abcdef123456"}}
        mock_response.raise_for_status = MagicMock()
        mock_get.return_value = mock_response

        sha = get_branch_head_sha("dev")
        assert sha == "abc1234567890abcdef1234567890abcdef123456"

    @patch("requests.get")
    def test_returns_none_on_timeout(self, mock_get):
        from media_preview_generator.version_check import get_branch_head_sha

        mock_get.side_effect = requests.exceptions.Timeout("Timeout")
        assert get_branch_head_sha("dev") is None

    @patch("requests.get")
    def test_returns_none_on_connection_error(self, mock_get):
        from media_preview_generator.version_check import get_branch_head_sha

        mock_get.side_effect = requests.exceptions.ConnectionError("fail")
        assert get_branch_head_sha("dev") is None

    @patch("requests.get")
    def test_returns_none_on_http_error(self, mock_get):
        from media_preview_generator.version_check import get_branch_head_sha

        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_get.side_effect = requests.exceptions.HTTPError(response=mock_response)
        assert get_branch_head_sha("dev") is None

    @patch("requests.get")
    def test_returns_none_on_empty_sha(self, mock_get):
        from media_preview_generator.version_check import get_branch_head_sha

        mock_response = MagicMock()
        mock_response.json.return_value = {"commit": {"sha": ""}}
        mock_response.raise_for_status = MagicMock()
        mock_get.return_value = mock_response
        assert get_branch_head_sha("dev") is None

    @patch("requests.get")
    def test_returns_none_on_request_exception(self, mock_get):
        from media_preview_generator.version_check import get_branch_head_sha

        mock_get.side_effect = requests.exceptions.RequestException("fail")
        assert get_branch_head_sha("dev") is None

    @patch("requests.get")
    def test_returns_none_on_unexpected_error(self, mock_get):
        from media_preview_generator.version_check import get_branch_head_sha

        mock_get.side_effect = RuntimeError("unexpected")
        assert get_branch_head_sha("dev") is None


class TestGetLatestGitHubReleaseExtra:
    """Additional edge cases for get_latest_github_release."""

    @patch("requests.get")
    def test_handles_generic_http_error(self, mock_get):
        """Test handling of generic HTTP error (not 404/429)."""
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.raise_for_status.side_effect = requests.exceptions.HTTPError(response=mock_response)
        mock_get.return_value = mock_response
        assert get_latest_github_release() is None

    @patch("requests.get")
    def test_handles_generic_request_exception(self, mock_get):
        mock_get.side_effect = requests.exceptions.RequestException("fail")
        assert get_latest_github_release() is None

    @patch("requests.get")
    def test_handles_key_error(self, mock_get):
        mock_response = MagicMock()
        mock_response.json.side_effect = KeyError("missing")
        mock_response.raise_for_status = MagicMock()
        mock_get.return_value = mock_response
        assert get_latest_github_release() is None

    @patch("requests.get")
    def test_handles_unexpected_exception(self, mock_get):
        mock_get.side_effect = RuntimeError("unexpected")
        assert get_latest_github_release() is None
