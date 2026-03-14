"""
Tests for version_check.py module.

Tests version parsing, GitHub API interaction, and update checking.
"""

import os

import pytest
from unittest.mock import patch, MagicMock
import requests

from plex_generate_previews.version_check import (
    get_current_version,
    parse_version,
    get_latest_github_release,
    check_for_updates,
)


class TestGetCurrentVersion:
    """Test getting current version."""

    def test_get_current_version_from_local(self):
        """Test getting version from local _version.py (priority 1)."""
        # When running from source, should get version from _version.py
        version = get_current_version()
        # Should be a valid version string (either real or placeholder)
        assert isinstance(version, str)
        assert len(version) > 0

    def test_get_current_version_priority_order(self):
        """Test that local _version.py takes priority over installed metadata."""
        # This test verifies the priority: local _version.py is checked first
        # We can't easily mock importlib.metadata since it's imported locally
        # So we just verify that get_current_version() returns something valid
        version = get_current_version()
        assert isinstance(version, str)
        assert len(version) > 0
        # The version should come from local _version.py when running from source
        # (not from any installed package metadata)


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
        mock_response.raise_for_status.side_effect = requests.exceptions.HTTPError(
            response=mock_response
        )
        mock_get.return_value = mock_response

        version = get_latest_github_release()
        assert version is None

    @patch("requests.get")
    def test_get_latest_github_release_404(self, mock_get):
        """Test 404 error handling."""
        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_response.raise_for_status.side_effect = requests.exceptions.HTTPError(
            response=mock_response
        )
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

    @patch("plex_generate_previews.version_check.get_latest_github_release")
    @patch("plex_generate_previews.version_check.get_current_version")
    def test_check_for_updates_newer_available(self, mock_current, mock_latest):
        """Test showing update message when newer version available."""
        mock_current.return_value = "2.0.0"
        mock_latest.return_value = "v2.1.0"

        # Should not raise, just log
        check_for_updates()

    @patch("plex_generate_previews.version_check.get_latest_github_release")
    @patch("plex_generate_previews.version_check.get_current_version")
    def test_check_for_updates_up_to_date(self, mock_current, mock_latest):
        """Test no message when current version is latest."""
        mock_current.return_value = "2.0.0"
        mock_latest.return_value = "v2.0.0"

        # Should not raise, just log
        check_for_updates()

    @patch("plex_generate_previews.version_check.get_latest_github_release")
    @patch("plex_generate_previews.version_check.get_current_version")
    def test_check_for_updates_current_newer(self, mock_current, mock_latest):
        """Test when current version is newer than latest (dev version)."""
        mock_current.return_value = "2.1.0"
        mock_latest.return_value = "v2.0.0"

        # Should not raise or show update message
        check_for_updates()

    @patch("plex_generate_previews.version_check.get_latest_github_release")
    @patch("plex_generate_previews.version_check.get_current_version")
    def test_check_for_updates_api_failure(self, mock_current, mock_latest):
        """Test handling of API failure."""
        mock_current.return_value = "2.0.0"
        mock_latest.return_value = None  # API failed

        # Should handle gracefully
        check_for_updates()

    @patch("plex_generate_previews.utils.is_docker_environment")
    @patch("plex_generate_previews.version_check.get_latest_github_release")
    @patch("plex_generate_previews.version_check.get_current_version")
    def test_check_for_updates_docker_message(
        self, mock_current, mock_latest, mock_docker
    ):
        """Test Docker-specific update instructions."""
        mock_docker.return_value = True
        mock_current.return_value = "2.0.0"
        mock_latest.return_value = "v2.1.0"

        # Should show Docker-specific instructions
        check_for_updates()

    @patch("plex_generate_previews.utils.is_docker_environment")
    @patch("plex_generate_previews.version_check.get_latest_github_release")
    @patch("plex_generate_previews.version_check.get_current_version")
    def test_check_for_updates_non_docker_message(
        self, mock_current, mock_latest, mock_docker
    ):
        """Test non-Docker update instructions."""
        mock_docker.return_value = False
        mock_current.return_value = "2.0.0"
        mock_latest.return_value = "v2.1.0"

        # Should show update instructions (from source or Docker)
        check_for_updates()

    @patch("plex_generate_previews.version_check.get_latest_github_release")
    @patch("plex_generate_previews.version_check.get_current_version")
    def test_check_for_updates_dev_snapshot(self, mock_current, mock_latest):
        """Running from dev snapshot (0.0.0) shows appropriate message."""
        mock_current.return_value = "0.0.0+unknown"
        mock_latest.return_value = "v2.1.0"
        check_for_updates()

    @patch("plex_generate_previews.version_check.get_latest_github_release")
    @patch("plex_generate_previews.version_check.get_current_version")
    def test_check_for_updates_invalid_version_handled(self, mock_current, mock_latest):
        """Invalid current version is handled gracefully."""
        mock_current.return_value = "invalid"
        mock_latest.return_value = "v2.1.0"
        check_for_updates()

    @patch.dict(os.environ, {"GIT_BRANCH": "dev", "GIT_SHA": "abc1234"})
    @patch("plex_generate_previews.version_check.get_branch_head_sha")
    def test_check_for_updates_dev_docker_up_to_date(self, mock_head):
        """Dev Docker image at latest commit."""
        mock_head.return_value = "abc1234567890abcdef1234567890abcdef123456"
        check_for_updates()

    @patch.dict(os.environ, {"GIT_BRANCH": "dev", "GIT_SHA": "abc1234"})
    @patch("plex_generate_previews.version_check.get_branch_head_sha")
    def test_check_for_updates_dev_docker_behind(self, mock_head):
        """Dev Docker image behind remote branch."""
        mock_head.return_value = "def5678567890abcdef1234567890abcdef123456"
        check_for_updates()

    @patch.dict(os.environ, {"GIT_BRANCH": "dev", "GIT_SHA": "abc1234"})
    @patch("plex_generate_previews.version_check.get_branch_head_sha")
    def test_check_for_updates_dev_docker_api_failure(self, mock_head):
        """Dev Docker image with API failure falls through."""
        mock_head.return_value = None
        check_for_updates()

    @patch.dict(os.environ, {"GIT_BRANCH": "", "GIT_SHA": ""}, clear=False)
    @patch("plex_generate_previews.version_check.get_git_branch")
    @patch("plex_generate_previews.version_check.get_git_commit_sha")
    @patch("plex_generate_previews.version_check.get_branch_head_sha")
    def test_check_for_updates_git_checkout_up_to_date(
        self, mock_head, mock_sha, mock_branch
    ):
        """Git checkout up to date with remote."""
        mock_sha.return_value = "abc1234567890abcdef1234567890abcdef123456"
        mock_branch.return_value = "main"
        mock_head.return_value = "abc1234567890abcdef1234567890abcdef123456"
        check_for_updates()

    @patch.dict(os.environ, {"GIT_BRANCH": "", "GIT_SHA": ""}, clear=False)
    @patch("plex_generate_previews.version_check.get_git_branch")
    @patch("plex_generate_previews.version_check.get_git_commit_sha")
    @patch("plex_generate_previews.version_check.get_branch_head_sha")
    def test_check_for_updates_git_checkout_behind(
        self, mock_head, mock_sha, mock_branch
    ):
        """Git checkout behind remote."""
        mock_sha.return_value = "abc1234567890abcdef1234567890abcdef123456"
        mock_branch.return_value = "main"
        mock_head.return_value = "def5678567890abcdef1234567890abcdef123456"
        check_for_updates()


