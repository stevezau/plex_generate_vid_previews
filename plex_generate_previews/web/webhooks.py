"""Webhook endpoints for Radarr/Sonarr/Custom integration.

Receives JSON POST payloads on media import, delays for Plex indexing,
debounces rapid imports, then triggers a job that processes only the
file path(s) from the payload(s) — no full-library scan.
"""

import base64
import json
import os
import secrets
import threading
from collections import deque
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path

from flask import Blueprint, jsonify, request
from loguru import logger

from .auth import api_token_required, validate_token
from .jobs import get_job_manager
from .settings_manager import get_settings_manager

webhooks_bp = Blueprint("webhooks_bp", __name__, url_prefix="/api/webhooks")

# Debounce timers and payload batches keyed by source (radarr / sonarr / custom).
_pending_timers: dict[str, threading.Timer] = {}
_pending_batches: dict[str, dict[str, object]] = {}
_pending_lock = threading.Lock()

# In-memory log of received webhook events, persisted to disk on each write.
_HISTORY_MAX = 100
_webhook_history: deque = deque(maxlen=_HISTORY_MAX)
_history_lock = threading.Lock()


def _history_file_path() -> Path:
    """Resolve the persistent webhook history file inside the config directory."""
    config_dir = os.environ.get("CONFIG_DIR", "/config")
    return Path(config_dir) / "webhook_history.json"


def _load_history_from_disk() -> None:
    """Load webhook history from the persistent JSON file on startup.

    Silently no-ops if the file is missing or corrupt — the in-memory deque
    will simply start empty.
    """
    path = _history_file_path()
    if not path.exists():
        return
    try:
        with open(path, "r") as f:
            entries = json.load(f)
        if isinstance(entries, list):
            with _history_lock:
                _webhook_history.clear()
                _webhook_history.extend(entries[-_HISTORY_MAX:])
            logger.debug(
                "Loaded {} webhook history entries from {}", len(_webhook_history), path
            )
    except Exception as exc:
        logger.warning("Failed to load webhook history from {}: {}", path, exc)


def _save_history_to_disk() -> None:
    """Persist the current webhook history deque to disk.

    Best-effort — failures are logged but never propagated.
    """
    try:
        from ..utils import atomic_json_save

        with _history_lock:
            snapshot = list(_webhook_history)
        atomic_json_save(str(_history_file_path()), snapshot)
    except Exception as exc:
        logger.debug("Failed to persist webhook history: {}", exc)


def _authenticate_webhook(f):
    """Check X-Auth-Token, Authorization Bearer, or Basic auth password as token.

    Collects candidate tokens from all sources and tries each against the
    webhook secret and app auth token.  ``X-Auth-Token`` is checked first
    because it is the dedicated webhook header; ``Authorization`` (Bearer /
    Basic) is a fallback.  This prevents browser-injected JWTs (e.g. from
    Tdarr's session) from shadowing the explicit webhook token.
    """

    @wraps(f)
    def decorated_function(*args, **kwargs):
        candidates: list[tuple[str, str]] = []

        x_token = request.headers.get("X-Auth-Token", "").strip()
        if x_token:
            candidates.append(("X-Auth-Token", x_token))

        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            bearer = auth_header[7:].strip()
            if bearer:
                candidates.append(("Bearer", bearer))
        elif auth_header.startswith("Basic "):
            try:
                decoded = base64.b64decode(auth_header[6:].encode()).decode(
                    "utf-8", errors="replace"
                )
                if ":" in decoded:
                    _, basic_pw = decoded.split(":", 1)
                    if basic_pw:
                        candidates.append(("Basic", basic_pw))
            except (ValueError, UnicodeDecodeError):
                logger.debug("Failed to decode Basic auth header")

        if not candidates:
            logger.warning(
                "Webhook: authentication failed (no token provided) — "
                "Remote={}, Path={}, Method={}",
                request.remote_addr,
                request.path,
                request.method,
            )
            return jsonify({"error": "Authentication required"}), 401

        settings = get_settings_manager()
        webhook_secret = settings.get("webhook_secret", "")

        for _method, token in candidates:
            if webhook_secret and secrets.compare_digest(token, webhook_secret):
                return f(*args, **kwargs)
            if validate_token(token):
                return f(*args, **kwargs)

        logger.warning(
            "Webhook: authentication failed (invalid token via {}) — "
            "Remote={}, Path={}",
            candidates[0][0],
            request.remote_addr,
            request.path,
        )
        return jsonify({"error": "Authentication required"}), 401

    return decorated_function


