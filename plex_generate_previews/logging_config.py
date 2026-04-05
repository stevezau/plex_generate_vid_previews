"""Centralised logging configuration.

Architecture
------------
:func:`setup_logging` is called **once** at startup (``create_app``) and
again on hot-reload when the user changes the log level in Settings.
Each invocation creates up to three loguru handlers — all at the same
configured level:

1. **Console** (stderr) — what ``docker logs`` shows.
2. **File** (``app.log``, JSONL) — persistent history read by
   ``GET /api/logs/history``.
3. **SocketIO broadcaster** — pushes live messages to the ``/logs``
   namespace so the web log viewer can stream in real time.

Only the handler IDs created by :func:`setup_logging` are tracked; per-job
log sinks added externally are left untouched during hot-reloads.

Set ``LOG_FORMAT=json`` (env var) or pass ``log_format="json"`` to emit
structured JSON on stderr for log-aggregation pipelines (ELK / Loki / etc.).
"""

import json as _json
import os
import sys
import threading
from typing import List, Optional

from loguru import logger
from rich.console import Console

# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

# Handler IDs managed by setup_logging() — only these are removed on
# hot-reload so that per-job log sinks (added externally) are preserved.
_managed_handler_ids: List[int] = []
_initial_setup_done: bool = False
_handler_lock = threading.Lock()

# Levels the SocketIO broadcaster will emit (excludes TRACE / SUCCESS).
_BROADCAST_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})

# Numeric level values for filtering in the history API.
LEVEL_ORDER = {
    "DEBUG": 10,
    "INFO": 20,
    "SUCCESS": 25,
    "WARNING": 30,
    "ERROR": 40,
    "CRITICAL": 50,
}

_CONSOLE_FORMAT = (
    "<green>{time:YYYY/MM/DD HH:mm:ss}</green> | "
    "{level.icon}  - <level>{message}</level>"
)

# ---------------------------------------------------------------------------
# Shared payload builder
# ---------------------------------------------------------------------------


def _compact_payload(record) -> dict:
    """Build the compact dict shared by the JSONL file handler and the
    SocketIO broadcaster.  Keys match what the log-viewer frontend expects.
    """
    return {
        "ts": record["time"].strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
        "level": record["level"].name,
        "msg": record["message"],
        "mod": record["name"].rsplit(".", 1)[-1] if record["name"] else "",
        "func": record["function"] or "",
        "line": record["line"],
    }


# ---------------------------------------------------------------------------
# Loguru helpers
# ---------------------------------------------------------------------------


def _jsonl_record_patcher(record) -> bool:
    """Loguru *filter* that pre-serialises a JSONL string into ``extra``.

    The file handler's format string ``{extra[_jsonl]}`` writes it directly.

    Returns:
        True always — level gating is done by loguru's ``level=`` parameter.
    """
    record["extra"]["_jsonl"] = _json.dumps(_compact_payload(record), default=str)
    return True


def _json_sink(message) -> None:
    """Loguru sink: one JSON object per record on *stderr*.

    Used when ``LOG_FORMAT=json``.  Includes worker-thread context
    (``worker_id``, ``gpu_index``, etc.) when present.
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
    extra = record.get("extra", {})
    for key in ("worker_id", "worker_type", "gpu_index", "media_title", "item_key"):
        if key in extra and extra[key] is not None:
            payload[key] = extra[key]
    sys.stderr.write(_json.dumps(payload, default=str) + "\n")
    sys.stderr.flush()


def get_app_log_path() -> str:
    """Return the absolute path to the structured ``app.log`` file."""
    log_dir = os.path.join(os.environ.get("CONFIG_DIR", "/config"), "logs")
    return os.path.join(log_dir, "app.log")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def setup_logging(
    log_level: str = "INFO",
    console: Console = None,
    log_format: str = None,
    rotation: str = "10 MB",
    retention: int=5,
) -> None:
    """Create (or replace) the managed loguru handlers.

    Called once at startup from ``create_app()`` and again whenever the
    user changes log settings via the web UI.

    Args:
        log_level: Minimum level for all three handlers.
        console: Rich Console for coordinated output (ignored when
            *log_format* is ``"json"``).
        log_format: ``"json"`` for structured JSON, ``"pretty"`` (default)
            for coloured output.  Falls back to ``LOG_FORMAT`` env var.
        rotation: Max file size before rotating (e.g. ``"10 MB"``).
        retention: Rotated files to keep (int) or duration (``"30 days"``).
    """
    if log_format is None:
        log_format = os.environ.get("LOG_FORMAT", "pretty").lower()

    global _managed_handler_ids, _initial_setup_done

    with _handler_lock:
        # On first call, strip loguru's default handler.
        # On subsequent calls, remove only *our* handlers so that per-job
        # log sinks added elsewhere are preserved.
        if not _initial_setup_done:
            logger.remove()
            _initial_setup_done = True
        else:
            for hid in _managed_handler_ids:
                try:
                    logger.remove(hid)
                except (ValueError, TypeError):
                    pass

        _managed_handler_ids = []

        # --- 1. Console (stderr) handler ---
        if log_format == "json":
            hid = logger.add(
                _json_sink,
                level=log_level,
                format="{message}",
                enqueue=True,
            )
        elif console:
            hid = logger.add(
                lambda msg: console.print(msg, end=""),
                level=log_level,
                format=_CONSOLE_FORMAT,
                enqueue=True,
            )
        else:
            hid = logger.add(
                sys.stderr,
                level=log_level,
                format=_CONSOLE_FORMAT,
                colorize=True,
                enqueue=True,
            )
        _managed_handler_ids.append(hid)

        # --- 2. Persistent JSONL file (app.log) ---
        log_dir = os.path.join(os.environ.get("CONFIG_DIR", "/config"), "logs")
        try:
            os.makedirs(log_dir, exist_ok=True)
            hid = logger.add(
                os.path.join(log_dir, "app.log"),
                level=log_level,
                format="{extra[_jsonl]}",
                filter=_jsonl_record_patcher,
                rotation=rotation,
                retention=retention,
                compression="gz",
                enqueue=True,
            )
            _managed_handler_ids.append(hid)
        except (PermissionError, OSError) as exc:
            logger.warning(f"Could not create log files: {exc}")

        # --- 3. SocketIO broadcaster (live log viewer) ---
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
    """Return the registered broadcaster, or *None*."""
    return _broadcaster


def set_log_broadcaster(b: "SocketIOLogBroadcaster") -> None:
    """Register the broadcaster (called once during ``create_app``)."""
    global _broadcaster
    _broadcaster = b


class SocketIOLogBroadcaster:
    """Loguru sink that pushes log records to SocketIO ``/logs`` clients.

    Each connected client joins rooms named after log levels
    (``"DEBUG"``, ``"INFO"``, …).  The record is emitted to the room
    matching its level so clients only receive messages at or above
    their chosen threshold.
    """

    def __init__(self, socketio) -> None:
        self._socketio = socketio

    def sink(self, message: str) -> None:
        """Loguru sink callable — build payload and emit."""
        record = message.record
        level_name = record["level"].name
        if level_name not in _BROADCAST_LEVELS:
            return

        try:
            self._socketio.emit(
                "log_message",
                _compact_payload(record),
                namespace="/logs",
                room=level_name,
            )
        except Exception:
            pass
