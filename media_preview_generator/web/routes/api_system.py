"""System, health, config, library, and log-history API routes."""

import json as _json
import os
import threading
import time

import urllib3
from flask import jsonify, request
from loguru import logger

from ...logging_config import LEVEL_ORDER, get_app_log_path
from ..auth import api_token_required, setup_or_auth_required
from ..jobs import get_job_manager
from . import api
from ._helpers import (
    _ensure_gpu_cache,
    _gpu_cache,
    _gpu_cache_lock,
    _param_to_bool,
)


def _get_timezone_info() -> dict:
    """Detect container timezone configuration.

    Returns a dict with the current timezone name and whether the TZ env var
    is set.  Includes a ``warning`` key when the container appears to be using
    the default UTC timezone without an explicit TZ variable — a common Docker
    misconfiguration that causes schedules and timestamps to be wrong.
    """
    tz_env = os.environ.get("TZ", "")
    system_tz = time.tzname[0]

    # No explicit TZ *and* system reports UTC → likely misconfigured container
    needs_warning = not tz_env and system_tz == "UTC"

    result: dict = {"timezone": system_tz, "tz_env_set": bool(tz_env)}
    if needs_warning:
        # HTML — the dashboard injects this into an alert via innerHTML
        # and the help lines need proper breaks to be readable.  The
        # Settings page has its own static markup for the same message.
        result["warning"] = (
            "Your container timezone is UTC (default). "
            "Scheduled jobs and log timestamps may not match your local time."
            "<br><br>"
            '<span class="small">To fix, either:</span>'
            '<ul class="small mb-0 mt-1">'
            "<li>Add <code>-v /etc/localtime:/etc/localtime:ro</code> to your "
            "Docker run command <em>(recommended)</em></li>"
            "<li>Or set <code>-e TZ=America/New_York</code> "
            "(replace with your timezone)</li>"
            "</ul>"
        )
    return result


@api.route("/system/timezone")
def get_timezone():
    """Return container timezone info and warn if misconfigured.

    No authentication required — timezone is not sensitive.
    """
    return jsonify(_get_timezone_info())


@api.route("/system/notifications")
def list_notifications():
    """Return active system notifications for the bell-icon dropdown.

    Filters out notifications the user has permanently dismissed (stored
    in ``settings.json``) and those dismissed for this process session.
    No authentication required — notifications contain environment
    diagnostics, not secrets.
    """
    from ..notifications import build_active_notifications
    from ..settings_manager import get_settings_manager

    try:
        dismissed = get_settings_manager().dismissed_notifications
    except Exception as exc:
        logger.warning(
            "Notifications: could not read the list of notifications you've previously dismissed "
            "({}: {}). For now, every active notification will be shown — including any you'd "
            "previously hidden. They'll hide again automatically once the settings file becomes "
            "readable. Check the recent log lines for any settings-load errors.",
            type(exc).__name__,
            exc,
        )
        dismissed = []

    notifications = build_active_notifications(dismissed_permanent=dismissed)
    return jsonify({"notifications": notifications})


@api.route("/system/notifications/<notification_id>/dismiss", methods=["POST"])
def dismiss_notification_session(notification_id: str):
    """Dismiss a notification for the current process session only.

    Cleared on container restart.  No authentication required.

    The schema-migration card is special: dismissing it clears the
    persistent ``_pending_migration_notice`` flag too, so the card never
    reappears (it was a one-shot announcement, not a recurring warning).
    """
    from ..notifications import (
        SCHEMA_MIGRATION_ID,
        dismiss_schema_migration_notice,
        dismiss_session,
    )

    dismiss_session(notification_id)
    if notification_id == SCHEMA_MIGRATION_ID:
        try:
            dismiss_schema_migration_notice()
        except Exception:
            logger.debug("Could not clear pending migration notice on dismiss", exc_info=True)
    return jsonify({"ok": True, "id": notification_id, "persisted": False})


@api.route("/system/notifications/<notification_id>/dismiss-permanent", methods=["POST"])
def dismiss_notification_permanent(notification_id: str):
    """Dismiss a notification permanently (persist to ``settings.json``).

    Survives container restarts.  No authentication required.
    """
    from ..settings_manager import get_settings_manager

    try:
        get_settings_manager().dismiss_notification_permanent(notification_id)
    except Exception as exc:
        logger.error(
            "Notifications: could not save your dismissal of notification {!r} ({}: {}). "
            "The notification will reappear on the next page reload. "
            "Check the config directory is writable (Docker: confirm volume mount permissions and PUID/PGID).",
            notification_id,
            type(exc).__name__,
            exc,
        )
        return (
            jsonify({"ok": False, "error": "Failed to persist dismissal"}),
            500,
        )
    return jsonify({"ok": True, "id": notification_id, "persisted": True})