# Max basenames to store per history entry (matches job config cap for UI consistency)
_HISTORY_FILES_PREVIEW_CAP = 20


def _add_history_entry(
    source: str,
    event_type: str,
    title: str,
    status: str,
    *,
    job_id: str | None = None,
    path_count: int | None = None,
    files_preview: list[str] | None = None,
) -> None:
    """Append an event to the webhook history and persist to disk.

    Optional batch metadata (job_id, path_count, files_preview) is included
    for triggered debounced batches so the UI can show which files were in the batch.
    """
    entry: dict[str, object] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "event_type": event_type,
        "title": title,
        "status": status,
    }
    if job_id is not None:
        entry["job_id"] = job_id
    if path_count is not None:
        entry["path_count"] = path_count
    if files_preview is not None:
        entry["files_preview"] = files_preview[:_HISTORY_FILES_PREVIEW_CAP]
    with _history_lock:
        _webhook_history.append(entry)
    _save_history_to_disk()


def _summarize_payload(data: dict, max_depth: int = 2) -> dict | str:
    """Return a shallow summary of a webhook payload for diagnostic logging.

    Replaces leaf values with their type/length to avoid leaking sensitive data
    while still showing the payload structure. Returns a string placeholder for
    non-dict inputs or when max_depth is exhausted.
    """
    if max_depth <= 0 or not isinstance(data, dict):
        return f"<{type(data).__name__}>"

    summary = {}
    for key, value in list(data.items())[:30]:
        if isinstance(value, dict):
            summary[key] = _summarize_payload(value, max_depth - 1)
        elif isinstance(value, list):
            summary[key] = f"<list[{len(value)}]>"
        elif isinstance(value, str):
            summary[key] = value if len(value) <= 120 else f"{value[:120]}…"
        else:
            summary[key] = repr(value)
    return summary


def _debounce_key(source: str) -> str:
    """Build a stable debounce key for source (radarr / sonarr / custom)."""
    return source


def _as_dict(value: object) -> dict:
    """Return value if dict-like, otherwise an empty dict."""
    return value if isinstance(value, dict) else {}


def _combine_path(base_path: str, relative_path: str) -> str:
    """Combine base and relative paths into a normalized absolute-ish path."""
    if not base_path or not relative_path:
        return ""
    return os.path.normpath(os.path.join(base_path, relative_path))


def _extract_radarr_file_path(payload: dict) -> str:
    """Extract a target file path from a Radarr Download webhook payload."""
    movie_file = _as_dict(payload.get("movieFile"))
    if movie_file.get("path"):
        return str(movie_file.get("path")).strip()

    combined = _combine_path(
        str(_as_dict(payload.get("movie")).get("folderPath", "")).strip(),
        str(movie_file.get("relativePath", "")).strip(),
    )
    return combined.strip()


def _extract_sonarr_file_path(payload: dict) -> str:
    """Extract a target file path from a Sonarr Download webhook payload."""
    episode_file = _as_dict(payload.get("episodeFile"))
    if episode_file.get("path"):
        return str(episode_file.get("path")).strip()

    combined = _combine_path(
        str(_as_dict(payload.get("series")).get("path", "")).strip(),
        str(episode_file.get("relativePath", "")).strip(),
    )
    return combined.strip()


def _format_sonarr_episode_title(series_title: str, episodes: object) -> str:
    """Build display title for Sonarr episode(s): 'Show Name S01E05' or 'Show Name S01E05, S01E06'.

    Args:
        series_title: Series name from payload.
        episodes: List of episode dicts with seasonNumber/episodeNumber, or non-list for series only.

    Returns:
        Series title with SxxExx suffix when episode data is present.

    """
    if not series_title:
        series_title = "Unknown"
    episode_list = episodes if isinstance(episodes, list) and episodes else []
    if not episode_list:
        return series_title.strip()

    parts = []
    for ep in episode_list:
        ep_dict = _as_dict(ep)
        s = ep_dict.get("seasonNumber")
        e = ep_dict.get("episodeNumber")
        if s is not None and e is not None:
            try:
                parts.append(f"S{int(s):02d}E{int(e):02d}")
            except (TypeError, ValueError):
                pass
    if not parts:
        return series_title.strip()
    return f"{series_title.strip()} {', '.join(parts)}"


