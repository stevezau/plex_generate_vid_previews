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
import threading
from typing import List

from loguru import logger
from rich.console import Console

# Handler IDs managed by setup_logging() — only these are removed on hot-reload
# so that per-job log sinks (added externally) are preserved.
_managed_handler_ids: List[int] = []
_initial_setup_done: bool = False
_handler_lock = threading.Lock()


# ---------------------------------------------------------------------------
# JSON serialiser for structured logging
# ---------------------------------------------------------------------------


def _json_sink(message) -> None:
    """Loguru sink that writes one JSON object per log record to *stderr*.

    Fields emitted:
        timestamp, level, message, logger, function, line, module,
        exception (string, only when present),
        worker_id, worker_type, gpu_index, media_title, item_key
            (present when emitted from a worker thread via logger.bind()).
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
    # Include structured context bound via logger.bind() (worker threads)
    extra = record.get("extra", {})
    for key in ("worker_id", "worker_type", "gpu_index", "media_title", "item_key"):
        if key in extra and extra[key] is not None:
            payload[key] = extra[key]
    sys.stderr.write(_json.dumps(payload, default=str) + "\n")
    sys.stderr.flush()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def setup_logging(
    log_level: str = "INFO",
    console: Console = None,
    log_format: str = None,
    rotation: str = "10 MB",
    retention=5,
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
        rotation: Max size before rotating log files (e.g. "10 MB").
        retention: Number of rotated log files to keep (int) or a time
            string like ``"30 days"``.
    """
    if log_format is None:
        log_format = os.environ.get("LOG_FORMAT", "pretty").lower()

    global _managed_handler_ids, _initial_setup_done

    with _handler_lock:
        if not _initial_setup_done:
            # First call — remove loguru's default stderr handler (and any others)
            logger.remove()
            _initial_setup_done = True
        else:
            # Hot-reload — only remove handlers we previously added, preserving
            # externally added sinks (e.g. per-job log capture handlers).
            for hid in _managed_handler_ids:
                try:
                    logger.remove(hid)
                except (ValueError, TypeError):
                    pass

        _managed_handler_ids = []

        if log_format == "json":
            # Structured JSON — one object per line on stderr
            hid = logger.add(
                _json_sink,
                level=log_level,
                format="{message}",  # _json_sink handles its own formatting
                enqueue=True,
            )
            _managed_handler_ids.append(hid)
        elif console:
            # Use provided console for coordinated output with progress bars
            hid = logger.add(
                lambda msg: console.print(msg, end=""),
                level=log_level,
                format="<green>{time:YYYY/MM/DD HH:mm:ss}</green> | {level.icon}  - <level>{message}</level>",
                enqueue=True,
            )
            _managed_handler_ids.append(hid)
        else:
            # Fallback to stderr for simple logging
            hid = logger.add(
                sys.stderr,
                level=log_level,
                format="<green>{time:YYYY/MM/DD HH:mm:ss}</green> | {level.icon}  - <level>{message}</level>",
                colorize=True,
                enqueue=True,
            )
            _managed_handler_ids.append(hid)

        # Add persistent error log file (always plain text, regardless of format)
        log_dir = os.path.join(os.environ.get("CONFIG_DIR", "/config"), "logs")
        try:
            os.makedirs(log_dir, exist_ok=True)
            error_log_path = os.path.join(log_dir, "error.log")
            hid = logger.add(
                error_log_path,
                level="ERROR",
                format="{time:YYYY/MM/DD HH:mm:ss} | {level} | {name}:{function}:{line} | {message}",
                rotation=rotation,
                retention=retention,
                compression="gz",
                enqueue=True,
            )
            _managed_handler_ids.append(hid)

            # Activity log (WARNING+) — captures retries, fallbacks, and failures
            # for post-run diagnosis without requiring DEBUG verbosity
            activity_log_path = os.path.join(log_dir, "activity.log")
            hid = logger.add(
                activity_log_path,
                level="WARNING",
                format="{time:YYYY/MM/DD HH:mm:ss} | {level} | {name}:{function}:{line} | {message}",
                rotation=rotation,
                retention=retention,
                compression="gz",
                enqueue=True,
            )
            _managed_handler_ids.append(hid)
        except (PermissionError, OSError) as e:
            logger.warning(f"Could not create log files: {e}")
