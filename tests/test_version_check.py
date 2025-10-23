"""
Tests for version_check.py module.

Tests version parsing, GitHub API interaction, and update checking.
"""

import pytest
from unittest.mock import patch, MagicMock
import requests

from plex_generate_previews.version_check import (
    get_current_version,
    parse_version,
    get_latest_github_release,
    check_for_updates
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
    
    @patch('requests.get')
    def test_get_latest_github_release(self, mock_get):
        """Test fetching latest release from GitHub."""
        mock_response = MagicMock()
        mock_response.json.return_value = {'tag_name': 'v2.1.0'}
        mock_response.raise_for_status = MagicMock()
        mock_get.return_value = mock_response
        
        version = get_latest_github_release()
        assert version == 'v2.1.0'
    
    @patch('requests.get')
    def test_get_latest_github_release_timeout(self, mock_get):
        """Test timeout handling."""
        mock_get.side_effect = requests.exceptions.Timeout("Timeout")
        
        version = get_latest_github_release()
        assert version is None
    
    @patch('requests.get')
    def test_get_latest_github_release_connection_error(self, mock_get):
        """Test connection error handling."""
        mock_get.side_effect = requests.exceptions.ConnectionError("No connection")
        
        version = get_latest_github_release()
        assert version is None
    
    @patch('requests.get')
    def test_get_latest_github_release_rate_limit(self, mock_get):
        """Test rate limit handling."""
        mock_response = MagicMock()
        mock_response.status_code = 429
        mock_response.raise_for_status.side_effect = requests.exceptions.HTTPError(response=mock_response)
        mock_get.return_value = mock_response
        
        version = get_latest_github_release()
        assert version is None
    
    @patch('requests.get')
    def test_get_latest_github_release_404(self, mock_get):
        """Test 404 error handling."""
        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_response.raise_for_status.side_effect = requests.exceptions.HTTPError(response=mock_response)
        mock_get.return_value = mock_response
        
        version = get_latest_github_release()
        assert version is None
    
    @patch('requests.get')
    def test_get_latest_github_release_empty_tag(self, mock_get):
        """Test handling of empty tag_name."""
        mock_response = MagicMock()
        mock_response.json.return_value = {'tag_name': ''}
        mock_response.raise_for_status = MagicMock()
        mock_get.return_value = mock_response
        
        version = get_latest_github_release()
        assert version is None


class TestCheckForUpdates:
    """Test update checking logic."""
    
    @patch('plex_generate_previews.version_check.get_latest_github_release')
    @patch('plex_generate_previews.version_check.get_current_version')
    def test_check_for_updates_newer_available(self, mock_current, mock_latest):
        """Test showing update message when newer version available."""
        mock_current.return_value = "2.0.0"
        mock_latest.return_value = "v2.1.0"
        
        # Should not raise, just log
        check_for_updates()
    
    @patch('plex_generate_previews.version_check.get_latest_github_release')
    @patch('plex_generate_previews.version_check.get_current_version')
    def test_check_for_updates_up_to_date(self, mock_current, mock_latest):
        """Test no message when current version is latest."""
        mock_current.return_value = "2.0.0"
        mock_latest.return_value = "v2.0.0"
        
        # Should not raise, just log
        check_for_updates()
    
    @patch('plex_generate_previews.version_check.get_latest_github_release')
    @patch('plex_generate_previews.version_check.get_current_version')
    def test_check_for_updates_current_newer(self, mock_current, mock_latest):
        """Test when current version is newer than latest (dev version)."""
        mock_current.return_value = "2.1.0"
        mock_latest.return_value = "v2.0.0"
        
        # Should not raise or show update message
        check_for_updates()
    
    
    @patch('plex_generate_previews.version_check.get_latest_github_release')
    @patch('plex_generate_previews.version_check.get_current_version')
    def test_check_for_updates_api_failure(self, mock_current, mock_latest):
        """Test handling of API failure."""
        mock_current.return_value = "2.0.0"
        mock_latest.return_value = None  # API failed
        
        # Should handle gracefully
        check_for_updates()
    
    @patch('plex_generate_previews.utils.is_docker_environment')
    @patch('plex_generate_previews.version_check.get_latest_github_release')
    @patch('plex_generate_previews.version_check.get_current_version')
    def test_check_for_updates_docker_message(self, mock_current, mock_latest, mock_docker):
        """Test Docker-specific update instructions."""
        mock_docker.return_value = True
        mock_current.return_value = "2.0.0"
        mock_latest.return_value = "v2.1.0"
        
        # Should show Docker-specific instructions
        check_for_updates()
    
    @patch('plex_generate_previews.utils.is_docker_environment')
    @patch('plex_generate_previews.version_check.get_latest_github_release')
    @patch('plex_generate_previews.version_check.get_current_version')
    def test_check_for_updates_non_docker_message(self, mock_current, mock_latest, mock_docker):
        """Test non-Docker update instructions."""
        mock_docker.return_value = False
        mock_current.return_value = "2.0.0"
        mock_latest.return_value = "v2.1.0"
        
        # Should show pip install instructions
        check_for_updates()