def _schedule_webhook_job(source: str, title: str, file_path: str) -> bool:
    """Schedule a debounced single-file webhook job and batch paths per source."""
    safe_source = str(source or "unknown")
    safe_title = str(title or "Unknown")
    normalized_input_path = str(file_path or "").strip()
    if not normalized_input_path:
        logger.warning(
            f"Webhook: {safe_source} Download for '{safe_title}' ignored (missing file path)"
        )
        return False

    settings = get_settings_manager()
    delay = int(settings.get("webhook_delay", 60))
    debounce_key = _debounce_key(safe_source)
    normalized_path = os.path.normpath(normalized_input_path)

    with _pending_lock:
        existing = _pending_timers.get(debounce_key)
        if existing:
            existing.cancel()

        batch = _pending_batches.get(debounce_key)
        if not batch:
            batch = {
                "source": source,
                "file_paths": set(),
                "titles": [],
            }
            _pending_batches[debounce_key] = batch
        batch["file_paths"].add(normalized_path)
        batch["titles"].append(safe_title)

        fire_at = datetime.now(timezone.utc).timestamp() + delay
        batch["fire_at"] = fire_at

        timer = threading.Timer(delay, _execute_webhook_job, args=[debounce_key])
        timer.daemon = True
        _pending_timers[debounce_key] = timer
        timer.start()
        path_count = len(batch["file_paths"])

    logger.info(
        f"Webhook: {safe_source} imported '{safe_title}' — scheduling job with "
        f"{path_count} path(s) in {delay}s"
    )
    return True


def _execute_webhook_job(debounce_key: str) -> None:
    """Execute a debounced batch of webhook file paths.

    Runs inside a threading.Timer callback, so all exceptions must be caught
    here — unhandled errors would be silently swallowed by the thread.
    """
    from .routes import _start_job_async

    with _pending_lock:
        batch = _pending_batches.pop(debounce_key, None)
        _pending_timers.pop(debounce_key, None)

    if not batch:
        logger.warning(f"Webhook: no pending batch found for key '{debounce_key}'")
        return

    source = str(batch.get("source", "unknown"))
    batch_titles = batch.get("titles") or []

    try:
        webhook_paths = sorted(
            path
            for path in batch.get("file_paths", set())
            if isinstance(path, str) and path
        )
        if not webhook_paths:
            logger.warning(
                f"Webhook: debounced batch for source '{source}' had no valid paths"
            )
            _add_history_entry(source, "Download", "", "ignored_no_paths")
            return

        basenames = [os.path.basename(p) for p in webhook_paths]
        first_title = batch_titles[0] if batch_titles else None
        if len(webhook_paths) == 1 and first_title:
            library_display = f"{source.title()}: {first_title}"
        elif len(webhook_paths) == 1:
            library_display = f"{source.title()}: {basenames[0]}"
        else:
            library_display = f"{source.title()}: {len(webhook_paths)} files"

        job_manager = get_job_manager()
        job = job_manager.create_job(
            library_name=library_display,
            config={
                "source": source,
                "path_count": len(webhook_paths),
                "webhook_basenames": basenames[:20],
            },
        )
        settings = get_settings_manager()
        selected_libraries = settings.get("selected_libraries", [])
        if not isinstance(selected_libraries, list):
            selected_libraries = []
        selected_libraries = [
            str(name).strip() for name in selected_libraries if str(name).strip()
        ]
        retry_count = max(0, min(10, int(settings.get("webhook_retry_count", 3))))
        retry_delay = max(10, min(300, int(settings.get("webhook_retry_delay", 30))))

        _start_job_async(
            job.id,
            {
                "selected_libraries": selected_libraries,
                "sort_by": "newest",
                "webhook_paths": webhook_paths,
                "webhook_retry_count": retry_count,
                "webhook_retry_delay": retry_delay,
            },
        )
        _add_history_entry(
            source,
            "Download",
            first_title or source,
            "triggered",
            job_id=job.id,
            path_count=len(webhook_paths),
            files_preview=basenames,
        )
    except Exception:
        title_label = batch_titles[0] if batch_titles else source
        logger.exception(
            "Webhook: failed to execute debounced job for source '{}' ({})",
            source,
            title_label,
        )
        _add_history_entry(source, "Download", title_label, "error")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@webhooks_bp.route("/radarr", methods=["POST"])
