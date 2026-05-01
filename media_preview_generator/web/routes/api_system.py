"""System, health, config, library, and log-history API routes."""

import json as _json
import os
import threading
import time

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
    except Exception:
        logger.exception(
            "Notifications: could not save your dismissal of notification {!r}. "
            "The notification will reappear on the next page reload. "
            "Check the config directory is writable (Docker: confirm volume mount permissions and PUID/PGID).",
            notification_id,
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
    except Exception:
        logger.exception(
            "Notifications: could not reset your list of dismissed notifications. "
            "Your dismissals are unchanged and the previously-hidden notifications will remain hidden. "
            "Check the config directory is writable (Docker: confirm volume mount permissions and PUID/PGID)."
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
    except Exception:
        logger.exception(
            "GPU re-scan failed. "
            "The GPU list shown in Settings won't refresh — the previous list is still in effect. "
            "The traceback above identifies the cause; if your GPU isn't visible to the container, "
            "verify the device is forwarded (Docker: --runtime=nvidia or --device /dev/dri:/dev/dri)."
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
    except Exception:
        logger.exception(
            "Could not load the system status panel for the dashboard. "
            "GPU info and running-job summary won't load until this is resolved — "
            "actual job processing is unaffected. "
            "The traceback above identifies the cause."
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

    err = (getattr(result, "message", "") or "").lower()
    if "401" in err or "403" in err or "unauth" in err or "forbid" in err:
        summary["status"] = "unauthorised"
    else:
        summary["status"] = "unreachable"
    if getattr(result, "message", ""):
        summary["error"] = result.message
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
    entries = [e for e in raw if isinstance(e, dict)] if isinstance(raw, list) else []

    # Probe servers in parallel so N offline servers don't each consume
    # the full timeout serially (cold-cache 3-server probe was 90s with
    # the default 30s timeout). Cap concurrency so a busy install with
    # many servers doesn't burst-spawn worker threads.
    summaries: list[dict] = []
    if entries:
        from concurrent.futures import ThreadPoolExecutor

        max_workers = min(8, max(1, len(entries)))
        with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="media-probe") as pool:
            summaries = list(pool.map(_probe_media_server_entry, entries))

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
    except Exception:
        logger.exception(
            "Could not load the runtime config for the API. "
            "The /api/system/config endpoint will return an error until this is resolved. "
            "The traceback above identifies the cause; verify settings.json is readable and valid JSON."
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
    # Sensitive container-host dirs the picker should never surface:
    # the picker is for media + config folders, not credential discovery.
    # /home is intentionally allowed — many users mount their library
    # under /home/<user>/Media or similar.
    "/etc",
    "/root",
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
