"""SocketIO event handlers for real-time job updates."""

from flask_socketio import disconnect, join_room, leave_room
from loguru import logger

from ..auth import is_authenticated


def register_socketio_handlers(socketio):
    """Register SocketIO event handlers."""

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