@_authenticate_webhook
def radarr_webhook():
    """Receive Radarr webhook payloads."""
    data = request.get_json(force=True, silent=True)
    if not data:
        logger.warning(
            "Webhook: Radarr request ignored (invalid or missing JSON body) "
            "— Host={}, Content-Type={}, Content-Length={}, Remote={}",
            request.host,
            request.content_type,
            request.content_length,
            request.remote_addr,
        )
        return jsonify({"success": False, "error": "Invalid or missing JSON body"}), 400

    event_type = str(data.get("eventType", "")).strip()

    if event_type == "Test":
        _add_history_entry("radarr", "Test", "", "test")
        return jsonify(
            {"success": True, "message": "Radarr webhook configured successfully"}
        )

    settings = get_settings_manager()
    if not settings.get("webhook_enabled", True):
        _add_history_entry("radarr", event_type, "", "disabled")
        logger.info(f"Webhook: Radarr event '{event_type}' ignored (webhooks disabled)")
        return jsonify({"success": True, "message": "Webhooks disabled"})

    if event_type != "Download":
        _add_history_entry("radarr", event_type, "", "ignored")
        logger.info(f"Webhook: Radarr event '{event_type}' ignored")
        return jsonify({"success": True, "message": f"Ignored event: {event_type}"})

    movie = _as_dict(data.get("movie"))
    movie_title = str(movie.get("title", "Unknown")).strip() or "Unknown"
    movie_file_path = _extract_radarr_file_path(data)

    was_queued = _schedule_webhook_job("radarr", movie_title, movie_file_path)
    if not was_queued:
        logger.debug(
            "Webhook: Radarr payload had no extractable file path. "
            "Structure: {}\nFull payload: {}",
            _summarize_payload(data),
            json.dumps(data, default=str, ensure_ascii=False),
        )
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


def _handle_sonarr_compatible_webhook(source: str):
    """Shared handler for Sonarr-compatible webhook payloads (Sonarr, Sportarr).

    Args:
        source: Identifier for history/debounce (e.g. ``"sonarr"``, ``"sportarr"``).

    Returns:
        Flask response tuple.
    """
    label = source.title()
    data = request.get_json(force=True, silent=True)
    if not data:
        logger.warning(
            "Webhook: {} request ignored (invalid or missing JSON body) "
            "— Host={}, Content-Type={}, Content-Length={}, Remote={}",
            label,
            request.host,
            request.content_type,
            request.content_length,
            request.remote_addr,
        )
        return jsonify({"success": False, "error": "Invalid or missing JSON body"}), 400

    event_type = str(data.get("eventType", "")).strip()

    if event_type == "Test":
        _add_history_entry(source, "Test", "", "test")
        return jsonify(
            {"success": True, "message": f"{label} webhook configured successfully"}
        )

    settings = get_settings_manager()
    if not settings.get("webhook_enabled", True):
        _add_history_entry(source, event_type, "", "disabled")
        logger.info(
            f"Webhook: {label} event '{event_type}' ignored (webhooks disabled)"
        )
        return jsonify({"success": True, "message": "Webhooks disabled"})

    if event_type not in ("Download", "OnDownload"):
        _add_history_entry(source, event_type, "", "ignored")
        logger.info(f"Webhook: {label} event '{event_type}' ignored")
        return jsonify({"success": True, "message": f"Ignored event: {event_type}"})

    series = _as_dict(data.get("series"))
    series_title = str(series.get("title", "Unknown")).strip() or "Unknown"
    display_title = _format_sonarr_episode_title(series_title, data.get("episodes"))
    episode_file_path = _extract_sonarr_file_path(data)

    was_queued = _schedule_webhook_job(source, display_title, episode_file_path)
    if not was_queued:
        logger.debug(
            "Webhook: {} payload had no extractable file path. "
            "Structure: {}\nFull payload: {}",
            label,
            _summarize_payload(data),
            json.dumps(data, default=str, ensure_ascii=False),
        )
        _add_history_entry(source, "Download", display_title, "ignored_no_path")
        return (
            jsonify(
                {
                    "success": True,
                    "message": f"Ignored '{display_title}' download: no file path in payload",
                }
            ),
            200,
        )

    _add_history_entry(source, "Download", display_title, "queued")

    return (
        jsonify(
            {"success": True, "message": f"Processing queued for '{display_title}'"}
        ),
        202,
    )


@webhooks_bp.route("/sonarr", methods=["POST"])
@_authenticate_webhook
def sonarr_webhook():
    """Receive Sonarr webhook payloads."""
    return _handle_sonarr_compatible_webhook("sonarr")


@webhooks_bp.route("/sportarr", methods=["POST"])
@_authenticate_webhook
def sportarr_webhook():
    """Receive Sportarr webhook payloads (Sonarr-compatible format)."""
    return _handle_sonarr_compatible_webhook("sportarr")