@api.route("/system/notifications/reset-dismissed", methods=["POST"])
@setup_or_auth_required
def reset_dismissed_notifications():
    """Clear all permanently-dismissed notifications.

    Exposed as a settings-page action so users who accidentally
    dismissed a notification can bring them back.  Requires auth because
    it modifies persistent settings state.
    """
    from ..notifications import reset_session
    from ..settings_manager import get_settings_manager

    try:
        get_settings_manager().reset_dismissed_notifications()
    except Exception as exc:
        logger.error(
            "Notifications: could not reset your list of dismissed notifications ({}: {}). "
            "Your dismissals are unchanged and the previously-hidden notifications will remain hidden. "
            "Check the config directory is writable (Docker: confirm volume mount permissions and PUID/PGID).",
            type(exc).__name__,
            exc,
        )
        return jsonify({"ok": False, "error": "Failed to reset"}), 500
    reset_session()
    return jsonify({"ok": True})


@api.route("/system/rescan-gpus", methods=["POST"])
@setup_or_auth_required
def rescan_gpus():
    """Force GPU re-detection and return updated list."""
    try:
        with _gpu_cache_lock:
            _gpu_cache["result"] = None
        _ensure_gpu_cache()
        with _gpu_cache_lock:
            gpus = _gpu_cache["result"] or []
        return jsonify({"gpus": gpus})
    except Exception as e:
        logger.error(
            "GPU re-scan failed ({}: {}). "
            "The GPU list shown in Settings won't refresh — the previous list is still in effect. "
            "Check the recent log lines above; if your GPU isn't visible to the container, "
            "verify the device is forwarded (Docker: --runtime=nvidia or --device /dev/dri:/dev/dri).",
            type(e).__name__,
            e,
        )
        return jsonify({"error": "GPU scan failed"}), 500


@api.route("/system/status")
@setup_or_auth_required
def get_system_status():
    """Get system status including GPU info.

    GPU detection runs lazily on first access and is cached for the lifetime
    of the process. Call clear_gpu_cache() to force a re-scan.
    """
    try:
        _ensure_gpu_cache()
        with _gpu_cache_lock:
            gpus = _gpu_cache["result"] or []

        job_manager = get_job_manager()
        running_job = job_manager.get_running_job()

        resp = {
            "gpus": gpus,
            "gpu_stats": [],
            "running_job": running_job.to_dict() if running_job else None,
            "pending_jobs": len(job_manager.get_pending_jobs()),
        }
        return jsonify(resp)
    except Exception as e:
        logger.error(
            "Could not load the system status panel for the dashboard ({}: {}). "
            "GPU info and running-job summary won't load until this is resolved — "
            "actual job processing is unaffected. "
            "Check the recent log lines above for the underlying cause.",
            type(e).__name__,
            e,
        )
        return jsonify({"error": "Failed to retrieve system status"}), 500


_media_server_status_cache: dict = {"result": None, "fetched_at": 0.0}
_media_server_status_lock = threading.Lock()
_MEDIA_SERVER_STATUS_TTL = 30  # seconds


def _probe_media_server_entry(entry: dict) -> dict:
    """Probe a single media-server registry entry for the dashboard.

    Returns a wire-friendly summary: id, name, type, enabled flag, url, and
    a coarse ``status`` ("connected" | "unreachable" | "unauthorised" |
    "disabled" | "misconfigured"). Errors are caught and surfaced via
    ``status`` + ``error`` so a single bad server can't break the dashboard.
    """
    from ...servers import server_config_from_dict
    from .api_servers import _instantiate_for_probe

    summary = {
        "id": str(entry.get("id") or ""),
        "name": str(entry.get("name") or ""),
        "type": str(entry.get("type") or "").lower(),
        "enabled": bool(entry.get("enabled", True)),
        "url": str(entry.get("url") or ""),
    }

    if not summary["enabled"]:
        summary["status"] = "disabled"
        return summary

    try:
        cfg = server_config_from_dict(entry)
    except Exception as exc:
        summary["status"] = "misconfigured"
        summary["error"] = str(exc)
        return summary

    try:
        live = _instantiate_for_probe(cfg)
    except Exception as exc:
        summary["status"] = "misconfigured"
        summary["error"] = str(exc)
        return summary
    if live is None:
        summary["status"] = "misconfigured"
        summary["error"] = "no probe client available for this server type"
        return summary

    try:
        result = live.test_connection()
    except Exception as exc:
        summary["status"] = "unreachable"
        summary["error"] = str(exc)
        return summary

    if result.ok:
        summary["status"] = "connected"
        if result.server_id:
            summary["server_id"] = result.server_id
        return summary

    err = (getattr(result, "error", "") or "").lower()
    if "401" in err or "403" in err or "unauth" in err or "forbid" in err:
        summary["status"] = "unauthorised"
    else:
        summary["status"] = "unreachable"
    if getattr(result, "error", ""):
        summary["error"] = result.error
    return summary


