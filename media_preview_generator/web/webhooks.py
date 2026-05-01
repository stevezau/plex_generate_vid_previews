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

# Recently dispatched (source, normalized_path) entries, used to drop
# duplicate webhook deliveries that arrive after the debounce batch has
# already fired — Plex in particular re-sends library.new events after
# metadata refreshes and analyzer reruns.
_recent_dispatches: dict[tuple[str, str], float] = {}
_RECENT_DISPATCH_TTL_SECONDS = 600

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
        with open(path) as f:
            entries = json.load(f)
        if isinstance(entries, list):
            with _history_lock:
                _webhook_history.clear()
                _webhook_history.extend(entries[-_HISTORY_MAX:])
            logger.debug("Loaded {} webhook history entries from {}", len(_webhook_history), path)
    except Exception as exc:
        logger.warning(
            "Could not load saved webhook history from {} ({}: {}). "
            "Starting with an empty history — past webhook activity won't show up on the Webhooks page, "
            "but new webhooks will still be received and recorded normally.",
            path,
            type(exc).__name__,
            exc,
        )


def _save_history_to_disk() -> None:
    """Persist the current webhook history deque to disk.

    Best-effort — failures are logged but never propagated.
    """
    try:
        from ..utils import atomic_json_save_with_backup

        with _history_lock:
            snapshot = list(_webhook_history)
        atomic_json_save_with_backup(str(_history_file_path()), snapshot)
    except Exception as exc:
        logger.debug("Failed to persist webhook history: {}", exc)


def _authenticate_webhook(f):
    """Check X-Auth-Token, Authorization Bearer, Basic auth, or ``?token=`` query param.

    Collects candidate tokens from all sources and tries each against the
    webhook secret and app auth token.  ``X-Auth-Token`` is checked first
    because it is the dedicated webhook header; ``Authorization`` (Bearer /
    Basic) is a fallback.  This prevents browser-injected JWTs (e.g. from
    Tdarr's session) from shadowing the explicit webhook token.

    The ``?token=`` query parameter exists specifically for the native
    Plex webhook: Plex's webhook UI offers no place to add headers or
    HTTP Basic credentials, so the only way to authenticate a request
    coming from Plex Media Server is to embed the token in the URL
    that's registered with plex.tv.
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
                decoded = base64.b64decode(auth_header[6:].encode()).decode("utf-8", errors="replace")
                if ":" in decoded:
                    _, basic_pw = decoded.split(":", 1)
                    if basic_pw:
                        candidates.append(("Basic", basic_pw))
            except (ValueError, UnicodeDecodeError):
                logger.debug("Failed to decode Basic auth header")

        query_token = request.args.get("token", "").strip()
        if query_token:
            candidates.append(("query", query_token))

        if not candidates:
            logger.warning(
                "Webhook from {} rejected: no authentication token in the request (URL={}, method={}). "
                "Add the webhook secret as either an Authorization Bearer header, an X-Auth-Token header, "
                "or a ?token= query parameter. Plex's webhook UI doesn't allow headers, so use the "
                "?token= form for Plex.",
                request.remote_addr,
                request.path,
                request.method,
            )
            return jsonify({"error": "Authentication required"}), 401

        settings = get_settings_manager()
        webhook_secret = settings.get("webhook_secret", "")

        # The inbound URL may carry ?server_id=<id> for routing (Plex Direct
        # registrations embed it so the dispatcher knows which configured
        # Plex sent the event). Auth itself uses only the global webhook
        # secret, then falls back to the API auth token.
        for _method, token in candidates:
            if webhook_secret and secrets.compare_digest(token, webhook_secret):
                return f(*args, **kwargs)
            if validate_token(token):
                return f(*args, **kwargs)

        logger.warning(
            "Webhook from {} rejected: token did not match (URL={}, supplied via {}). "
            "Other webhooks with the correct secret are still being accepted. "
            "Check the webhook secret in Settings → Webhooks matches what your sender is sending; "
            "regenerate the secret on this page if you need to rotate it.",
            request.remote_addr,
            request.path,
            candidates[0][0],
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
    server_id: str | None = None,
    server_name: str | None = None,
    server_type: str | None = None,
) -> None:
    """Append an event to the webhook history and persist to disk.

    Optional batch metadata (job_id, path_count, files_preview) is included
    for triggered debounced batches so the UI can show which files were in
    the batch. ``server_id`` / ``server_name`` / ``server_type`` capture the
    pinned destination for multi-server-aware history (the new ``?server_id=``
    query param on inbound webhook URLs).
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
    if server_id:
        entry["server_id"] = server_id
    if server_name:
        entry["server_name"] = server_name
    if server_type:
        entry["server_type"] = server_type
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