@webhooks_bp.route("/custom", methods=["POST"])
@_authenticate_webhook
def custom_webhook():
    """Receive custom webhook payloads (e.g. from Tdarr, scripts, or other tools).

    Expected JSON body:
        file_path  (str):        Single file path to process.
        file_paths (list[str]):  Multiple file paths to process.
        title      (str, opt):   Display label for history/jobs (defaults to first basename).
        eventType  (str, opt):   Set to "Test" to verify connectivity without processing.

    At least one of ``file_path`` or ``file_paths`` is required (unless eventType is "Test").
    """
    data = request.get_json(force=True, silent=True)
    if not data:
        logger.warning(
            "Webhook: Custom request ignored (invalid or missing JSON body) "
            "— Host={}, Content-Type={}, Content-Length={}, Remote={}",
            request.host,
            request.content_type,
            request.content_length,
            request.remote_addr,
        )
        return jsonify({"success": False, "error": "Invalid or missing JSON body"}), 400

    event_type = str(data.get("eventType", "")).strip()

    if event_type == "Test":
        _add_history_entry("custom", "Test", "", "test")
        return jsonify(
            {"success": True, "message": "Custom webhook configured successfully"}
        )

    settings = get_settings_manager()
    if not settings.get("webhook_enabled", True):
        _add_history_entry("custom", event_type or "Custom", "", "disabled")
        logger.info("Webhook: Custom event ignored (webhooks disabled)")
        return jsonify({"success": True, "message": "Webhooks disabled"})

    paths = _extract_custom_paths(data)
    if not paths:
        logger.debug(
            "Webhook: Custom payload had no extractable file path. "
            "Structure: {}\nFull payload: {}",
            _summarize_payload(data),
            json.dumps(data, default=str, ensure_ascii=False),
        )
        _add_history_entry("custom", "Custom", "", "ignored_no_path")
        return (
            jsonify(
                {
                    "success": False,
                    "error": "Payload must include 'file_path' (string) or 'file_paths' (array of strings)",
                }
            ),
            400,
        )

    title = str(data.get("title", "")).strip() or os.path.basename(paths[0])

    for path in paths:
        _schedule_webhook_job("custom", title, path)

    _add_history_entry("custom", "Custom", title, "queued")

    noun = "file" if len(paths) == 1 else "files"
    return (
        jsonify(
            {
                "success": True,
                "message": f"Processing queued for {len(paths)} {noun}",
            }
        ),
        202,
    )


def _extract_custom_paths(data: dict) -> list[str]:
    """Extract file paths from a custom webhook payload.

    Accepts ``file_path`` (single string) or ``file_paths`` (list of strings).
    Returns a de-duplicated list of non-empty, normalized paths.
    """
    raw_paths: list[str] = []

    single = data.get("file_path")
    if isinstance(single, str) and single.strip():
        raw_paths.append(single.strip())

    multi = data.get("file_paths")
    if isinstance(multi, list):
        for item in multi:
            if isinstance(item, str) and item.strip():
                raw_paths.append(item.strip())

    seen: set[str] = set()
    unique: list[str] = []
    for p in raw_paths:
        normalized = os.path.normpath(p)
        if normalized not in seen:
            seen.add(normalized)
            unique.append(normalized)
    return unique


@webhooks_bp.route("/history")
@api_token_required
def get_webhook_history():
    """Return recent webhook events (newest first)."""
    with _history_lock:
        events = list(reversed(_webhook_history))
    return jsonify({"events": events})


@webhooks_bp.route("/history", methods=["DELETE"])
@api_token_required
def clear_webhook_history():
    """Clear all webhook history (memory and disk)."""
    with _history_lock:
        _webhook_history.clear()
    _save_history_to_disk()
    return jsonify({"success": True})


@webhooks_bp.route("/pending")
@api_token_required
def get_pending_webhooks():
    """Return currently pending (debouncing) webhook batches with countdown info."""
    now = datetime.now(timezone.utc).timestamp()
    pending = []
    with _pending_lock:
        for key, batch in _pending_batches.items():
            fire_at = batch.get("fire_at", 0)
            remaining = max(0, fire_at - now)
            titles = batch.get("titles", [])
            pending.append(
                {
                    "source": batch.get("source", key),
                    "file_count": len(batch.get("file_paths", set())),
                    "first_title": titles[0] if titles else "",
                    "fire_at": datetime.fromtimestamp(
                        fire_at, tz=timezone.utc
                    ).isoformat()
                    if fire_at
                    else None,
                    "remaining_seconds": round(remaining, 1),
                }
            )
    return jsonify({"pending": pending})
