"""
Tests for utils.py module.

Tests title formatting, path sanitization, Docker detection,
and working directory setup.
"""

import os
import tempfile
import pytest
from unittest.mock import patch, MagicMock
from collections import namedtuple

from plex_generate_previews.utils import (
    calculate_title_width,
    format_display_title,
    sanitize_path,
    is_docker_environment,
    is_windows,
    setup_working_directory
)


class TestCalculateTitleWidth:
    """Test terminal width calculation for title display."""
    
    @patch('shutil.get_terminal_size')
    def test_calculate_title_width(self, mock_terminal_size):
        """Test basic title width calculation."""
        # Mock terminal with 120 columns
        TerminalSize = namedtuple('TerminalSize', ['columns', 'lines'])
        mock_terminal_size.return_value = TerminalSize(columns=120, lines=30)
        
        width = calculate_title_width()
        
        # Should return a reasonable width
        assert 20 <= width <= 50
    
    @patch('shutil.get_terminal_size')
    def test_calculate_title_width_small_terminal(self, mock_terminal_size):
        """Test minimum width is enforced."""
        # Mock very small terminal
        TerminalSize = namedtuple('TerminalSize', ['columns', 'lines'])
        mock_terminal_size.return_value = TerminalSize(columns=50, lines=24)
        
        width = calculate_title_width()
        
        # Should return at least 20
        assert width >= 20
    
    @patch('shutil.get_terminal_size')
    def test_calculate_title_width_large_terminal(self, mock_terminal_size):
        """Test maximum width is capped."""
        # Mock very large terminal
        TerminalSize = namedtuple('TerminalSize', ['columns', 'lines'])
        mock_terminal_size.return_value = TerminalSize(columns=300, lines=60)
        
        width = calculate_title_width()
        
        # Should not exceed 50
        assert width <= 50


class TestFormatDisplayTitle:
    """Test display title formatting."""
    
    def test_format_display_title_episode_short(self):
        """Test episode title that fits within width."""
        title = "Breaking Bad S01E01"
        result = format_display_title(title, 'episode', title_max_width=30)
        
        # Should not be truncated
        assert "Breaking Bad" in result
        assert "S01E01" in result
        # Should be padded to exact width
        assert len(result) == 30
    
    def test_format_display_title_episode_long(self):
        """Test episode title truncation."""
        title = "A Very Long Show Name That Exceeds The Width S01E01"
        result = format_display_title(title, 'episode', title_max_width=30)
        
        # Should preserve S01E01
        assert "S01E01" in result
        # Should be truncated
        assert "..." in result
        # Length should not exceed max
        assert len(result) <= 30
    
    def test_format_display_title_movie(self):
        """Test movie title formatting."""
        title = "The Shawshank Redemption"
        result = format_display_title(title, 'movie', title_max_width=30)
        
        # Should contain title
        assert "Shawshank" in result or title in result
        # Should be padded
        assert len(result) == 30
    
    def test_format_display_title_movie_long(self):
        """Test long movie title truncation."""
        title = "A Very Long Movie Title That Definitely Exceeds The Maximum Width"
        result = format_display_title(title, 'movie', title_max_width=30)
        
        # Should be truncated
        assert "..." in result
        # Should not exceed max
        assert len(result) <= 30
    
    def test_format_display_title_preserves_season_episode(self):
        """Test that season/episode is always preserved."""
        title = "Super Long Show Name That Goes On And On S05E12"
        result = format_display_title(title, 'episode', title_max_width=25)
        
        # Must preserve the season/episode
        assert "S05E12" in result


class TestSanitizePath:
    """Test path sanitization."""
    
    @patch('os.name', 'nt')
    def test_sanitize_path_windows(self):
        """Test Windows path conversion."""
        path = "/data/movies/test.mkv"
        result = sanitize_path(path)
        
        # Should convert to backslashes
        assert "\\" in result
        assert "/" not in result
    
    @patch('os.name', 'posix')
    def test_sanitize_path_unix(self):
        """Test Unix path remains unchanged."""
        path = "/data/movies/test.mkv"
        result = sanitize_path(path)
        
        # Should remain unchanged
        assert result == path
    
    @patch('os.name', 'nt')
    def test_sanitize_path_windows_mixed(self):
        """Test Windows handles mixed slashes."""
        path = "/data\\movies/test.mkv"
        result = sanitize_path(path)
        
        # All slashes should be backslashes
        assert "/" not in result
        assert "\\" in result


class TestIsWindows:
    """Test Windows platform detection."""
    
    @patch('os.name', 'nt')
    def test_is_windows_on_windows(self):
        """Test detection on Windows platform."""
        result = is_windows()
        assert result is True
    
    @patch('os.name', 'posix')
    def test_is_windows_on_posix(self):
        """Test detection on POSIX platform (Linux/macOS)."""
        result = is_windows()
        assert result is False


class TestIsDockerEnvironment:
    """Test Docker environment detection."""
    
    @patch('os.path.exists')
    def test_is_docker_environment_dockerenv(self, mock_exists):
        """Test detection via /.dockerenv file."""
        mock_exists.side_effect = lambda path: path == '/.dockerenv'
        
        result = is_docker_environment()
        assert result is True
    
    @patch('os.path.exists')
    def test_is_docker_environment_container_env(self, mock_exists):
        """Test detection via container env variable."""
        mock_exists.return_value = False
        
        with patch.dict('os.environ', {'container': 'docker'}):
            result = is_docker_environment()
            assert result is True
    
    @patch('os.path.exists')
    def test_is_docker_environment_docker_container_env(self, mock_exists):
        """Test detection via DOCKER_CONTAINER env variable."""
        mock_exists.return_value = False
        
        with patch.dict('os.environ', {'DOCKER_CONTAINER': 'true'}, clear=True):
            result = is_docker_environment()
            assert result is True
    
    @patch('os.path.exists')
    def test_is_docker_environment_hostname(self, mock_exists):
        """Test detection via hostname containing 'docker'."""
        mock_exists.return_value = False
        
        with patch.dict('os.environ', {'HOSTNAME': 'my-docker-container-123'}, clear=True):
            result = is_docker_environment()
            assert result is True
    
    @patch('os.path.exists')
    def test_is_docker_environment_not_docker(self, mock_exists):
        """Test non-Docker environment."""
        mock_exists.return_value = False
        
        with patch.dict('os.environ', {}, clear=True):
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
