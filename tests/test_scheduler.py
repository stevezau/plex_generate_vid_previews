"""
Tests for scheduler.py module.

Tests scheduled scanning functionality using recentlyAdded endpoint.
"""

import pytest
import time
from unittest.mock import MagicMock, patch, Mock
from loguru import logger

from plex_generate_previews.scheduler import Scheduler, get_recently_added_items
from plex_generate_previews.config import Config


class TestScheduler:
    """Test Scheduler class."""
    
    def test_scheduler_initialization(self, mock_config):
        """Test scheduler initialization."""
        mock_plex = MagicMock()
        callback = MagicMock()
        # Ensure scan_interval is an int, not a MagicMock
        if not isinstance(mock_config.scan_interval, int):
            mock_config.scan_interval = 60
        
        scheduler = Scheduler(mock_plex, mock_config, callback, full_scan=False)
        
        assert scheduler.plex == mock_plex
        assert scheduler.config == mock_config
        assert scheduler.callback == callback
        assert scheduler.full_scan is False
        assert scheduler.running is False
        assert isinstance(scheduler.scan_interval, int)
    
    def test_scheduler_start_stop(self, mock_config):
        """Test scheduler start and stop."""
        mock_plex = MagicMock()
        callback = MagicMock()
        # Ensure scan_interval is an int, not a MagicMock
        if not isinstance(mock_config.scan_interval, int):
            mock_config.scan_interval = 60
        
        scheduler = Scheduler(mock_plex, mock_config, callback, full_scan=False)
        
        scheduler.start()
        
        assert scheduler.running is True
        assert scheduler.thread is not None
        
        # Wait briefly for thread to start
        time.sleep(0.1)
        
        scheduler.stop()
        
        assert scheduler.running is False
    
    def test_scheduler_is_running(self, mock_config):
        """Test is_running check."""
        mock_plex = MagicMock()
        callback = MagicMock()
        # Ensure scan_interval is an int, not a MagicMock
        if not isinstance(mock_config.scan_interval, int):
            mock_config.scan_interval = 60
        
        scheduler = Scheduler(mock_plex, mock_config, callback, full_scan=False)
        
        assert scheduler.is_running() is False
        
        scheduler.start()
        time.sleep(0.1)  # Wait for thread to start
        assert scheduler.is_running() is True
        
        scheduler.stop()
        assert scheduler.is_running() is False


class TestGetRecentlyAddedItems:
    """Test get_recently_added_items function."""
    
    @patch('plex_generate_previews.scheduler.retry_plex_call')
    def test_get_recently_added_with_plexapi_method(self, mock_retry, mock_config):
        """Test querying recentlyAdded when plexapi has the method."""
        mock_plex = MagicMock()
        mock_plex.library.recentlyAdded = MagicMock()
        
        # Mock XML response
        mock_xml = MagicMock()
        mock_xml.findall.return_value = []
        mock_retry.return_value = mock_xml
        
        # Mock sections
        mock_section = MagicMock()
        mock_section.key = '1'
        mock_section.title = 'Movies'
        mock_section.METADATA_TYPE = 'movie'
        mock_plex.library.sections.return_value = [mock_section]
        
        result = get_recently_added_items(mock_plex, mock_config, full_scan=False)
        
        assert isinstance(result, list)
    
    @patch('plex_generate_previews.scheduler.retry_plex_call')
    def test_get_recently_added_with_raw_query(self, mock_retry, mock_config):
        """Test querying recentlyAdded using raw query when plexapi doesn't have method."""
        mock_plex = MagicMock()
        # Remove recentlyAdded method
        del mock_plex.library.recentlyAdded
        
        # Mock XML response
        mock_xml = MagicMock()
        mock_xml.findall.return_value = []
        mock_retry.return_value = mock_xml
        
        # Mock sections
        mock_section = MagicMock()
        mock_section.key = '1'
        mock_section.title = 'Movies'
        mock_section.METADATA_TYPE = 'movie'
        mock_plex.library.sections.return_value = [mock_section]
        
        result = get_recently_added_items(mock_plex, mock_config, full_scan=False)
        
        assert isinstance(result, list)
    
    @patch('plex_generate_previews.plex_client.get_library_sections')
    def test_get_recently_added_full_scan(self, mock_get_sections, mock_config):
        """Test full scan mode."""
        mock_plex = MagicMock()
        mock_section = MagicMock()
        mock_get_sections.return_value = [(mock_section, [])]
        
        result = get_recently_added_items(mock_plex, mock_config, full_scan=True)
        
        assert isinstance(result, list)
        assert len(result) == 1
        mock_get_sections.assert_called_once_with(mock_plex, mock_config)

