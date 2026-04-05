"""SocketIO event handlers for real-time job and log updates."""

from flask_socketio import disconnect, join_room, leave_room
from loguru import logger

from ..auth import is_authenticated

# Ordered list of log levels from most to least verbose
_LOG_LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
_VALID_LEVELS = set(_LOG_LEVELS)


def _join_rooms_for_level(min_level: str) -> None:
    """Join SocketIO rooms for *min_level* and every level above it."""
    idx = _LOG_LEVELS.index(min_level) if min_level in _VALID_LEVELS else 1
    for lvl in _LOG_LEVELS[idx:]:
        join_room(lvl)


def _leave_all_level_rooms() -> None:
    """Leave every log-level room."""
    for lvl in _LOG_LEVELS:
        leave_room(lvl)


def register_socketio_handlers(socketio) -> None:
    """Register SocketIO event handlers."""

    # ----- /jobs namespace -----

    @socketio.on("connect", namespace="/jobs")
    def handle_connect():
        """Handle client connection."""
        if not is_authenticated():
            disconnect()
            return False
        logger.debug("Client connected to jobs namespace")

    @socketio.on("disconnect", namespace="/jobs")
    def handle_disconnect():
        """Handle client disconnection."""
        logger.debug("Client disconnected from jobs namespace")

    @socketio.on("subscribe", namespace="/jobs")
    def handle_subscribe(data):
        """Subscribe to job updates."""
        if not is_authenticated():
            disconnect()
            return
        job_id = data.get("job_id")
        if job_id:
            join_room(job_id)
            logger.debug(f"Client subscribed to job {job_id}")

    @socketio.on("unsubscribe", namespace="/jobs")
    def handle_unsubscribe(data):
        """Unsubscribe from job updates."""
        if not is_authenticated():
            disconnect()
            return
        job_id = data.get("job_id")
        if job_id:
            leave_room(job_id)

    # ----- /logs namespace -----

    @socketio.on("connect", namespace="/logs")
    def handle_logs_connect():
        """Authenticate and subscribe to the server's configured log level by default."""
        if not is_authenticated():
            disconnect()
            return False
        from ..settings_manager import get_settings_manager

        default_level = get_settings_manager().get("log_level", "INFO").upper()
        if default_level not in _VALID_LEVELS:
            default_level = "INFO"
        _join_rooms_for_level(default_level)

    @socketio.on("disconnect", namespace="/logs")
    def handle_logs_disconnect():
        """Clean up on disconnect (rooms are auto-removed by Flask-SocketIO)."""
        pass

    @socketio.on("set_level", namespace="/logs")
    def handle_set_level(data):
        """Change the minimum log level the client receives."""
        if not is_authenticated():
            disconnect()
            return
        level = (data.get("level") or "INFO").upper()
        if level not in _VALID_LEVELS:
            level = "INFO"
        _leave_all_level_rooms()
        _join_rooms_for_level(level)
