"""
Tests for logging configuration.
"""
import os
from unittest.mock import patch, MagicMock
from plex_generate_previews.logging_config import setup_logging


class TestLoggingConfig:
    """Test logging configuration."""
    
    @patch('plex_generate_previews.logging_config.os.makedirs')
    @patch('plex_generate_previews.logging_config.logger')
    def test_setup_logging_default(self, mock_logger, mock_makedirs):
        """Test setup logging with default level."""
        setup_logging()
        
        # Should configure logger
        mock_logger.remove.assert_called()
        mock_logger.add.assert_called()
    
    @patch('plex_generate_previews.logging_config.os.makedirs')
    @patch('plex_generate_previews.logging_config.logger')
    def test_setup_logging_debug(self, mock_logger, mock_makedirs):
        """Test setup logging with DEBUG level."""
        setup_logging('DEBUG')
        
        mock_logger.remove.assert_called_once()
        mock_logger.add.assert_called()
    
    @patch('plex_generate_previews.logging_config.os.makedirs')
    @patch('plex_generate_previews.logging_config.logger')
    def test_setup_logging_with_console(self, mock_logger, mock_makedirs):
        """Test setup logging with console parameter."""
        mock_console = MagicMock()
        
        setup_logging('INFO', console=mock_console)
        
        mock_logger.remove.assert_called_once()
        mock_logger.add.assert_called()

    @patch('plex_generate_previews.logging_config.os.makedirs')
    @patch('plex_generate_previews.logging_config.logger')
    def test_setup_logging_adds_error_file_handler(self, mock_logger, mock_makedirs):
        """Test that setup_logging adds a persistent error log file handler."""
        setup_logging()
        
        # Should have 2 calls to add: stderr + error log file
        assert mock_logger.add.call_count == 2
        
        # Second call should be for the error log file
        error_call = mock_logger.add.call_args_list[1]
        assert error_call.kwargs.get('level') == 'ERROR'
        assert error_call.kwargs.get('rotation') == '10 MB'
        assert error_call.kwargs.get('retention') == '30 days'

    @patch('plex_generate_previews.logging_config.os.makedirs', side_effect=PermissionError)
    @patch('plex_generate_previews.logging_config.logger')
    def test_setup_logging_handles_permission_error(self, mock_logger, mock_makedirs):
        """Test that setup_logging handles permission errors for log directory."""
        setup_logging()
        
        # Should still add stderr handler but not error file handler
        mock_logger.remove.assert_called_once()
        assert mock_logger.add.call_count == 1

    def test_setup_logging_creates_error_log(self, tmp_path):
        """Test that setup_logging creates the error log file on disk."""
        from loguru import logger
        
        with patch.dict(os.environ, {'CONFIG_DIR': str(tmp_path)}):
            # Reset logger state
            logger.remove()
            setup_logging()
        
        # Log directory should have been created
        log_dir = str(tmp_path / 'logs')
        assert os.path.isdir(log_dir)
        
        # Clean up handlers we added
        logger.remove()