def _debounce_key(source: str, server_id: str | None = None) -> str:
    """Build a stable debounce key — separate batches per (source, server_id)
    so a Radarr → Plex import never gets bundled with a Radarr → Emby import.
    """
    if server_id:
        return f"{source}::{server_id}"
    return source


def _resolve_webhook_server_context(server_id: str | None) -> tuple[str | None, str | None, str | None]:
    """Look up a configured server by id and return ``(id, name, type)``.

    Used by webhook handlers when the inbound URL carries ``?server_id=<id>``.
    Returns (None, None, None) when the id is missing or unknown — the job is
    then treated as "all owning servers" (legacy behaviour).
    """
    if not server_id:
        return None, None, None
    try:
        raw = get_settings_manager().get("media_servers") or []
    except Exception:
        return None, None, None
    if not isinstance(raw, list):
        return None, None, None
    entry = next((e for e in raw if isinstance(e, dict) and e.get("id") == server_id), None)
    if entry is None:
        return None, None, None
    return (entry.get("id"), entry.get("name") or entry.get("id"), (entry.get("type") or "").lower() or None)


def _as_dict(value: object) -> dict:
    """Return value if dict-like, otherwise an empty dict."""
    return value if isinstance(value, dict) else {}


def _combine_path(base_path: str, relative_path: str) -> str:
    """Combine base and relative paths into a normalized absolute-ish path."""
    if not base_path or not relative_path:
        return ""
    return os.path.normpath(os.path.join(base_path, relative_path)).replace("\\", "/")


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
    """Extract a target file path from a Sonarr/Sportarr Download webhook payload.

    Checks (in order):
    1. ``episodeFile.path``  (standard Sonarr)
    2. ``series.path`` + ``episodeFile.relativePath``  (Sonarr fallback)
    3. ``filePath``  (Sportarr flat payload)
    """
    episode_file = _as_dict(payload.get("episodeFile"))
    if episode_file.get("path"):
        return str(episode_file.get("path")).strip()

    combined = _combine_path(
        str(_as_dict(payload.get("series")).get("path", "")).strip(),
        str(episode_file.get("relativePath", "")).strip(),
    )
    if combined.strip():
        return combined.strip()

    # Sportarr uses a flat filePath key at the root level
    file_path = str(payload.get("filePath", "")).strip()
    return file_path


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


def _format_plex_title_from_metadata(metadata: dict) -> str | None:
    """Build a descriptive job title from a Plex webhook Metadata dict.

    Episodes return ``"Show - SxxExx - Episode Title"`` (or just
    ``"Show - SxxExx"`` when the episode title is blank or the generic
    ``"Episode N"`` placeholder Plex uses for shows without canonical titles).
    Movies return ``"Title (Year)"`` (or bare title if year is missing).
    Returns ``None`` when required fields are absent so the caller can fall
    back to a ratingKey lookup or the raw ``metadata.title``.
    """
    if not isinstance(metadata, dict):
        return None
    item_type = str(metadata.get("type", "")).strip().lower()

    if item_type == "episode":
        show = str(metadata.get("grandparentTitle", "")).strip()
        if not show:
            return None
        try:
            season_num = int(metadata.get("parentIndex"))
            episode_num = int(metadata.get("index"))
        except (TypeError, ValueError):
            return None
        season_episode = f"S{season_num:02d}E{episode_num:02d}"
        ep_title = str(metadata.get("title", "")).strip()
        tautology = f"episode {episode_num}"
        if not ep_title or ep_title.lower() == tautology:
            return f"{show} - {season_episode}"
        return f"{show} - {season_episode} - {ep_title}"

    if item_type == "movie":
        title = str(metadata.get("title", "")).strip()
        if not title:
            return None
        year = metadata.get("year")
        try:
            year_int = int(year) if year is not None else None
        except (TypeError, ValueError):
            year_int = None
        if year_int:
            return f"{title} ({year_int})"
        return title

    return None