@api.route("/system/media-servers")
@setup_or_auth_required
def get_media_servers_status():
    """Per-server reachability summary for the dashboard.

    Returns one row per configured ``media_servers`` entry, each tagged
    with a status string the UI maps to a coloured badge. Cached for 30s
    so a busy dashboard doesn't open a TCP connection per refresh; the
    settings UI can call ``/api/servers/<id>/test-connection`` for an
    immediate probe.
    """
    from ..settings_manager import get_settings_manager

    now = time.time()
    with _media_server_status_lock:
        cached = _media_server_status_cache["result"]
        fetched = _media_server_status_cache["fetched_at"]
        if cached is not None and (now - fetched) < _MEDIA_SERVER_STATUS_TTL:
            return jsonify({"servers": cached, "cached": True, "ttl": _MEDIA_SERVER_STATUS_TTL})

    raw = get_settings_manager().get("media_servers") or []
    entries = list(raw) if isinstance(raw, list) else []

    summaries = [_probe_media_server_entry(e) for e in entries if isinstance(e, dict)]

    with _media_server_status_lock:
        _media_server_status_cache["result"] = summaries
        _media_server_status_cache["fetched_at"] = time.time()

    return jsonify({"servers": summaries, "cached": False, "ttl": _MEDIA_SERVER_STATUS_TTL})


@api.route("/system/config")
@api_token_required
def get_config():
    """Get current configuration."""
    try:
        from ...config import get_cached_config
        from ..settings_manager import get_settings_manager

        config = get_cached_config()
        settings = get_settings_manager()
        if config is None:
            return jsonify(
                {
                    "plex_url": settings.plex_url or "",
                    "plex_token": "****" if settings.plex_token else "",
                    "plex_config_folder": settings.plex_config_folder or "",
                    "plex_verify_ssl": settings.plex_verify_ssl,
                    "config_error": "Configuration incomplete. Complete the setup wizard.",
                    "gpu_config": settings.gpu_config,
                    "gpu_threads": settings.gpu_threads,
                    "cpu_threads": settings.cpu_threads,
                    "ffmpeg_threads": settings.get("ffmpeg_threads", 2),
                }
            )

        resp = {
            "plex_url": config.plex_url or "",
            "plex_token": "****" if config.plex_token else "",
            "plex_config_folder": config.plex_config_folder or "",
            "plex_verify_ssl": config.plex_verify_ssl,
            "plex_local_videos_path_mapping": config.plex_local_videos_path_mapping or "",
            "plex_videos_path_mapping": config.plex_videos_path_mapping or "",
            "thumbnail_interval": config.plex_bif_frame_interval,
            "thumbnail_quality": config.thumbnail_quality,
            "regenerate_thumbnails": config.regenerate_thumbnails,
            "gpu_config": config.gpu_config,
            "gpu_threads": config.gpu_threads,
            "cpu_threads": config.cpu_threads,
            "ffmpeg_threads": config.ffmpeg_threads,
            "log_level": config.log_level,
        }
        if config.gpu_threads == 0 and config.cpu_threads == 0:
            resp["config_warning"] = (
                "No workers configured — jobs will remain pending until GPU or CPU workers are added."
            )
        return jsonify(resp)
    except Exception as e:
        logger.error(
            "Could not load the runtime config for the API ({}: {}). "
            "The /api/system/config endpoint will return an error until this is resolved. "
            "Check the recent log lines above for the underlying cause; "
            "verify settings.json is readable and valid JSON.",
            type(e).__name__,
            e,
        )
        return jsonify({"error": "Failed to retrieve configuration"}), 500


_version_cache: dict = {"result": None, "fetched_at": 0.0}
_version_cache_lock = threading.Lock()
_VERSION_CACHE_TTL = 3600  # seconds


