"""
Logging configuration for Plex Video Preview Generator.

Centralized logging setup to avoid circular imports and provide
consistent logging configuration across the application.
"""

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
        import sys
        logger.add(
            sys.stderr,
            level=log_level,
            format='<green>{time:YYYY/MM/DD HH:mm:ss}</green> | {level.icon}  - <level>{message}</level>',
            colorize=True,
            enqueue=True
        )

