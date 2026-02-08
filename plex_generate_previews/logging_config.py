"""
Logging configuration for Plex Video Preview Generator.

Centralized logging setup to avoid circular imports and provide
consistent logging configuration across the application.
"""

import os
import sys

from loguru import logger
from rich.console import Console


def setup_logging(log_level: str = 'INFO', console: Console = None) -> None:
    """
    Set up logging configuration with shared Rich console.
    
    Args:
        log_level: Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        console: Rich Console instance for coordinated output (optional)
    """
    logger.remove()
    
    if console:
        # Use provided console for coordinated output with progress bars
        logger.add(
            lambda msg: console.print(msg, end=''),
            level=log_level,
            format='<green>{time:YYYY/MM/DD HH:mm:ss}</green> | {level.icon}  - <level>{message}</level>',
            enqueue=True
        )
    else:
        # Fallback to stderr for simple logging
        logger.add(
            sys.stderr,
            level=log_level,
            format='<green>{time:YYYY/MM/DD HH:mm:ss}</green> | {level.icon}  - <level>{message}</level>',
            colorize=True,
            enqueue=True
        )
    
    # Add persistent error log file
    log_dir = os.path.join(os.environ.get('CONFIG_DIR', '/config'), 'logs')
    try:
        os.makedirs(log_dir, exist_ok=True)
        error_log_path = os.path.join(log_dir, 'error.log')
        logger.add(
            error_log_path,
            level='ERROR',
            format='{time:YYYY/MM/DD HH:mm:ss} | {level} | {name}:{function}:{line} | {message}',
            rotation='10 MB',
            retention='30 days',
            compression='gz',
            enqueue=True,
        )
    except (PermissionError, OSError):
        pass  # Skip file logging if config directory is not writable