def _get_version_info() -> dict:
    """Build version info, using a 1-hour TTL cache for the GitHub API call.

    The installed version and install_type are cheap to compute and never
    change at runtime, but the latest-release lookup hits the GitHub API,
    so we cache the full result for ``_VERSION_CACHE_TTL`` seconds.

    Returns:
        Dict with current_version, latest_version, update_available,
        and install_type.
    """
    with _version_cache_lock:
        if (
            _version_cache["result"] is not None
            and (time.monotonic() - _version_cache["fetched_at"]) < _VERSION_CACHE_TTL
        ):
            return _version_cache["result"]

    from ...utils import is_docker_environment
    from ...version_check import (
        get_branch_head_sha,
        get_current_version,
        get_latest_github_release,
        parse_version,
    )

    git_branch_raw = (os.environ.get("GIT_BRANCH") or "").strip()
    git_sha_raw = (os.environ.get("GIT_SHA") or "").strip()

    # Dockerfile ARG defaults are the literal string "unknown".
    is_local_docker = git_branch_raw == "unknown" and git_sha_raw == "unknown"
    git_branch = "" if git_branch_raw == "unknown" else git_branch_raw
    git_sha = "" if git_sha_raw == "unknown" else git_sha_raw

    update_available = False
    latest_version = None

    if is_local_docker:
        # Local Docker build (Dockerfile defaults, not CI)
        install_type = "local_docker"
        current_version = "local build"
        latest_version = get_latest_github_release()

    elif git_branch.lower().startswith("pr-") and git_sha:
        # PR CI build -- show "PR-123", reference the latest release, no update banner
        install_type = "pr_build"
        pr_num = git_branch.split("-", 1)[1]
        current_version = f"PR-{pr_num}"
        latest_version = get_latest_github_release()

    elif git_branch and git_sha:
        # CI Docker build -- distinguish release tags from dev branches
        try:
            parse_version(git_branch)
            # GIT_BRANCH is a version tag (e.g. 3.4.1) -- release image
            install_type = "docker"
            current_version = git_branch.lstrip("v")
            latest_version = get_latest_github_release()
            if latest_version:
                try:
                    update_available = parse_version(latest_version) > parse_version(current_version)
                except ValueError:
                    logger.debug("Could not compare versions for update check")
        except ValueError:
            # GIT_BRANCH is a branch name (e.g. dev) -- dev image
            install_type = "dev_docker"
            current_version = f"{git_branch}@{git_sha[:7]}"
            head_sha = get_branch_head_sha(git_branch)
            if head_sha and not head_sha.startswith(git_sha):
                update_available = True
            latest_version = f"{git_branch}@{head_sha[:7]}" if head_sha else None

    else:
        # Non-Docker: source checkout or pip install
        install_type = "source" if not is_docker_environment() else "docker"
        current_version = get_current_version()
        latest_version = get_latest_github_release()
        if latest_version:
            try:
                update_available = parse_version(latest_version) > parse_version(current_version)
            except ValueError:
                logger.debug("Could not compare versions for update check")

    result = {
        "current_version": current_version,
        "latest_version": latest_version,
        "update_available": update_available,
        "install_type": install_type,
    }

    with _version_cache_lock:
        _version_cache["result"] = result
        _version_cache["fetched_at"] = time.monotonic()

    return result


@api.route("/system/version")
@setup_or_auth_required
def get_version_info():
    """Get installed version and latest available version.

    Returns:
        JSON with current_version, latest_version, update_available,
        and install_type fields. latest_version may be null if the
        GitHub API is unreachable. Results are cached for 1 hour.
    """
    return jsonify(_get_version_info())


@api.route("/health")
def health_check():
    """Health check endpoint (no auth required)."""
    return jsonify({"status": "healthy"})


# ---------------------------------------------------------------------------
# Log history (reads from the JSONL app.log file)
# ---------------------------------------------------------------------------

_MAX_HISTORY_LINES = 2000
_READ_CHUNK = 64 * 1024  # 64 KB chunks for reverse reading


def _read_tail_lines(path: str, max_lines: int) -> list[str]:
    """Read the last *max_lines* lines from *path* efficiently.

    Reads backwards in fixed-size chunks to avoid loading the entire file.
    Returns lines in chronological (oldest-first) order.
    """
    try:
        size = os.path.getsize(path)
    except OSError:
        return []

    lines: list[str] = []
    with open(path, "rb") as f:
        offset = size
        partial = b""
        while offset > 0 and len(lines) < max_lines:
            read_size = min(_READ_CHUNK, offset)
            offset -= read_size
            f.seek(offset)
            chunk = f.read(read_size) + partial
            chunk_lines = chunk.split(b"\n")
            partial = chunk_lines[0]
            for raw in reversed(chunk_lines[1:]):
                if raw:
                    lines.append(raw.decode("utf-8", errors="replace"))
                if len(lines) >= max_lines:
                    break
        if partial and len(lines) < max_lines:
            lines.append(partial.decode("utf-8", errors="replace"))

    lines.reverse()
    return lines


@api.route("/logs/history")
@setup_or_auth_required
def get_log_history():
    """Return recent log entries from the persistent app.log file.

    Query params:
        limit: Max lines to return (default 500, max 2000).
        level: Minimum log level filter (default: configured log_level).
        before: ISO timestamp cursor — only return entries older than this.
    """
    try:
        limit = min(int(request.args.get("limit", 500)), _MAX_HISTORY_LINES)
    except (ValueError, TypeError):
        limit = 500
    min_level = (request.args.get("level") or "").upper()
    before = request.args.get("before", "")

    if min_level not in LEVEL_ORDER:
        min_level = ""
    min_level_val = LEVEL_ORDER.get(min_level, 0)

    log_path = get_app_log_path()
    raw_lines = _read_tail_lines(log_path, max_lines=limit * 3)

    result: list[dict] = []
    for raw in raw_lines:
        try:
            entry = _json.loads(raw)
        except (ValueError, TypeError):
            continue
        entry_level = entry.get("level", "")
        if min_level_val and LEVEL_ORDER.get(entry_level, 0) < min_level_val:
            continue
        if before and entry.get("ts", "") >= before:
            continue
        result.append(entry)

    # Trim to the requested limit (keep the newest entries)
    if len(result) > limit:
        result = result[-limit:]

    oldest_ts = result[0]["ts"] if result else ""
    return jsonify(
        {
            "lines": result,
            "has_more": len(raw_lines) >= limit * 3,
            "oldest_ts": oldest_ts,
        }
    )


