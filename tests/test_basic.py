"""
Basic functionality tests for plex_generate_previews.
"""

import pytest
import subprocess
import sys
from unittest.mock import patch, MagicMock


class TestBasicFunctionality:
    """Test basic functionality without complex mocking."""
    
    def test_package_imports(self):
        """Test that the package can be imported."""
        import plex_generate_previews
        import re
        assert hasattr(plex_generate_previews, '__version__')
        # Version should be in PEP 440 format (e.g., "2.0.0", "2.1.2.post0", "0.0.0+unknown", "2.3.1.dev5+g1234abc")
        # Pattern matches setuptools-scm generated versions
        version_pattern = r'^\d+\.\d+\.\d+(?:\.(?:post|dev)\d+)?(?:\+[a-zA-Z0-9.-]+)?$'
        assert re.match(version_pattern, plex_generate_previews.__version__), \
            f"Version '{plex_generate_previews.__version__}' doesn't match PEP 440 format"
    
    def test_cli_help(self):
        """Test that CLI help works."""
        result = subprocess.run([
            sys.executable, '-m', 'plex_generate_previews', '--help'
        ], capture_output=True, text=True)
        assert result.returncode == 0
        assert 'Generate video preview thumbnails' in result.stdout
    
    def test_list_gpus(self):
        """Test that GPU listing works."""
        result = subprocess.run([
            sys.executable, '-m', 'plex_generate_previews', '--list-gpus'
        ], capture_output=True, text=True)
        assert result.returncode == 0
        assert 'Detecting available GPUs' in result.stdout
    
    def test_invalid_config_returns_error(self):
        """Test that invalid configuration returns error."""
        result = subprocess.run([
            sys.executable, '-m', 'plex_generate_previews',
            '--plex-url', 'http://localhost:32400'
            # Missing required plex-token and plex-config-folder
        ], capture_output=True, text=True)
        # The app will try to connect to Plex and fail, which is expected
        # We just want to make sure it doesn't crash with a Python error
        assert result.returncode in [0, 1]  # Either success or expected failure
        # Check for various expected error messages
        expected_errors = [
            'Failed to connect to Plex server',
            'PLEX_TOKEN is required',
            'FFmpeg not found'
        ]
        output = result.stdout + result.stderr
        assert any(error in output for error in expected_errors), f"Expected one of {expected_errors} in output: {output}"


class TestConfigFunctions:
    """Test configuration functions directly."""
    
    def test_get_config_value_cli_precedence(self):
        """Test that CLI args take precedence over env vars."""
        from plex_generate_previews.config import get_config_value
        
        cli_args = MagicMock()
        cli_args.test_field = "cli_value"
        
        with patch.dict('os.environ', {'TEST_FIELD': 'env_value'}):
            result = get_config_value(cli_args, 'test_field', 'TEST_FIELD', 'default')
            assert result == "cli_value"
    
    def test_get_config_value_env_fallback(self):
        """Test that env vars are used when CLI args are None."""
        from plex_generate_previews.config import get_config_value
        
        cli_args = MagicMock()
        cli_args.test_field = None
        
        with patch.dict('os.environ', {'TEST_FIELD': 'env_value'}):
            result = get_config_value(cli_args, 'test_field', 'TEST_FIELD', 'default')
            assert result == "env_value"
    
    def test_get_config_value_default_fallback(self):
        """Test that defaults are used when neither CLI nor env are set."""
        from plex_generate_previews.config import get_config_value
        
        cli_args = MagicMock()
        cli_args.test_field = None
        
        with patch.dict('os.environ', {}, clear=True):
            result = get_config_value(cli_args, 'test_field', 'TEST_FIELD', 'default')
            assert result == "default"


class TestGPUDetection:
    """Test GPU detection functionality."""
    
    def test_format_gpu_info(self):
        """Test GPU info formatting."""
        from plex_generate_previews.gpu_detection import format_gpu_info
        
        # Test NVIDIA formatting
        nvidia_info = format_gpu_info('cuda', 0, 'NVIDIA GeForce RTX 3080')
        assert 'NVIDIA' in nvidia_info
        assert 'RTX 3080' in nvidia_info
        assert 'cuda' in nvidia_info.lower()
        
        # Test AMD formatting
        amd_info = format_gpu_info('vaapi', '/dev/dri/renderD128', 'AMD Radeon RX 6800 XT')
        assert 'AMD' in amd_info
        assert 'RX 6800 XT' in amd_info
        assert 'vaapi' in amd_info.lower()
    
    def test_ffmpeg_version_check(self):
        """Test FFmpeg version checking."""
        from plex_generate_previews.gpu_detection import _get_ffmpeg_version, _check_ffmpeg_version
        
        # Test version parsing
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="ffmpeg version 7.1.1-1ubuntu1.2 Copyright..."
            )
            version = _get_ffmpeg_version()
            assert version == (7, 1, 1)
        
        # Test version checking
        with patch('plex_generate_previews.gpu_detection._get_ffmpeg_version') as mock_get_version:
            mock_get_version.return_value = (7, 1, 0)
            assert _check_ffmpeg_version() is True
            
            mock_get_version.return_value = (6, 9, 0)
            assert _check_ffmpeg_version() is False


class TestCLIFunctions:
    """Test CLI functions."""
    
    def test_parse_arguments(self):
        """Test argument parsing."""
        from plex_generate_previews.cli import parse_arguments
        
        with patch('sys.argv', ['plex-generate-previews', '--plex-url', 'http://localhost:32400']):
            args = parse_arguments()
            assert args.plex_url == 'http://localhost:32400'
    
    def test_application_state(self):
        """Test application state management."""
        from plex_generate_previews.cli import ApplicationState
        
        state = ApplicationState()
        assert state.config is None
        assert state.console is not None
        
        # Test setting config
        config = MagicMock()
        state.set_config(config)
        assert state.config == config
        
        # Test cleanup without config
        state.cleanup()  # Should not raise any exceptions
