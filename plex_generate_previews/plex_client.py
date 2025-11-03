"""
Plex Media Server client and API interactions.

Handles Plex server connection, XML parsing monkey patch for debugging,
library querying, and duplicate location filtering.
"""

import os
import time
import http.client
import xml.etree.ElementTree
import requests
import urllib3
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from loguru import logger

# Disable SSL warnings
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from .config import Config


def retry_plex_call(func, *args, max_retries=3, retry_delay=1.0, **kwargs):
    """
    Retry a Plex API call if it fails due to XML parsing errors.
    
    This handles cases where Plex returns incomplete XML due to being busy.
    
    Args:
        func: Function to call
        *args: Positional arguments for the function
        max_retries: Maximum number of retries (default: 3)
        retry_delay: Delay between retries in seconds (default: 1.0)
        **kwargs: Keyword arguments for the function
        
    Returns:
        Result of the function call
        
    Raises:
        Exception: If all retries fail
    """
    last_exception = None
    
    for attempt in range(max_retries + 1):
        try:
            return func(*args, **kwargs)
        except xml.etree.ElementTree.ParseError as e:
            last_exception = e
            if attempt < max_retries:
                logger.warning(f"XML parsing error on attempt {attempt + 1}/{max_retries + 1}: {e}")
                logger.info(f"Retrying in {retry_delay} seconds... (Plex may be busy)")
                time.sleep(retry_delay)
                retry_delay *= 1.5  # Exponential backoff
            else:
                logger.error(f"XML parsing failed after {max_retries + 1} attempts: {e}")
        except Exception as e:
            # For non-XML errors, don't retry
            raise e
    
    # If we get here, all retries failed
    raise last_exception


def plex_server(config: Config):
    """
    Create Plex server connection with retry strategy and XML debugging.
    
    Args:
        config: Configuration object
        
    Returns:
        PlexServer: Configured Plex server instance
        
    Raises:
        ConnectionError: If unable to connect to Plex server
        requests.exceptions.RequestException: If connection fails after retries
    """
    # Plex Interface with retry strategy
    retry_strategy = Retry(
        total=3,
        backoff_factor=0.3,
        status_forcelist=[500, 502, 503, 504],
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session = requests.Session()
    session.verify = False
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    
    # Create Plex server instance with proper error handling
    from plexapi.server import PlexServer
    try:
        logger.info(f"Connecting to Plex server at {config.plex_url}...")
        plex = PlexServer(config.plex_url, config.plex_token, timeout=config.plex_timeout, session=session)
        logger.info("Successfully connected to Plex server")
        return plex
    except (requests.exceptions.ConnectionError, requests.exceptions.ConnectTimeout, 
            requests.exceptions.ReadTimeout, requests.exceptions.RequestException) as e:
        logger.error(f"Failed to connect to Plex server at {config.plex_url}")
        logger.error(f"Connection error: {e}")
        logger.error("Please check:")
        logger.error("  - Plex server is running and accessible")
        logger.error("  - Plex URL is correct (including http:// or https://)")
        logger.error("  - Network connectivity to Plex server")
        logger.error("  - Firewall settings allow connections to port 32400")
        raise ConnectionError(f"Unable to connect to Plex server at {config.plex_url}: {e}") from e


def filter_duplicate_locations(media_items):
    """
    Filter out duplicate media items based on file locations.
    
    This function prevents processing the same video file multiple times
    when it appears in multiple episodes (common with multi-part episodes).
    It keeps the first occurrence and skips subsequent duplicates.
    
    Args:
        media_items: List of tuples (key, locations, title, media_type)
    
    Returns:
        list: Filtered list of tuples (key, title, media_type) without duplicates
    """
    seen_locations = set()
    filtered_items = []
    
    for key, locations, title, media_type in media_items:            
        # Check if any location has been seen before
        if any(location in seen_locations for location in locations):
            continue
            
        # Add all locations to seen set and keep this item
        seen_locations.update(locations)
        filtered_items.append((key, title, media_type))  # Return tuple with key, title, and media_type
    
    return filtered_items


def get_library_sections(plex, config: Config):
    """
    Get all library sections from Plex server.
    
    Args:
        plex: Plex server instance
        config: Configuration object
        
    Yields:
        tuple: (section, media_items) for each library
    """
    import time
    
    # Step 1: Get all library sections (1 API call)
    logger.info("Getting all Plex library sections...")
    start_time = time.time()
    
    try:
        sections = retry_plex_call(plex.library.sections)
    except (requests.exceptions.RequestException, http.client.BadStatusLine, xml.etree.ElementTree.ParseError) as e:
        logger.error(f"Failed to get Plex library sections after retries: {e}")
        logger.error(f"Exception type: {type(e).__name__}")
        logger.error("Cannot proceed without library access. Please check your Plex server status.")
        return
    
    sections_time = time.time() - start_time
    logger.info(f"Retrieved {len(sections)} library sections in {sections_time:.2f} seconds")
    
    # Step 2: Filter and process each library
    for section in sections:
        # Skip libraries that aren't in the PLEX_LIBRARIES list if it's not empty
        if config.plex_libraries and section.title.lower() not in config.plex_libraries:
            logger.info('Skipping library \'{}\' as it\'s not in the configured libraries list'.format(section.title))
            continue

        logger.info('Getting media files from library \'{}\'...'.format(section.title))
        library_start_time = time.time()

        try:
            if section.METADATA_TYPE == 'episode':
                # Get episodes with locations for duplicate filtering
                search_results = retry_plex_call(section.search, libtype='episode')
                media_with_locations = []
                for m in search_results:
                    # Format episode title as "Show Title S01E01"
                    show_title = m.grandparentTitle
                    season_episode = m.seasonEpisode.upper()
                    formatted_title = f"{show_title} {season_episode}"
                    media_with_locations.append((m.key, m.locations, formatted_title, 'episode'))
                # Filter out multi episode files based on file locations
                media = filter_duplicate_locations(media_with_locations)
            elif section.METADATA_TYPE == 'movie':
                search_results = retry_plex_call(section.search)
                media = [(m.key, m.title, 'movie') for m in search_results]
            else:
                logger.info('Skipping library {} as \'{}\' is unsupported'.format(section.title, section.METADATA_TYPE))
                continue
        except (requests.exceptions.RequestException, http.client.BadStatusLine, xml.etree.ElementTree.ParseError) as e:
            logger.error(f"Failed to search library '{section.title}' after retries: {e}")
            logger.error(f"Exception type: {type(e).__name__}")
            logger.warning(f"Skipping library '{section.title}' due to error")
            continue

        library_time = time.time() - library_start_time
        logger.info('Retrieved {} media files from library \'{}\' in {:.2f} seconds'.format(len(media), section.title, library_time))
        yield section, media
