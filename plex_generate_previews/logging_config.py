"""
Logging configuration for Plex Video Preview Generator.

Centralized logging setup to avoid circular imports and provide
consistent logging configuration across the application.

Set ``LOG_FORMAT=json`` (environment variable) or pass ``log_format="json"``
to :func:`setup_logging` to enable structured JSON logging — useful for
log-aggregation pipelines (ELK, Loki, Datadog, etc.).  The default
``"pretty"`` format uses Rich console colouring.
"""

import json as _json
import os
import sys

from loguru import logger
from rich.console import Console


# ---------------------------------------------------------------------------
# JSON serialiser for structured logging
# ---------------------------------------------------------------------------


def _json_sink(message) -> None:
    """Loguru sink that writes one JSON object per log record to *stderr*.

    Fields emitted:
        timestamp, level, message, logger, function, line, module,
        exception (string, only when present).
    """
    record = message.record
    payload = {
        "timestamp": record["time"].isoformat(),
        "level": record["level"].name,
        "message": record["message"],
        "logger": record["name"],
        "function": record["function"],
        "line": record["line"],
        "module": record["module"],
    }
    if record["exception"] is not None:
        payload["exception"] = str(record["exception"])
    sys.stderr.write(_json.dumps(payload, default=str) + "\n")
    sys.stderr.flush()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def setup_logging(
    log_level: str = "INFO",
    console: Console = None,
    log_format: str = None,
) -> None:
    """
    Set up logging configuration.

    Args:
        log_level: Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL).
        console: Rich Console instance for coordinated output (optional,
            ignored when *log_format* is ``"json"``).
        log_format: ``"json"`` for structured JSON output, ``"pretty"``
            (default) for human-readable Rich/coloured output.  Falls back
            to the ``LOG_FORMAT`` environment variable when *None*.
    """
    if log_format is None:
        log_format = os.environ.get("LOG_FORMAT", "pretty").lower()

    logger.remove()

    if log_format == "json":
        # Structured JSON — one object per line on stderr
        logger.add(
            _json_sink,
            level=log_level,
            format="{message}",  # _json_sink handles its own formatting
            enqueue=True,
        )
    elif console:
        # Use provided console for coordinated output with progress bars
        logger.add(
            lambda msg: console.print(msg, end=""),
            level=log_level,
            format="<green>{time:YYYY/MM/DD HH:mm:ss}</green> | {level.icon}  - <level>{message}</level>",
            enqueue=True,
        )
    else:
        # Fallback to stderr for simple logging
        logger.add(
            sys.stderr,
            level=log_level,
            format="<green>{time:YYYY/MM/DD HH:mm:ss}</green> | {level.icon}  - <level>{message}</level>",
            colorize=True,
            enqueue=True,
        )

    # Add persistent error log file (always plain text, regardless of format)
    log_dir = os.path.join(os.environ.get("CONFIG_DIR", "/config"), "logs")
    try:
        os.makedirs(log_dir, exist_ok=True)
        error_log_path = os.path.join(log_dir, "error.log")
        logger.add(
            error_log_path,
            level="ERROR",
            format="{time:YYYY/MM/DD HH:mm:ss} | {level} | {name}:{function}:{line} | {message}",
            rotation="10 MB",
            retention="30 days",
            compression="gz",
            enqueue=True,
        )
    except (PermissionError, OSError) as e:
        logger.warning(f"Could not create error log file: {e}")
