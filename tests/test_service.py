"""
Tests for service.py module.

Tests daemon service functionality and mode selection.
"""

import pytest
import time
from unittest.mock import MagicMock, patch, Mock
from loguru import logger

from plex_generate_previews.service import DaemonService, run_daemon_service
from plex_generate_previews.config import Config


class TestDaemonService:
    """Test DaemonService class."""
    
    def test_daemon_service_initialization(self, mock_config):
        """Test daemon service initialization."""
        selected_gpus = []
        
        service = DaemonService(mock_config, selected_gpus)
        
        assert service.config == mock_config
        assert service.selected_gpus == selected_gpus
        assert service.plex is None
        assert service.alert_listener is None
        assert service.scheduler is None
        assert service.running is False
    
    @patch('plex_generate_previews.service.plex_server')
    @patch('plex_generate_previews.service.WorkerPool')
    @patch('plex_generate_previews.service.AlertListenerWrapper')
    def test_start_realtime_mode(self, mock_alert_listener, mock_worker_pool, mock_plex_server, mock_config):
        """Test starting daemon service in watch mode."""
        mock_config.daemon_mode = 'watch'
        mock_plex = MagicMock()
        mock_plex_server.return_value = mock_plex
        
        mock_listener = MagicMock()
        mock_listener.is_running.return_value = True
        mock_alert_listener.return_value = mock_listener
        
        selected_gpus = []
        service = DaemonService(mock_config, selected_gpus)
        
        service.start()
        
        assert service.running is True
        assert service.alert_listener is not None
        mock_alert_listener.assert_called_once()
        mock_listener.start.assert_called_once()
        
        service.stop()
    
    @patch('plex_generate_previews.service.plex_server')
    @patch('plex_generate_previews.service.WorkerPool')
    @patch('plex_generate_previews.service.Scheduler')
    def test_start_scheduled_mode(self, mock_scheduler, mock_worker_pool, mock_plex_server, mock_config):
        """Test starting daemon service in scheduled mode."""
        mock_config.daemon_mode = 'scheduled'
        mock_plex = MagicMock()
        mock_plex_server.return_value = mock_plex
        
        mock_scheduler_instance = MagicMock()
        mock_scheduler.return_value = mock_scheduler_instance
        
        selected_gpus = []
        service = DaemonService(mock_config, selected_gpus)
        
        service.start()
        
        assert service.running is True
        assert service.scheduler is not None
        mock_scheduler.assert_called_once()
        mock_scheduler_instance.start.assert_called_once()
        
        service.stop()
    
    @patch('plex_generate_previews.service.plex_server')
    @patch('plex_generate_previews.service.WorkerPool')
    @patch('plex_generate_previews.service.AlertListenerWrapper')
    @patch('plex_generate_previews.service.Scheduler')
    def test_start_realtime_mode_success(self, mock_scheduler, mock_alert_listener, mock_worker_pool, mock_plex_server, mock_config):
        """Test starting daemon service in watch mode with AlertListener success."""
        mock_config.daemon_mode = 'watch'
        mock_plex = MagicMock()
        mock_plex_server.return_value = mock_plex
        
        mock_listener = MagicMock()
        mock_listener.is_running.return_value = True
        mock_alert_listener.return_value = mock_listener
        
        selected_gpus = []
        service = DaemonService(mock_config, selected_gpus)
        
        service.start()
        
        assert service.running is True
        assert service.alert_listener is not None
        assert service.scheduler is None
        mock_alert_listener.assert_called_once()
        mock_scheduler.assert_not_called()
        
        service.stop()
    
    @patch('plex_generate_previews.service.plex_server')
    @patch('plex_generate_previews.service.WorkerPool')
    @patch('plex_generate_previews.service.AlertListenerWrapper')
    def test_start_realtime_mode_fallback(self, mock_alert_listener, mock_worker_pool, mock_plex_server, mock_config):
        """Test starting daemon service in watch mode with AlertListener failure raises error (no fallback)."""
        mock_config.daemon_mode = 'watch'
        mock_plex = MagicMock()
        mock_plex_server.return_value = mock_plex
        
        mock_listener = MagicMock()
        mock_listener.is_running.return_value = False  # AlertListener fails
        mock_alert_listener.return_value = mock_listener
        
        selected_gpus = []
        service = DaemonService(mock_config, selected_gpus)
        
        # Should raise error instead of falling back
        with pytest.raises(RuntimeError, match="Watch mode failed to connect"):
            service.start()
        
        # Should not be running
        assert service.running is False
        assert service.scheduler is None
    
    def test_process_item_callback(self, mock_config):
        """Test processing item from callback."""
        selected_gpus = []
        service = DaemonService(mock_config, selected_gpus)
        service.plex = MagicMock()
        
        with patch('plex_generate_previews.service.process_item') as mock_process:
            service._process_item_callback('/library/metadata/12345', 'library.new')
            
            mock_process.assert_called_once()
    
    def test_stop_service(self, mock_config):
        """Test stopping daemon service."""
        selected_gpus = []
        service = DaemonService(mock_config, selected_gpus)
        service.running = True
        
        # Create mocks
        mock_alert_listener = MagicMock()
        mock_scheduler = MagicMock()
        mock_worker_pool = MagicMock()
        
        # Set them on the service
        service.alert_listener = mock_alert_listener
        service.scheduler = mock_scheduler
        service.worker_pool = mock_worker_pool
        
        service.stop()
        
        assert service.running is False
        mock_alert_listener.stop.assert_called_once()
        mock_scheduler.stop.assert_called_once()
        mock_worker_pool.shutdown.assert_called_once()

