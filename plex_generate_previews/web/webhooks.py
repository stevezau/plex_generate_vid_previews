"""
Webhook endpoints for Radarr/Sonarr integration.

Receives JSON POST payloads on media import, delays for Plex indexing,
debounces rapid imports, then triggers a library-scan job sorted by newest.
"""

import secrets
import threading
from collections import deque
from datetime import datetime, timezone
from functools import wraps

from flask import Blueprint, jsonify, request
from loguru import logger

from .auth import api_token_required, validate_token
from .jobs import get_job_manager
from .settings_manager import get_settings_manager

webhooks_bp = Blueprint("webhooks_bp", __name__, url_prefix="/api/webhooks")

# Debounce timers keyed by library name
_pending_timers: dict[str, threading.Timer] = {}
_pending_lock = threading.Lock()

# In-memory log of received webhook events (diagnostic/transient)
_webhook_history: deque = deque(maxlen=100)


def _authenticate_webhook(f):
    """Check X-Auth-Token / Authorization: Bearer against webhook_secret or app token."""

    @wraps(f)
    def decorated_function(*args, **kwargs):
        token = ""

        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]

        if not token:
            token = request.headers.get("X-Auth-Token", "")

        if not token:
            return jsonify({"error": "Authentication required"}), 401

        settings = get_settings_manager()
        webhook_secret = settings.get("webhook_secret", "")

        if webhook_secret and secrets.compare_digest(token, webhook_secret):
            return f(*args, **kwargs)

        if validate_token(token):
            return f(*args, **kwargs)

        return jsonify({"error": "Authentication required"}), 401

    return decorated_function


def _add_history_entry(source: str, event_type: str, title: str, status: str) -> None:
    """Append an event to the in-memory webhook history."""
    _webhook_history.append(
        {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "source": source,
            "event_type": event_type,
            "title": title,
            "status": status,
        }
    )


def _schedule_webhook_job(library_name: str, source: str, title: str) -> None:
    """Schedule a job after the configured delay, debouncing rapid imports."""
    settings = get_settings_manager()
    delay = int(settings.get("webhook_delay", 60))

    with _pending_lock:
        existing = _pending_timers.get(library_name)
        if existing:
            existing.cancel()

        timer = threading.Timer(
            delay, _execute_webhook_job, args=[library_name, source]
        )
        timer.daemon = True
        _pending_timers[library_name] = timer
        timer.start()

    logger.info(
        f"Webhook: {source} imported '{title}' — scheduling job for "
        f"'{library_name}' in {delay}s"
    )


def _execute_webhook_job(library_name: str, source: str) -> None:
    """Execute the actual job after the debounce delay."""
    from .routes import _start_job_async

    job_manager = get_job_manager()
    job = job_manager.create_job(
        library_name=f"Webhook: {library_name}",
        config={"source": source},
    )

    selected = [library_name] if library_name else []
    _start_job_async(job.id, {"selected_libraries": selected, "sort_by": "newest"})
    _add_history_entry(source, "Download", library_name, "triggered")

    with _pending_lock:
        _pending_timers.pop(library_name, None)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@webhooks_bp.route("/radarr", methods=["POST"])
@_authenticate_webhook
def radarr_webhook():
    """Receive Radarr webhook payloads."""
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"success": False, "error": "Invalid or missing JSON body"}), 400

    event_type = data.get("eventType", "")

    if event_type == "Test":
        _add_history_entry("radarr", "Test", "", "test")
        return jsonify(
            {"success": True, "message": "Radarr webhook configured successfully"}
        )

    settings = get_settings_manager()
    if not settings.get("webhook_enabled", True):
        _add_history_entry("radarr", event_type, "", "disabled")
        return jsonify({"success": True, "message": "Webhooks disabled"})

    if event_type != "Download":
        _add_history_entry("radarr", event_type, "", "ignored")
        return jsonify({"success": True, "message": f"Ignored event: {event_type}"})

    movie = data.get("movie", {})
    movie_title = movie.get("title", "Unknown")

    library_name = settings.get("webhook_radarr_library", "")
    _schedule_webhook_job(library_name, "radarr", movie_title)
    _add_history_entry("radarr", "Download", movie_title, "queued")

    return (
        jsonify({"success": True, "message": f"Processing queued for '{movie_title}'"}),
        202,
    )


@webhooks_bp.route("/sonarr", methods=["POST"])
@_authenticate_webhook
def sonarr_webhook():
    """Receive Sonarr webhook payloads."""
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"success": False, "error": "Invalid or missing JSON body"}), 400

    event_type = data.get("eventType", "")

    if event_type == "Test":
        _add_history_entry("sonarr", "Test", "", "test")
        return jsonify(
            {"success": True, "message": "Sonarr webhook configured successfully"}
        )

    settings = get_settings_manager()
    if not settings.get("webhook_enabled", True):
        _add_history_entry("sonarr", event_type, "", "disabled")
        return jsonify({"success": True, "message": "Webhooks disabled"})

    if event_type != "Download":
        _add_history_entry("sonarr", event_type, "", "ignored")
        return jsonify({"success": True, "message": f"Ignored event: {event_type}"})

    series = data.get("series", {})
    series_title = series.get("title", "Unknown")

    library_name = settings.get("webhook_sonarr_library", "")
    _schedule_webhook_job(library_name, "sonarr", series_title)
    _add_history_entry("sonarr", "Download", series_title, "queued")

    return (
        jsonify(
            {"success": True, "message": f"Processing queued for '{series_title}'"}
        ),
        202,
    )


@webhooks_bp.route("/history")
@api_token_required
def get_webhook_history():
    """Return recent webhook events (newest first)."""
    return jsonify({"events": list(reversed(_webhook_history))})


@webhooks_bp.route("/history", methods=["DELETE"])
@api_token_required
def clear_webhook_history():
    """Clear all webhook history."""
    _webhook_history.clear()
    return jsonify({"success": True})