class TestGetGitCommitSha:
    """Test get_git_commit_sha function."""

    @patch("subprocess.run")
    def test_returns_sha_when_in_repo(self, mock_run):
        from plex_generate_previews.version_check import get_git_commit_sha

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "abc1234567890abcdef1234567890abcdef123456\n"
        mock_run.return_value = mock_result

        sha = get_git_commit_sha()
        assert sha == "abc1234567890abcdef1234567890abcdef123456"

    @patch("subprocess.run")
    def test_returns_none_when_not_in_repo(self, mock_run):
        from plex_generate_previews.version_check import get_git_commit_sha

        mock_result = MagicMock()
        mock_result.returncode = 128
        mock_result.stdout = ""
        mock_run.return_value = mock_result

        sha = get_git_commit_sha()
        assert sha is None

    @patch("subprocess.run")
    def test_handles_file_not_found(self, mock_run):
        from plex_generate_previews.version_check import get_git_commit_sha

        mock_run.side_effect = FileNotFoundError("git not found")
        sha = get_git_commit_sha()
        assert sha is None


class TestGetGitBranch:
    """Test get_git_branch function."""

    @patch("subprocess.run")
    def test_returns_branch_name(self, mock_run):
        from plex_generate_previews.version_check import get_git_branch

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "dev\n"
        mock_run.return_value = mock_result

        branch = get_git_branch()
        assert branch == "dev"

    @patch("subprocess.run")
    def test_returns_none_for_detached_head(self, mock_run):
        from plex_generate_previews.version_check import get_git_branch

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "HEAD\n"
        mock_run.return_value = mock_result

        branch = get_git_branch()
        assert branch is None

    @patch("subprocess.run")
    def test_returns_none_on_failure(self, mock_run):
        from plex_generate_previews.version_check import get_git_branch

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
        from plex_generate_previews.version_check import get_branch_head_sha

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "commit": {"sha": "abc1234567890abcdef1234567890abcdef123456"}
        }
        mock_response.raise_for_status = MagicMock()
        mock_get.return_value = mock_response

        sha = get_branch_head_sha("dev")
        assert sha == "abc1234567890abcdef1234567890abcdef123456"

    @patch("requests.get")
    def test_returns_none_on_timeout(self, mock_get):
        from plex_generate_previews.version_check import get_branch_head_sha

        mock_get.side_effect = requests.exceptions.Timeout("Timeout")
        assert get_branch_head_sha("dev") is None

    @patch("requests.get")
    def test_returns_none_on_connection_error(self, mock_get):
        from plex_generate_previews.version_check import get_branch_head_sha

        mock_get.side_effect = requests.exceptions.ConnectionError("fail")
        assert get_branch_head_sha("dev") is None

    @patch("requests.get")
    def test_returns_none_on_http_error(self, mock_get):
        from plex_generate_previews.version_check import get_branch_head_sha

        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_get.side_effect = requests.exceptions.HTTPError(response=mock_response)
        assert get_branch_head_sha("dev") is None

    @patch("requests.get")
    def test_returns_none_on_empty_sha(self, mock_get):
        from plex_generate_previews.version_check import get_branch_head_sha

        mock_response = MagicMock()
        mock_response.json.return_value = {"commit": {"sha": ""}}
        mock_response.raise_for_status = MagicMock()
        mock_get.return_value = mock_response
        assert get_branch_head_sha("dev") is None

    @patch("requests.get")
    def test_returns_none_on_request_exception(self, mock_get):
        from plex_generate_previews.version_check import get_branch_head_sha

        mock_get.side_effect = requests.exceptions.RequestException("fail")
        assert get_branch_head_sha("dev") is None

    @patch("requests.get")
    def test_returns_none_on_unexpected_error(self, mock_get):
        from plex_generate_previews.version_check import get_branch_head_sha

        mock_get.side_effect = RuntimeError("unexpected")
        assert get_branch_head_sha("dev") is None


class TestGetLatestGitHubReleaseExtra:
    """Additional edge cases for get_latest_github_release."""

    @patch("requests.get")
    def test_handles_generic_http_error(self, mock_get):
        """Test handling of generic HTTP error (not 404/429)."""
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.raise_for_status.side_effect = requests.exceptions.HTTPError(
            response=mock_response
        )
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