_GITHUB_RELEASES_URL = "https://api.github.com/repos/stevezau/media_preview_generator/releases"
_RELEASES_CACHE: dict = {"result": None, "fetched_at": 0.0}
_RELEASES_CACHE_TTL = 3600


def _fetch_github_releases(limit: int = 10) -> list:
    """Fetch recent GitHub releases with TTL caching.

    Args:
        limit: Max releases to return.

    Returns:
        List of dicts with version, date, and body (markdown).
    """
    now = time.monotonic()
    if _RELEASES_CACHE["result"] is not None and (now - _RELEASES_CACHE["fetched_at"]) < _RELEASES_CACHE_TTL:
        return _RELEASES_CACHE["result"][:limit]

    import requests as req

    try:
        resp = req.get(
            _GITHUB_RELEASES_URL,
            headers={"User-Agent": "media-preview-generator"},
            params={"per_page": limit},
            timeout=5,
        )
        resp.raise_for_status()
        entries = []
        for rel in resp.json():
            if rel.get("draft"):
                continue
            entries.append(
                {
                    "version": (rel.get("tag_name") or "").lstrip("v"),
                    "name": rel.get("name") or rel.get("tag_name") or "",
                    "date": rel.get("published_at") or "",
                    "body": rel.get("body") or "",
                    "url": rel.get("html_url") or "",
                }
            )
        _RELEASES_CACHE["result"] = entries
        _RELEASES_CACHE["fetched_at"] = time.monotonic()
        return entries[:limit]
    except Exception as e:
        logger.debug("Failed to fetch GitHub releases: {}", e)
        return []


@api.route("/system/whats-new")
@setup_or_auth_required
def get_whats_new():
    """Return changelog entries the user hasn't seen yet.

    Compares the current running version against ``last_seen_version``
    stored in settings.  On first install (no ``last_seen_version``),
    silently sets it to the current version and returns nothing.
    """
    from ..settings_manager import get_settings_manager

    settings = get_settings_manager()
    version_info = _get_version_info()
    current = version_info.get("current_version", "")

    if not current or current in ("0.0.0", "0.0.0.dev0", "local build"):
        return jsonify({"has_new": False, "entries": []})

    last_seen = settings.get("last_seen_version", "")

    if not last_seen:
        settings.update({"last_seen_version": current})
        return jsonify({"has_new": False, "entries": []})

    if last_seen == current:
        return jsonify({"has_new": False, "entries": []})

    from ...version_check import parse_version

    releases = _fetch_github_releases(limit=10)
    unseen = []
    for entry in releases:
        v = entry["version"]
        if not v:
            continue
        try:
            if parse_version(v) > parse_version(last_seen):
                unseen.append(entry)
        except ValueError:
            continue

    return jsonify({"has_new": len(unseen) > 0, "entries": unseen})


@api.route("/system/whats-new/dismiss", methods=["POST"])
@setup_or_auth_required
def dismiss_whats_new():
    """Mark the current version's changelog as seen."""
    from ..settings_manager import get_settings_manager

    settings = get_settings_manager()
    version_info = _get_version_info()
    current = version_info.get("current_version", "")
    if current and current not in ("0.0.0", "0.0.0.dev0", "local build"):
        settings.update({"last_seen_version": current})
    return jsonify({"ok": True})


_library_cache: dict = {"result": None, "fetched_at": 0.0}
_library_cache_lock = threading.Lock()
_LIBRARY_CACHE_TTL = 300  # 5 minutes


def clear_library_cache() -> None:
    """Reset the Plex library cache.

    Useful for tests and when settings change (e.g. Plex URL updated).
    """
    with _library_cache_lock:
        _library_cache["result"] = None
        _library_cache["fetched_at"] = 0.0


_SPORTS_AGENT_PATTERNS = ("sportarr", "sportscanner")


def classify_library_type(section_type: str, agent: str) -> str:
    """Derive a display-friendly library type from Plex section type and agent.

    Args:
        section_type: Plex library type (``"movie"``, ``"show"``, etc.).
        agent: Plex metadata agent identifier string.

    Returns:
        One of ``"movie"``, ``"show"``, ``"sports"``, or ``"other_videos"``.
    """
    agent_lower = (agent or "").lower()
    if section_type == "show":
        for pattern in _SPORTS_AGENT_PATTERNS:
            if pattern in agent_lower:
                return "sports"
        return "show"
    if section_type == "movie":
        if agent_lower == "com.plexapp.agents.none":
            return "other_videos"
        return "movie"
    return section_type


