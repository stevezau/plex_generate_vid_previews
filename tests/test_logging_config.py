"""
Tests for logging configuration.
"""
import pytest
from unittest.mock import patch, MagicMock
from plex_generate_previews.logging_config import setup_logging


class TestLoggingConfig:
    """Test logging configuration."""
    
    @patch('plex_generate_previews.logging_config.logger')
    def test_setup_logging_default(self, mock_logger):
        """Test setup logging with default level."""
        setup_logging()
        
        # Should configure logger
        mock_logger.remove.assert_called()
        mock_logger.add.assert_called()
    
    @patch('plex_generate_previews.logging_config.logger')
    def test_setup_logging_debug(self, mock_logger):
        """Test setup logging with DEBUG level."""
        setup_logging('DEBUG')
        
        mock_logger.remove.assert_called_once()
        mock_logger.add.assert_called()
    
    @patch('plex_generate_previews.logging_config.logger')
    def test_setup_logging_with_console(self, mock_logger):
        """Test setup logging with console parameter."""
        mock_console = MagicMock()
        
        setup_logging('INFO', console=mock_console)
        
        mock_logger.remove.assert_called_once()
        mock_logger.add.assert_called()