def _format_plex_title_from_item(item) -> str | None:
    """Same format as :func:`_format_plex_title_from_metadata` but for a
    ``plexapi`` item object. Used when the webhook payload lacks structured
    fields and we have to fall back to a ratingKey lookup.
    """
    if item is None:
        return None
    synthetic: dict = {
        "type": getattr(item, "type", ""),
        "grandparentTitle": getattr(item, "grandparentTitle", ""),
        "parentIndex": getattr(item, "parentIndex", None),
        "index": getattr(item, "index", None),
        "title": getattr(item, "title", ""),
        "year": getattr(item, "year", None),
    }
    return _format_plex_title_from_metadata(synthetic)


def _schedule_webhook_job(source: str, title: str, file_path: str, server_id: str | None = None) -> bool:
    """Schedule a debounced single-file webhook job and batch paths per (source, server_id)."""
    safe_source = str(source or "unknown")
    safe_title = str(title or "Unknown")
    normalized_input_path = str(file_path or "").strip()
    if not normalized_input_path:
        logger.warning(
            "Webhook from {}: ignored {!r} — the payload didn't carry a file path. "
            "Other webhooks from this source are still being processed. "
            "If this keeps happening, check the sending tool's webhook template includes the file path "
            "(Radarr: 'movieFile.path' or 'movie.folderPath'+'movieFile.relativePath'; "
            "Sonarr: 'episodeFile.path' or 'series.path'+'episodeFile.relativePath').",
            safe_source,
            safe_title,
        )
        return False

    settings = get_settings_manager()
    delay = int(settings.get("webhook_delay", 60))
    debounce_key = _debounce_key(safe_source, server_id)
    normalized_path = os.path.normpath(normalized_input_path).replace("\\", "/")
    dedup_key = (safe_source, server_id or "", normalized_path)

    with _pending_lock:
        now_ts = datetime.now(timezone.utc).timestamp()

        # Opportunistically prune expired dedup entries — keeps the dict bounded.
        expired = [key for key, ts in _recent_dispatches.items() if now_ts - ts >= _RECENT_DISPATCH_TTL_SECONDS]
        for key in expired:
            _recent_dispatches.pop(key, None)

        recent_ts = _recent_dispatches.get(dedup_key)
        if recent_ts is not None:
            age = int(now_ts - recent_ts)
            logger.info(
                "Webhook: {} duplicate of '{}' ignored (already dispatched {}s ago)", safe_source, safe_title, age
            )
            dedup_skip = True
        else:
            dedup_skip = False

        if not dedup_skip:
            existing = _pending_timers.get(debounce_key)
            if existing:
                existing.cancel()

            batch = _pending_batches.get(debounce_key)
            if not batch:
                batch = {
                    "source": source,
                    "file_paths": set(),
                    "titles": [],
                    "server_id": server_id,
                }
                _pending_batches[debounce_key] = batch
            batch["file_paths"].add(normalized_path)
            batch["titles"].append(safe_title)

            fire_at = now_ts + delay
            batch["fire_at"] = fire_at

            timer = threading.Timer(delay, _execute_webhook_job, args=[debounce_key])
            timer.daemon = True
            _pending_timers[debounce_key] = timer
            timer.start()
            path_count = len(batch["file_paths"])

    if dedup_skip:
        _add_history_entry(safe_source, "Download", safe_title, "deduped")
        return False

    logger.info(
        "Webhook: {} imported '{}' — scheduling job with {} path(s) in {}s", safe_source, safe_title, path_count, delay
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
        logger.warning(
            "Webhook batch '{}' fired but the pending list was already empty — "
            "this usually happens when two debounce timers race; safe to ignore unless it repeats often.",
            debounce_key,
        )
        return

    source = str(batch.get("source", "unknown"))
    batch_titles = batch.get("titles") or []

    try:
        webhook_paths = sorted(path for path in batch.get("file_paths", set()) if isinstance(path, str) and path)
        if not webhook_paths:
            logger.warning(
                "Webhook from {}: debounced batch had no valid file paths after de-duplication, "
                "so no job was created. Other webhooks are still being processed. "
                "If you expected files here, the sender most likely posted blank or duplicate paths — "
                "check the recent webhook history on the Webhooks page for the raw payloads.",
                source,
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

        batch_server_id = batch.get("server_id")
        b_sid, b_sname, b_stype = _resolve_webhook_server_context(batch_server_id)

        job_manager = get_job_manager()
        job = job_manager.create_job(
            library_name=library_display,
            config={
                "source": source,
                "path_count": len(webhook_paths),
                "webhook_basenames": basenames[:_HISTORY_FILES_PREVIEW_CAP],
            },
            server_id=b_sid,
            server_name=b_sname,
            server_type=b_stype,
        )
        settings = get_settings_manager()
        # ``selected_libraries`` is a Plex-only filter (the dispatch path
        # uses it to scope a Plex full-scan to specific Section IDs).
        # Only attach it when the webhook is unpinned OR pinned to Plex —
        # for an Emby/Jellyfin-pinned webhook, pushing the Plex library
        # list as an override is wrong on its face and could mis-filter
        # if the dispatch path ever consumes it for non-Plex jobs.
        from ..config import derive_legacy_plex_view

        plex_view = derive_legacy_plex_view(settings.get("media_servers") or [])
        selected_libraries: list[str] = []
        if not b_stype or b_stype == "plex":
            raw_libs = plex_view.get("selected_libraries") or settings.get("selected_libraries", [])
            if isinstance(raw_libs, list):
                selected_libraries = [str(name).strip() for name in raw_libs if str(name).strip()]
        retry_count = max(0, min(10, int(settings.get("webhook_retry_count", 3))))
        retry_delay = max(10, min(300, int(settings.get("webhook_retry_delay", 30))))

        with _pending_lock:
            dispatch_ts = datetime.now(timezone.utc).timestamp()
            for p in webhook_paths:
                _recent_dispatches[(source, batch_server_id or "", p)] = dispatch_ts

        overrides = {
            "sort_by": "newest",
            "webhook_paths": webhook_paths,
            "webhook_retry_count": retry_count,
            "webhook_retry_delay": retry_delay,
        }
        if selected_libraries:
            overrides["selected_libraries"] = selected_libraries
        if b_sid:
            overrides["server_id"] = b_sid
        _start_job_async(job.id, overrides)
        _add_history_entry(
            source,
            "Download",
            first_title or source,
            "triggered",
            job_id=job.id,
            path_count=len(webhook_paths),
            files_preview=basenames,
            server_id=b_sid,
            server_name=b_sname,
            server_type=b_stype,
        )
    except Exception:
        title_label = batch_titles[0] if batch_titles else source
        logger.exception(
            "Webhook from {}: could not start the processing job for {!r} due to an unexpected error. "
            "Other webhooks are still being accepted, but this batch is lost. "
            "See the traceback below for the underlying cause; if it repeats, please open a GitHub issue "
            "with the recent log lines.",
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
            "Webhook from {}: Radarr request ignored — the body wasn't valid JSON "
            "(Content-Type={}, Content-Length={}). Other webhooks are still being accepted. "
            "Make sure Radarr is configured to send JSON (Settings → Connect → your webhook → "
            "Method = POST). If you've added a custom proxy, check it isn't transforming the body.",
            request.remote_addr,
            request.content_type,
            request.content_length,
        )
        return jsonify({"success": False, "error": "Invalid or missing JSON body"}), 400

    event_type = str(data.get("eventType", "")).strip()

    if event_type == "Test":
        _add_history_entry("radarr", "Test", "", "test")
        return jsonify({"success": True, "message": "Radarr webhook configured successfully"})

    settings = get_settings_manager()
    if not settings.get("webhook_enabled", True):
        _add_history_entry("radarr", event_type, "", "disabled")
        logger.info("Webhook: Radarr event '{}' ignored (webhooks disabled)", event_type)
        return jsonify({"success": True, "message": "Webhooks disabled"})

    if event_type != "Download":
        _add_history_entry("radarr", event_type, "", "ignored")
        logger.info("Webhook: Radarr event '{}' ignored", event_type)
        return jsonify({"success": True, "message": f"Ignored event: {event_type}"})

    movie = _as_dict(data.get("movie"))
    movie_title = str(movie.get("title", "Unknown")).strip() or "Unknown"
    movie_file_path = _extract_radarr_file_path(data)

    server_id = (request.args.get("server_id") or "").strip() or None
    kwargs = {"server_id": server_id} if server_id else {}
    was_queued = _schedule_webhook_job("radarr", movie_title, movie_file_path, **kwargs)
    if not was_queued:
        logger.debug(
            "Webhook: Radarr payload had no extractable file path. Structure: {}\nFull payload: {}",
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
            "Webhook from {}: {} request ignored — the body wasn't valid JSON "
            "(Content-Type={}, Content-Length={}). Other webhooks are still being accepted. "
            "Check the {} Connect webhook is configured for POST with JSON body; if you've added a "
            "proxy in front, confirm it isn't transforming the body.",
            request.remote_addr,
            label,
            request.content_type,
            request.content_length,
            label,
        )
        return jsonify({"success": False, "error": "Invalid or missing JSON body"}), 400

    event_type = str(data.get("eventType", "")).strip()

    if event_type == "Test":
        _add_history_entry(source, "Test", "", "test")
        return jsonify({"success": True, "message": f"{label} webhook configured successfully"})

    settings = get_settings_manager()
    if not settings.get("webhook_enabled", True):
        _add_history_entry(source, event_type, "", "disabled")
        logger.info("Webhook: {} event '{}' ignored (webhooks disabled)", label, event_type)
        return jsonify({"success": True, "message": "Webhooks disabled"})

    if event_type not in ("Download", "OnDownload"):
        _add_history_entry(source, event_type, "", "ignored")
        logger.info("Webhook: {} event '{}' ignored", label, event_type)
        return jsonify({"success": True, "message": f"Ignored event: {event_type}"})

    series = _as_dict(data.get("series"))
    series_title = str(series.get("title", "")).strip()
    # Sportarr uses eventTitle / instanceName instead of series.title
    if not series_title:
        series_title = str(data.get("eventTitle", "")).strip() or str(data.get("instanceName", "")).strip() or "Unknown"
    display_title = _format_sonarr_episode_title(series_title, data.get("episodes"))
    episode_file_path = _extract_sonarr_file_path(data)

    server_id = (request.args.get("server_id") or "").strip() or None
    kwargs = {"server_id": server_id} if server_id else {}
    was_queued = _schedule_webhook_job(source, display_title, episode_file_path, **kwargs)
    if not was_queued:
        logger.debug(
            "Webhook: {} payload had no extractable file path. Structure: {}\nFull payload: {}",
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
        jsonify({"success": True, "message": f"Processing queued for '{display_title}'"}),
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


def _extract_plex_payload(req) -> tuple[dict | None, str | None]:
    """Extract the JSON payload from a Plex multipart webhook request.

    Plex sends ``multipart/form-data`` with a ``payload`` part containing
    the JSON event body and (for ``media.play`` / ``media.rate`` events)
    a second part with a JPEG thumbnail.  Whether the payload arrives as
    a regular form field or a file part depends on the client / proxy
    in front of Plex, so we check both.  As a convenience for tests and
    curl-based debugging we also accept a raw JSON body — Plex itself
    will never send that.
    """
    raw: str | None = None

    if req.form:
        raw = req.form.get("payload")

    if raw is None and req.files:
        file_part = req.files.get("payload")
        if file_part is not None:
            try:
                raw = file_part.read().decode("utf-8", errors="replace")
            except Exception as exc:
                return None, f"could not read payload part: {exc}"

    if raw is None:
        try:
            data = req.get_json(force=True, silent=True)
        except Exception:
            data = None
        if isinstance(data, dict):
            return data, None
        return None, "missing 'payload' field"

    try:
        data = json.loads(raw)
    except (TypeError, ValueError) as exc:
        return None, f"invalid JSON in payload: {exc}"
    if not isinstance(data, dict):
        return None, "payload must be a JSON object"
    return data, None


def _resolve_plex_paths_from_rating_key(
    rating_key: int | str,
) -> tuple[list[str], str | None]:
    """Look up a Plex item by ratingKey and return its file paths and a
    formatted display title.

    Plex's ``library.new`` payload identifies items by ratingKey but
    does not consistently include file paths in the Metadata block.
    This helper instantiates a PlexServer using the configured token,
    walks ``item.media[*].parts[*].file`` to recover the paths, and
    derives a descriptive title from the same fetched item so callers
    don't need a second Plex round trip.

    Returns ``([], None)`` on any failure (item not found, Plex
    unreachable, no media parts).
    """
    try:
        from ..config import load_config
        from ..plex_client import plex_server, retry_plex_call
    except ImportError as exc:
        logger.debug("Webhook: cannot import plex client modules: {}", exc)
        return [], None

    try:
        # log_validation_errors=False — the warning below is the
        # user-facing message; the ❌ Configuration Error preamble
        # would just duplicate it on every Plex webhook fire.
        config = load_config(log_validation_errors=False)
    except Exception as exc:
        logger.warning(
            "Plex webhook lookup: could not load the app's saved settings ({}: {}). "
            "The incoming Plex webhook for this item is being ignored — open the Settings page and "
            "confirm it saves cleanly, then try again.",
            type(exc).__name__,
            exc,
        )
        return [], None

    try:
        plex = plex_server(config)
    except Exception as exc:
        logger.warning(
            "Plex webhook lookup: could not connect to Plex to look up the item ({}: {}). "
            "The incoming webhook for this item is being skipped this time. "
            "Verify the Plex URL/token in Settings and that Plex is reachable from this app.",
            type(exc).__name__,
            exc,
        )
        return [], None

    try:
        item = retry_plex_call(plex.fetchItem, int(rating_key))
    except Exception as exc:
        logger.warning(
            "Plex webhook lookup: Plex returned an error when asked for item ratingKey={} ({}: {}). "
            "This webhook is being skipped — Plex itself is reachable, but doesn't recognise that "
            "ratingKey. The item may still be analysing; another library.new event should fire when "
            "Plex finishes. If it doesn't, trigger 'Refresh Metadata' on the item.",
            rating_key,
            type(exc).__name__,
            exc,
        )
        return [], None

    paths: list[str] = []
    media_list = getattr(item, "media", None) or []
    for media in media_list:
        for part in getattr(media, "parts", None) or []:
            file_path = getattr(part, "file", None)
            if file_path:
                paths.append(str(file_path))

    display_title = _format_plex_title_from_item(item)
    return paths, display_title


def _classify_plex_event(data: dict) -> tuple[str, tuple | None]:
    """Decide what to do with a parsed Plex webhook payload's top-level event.

    Returns ``(event_name, early_response)``. ``early_response`` is non-None
    when the dispatcher should reply immediately (test ping, master switch
    off, or non-library.new noise event) — the tuple is the
    ``(jsonify_body, status)`` to return verbatim. ``early_response`` is
    None when the event is a real ``library.new`` that needs full path
    resolution + dispatch.

    History entries for the early-exit cases are written here so the caller
    doesn't repeat the wiring.
    """
    event = str(data.get("event", "")).strip()

    if event == "test.ping":
        _add_history_entry("plex", "Test", "", "test")
        return event, (jsonify({"success": True, "message": "Plex webhook endpoint reachable"}), 200)

    settings = get_settings_manager()
    if not settings.get("webhook_enabled", True):
        _add_history_entry("plex", event or "Plex", "", "disabled")
        logger.info("Webhook: Plex event '{}' ignored (master webhook switch off)", event)
        return event, (jsonify({"success": True, "message": "Webhooks disabled"}), 200)

    if event != "library.new":
        # Plex always sends every event the user has subscribed the URL to —
        # ignoring noise like media.play silently is intentional.
        _add_history_entry("plex", event or "Plex", "", "ignored")
        logger.debug("Webhook: Plex event '{}' ignored (not library.new)", event)
        return event, (jsonify({"success": True, "message": f"Ignored event: {event}"}), 200)

    return event, None


def _resolve_plex_webhook_paths_and_title(metadata: dict, rating_key) -> tuple[list[str], str]:
    """Recover the file paths + display title for a library.new Plex webhook.

    Tries the cheap path first: file paths embedded in the payload's
    ``Media[].Part[].file`` fields. Plex includes these inconsistently
    (present for some media types but not all), so falls back to a Plex
    API lookup by ratingKey when the embedded paths or title are missing.

    Returns ``(paths, display_title)``. Either may be empty if every
    resolution strategy fails — the caller is responsible for the
    "couldn't recover" warning + history entry.
    """
    raw_title = str(metadata.get("title", "")).strip() or "Plex item"
    display_title = _format_plex_title_from_metadata(metadata)

    paths: list[str] = []
    media_list = metadata.get("Media")
    if isinstance(media_list, list):
        for media in media_list:
            parts = _as_dict(media).get("Part")
            if isinstance(parts, list):
                for part in parts:
                    file_path = _as_dict(part).get("file")
                    if isinstance(file_path, str) and file_path.strip():
                        paths.append(file_path.strip())

    if not paths or display_title is None:
        resolved_paths, resolved_title = _resolve_plex_paths_from_rating_key(rating_key)
        if not paths:
            paths = resolved_paths
        if display_title is None:
            display_title = resolved_title

    if display_title is None:
        display_title = raw_title

    return paths, display_title


@webhooks_bp.route("/plex", methods=["POST"])
@_authenticate_webhook
def plex_webhook():
    """Receive native Plex webhook payloads (Plex Pass).

    Plex POSTs ``multipart/form-data`` with a JSON ``payload`` field on
    server events.  We only act on ``library.new`` events; everything
    else (media.play, media.rate, library.on.deck, etc.) is acknowledged
    with 200 so Plex doesn't retry.

    The endpoint also accepts two synthetic events used by the UI:

    * ``test.ping`` — sent by the "Test reachability" button.  Returns
      success without resolving paths or scheduling work, and records a
      "test" history entry so the user can see it landed.
    * ``library.new`` payloads with no Metadata are treated as malformed.
    """
    data, parse_error = _extract_plex_payload(request)
    if data is None:
        logger.warning(
            "Webhook from {}: Plex request ignored — {} (Content-Type={}). "
            "Other Plex webhooks are still being accepted. "
            "Plex normally sends multipart/form-data with a 'payload' field; if you're seeing this "
            "repeatedly, re-register the webhook from Settings → Webhooks → Re-register with Plex.",
            request.remote_addr,
            parse_error,
            request.content_type,
        )
        return jsonify({"success": False, "error": parse_error}), 400

    _event, early = _classify_plex_event(data)
    if early is not None:
        return early

    metadata = _as_dict(data.get("Metadata"))
    rating_key = metadata.get("ratingKey")

    if not rating_key:
        raw_title = str(metadata.get("title", "")).strip() or "Plex item"
        fallback_display = _format_plex_title_from_metadata(metadata) or raw_title
        logger.warning(
            "Plex 'library.new' webhook for {!r} arrived without a ratingKey — we can't look the item up. "
            "Plex usually includes this; if you see this often the webhook source may be a third-party tool "
            "sending a stripped-down payload. Payload structure for diagnosis: {}",
            fallback_display,
            _summarize_payload(data),
        )
        _add_history_entry("plex", "library.new", fallback_display, "ignored_no_path")
        return jsonify({"success": False, "error": "Missing Metadata.ratingKey"}), 400

    paths, display_title = _resolve_plex_webhook_paths_and_title(metadata, rating_key)

    if not paths:
        logger.warning(
            "Plex 'library.new' webhook for {!r} (ratingKey={}) had no file paths attached and we "
            "couldn't recover any from a Plex API lookup either. The item won't be processed. "
            "This usually means Plex hasn't finished analyzing the file yet — it should fire again "
            "when analysis completes. If it doesn't, trigger a 'Refresh Metadata' on that item in Plex.",
            display_title,
            rating_key,
        )
        _add_history_entry("plex", "library.new", display_title, "ignored_no_path")
        return (
            jsonify(
                {
                    "success": True,
                    "message": (f"No file paths found for '{display_title}' (ratingKey={rating_key})"),
                }
            ),
            200,
        )

    queued_any = any(_schedule_webhook_job("plex", display_title, path) for path in paths)

    if not queued_any:
        _add_history_entry("plex", "library.new", display_title, "ignored_no_path")
        return (
            jsonify(
                {
                    "success": True,
                    "message": f"No valid paths queued for '{display_title}'",
                }
            ),
            200,
        )

    _add_history_entry("plex", "library.new", display_title, "queued")
    return (
        jsonify({"success": True, "message": f"Processing queued for '{display_title}'"}),
        202,
    )


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
            "Webhook from {}: Custom request ignored — the body wasn't valid JSON "
            "(Content-Type={}, Content-Length={}). Other webhooks are still being accepted. "
            "Custom webhooks need a JSON body with at least 'file_path' (string) or 'file_paths' "
            "(array of strings). Confirm Content-Type: application/json on the sender's side.",
            request.remote_addr,
            request.content_type,
            request.content_length,
        )
        return jsonify({"success": False, "error": "Invalid or missing JSON body"}), 400

    event_type = str(data.get("eventType", "")).strip()

    if event_type == "Test":
        _add_history_entry("custom", "Test", "", "test")
        return jsonify({"success": True, "message": "Custom webhook configured successfully"})

    settings = get_settings_manager()
    if not settings.get("webhook_enabled", True):
        _add_history_entry("custom", event_type or "Custom", "", "disabled")
        logger.info("Webhook: Custom event ignored (webhooks disabled)")
        return jsonify({"success": True, "message": "Webhooks disabled"})

    paths = _extract_custom_paths(data)
    if not paths:
        logger.debug(
            "Webhook: Custom payload had no extractable file path. Structure: {}\nFull payload: {}",
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

    server_id = (request.args.get("server_id") or "").strip() or None
    kwargs = {"server_id": server_id} if server_id else {}
    for path in paths:
        _schedule_webhook_job("custom", title, path, **kwargs)

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
        normalized = os.path.normpath(p).replace("\\", "/")
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
                    "fire_at": datetime.fromtimestamp(fire_at, tz=timezone.utc).isoformat() if fire_at else None,
                    "remaining_seconds": round(remaining, 1),
                }
            )
    return jsonify({"pending": pending})
