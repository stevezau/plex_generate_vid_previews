"""
Tests for CLI functionality.
"""

import pytest
import sys
from unittest.mock import patch, MagicMock
from plex_generate_previews.cli import (
    parse_arguments,
    setup_logging,
    list_gpus,
    ApplicationState
)


class TestArgumentParsing:
    """Test command-line argument parsing."""
    
    def test_parse_arguments_basic(self):
        """Test basic argument parsing."""
        with patch('sys.argv', ['plex-generate-previews', '--plex-url', 'http://localhost:32400']):
            args = parse_arguments()
            assert args.plex_url == 'http://localhost:32400'
    
    def test_parse_arguments_help(self):
        """Test help argument."""
        with patch('sys.argv', ['plex-generate-previews', '--help']):
            with pytest.raises(SystemExit):
                parse_arguments()
    
    def test_parse_arguments_list_gpus(self):
        """Test list-gpus argument."""
        with patch('sys.argv', ['plex-generate-previews', '--list-gpus']):
            args = parse_arguments()
            assert args.list_gpus is True


class TestApplicationState:
    """Test application state management."""
    
    def test_application_state_init(self):
        """Test application state initialization."""
        state = ApplicationState()
        assert state.config is None
        assert state.console is not None
    
    def test_set_config(self):
        """Test setting configuration."""
        state = ApplicationState()
        config = MagicMock()
        state.set_config(config)
        assert state.config == config
    
    @patch('plex_generate_previews.cli.clear_directory')
    @patch('os.path.isdir')
    @patch('shutil.rmtree')
    def test_cleanup_with_config(self, mock_rmtree, mock_isdir, mock_clear_dir):
        """Test cleanup with configuration."""
        state = ApplicationState()
        config = MagicMock()
        config.working_tmp_folder = '/tmp/test/working'
        config.tmp_folder = '/tmp/test'
        config.tmp_folder_created_by_us = False
        state.set_config(config)
        
        mock_isdir.return_value = True
        
        state.cleanup()
        
        # Should clean up working folder first
        assert mock_rmtree.call_count == 1
        mock_rmtree.assert_any_call('/tmp/test/working')
        
        # Should clear tmp_folder contents (not delete since we didn't create it)
        mock_clear_dir.assert_called_once_with('/tmp/test')
    
    def test_cleanup_without_config(self):
        """Test cleanup without configuration."""
        state = ApplicationState()
        # Should not raise any exceptions
        state.cleanup()
    
    @patch('os.path.isdir')
    @patch('shutil.rmtree')
    def test_cleanup_with_created_folder(self, mock_rmtree, mock_isdir):
        """Test cleanup when we created the tmp folder."""
        state = ApplicationState()
        config = MagicMock()
        config.working_tmp_folder = '/tmp/test/working'
        config.tmp_folder = '/tmp/test'
        config.tmp_folder_created_by_us = True
        state.set_config(config)
        
        mock_isdir.return_value = True
        
        state.cleanup()
        
        # Should clean up both working folder and base tmp folder
        assert mock_rmtree.call_count == 2
        mock_rmtree.assert_any_call('/tmp/test/working')
        mock_rmtree.assert_any_call('/tmp/test')


class TestLogging:
    """Test logging setup."""
    
    @patch('plex_generate_previews.logging_config.logger')
    def test_setup_logging(self, mock_logger):
        """Test logging setup."""
        setup_logging('DEBUG')
        mock_logger.remove.assert_called_once()
        mock_logger.add.assert_called_once()


class TestGPUListing:
    """Test GPU listing functionality."""
    
    @patch('plex_generate_previews.cli.detect_all_gpus')
    @patch('plex_generate_previews.cli.logger')
    def test_list_gpus_no_gpus(self, mock_logger, mock_detect):
        """Test listing GPUs when none are detected."""
        mock_detect.return_value = []
        
        list_gpus()
        
        mock_logger.info.assert_any_call('❌ No GPUs detected')
        mock_logger.info.assert_any_call('💡 Use --cpu-threads to run with CPU-only processing')
    
    @patch('plex_generate_previews.cli.detect_all_gpus')
    @patch('plex_generate_previews.cli.format_gpu_info')
    @patch('plex_generate_previews.cli.logger')
    def test_list_gpus_with_gpus(self, mock_logger, mock_format, mock_detect):
        """Test listing GPUs when GPUs are detected."""
        mock_detect.return_value = [
            ('cuda', 0, {'name': 'NVIDIA GeForce RTX 3080'}),
            ('vaapi', '/dev/dri/renderD128', {'name': 'AMD Radeon RX 6800 XT'})
        ]
        mock_format.side_effect = ['NVIDIA GeForce RTX 3080 (CUDA)', 'AMD Radeon RX 6800 XT (VAAPI)']
        
        list_gpus()
        
        mock_logger.info.assert_any_call('✅ Found 2 GPU(s):')
        mock_logger.info.assert_any_call('  [0] NVIDIA GeForce RTX 3080 (CUDA)')
        mock_logger.info.assert_any_call('  [1] AMD Radeon RX 6800 XT (VAAPI)')