def _fetch_libraries_via_http(
    plex_url: str,
    plex_token: str,
    verify_ssl: bool = True,
) -> list:
    """Fetch Plex libraries via direct HTTP request.

    Args:
        plex_url: Plex server URL
        plex_token: Plex authentication token
        verify_ssl: Whether to verify the server's TLS certificate

    Returns:
        List of library dicts with id, name, type, agent, and display_type.
    """
    import requests

    if not verify_ssl:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    response = requests.get(
        f"{plex_url.rstrip('/')}/library/sections",
        headers={"X-Plex-Token": plex_token, "Accept": "application/json"},
        timeout=10,
        verify=verify_ssl,
    )
    response.raise_for_status()
    data = response.json()

    libraries = []
    for section in data.get("MediaContainer", {}).get("Directory", []):
        section_type = section.get("type")
        if section_type not in ("movie", "show"):
            continue
        agent = section.get("agent", "")
        libraries.append(
            {
                "id": str(section.get("key")),
                "name": section.get("title"),
                "type": section_type,
                "agent": agent,
                "display_type": classify_library_type(section_type, agent),
                "server_id": None,  # setup-wizard fast path: media_servers entry doesn't exist yet
                "server_name": None,
                "server_type": "plex",
            }
        )
    return libraries


def _libraries_for_configured_server(server_id: str) -> tuple[list[dict] | None, str | None, int]:
    """List libraries for one configured media server via the registry.

    Returns ``(libraries, error_message, http_status)``. ``libraries`` is None
    when ``error_message`` is set. Used by the multi-server library picker so
    the same endpoint serves Plex, Emby, and Jellyfin uniformly — each row is
    tagged with the originating ``server_id`` / ``server_name`` / ``server_type``
    so the Schedules picker can disambiguate same-named libraries.
    """
    from ...servers import ServerRegistry
    from ..settings_manager import get_settings_manager

    settings = get_settings_manager()
    raw_servers = settings.get("media_servers") or []
    if not isinstance(raw_servers, list):
        raw_servers = []

    target = next((s for s in raw_servers if isinstance(s, dict) and s.get("id") == server_id), None)
    if target is None:
        return None, f"server {server_id!r} not configured", 404
    if not target.get("enabled", True):
        return [], None, 200

    try:
        registry = ServerRegistry.from_settings(raw_servers, legacy_config=None)
    except Exception as exc:
        logger.warning(
            "Could not build server registry to list libraries for {} ({}: {}). "
            "Verify the server's configuration on the Servers page.",
            server_id,
            type(exc).__name__,
            exc,
        )
        return None, f"server registry unavailable: {exc}", 500

    server = registry.get(server_id)
    if server is None:
        return None, f"server {server_id!r} not instantiable", 500

    rows: list[dict] = []
    try:
        for lib in server.list_libraries():
            rows.append(
                {
                    "id": str(lib.id),
                    "name": lib.name,
                    "type": lib.kind or "",
                    "agent": "",
                    "display_type": (lib.kind or "").lower() or "library",
                    "server_id": server_id,
                    "server_name": target.get("name") or "",
                    "server_type": (target.get("type") or "").lower(),
                }
            )
    except Exception as exc:
        logger.warning(
            "Could not list libraries for {} ({}: {}). The schedules library picker will show no entries for this server. "
            "Verify the server is reachable on the Servers page (Test Connection).",
            target.get("name") or server_id,
            type(exc).__name__,
            exc,
        )
        return None, f"failed to list libraries: {exc}", 502
    return rows, None, 200


def _libraries_for_all_configured_servers() -> list[dict]:
    """Aggregate libraries across every configured + enabled media server.

    Each row is tagged with ``server_id`` / ``server_name`` / ``server_type``
    so a single picker can disambiguate "Movies (Home Plex)" from
    "Movies (Living Room Emby)". Servers that fail to enumerate are skipped
    silently after a warning — one bad server can't block the picker.
    """
    from ..settings_manager import get_settings_manager

    settings = get_settings_manager()
    raw_servers = settings.get("media_servers") or []
    if not isinstance(raw_servers, list):
        return []

    rows: list[dict] = []
    for entry in raw_servers:
        if not isinstance(entry, dict) or not entry.get("enabled", True):
            continue
        sid = entry.get("id")
        if not sid:
            continue
        libs, err, _status = _libraries_for_configured_server(sid)
        if err:
            continue
        rows.extend(libs or [])
    return rows


