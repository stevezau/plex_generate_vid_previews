"""
Scheduled scanning logic using Plex's recentlyAdded endpoint.

Periodically queries Plex for recently added items and processes them.
"""

import time
import threading
from typing import Callable, Optional, List, Tuple
from loguru import logger

from .plex_client import retry_plex_call, filter_duplicate_locations
from .config import Config


def get_recently_added_items(plex, config: Config, full_scan: bool = False) -> List[Tuple]:
    """
    Query Plex for recently added items.
    
    Args:
        plex: Plex server instance
        config: Configuration object
        full_scan: If True, query all items instead of just recently added
        
    Returns:
        List of tuples (section, media_items) for each library
    """
    if full_scan:
        # Use existing get_library_sections for full scan
        from .plex_client import get_library_sections
        return list(get_library_sections(plex, config))
    
    # Query recentlyAdded endpoint
    logger.info("Querying Plex for recently added items...")
    start_time = time.time()
    
    try:
        # Try to use plexapi's recentlyAdded if available
        # Otherwise, use raw query
        try:
            # Check if plexapi has recentlyAdded method on library
            if hasattr(plex.library, 'recentlyAdded'):
                recently_added = retry_plex_call(plex.library.recentlyAdded)
            else:
                # Fall back to raw query
                recently_added = retry_plex_call(plex.query, '/library/recentlyAdded')
        except AttributeError:
            # Fall back to raw query
            recently_added = retry_plex_call(plex.query, '/library/recentlyAdded')
        
        # Parse the results
        sections = {}
        media_items_by_section = {}
        
        # Get all sections first to map items to sections
        all_sections = retry_plex_call(plex.library.sections)
        section_map = {s.key: s for s in all_sections}
        
        # Filter sections based on config
        if config.plex_libraries:
            section_map = {
                k: v for k, v in section_map.items()
                if v.title.lower() in [lib.lower() for lib in config.plex_libraries]
            }
        
        # Parse recently added items
        for video in recently_added.findall('.//Video'):
            rating_key = video.get('ratingKey')
            if not rating_key:
                continue
            
            # Get section key from video
            library_section_key = video.get('librarySectionKey')
            if not library_section_key or library_section_key not in section_map:
                continue
            
            section = section_map[library_section_key]
            
            # Skip unsupported library types
            if section.METADATA_TYPE not in ('movie', 'episode'):
                continue
            
            # Extract item key
            item_key = video.get('key')
            if not item_key:
                continue
            
            # Get media title and type
            if section.METADATA_TYPE == 'episode':
                show_title = video.get('grandparentTitle', 'Unknown')
                season_episode = video.get('index', '')  # Episode number
                season_num = video.get('parentIndex', '')  # Season number
                if season_num and season_episode:
                    formatted_title = f"{show_title} S{season_num:02d}E{season_episode:02d}"
                else:
                    formatted_title = f"{show_title} {video.get('title', 'Unknown Episode')}"
                media_type = 'episode'
                
                # Get locations for duplicate filtering
                locations = []
                for media in video.findall('.//Media'):
                    for part in media.findall('.//Part'):
                        file_path = part.get('file')
                        if file_path:
                            locations.append(file_path)
                
                if section.key not in media_items_by_section:
                    media_items_by_section[section.key] = []
                    sections[section.key] = section
                
                media_items_by_section[section.key].append((item_key, locations, formatted_title, media_type))
                
            elif section.METADATA_TYPE == 'movie':
                title = video.get('title', 'Unknown')
                media_type = 'movie'
                
                if section.key not in media_items_by_section:
                    media_items_by_section[section.key] = []
                    sections[section.key] = section
                
                # For movies, we don't need locations, just key, title, type
                media_items_by_section[section.key].append((item_key, title, media_type))
        
        # Process results and filter duplicates for episodes
        results = []
        for section_key, section in sections.items():
            items = media_items_by_section.get(section_key, [])
            
            if section.METADATA_TYPE == 'episode':
                # Filter duplicates by location
                filtered_items = filter_duplicate_locations(items)
            else:
                # Movies - items are already in format (key, title, type)
                filtered_items = items
            
            if filtered_items:
                results.append((section, filtered_items))
        
        query_time = time.time() - start_time
        total_items = sum(len(items) for _, items in results)
        logger.info(f"Found {total_items} recently added items in {len(results)} library section(s) in {query_time:.2f} seconds")
        
        return results
        
    except Exception as e:
        logger.error(f"Failed to query recently added items: {e}")
        logger.debug(f"Exception type: {type(e).__name__}", exc_info=True)
        
        # Fall back to full library scan if recentlyAdded fails
        logger.warning("Falling back to full library scan...")
        from .plex_client import get_library_sections
        return list(get_library_sections(plex, config))


class Scheduler:
    """
    Scheduler that periodically queries Plex for recently added items.
    """
    
    def __init__(self, plex, config: Config, callback: Callable, full_scan: bool = False):
        """
        Initialize scheduler.
        
        Args:
            plex: Plex server instance
            config: Configuration object
            callback: Callback function to process items (section, media_items)
            full_scan: If True, process entire library instead of just new items
        """
        self.plex = plex
        self.config = config
        self.callback = callback
        self.full_scan = full_scan
        self.running = False
        self.thread = None
        self.scan_interval = config.scan_interval * 60  # Convert minutes to seconds
        self._lock = threading.Lock()
    
    def _run_scheduler(self):
        """Run the scheduler in a separate thread."""
        logger.info(f"Scheduler started (scan interval: {self.config.scan_interval} minutes, full_scan: {self.full_scan})")
        
        while self.running:
            try:
                # Query for recently added items (or full scan)
                results = get_recently_added_items(self.plex, self.config, self.full_scan)
                
                # Process each section
                for section, media_items in results:
                    if not media_items:
                        continue
                    
                    logger.info(f"Processing {len(media_items)} items from library '{section.title}'")
                    
                    # Call callback to process items
                    self.callback(section, media_items)
                
                # Wait for next scan interval
                logger.info(f"Next scan in {self.config.scan_interval} minutes...")
                
                # Sleep in small increments to allow graceful shutdown
                sleep_time = 0
                while sleep_time < self.scan_interval and self.running:
                    time.sleep(min(10, self.scan_interval - sleep_time))
                    sleep_time += 10
                
            except Exception as e:
                if not self.running:
                    break
                
                logger.error(f"Scheduler error: {e}")
                logger.debug(f"Exception type: {type(e).__name__}", exc_info=True)
                
                # Wait before retrying
                logger.info(f"Retrying in {self.config.scan_interval} minutes...")
                sleep_time = 0
                while sleep_time < self.scan_interval and self.running:
                    time.sleep(min(10, self.scan_interval - sleep_time))
                    sleep_time += 10
    
    def start(self):
        """Start the scheduler in a background thread."""
        with self._lock:
            if self.running:
                logger.warning("Scheduler is already running")
                return
            
            self.running = True
            self.thread = threading.Thread(target=self._run_scheduler, daemon=True)
            self.thread.start()
            logger.info("Scheduler started in background thread")
    
    def stop(self):
        """Stop the scheduler gracefully."""
        with self._lock:
            if not self.running:
                return
            
            logger.info("Stopping scheduler...")
            self.running = False
            
            # Wait for thread to finish (with timeout)
            if self.thread and self.thread.is_alive():
                self.thread.join(timeout=10)
                if self.thread.is_alive():
                    logger.warning("Scheduler thread did not stop within timeout")
            
            logger.info("Scheduler stopped")
    
    def is_running(self) -> bool:
        """Check if scheduler is running."""
        return self.running and (self.thread is not None and self.thread.is_alive())

