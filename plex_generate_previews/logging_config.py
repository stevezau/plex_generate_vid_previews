"""Logging configuration for Plex Video Preview Generator.

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
from typing import List, Optional

from loguru import logger
from rich.console import Console

# Handler IDs managed by setup_logging() — only these are removed on hot-reload
# so that per-job log sinks (added externally) are preserved.
_managed_handler_ids: List[int] = []
_initial_setup_done: bool = False
_handler_lock = threading.Lock()

# Log levels the SocketIO broadcaster will emit (filters out TRACE/SUCCESS)
_BROADCAST_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})


# Numeric level values for filtering in the history API (public — used by api_system)
LEVEL_ORDER = {
    "DEBUG": 10,
    "INFO": 20,
    "SUCCESS": 25,
    "WARNING": 30,
    "ERROR": 40,
    "CRITICAL": 50,
}


def _jsonl_record_patcher(record) -> bool:
    """Loguru filter that pre-computes a compact JSONL string for the app.log handler.

    Stores the serialized payload in ``record["extra"]["_jsonl"]`` so the
    format string ``{extra[_jsonl]}`` writes it directly to the file.

    Returns:
        True (always passes; level gating is handled by loguru's ``level=`` parameter).
    """
    payload = {
        "ts": record["time"].strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
        "level": record["level"].name,
        "msg": record["message"],
        "mod": record["name"].rsplit(".", 1)[-1] if record["name"] else "",
        "func": record["function"] or "",
        "line": record["line"],
    }
    record["extra"]["_jsonl"] = _json.dumps(payload, default=str)
    return True


def get_app_log_path() -> str:
    """Return the absolute path to the structured ``app.log`` file."""
    log_dir = os.path.join(os.environ.get("CONFIG_DIR", "/config"), "logs")
    return os.path.join(log_dir, "app.log")


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
    """Set up logging configuration.

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

        # Persistent structured log file — captures all levels in JSONL format
        # so the web UI can serve historical logs via /api/logs/history.
        log_dir = os.path.join(os.environ.get("CONFIG_DIR", "/config"), "logs")
        try:
            os.makedirs(log_dir, exist_ok=True)
            app_log_path = os.path.join(log_dir, "app.log")
            hid = logger.add(
                app_log_path,
                level=log_level,
                format="{extra[_jsonl]}",
                filter=_jsonl_record_patcher,
                rotation=rotation,
                retention=retention,
                compression="gz",
                enqueue=True,
            )
            _managed_handler_ids.append(hid)
        except (PermissionError, OSError) as e:
            logger.warning(f"Could not create log files: {e}")

        # Attach the SocketIO broadcaster if one has been registered.
        # Uses the configured log_level so clients can't see below the
        # server's minimum — the viewer's filter buttons are capped at this.
        broadcaster = get_log_broadcaster()
        if broadcaster is not None:
            hid = logger.add(
                broadcaster.sink,
                level=log_level,
                format="{message}",
                enqueue=True,
            )
            _managed_handler_ids.append(hid)


# ---------------------------------------------------------------------------
# SocketIO live-log broadcaster
# ---------------------------------------------------------------------------

_broadcaster: Optional["SocketIOLogBroadcaster"] = None


def get_log_broadcaster() -> Optional["SocketIOLogBroadcaster"]:
    """Return the registered broadcaster, or None."""
    return _broadcaster


def set_log_broadcaster(b: "SocketIOLogBroadcaster") -> None:
    """Register the broadcaster instance (called once during app startup)."""
    global _broadcaster
    _broadcaster = b


class SocketIOLogBroadcaster:
    """Loguru sink that broadcasts log records to SocketIO ``/logs`` clients.

    Each connected client joins SocketIO rooms named after log levels
    (``"DEBUG"``, ``"INFO"``, etc.). When a log record arrives, it is
    emitted to the room matching its level, so clients only receive
    messages at or above their chosen threshold.
    """

    def __init__(self, socketio) -> None:
        self._socketio = socketio

    def sink(self, message) -> None:
        """Loguru sink callable — serialize and emit the record."""
        record = message.record
        level_name = record["level"].name
        if level_name not in _BROADCAST_LEVELS:
            return

        payload = {
            "ts": record["time"].strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
            "level": level_name,
            "msg": record["message"],
            "mod": record["name"].rsplit(".", 1)[-1] if record["name"] else "",
            "func": record["function"] or "",
            "line": record["line"],
        }

        try:
            self._socketio.emit(
                "log_message",
                payload,
                namespace="/logs",
                room=level_name,
            )
        except Exception:
            pass