@api.route("/libraries")
@api_token_required
def get_libraries():
    """Get available libraries from one or all configured media servers.

    Modes:
      * ``?server_id=<id>`` — list libraries from that configured server
        (Plex / Emby / Jellyfin). Each row is tagged with its server identity.
      * ``?url=&token=`` — Setup-wizard fast path: hit Plex directly with
        credentials that haven't been persisted yet (Plex-only).
      * (no params) — list libraries across every enabled configured server,
        tagged with ``server_id`` / ``server_name`` / ``server_type`` so a
        single picker can disambiguate same-named libraries.

    Results from saved credentials are cached for 5 minutes to avoid hitting
    the server on every page load.
    """
    server_id_arg = (request.args.get("server_id") or "").strip()
    if server_id_arg:
        libs, err, status = _libraries_for_configured_server(server_id_arg)
        if err:
            return jsonify({"error": err, "libraries": []}), status
        return jsonify({"libraries": libs})

    try:
        import requests as req_lib

        from ..settings_manager import get_settings_manager

        settings = get_settings_manager()

        plex_url = request.args.get("url")
        plex_token = request.args.get("token")
        verify_ssl = _param_to_bool(request.args.get("verify_ssl"), settings.plex_verify_ssl)
        # No explicit overrides → aggregate across every configured server
        # (Plex + Emby + Jellyfin). The dashboard and Start-Job modal both
        # call /api/libraries with no params and expect the full list. The
        # old "Plex-only when Plex is configured" path silently dropped
        # Emby/Jellyfin libraries — see api_system.py:1579.
        #
        # The legacy single-Plex install (``plex_url``/``plex_token`` set
        # but ``media_servers`` empty) falls through to the Plex-only
        # branch below so existing behaviour is preserved.
        if not plex_url and not plex_token:
            raw_servers = settings.get("media_servers") or []
            if isinstance(raw_servers, list) and raw_servers:
                return jsonify({"libraries": _libraries_for_all_configured_servers()})
            if not settings.plex_url:
                return jsonify({"libraries": _libraries_for_all_configured_servers()})

        # Track whether explicit overrides were provided (setup wizard)
        has_overrides = bool(plex_url or plex_token)

        if not plex_url or not plex_token:
            plex_url = plex_url or settings.plex_url
            plex_token = plex_token or settings.plex_token

        if not plex_url or not plex_token:
            try:
                from ...config import get_cached_config
                from ...plex_client import plex_server

                config = get_cached_config()
                if config is None:
                    return jsonify(
                        {
                            "error": "Plex not configured. Complete setup in Settings.",
                            "libraries": [],
                        }
                    ), 400

                plex = plex_server(config)

                # Tag rows with server_id when the Plex entry exists in
                # media_servers (it almost always does post-migration); falls
                # back to None for the rare legacy-globals-only install.
                plex_entry = next(
                    (
                        e
                        for e in (settings.get("media_servers") or [])
                        if isinstance(e, dict) and (e.get("type") or "").lower() == "plex" and e.get("enabled", True)
                    ),
                    None,
                )
                plex_sid = (plex_entry or {}).get("id") or None
                plex_sname = (plex_entry or {}).get("name") or None

                libraries = []
                for section in plex.library.sections():
                    if section.type in ("movie", "show"):
                        agent = getattr(section, "agent", "") or ""
                        libraries.append(
                            {
                                "id": str(section.key),
                                "name": section.title,
                                "type": section.type,
                                "agent": agent,
                                "display_type": classify_library_type(section.type, agent),
                                "server_id": plex_sid,
                                "server_name": plex_sname,
                                "server_type": "plex",
                            }
                        )

                return jsonify({"libraries": libraries})
            except Exception as e:
                logger.error(
                    "Could not load Plex libraries using the saved configuration ({}: {}). "
                    "The library picker will show 'Plex not configured. Complete setup in Settings.' "
                    "Verify the Plex URL and token in Settings, and that Plex is reachable from this app.",
                    type(e).__name__,
                    e,
                )
                return jsonify(
                    {
                        "error": "Plex not configured. Complete setup in Settings.",
                        "libraries": [],
                    }
                ), 400

        # Use cached result when loading with saved credentials (not
        # during setup wizard where explicit overrides are provided).
        if not has_overrides:
            with _library_cache_lock:
                cached = _library_cache["result"]
                age = time.monotonic() - _library_cache["fetched_at"]
            if cached is not None and age < _LIBRARY_CACHE_TTL:
                return jsonify({"libraries": cached})

        libraries = _fetch_libraries_via_http(
            plex_url,
            plex_token,
            verify_ssl=verify_ssl,
        )

        if not has_overrides:
            with _library_cache_lock:
                _library_cache["result"] = libraries
                _library_cache["fetched_at"] = time.monotonic()

        return jsonify({"libraries": libraries})

    except req_lib.ConnectionError:
        detail = f"Could not connect to Plex at {plex_url}"
        logger.error(
            "Plex libraries: could not connect to Plex at {} (network unreachable / refused). "
            "The library picker will fail until Plex is reachable. "
            "Verify the URL is correct and that Plex is running and reachable from this app.",
            plex_url,
        )
        return jsonify(
            {
                "error": f"{detail}. Check the server URL and ensure Plex is running and reachable from this host.",
                "libraries": [],
            }
        ), 502
    except req_lib.Timeout:
        detail = f"Connection to Plex at {plex_url} timed out"
        logger.error(
            "Plex libraries: connection to Plex at {} timed out. "
            "The library picker will fail until Plex responds. "
            "Plex may be overloaded or unreachable — try again in a minute.",
            plex_url,
        )
        return jsonify(
            {
                "error": f"{detail}. The server may be overloaded or unreachable.",
                "libraries": [],
            }
        ), 504
    except req_lib.HTTPError as e:
        status = e.response.status_code if e.response is not None else "unknown"
        if status == 401:
            detail = "Plex rejected the authentication token"
            hint = "Re-authenticate with Plex or check your token."
        elif status == 403:
            detail = "Access denied by Plex server"
            hint = "Ensure your account has access to this server."
        else:
            detail = f"Plex returned HTTP {status}"
            hint = "Check Plex server logs for details."
        logger.error(
            "Plex libraries: Plex returned HTTP {} — {}. The library picker will fail until this is resolved. {}",
            status,
            detail,
            hint,
        )
        return jsonify({"error": f"{detail}. {hint}", "libraries": []}), 502
    except Exception as e:
        logger.error(
            "Plex libraries: could not retrieve the library list ({}: {}). "
            "The library picker will show an error until this is fixed. "
            "Check the recent log lines for the underlying cause; "
            "verify the Plex URL/token in Settings and that Plex is reachable.",
            type(e).__name__,
            e,
        )
        return jsonify({"error": f"Failed to retrieve libraries: {e}", "libraries": []}), 500


