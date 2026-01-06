"""
Tests for plex_watch.py module.

Tests Plex watch mode functionality, error handling, and reconnection logic.
"""

import pytest
import time
import threading
from unittest.mock import MagicMock, patch, Mock
from loguru import logger

from plex_generate_previews.plex_watch import AlertListenerWrapper, RECONNECT_DELAY, MAX_RECONNECT_ATTEMPTS
from plex_generate_previews.config import Config


class TestAlertListenerWrapper:
    """Test AlertListener wrapper."""
    
    def test_alert_listener_initialization(self, mock_config):
        """Test AlertListener wrapper initialization."""
        mock_plex = MagicMock()
        callback = MagicMock()
        
        wrapper = AlertListenerWrapper(mock_plex, callback, mock_config)
        
        assert wrapper.plex == mock_plex
        assert wrapper.callback == callback
        assert wrapper.config == mock_config
        assert wrapper.listener is None
        assert wrapper.running is False
        assert wrapper.reconnect_attempts == 0
    
    def test_handle_alert_dict_structure(self, mock_config):
        """Test handling alert with dict structure."""
        mock_plex = MagicMock()
        callback = MagicMock()
        
        wrapper = AlertListenerWrapper(mock_plex, callback, mock_config)
        
        # Test with dict structure (common case)
        alert_dict = {
            'type': 'library.new',
            'itemKey': '/library/metadata/12345'
        }
        
        wrapper._handle_alert(alert_dict)
        
        callback.assert_called_once_with('/library/metadata/12345', 'library.new')
    
    def test_handle_alert_object_structure(self, mock_config):
        """Test handling alert with object structure."""
        mock_plex = MagicMock()
        callback = MagicMock()
        
        wrapper = AlertListenerWrapper(mock_plex, callback, mock_config)
        
        # Test with object structure
        alert_obj = MagicMock()
        alert_obj.type = 'library.new'
        alert_obj.itemKey = '/library/metadata/12345'
        
        wrapper._handle_alert(alert_obj)
        
        callback.assert_called_once_with('/library/metadata/12345', 'library.new')
    
    def test_handle_alert_rating_key(self, mock_config):
        """Test handling alert with ratingKey instead of itemKey."""
        mock_plex = MagicMock()
        callback = MagicMock()
        
        wrapper = AlertListenerWrapper(mock_plex, callback, mock_config)
        
        # Test with ratingKey
        alert_dict = {
            'type': 'library.new',
            'ratingKey': '12345'
        }
        
        wrapper._handle_alert(alert_dict)
        
        callback.assert_called_once_with('/library/metadata/12345', 'library.new')
    
    def test_handle_alert_ignores_other_types(self, mock_config):
        """Test that alerts with other types are ignored."""
        mock_plex = MagicMock()
        callback = MagicMock()
        
        wrapper = AlertListenerWrapper(mock_plex, callback, mock_config)
        
        # Test with ignored alert type
        alert_dict = {
            'type': 'playback.stopped',
            'itemKey': '/library/metadata/12345'
        }
        
        wrapper._handle_alert(alert_dict)
        
        callback.assert_not_called()
    
    def test_handle_alert_media_scanner_finished(self, mock_config):
        """Test handling media.scanner.finished alert."""
        mock_plex = MagicMock()
        callback = MagicMock()
        
        wrapper = AlertListenerWrapper(mock_plex, callback, mock_config)
        
        alert_dict = {
            'type': 'media.scanner.finished',
            'itemKey': '/library/metadata/67890'
        }
        
        wrapper._handle_alert(alert_dict)
        
        # media.scanner.finished now triggers a check for recently added items
        # by passing None as item_key
        callback.assert_called_once_with(None, 'media.scanner.finished')
    
    @patch('plexapi.alert.AlertListener')
    def test_start_alert_listener(self, mock_alert_listener_class, mock_config):
        """Test starting AlertListener."""
        mock_plex = MagicMock()
        callback = MagicMock()
        mock_listener = MagicMock()
        # Make start block briefly then return (simulate connection)
        def mock_start():
            time.sleep(0.05)  # Simulate brief connection
        mock_listener.start = mock_start
        mock_alert_listener_class.return_value = mock_listener
        
        wrapper = AlertListenerWrapper(mock_plex, callback, mock_config)
        wrapper.start()
        
        # Wait a bit for thread to start
        time.sleep(0.15)
        
        assert wrapper.running is True
        assert wrapper.thread is not None
        
        wrapper.stop()
    
    def test_stop_alert_listener(self, mock_config):
        """Test stopping AlertListener."""
        mock_plex = MagicMock()
        callback = MagicMock()
        
        wrapper = AlertListenerWrapper(mock_plex, callback, mock_config)
        wrapper.running = True
        wrapper.listener = MagicMock()
        wrapper.thread = threading.Thread(target=lambda: time.sleep(0.1), daemon=True)
        wrapper.thread.start()
        
        wrapper.stop()
        
        assert wrapper.running is False
        wrapper.listener.stop.assert_called_once()
    
    def test_is_running(self, mock_config):
        """Test is_running check."""
        mock_plex = MagicMock()
        callback = MagicMock()
        
        wrapper = AlertListenerWrapper(mock_plex, callback, mock_config)
        
        assert wrapper.is_running() is False
        
        wrapper.running = True
        # Create a thread that will stay alive briefly
        wrapper.thread = threading.Thread(target=lambda: time.sleep(0.1), daemon=True)
        wrapper.thread.start()
        
        # Check immediately while thread is alive
        assert wrapper.is_running() is True
        
        wrapper.thread.join(timeout=0.2)
    
    @patch('plexapi.alert.AlertListener')
    def test_reconnection_on_failure(self, mock_alert_listener_class, mock_config):
        """Test reconnection logic on failure."""
        mock_plex = MagicMock()
        callback = MagicMock()
        
        # Make AlertListener raise exception on start
        mock_listener = MagicMock()
        mock_listener.start.side_effect = Exception("Connection failed")
        mock_alert_listener_class.return_value = mock_listener
        
        wrapper = AlertListenerWrapper(mock_plex, callback, mock_config)
        wrapper.start()
        
        # Wait a bit for first connection attempt and reconnection logic
        time.sleep(0.2)
        
        # Check that it's trying to reconnect
        assert wrapper.running is True
        
        # Stop immediately to avoid long reconnection delays
        wrapper.stop()
    
    def test_handle_alert_no_item_key(self, mock_config):
        """Test handling alert without item key."""
        mock_plex = MagicMock()
        callback = MagicMock()
        
        wrapper = AlertListenerWrapper(mock_plex, callback, mock_config)
        
        # Test with alert that has no item key
        alert_dict = {
            'type': 'library.new'
        }
        
        wrapper._handle_alert(alert_dict)
        
        callback.assert_not_called()

