"""
Utility functions for Plex Video Preview Generator.

Contains general-purpose utility functions that can be reused across
different modules in the application.
"""

import os
import shutil
import time
import uuid


def calculate_title_width():
    """
    Calculate optimal title width based on terminal size.
    
    Calculates the maximum number of characters that can be used for
    displaying media titles in the progress bars, accounting for all
    other UI elements.
    
    Returns:
        int: Maximum characters for title display (20-50 range)
    """
    terminal_width = shutil.get_terminal_size().columns
    
    worker_prefix = 7  # "GPU 0: " or "CPU 0: "
    percentage = 6     # " 100% "
    time_elapsed = 8   # " 00:00:00 "
    count_display = 12 # " (1/10) "
    speed_display = 8  # " 2.5x "
    progress_bar = 20  # Approximate progress bar width
    
    reserved_space = worker_prefix + percentage + time_elapsed + count_display + speed_display + progress_bar
    available_width = terminal_width - reserved_space
    
    # Set reasonable limits: minimum 20 chars, maximum 50 chars
    return max(min(available_width, 50), 20)


def format_display_title(title: str, media_type: str, title_max_width: int) -> str:
    """
    Format and truncate display title based on media type.
    
    Args:
        title: The media title to format
        media_type: 'episode' or 'movie'
        title_max_width: Maximum width for the title
        
    Returns:
        str: Formatted and truncated title
    """
    if media_type == 'episode':
        # For episodes, ensure S01E01 format is always visible
        if len(title) > title_max_width:
            # Simple truncation: keep last 6 chars (S01E01) + show title
            season_episode = title[-6:]  # Last 6 characters (S01E01)
            available_space = title_max_width - 6 - 3  # 6 for S01E01, 3 for "..."
            if available_space > 0:
                show_title = title[:-6].strip()  # Everything except last 6 chars
                if len(show_title) > available_space:
                    show_title = show_title[:available_space]
                display_title = f"{show_title}...{season_episode}"
            else:
                # Not enough space, just show the season/episode
                display_title = f"...{season_episode}"
        else:
            display_title = title
    else:
        # For movies, use the title as-is
        display_title = title
        
        # Regular truncation for movies
        if len(display_title) > title_max_width:
            display_title = display_title[:title_max_width-3] + "..."  # Leave room for "..."
    
    # Add padding to prevent progress bar jumping (only if not already truncated)
    if len(display_title) <= title_max_width:
        padding_needed = title_max_width - len(display_title)
        display_title = display_title + " " * padding_needed
    
    return display_title


def is_docker_environment() -> bool:
    """Check if running inside a Docker container."""
    return (
        os.path.exists('/.dockerenv') or 
        os.environ.get('container') == 'docker' or
        os.environ.get('DOCKER_CONTAINER') == 'true' or
        'docker' in os.environ.get('HOSTNAME', '').lower()
    )


def is_windows() -> bool:
    """Check if running on Windows operating system."""
    return os.name == 'nt'


def is_macos() -> bool:
    """Check if running on macOS operating system."""
    import platform
    return platform.system() == 'Darwin'


def sanitize_path(path: str) -> str:
    """
    Sanitize file path for cross-platform compatibility.
    
    On Windows:
    - Converts forward slashes to backslashes
    - Handles UNC paths (\\\\server\\share)
    - Normalizes path separators
    
    On Linux/macOS:
    - Returns path as-is with normalization
    
    Args:
        path: The file path to sanitize
        
    Returns:
        str: Sanitized file path
    """
    if os.name == 'nt':
        # Handle UNC paths: //server/share -> \\server\share
        if path.startswith('//'):
            path = '\\\\' + path[2:].replace('/', '\\')
        else:
            path = path.replace('/', '\\')
    
    # Normalize path (removes redundant separators and up-level references)
    return os.path.normpath(path)


def setup_working_directory(tmp_folder: str) -> str:
    """
    Create and set up a unique working temporary directory.
    
    Args:
        tmp_folder: Base temporary folder path
        
    Returns:
        str: Path to the created working directory
        
    Raises:
        OSError: If directory creation fails
    """
    # Create a unique subfolder for this run to avoid conflicts
    unique_id = f"plex_previews_{int(time.time())}_{str(uuid.uuid4())[:8]}"
    working_tmp_folder = os.path.join(tmp_folder, unique_id)
    
    # Create our specific working directory
    os.makedirs(working_tmp_folder, exist_ok=True)
    
    return working_tmp_folder


