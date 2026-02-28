"""
Webhook endpoints for Radarr/Sonarr integration.

Receives JSON POST payloads on media import, delays for Plex indexing,
debounces rapid imports, then triggers a job that processes only the
file path(s) from the payload(s) — no full-library scan.
"""

import os
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

# Debounce timers and payload batches keyed by source (radarr / sonarr).
_pending_timers: dict[str, threading.Timer] = {}
_pending_batches: dict[str, dict[str, object]] = {}
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


def _debounce_key(source: str) -> str:
    """Build a stable debounce key for source (Radarr vs Sonarr)."""
    return source


def _combine_path(base_path: str, relative_path: str) -> str:
    """Combine base and relative paths into a normalized absolute-ish path."""
    if not base_path or not relative_path:
        return ""
    return os.path.normpath(os.path.join(base_path, relative_path))


def _extract_radarr_file_path(payload: dict) -> str:
    """Extract a target file path from a Radarr Download webhook payload."""
    movie_file = payload.get("movieFile") or {}
    if movie_file.get("path"):
        return str(movie_file.get("path")).strip()

    combined = _combine_path(
        str((payload.get("movie") or {}).get("folderPath", "")).strip(),
        str(movie_file.get("relativePath", "")).strip(),
    )
    return combined.strip()


def _extract_sonarr_file_path(payload: dict) -> str:
    """Extract a target file path from a Sonarr Download webhook payload."""
    episode_file = payload.get("episodeFile") or {}
    if episode_file.get("path"):
        return str(episode_file.get("path")).strip()

    combined = _combine_path(
        str((payload.get("series") or {}).get("path", "")).strip(),
        str(episode_file.get("relativePath", "")).strip(),
    )
    return combined.strip()


def _schedule_webhook_job(source: str, title: str, file_path: str) -> bool:
    """Schedule a debounced single-file webhook job and batch paths per source."""
    if not file_path:
        logger.warning(
            f"Webhook: {source} Download for '{title}' ignored (missing file path)"
        )
        return False

    settings = get_settings_manager()
    delay = int(settings.get("webhook_delay", 60))
    debounce_key = _debounce_key(source)
    normalized_path = os.path.normpath(file_path)

    with _pending_lock:
        existing = _pending_timers.get(debounce_key)
        if existing:
            existing.cancel()

        batch = _pending_batches.get(debounce_key)
        if not batch:
            batch = {
                "source": source,
                "file_paths": set(),
            }
            _pending_batches[debounce_key] = batch
        batch["file_paths"].add(normalized_path)

        timer = threading.Timer(
            delay, _execute_webhook_job, args=[debounce_key]
        )
        timer.daemon = True
        _pending_timers[debounce_key] = timer
        timer.start()
        path_count = len(batch["file_paths"])

    logger.info(
        f"Webhook: {source} imported '{title}' — scheduling job with "
        f"{path_count} path(s) in {delay}s"
    )
    return True


def _execute_webhook_job(debounce_key: str) -> None:
    """Execute a debounced batch of webhook file paths."""
    from .routes import _start_job_async

    with _pending_lock:
        batch = _pending_batches.pop(debounce_key, None)
        _pending_timers.pop(debounce_key, None)

    if not batch:
        logger.warning(f"Webhook: no pending batch found for key '{debounce_key}'")
        return

    source = str(batch.get("source", "unknown"))
    webhook_paths = sorted(
        path for path in batch.get("file_paths", set()) if isinstance(path, str) and path
    )
    if not webhook_paths:
        logger.warning(
            f"Webhook: debounced batch for source '{source}' had no valid paths"
        )
        _add_history_entry(source, "Download", "", "ignored_no_paths")
        return

    job_manager = get_job_manager()
    job = job_manager.create_job(
        library_name=f"Webhook: {source.title()}",
        config={"source": source, "path_count": len(webhook_paths)},
    )

    _start_job_async(
        job.id,
        {
            "selected_libraries": [],
            "sort_by": "newest",
            "webhook_paths": webhook_paths,
        },
    )
    _add_history_entry(source, "Download", source, "triggered")


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
    movie_file_path = _extract_radarr_file_path(data)

    was_queued = _schedule_webhook_job("radarr", movie_title, movie_file_path)
    if not was_queued:
        _add_history_entry("radarr", "Download", movie_title, "ignored_no_path")
        return (
            jsonify(
                {
                    "success": True,
                    "message": f"Ignored '{movie_title}' download: no file path in payload",
                }
            ),
            200,
        )

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
    episode_file_path = _extract_sonarr_file_path(data)

    was_queued = _schedule_webhook_job("sonarr", series_title, episode_file_path)
    if not was_queued:
        _add_history_entry("sonarr", "Download", series_title, "ignored_no_path")
        return (
            jsonify(
                {
                    "success": True,
                    "message": f"Ignored '{series_title}' download: no file path in payload",
                }
            ),
            200,
        )

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