# Folders considered system-internal — never browsable through the UI even
# though docker would happily mount them. The picker is for media + config
# folders, not /proc inspection.
_BROWSE_DENYLIST = (
    "/proc",
    "/sys",
    "/dev",
    "/run",
    "/var/run",
    "/var/log",
    "/boot",
)


def _is_in_denylist(path: str) -> bool:
    """True when the canonical path lives under one of the system dirs."""
    p = os.path.normpath(path)
    for prefix in _BROWSE_DENYLIST:
        if p == prefix or p.startswith(prefix + "/"):
            return True
    return False


@api.route("/system/browse")
@api_token_required
def browse_directories():
    """List sub-directories of an absolute path on the running container.

    Used by the folder-picker modal so users can pick path-mapping locals or
    the Plex config folder without typing. Lists directories only — files are
    omitted, since every input that opens this picker stores a directory path.

    Query params:
        path: absolute path to list (default ``/``).
        show_hidden: ``1`` to include dot-prefixed entries (default ``0``).

    Returns JSON:
        ``{"path": str, "parent": str|null, "entries": [{"name", "path"}], "error": str|null}``
    """
    raw_path = (request.args.get("path") or "/").strip() or "/"
    show_hidden = request.args.get("show_hidden") in ("1", "true", "yes")

    if "\x00" in raw_path:
        return jsonify({"path": "/", "parent": None, "entries": [], "error": "Invalid path"}), 400

    if not raw_path.startswith("/"):
        return jsonify({"path": "/", "parent": None, "entries": [], "error": "Path must be absolute"}), 400

    try:
        canonical = os.path.realpath(raw_path)
    except OSError as exc:
        return jsonify({"path": raw_path, "parent": None, "entries": [], "error": str(exc)}), 400

    if _is_in_denylist(canonical):
        return jsonify({"path": canonical, "parent": None, "entries": [], "error": "Path is not browsable"}), 403

    if not os.path.exists(canonical):
        return jsonify({"path": canonical, "parent": None, "entries": [], "error": "Folder not found"}), 404
    if not os.path.isdir(canonical):
        return jsonify({"path": canonical, "parent": None, "entries": [], "error": "Not a directory"}), 400

    parent = None if canonical == "/" else os.path.dirname(canonical) or "/"

    try:
        with os.scandir(canonical) as it:
            entries = []
            for entry in it:
                if not show_hidden and entry.name.startswith("."):
                    continue
                try:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                child = os.path.join(canonical, entry.name)
                if _is_in_denylist(child):
                    continue
                entries.append({"name": entry.name, "path": child})
    except PermissionError:
        return jsonify({"path": canonical, "parent": parent, "entries": [], "error": "Permission denied"}), 403
    except OSError as exc:
        return jsonify({"path": canonical, "parent": parent, "entries": [], "error": str(exc)}), 500

    entries.sort(key=lambda e: e["name"].lower())
    return jsonify({"path": canonical, "parent": parent, "entries": entries, "error": None})
