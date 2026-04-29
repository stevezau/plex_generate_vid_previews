"""Settings and setup wizard API routes."""

import os
from urllib.parse import urlparse

from flask import jsonify, request
from loguru import logger

from ...config import validate_processing_thread_totals
from ...utils import is_docker_environment
from ..auth import api_token_required, setup_or_auth_required
from . import api
from ._helpers import (
    MEDIA_ROOT,
    PLEX_DATA_ROOT,
    _safe_resolve_within,
)

_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def _url_has_loopback_host(url: str) -> bool:
    """True when the URL's hostname resolves to the local loopback interface."""
    if not url:
        return False
    try:
        host = (urlparse(url).hostname or "").lower()
    except (ValueError, AttributeError):
        return False
    return host in _LOOPBACK_HOSTS


def _loopback_in_docker_warning(url: str) -> str | None:
    """Return a user-facing warning when `url` points at localhost from a container.

    Inside a Docker container, `localhost` refers to the container itself —
    not the host — so any URL the user configured against `localhost` will be
    unreachable from the running app. Outside Docker (native install) we
    stay out of the way because loopback URLs work fine there.
    """
    if _url_has_loopback_host(url) and is_docker_environment():
        return (
            f"The webhook URL '{url}' points to localhost, which inside a Docker "
            "container refers to the container itself — not the host. Use the "
            "Docker host's LAN IP or a DNS name reachable from both this "
            "container and your Plex Media Server "
            "(e.g. http://192.168.1.50:9191/api/webhooks/incoming)."
        )
    return None


def _reconcile_live_gpu_workers(settings) -> None:
    """Sync the live WorkerPool with the current gpu_config.

    After gpu_config is persisted, rebuild the selected-GPU list and
    reconcile the running pool so that disabled GPUs are removed and
    newly enabled GPUs are added without requiring a restart.
    """
    try:
        from .api_jobs import _get_shared_worker_pool
        from .job_runner import _build_selected_gpus

        pool = _get_shared_worker_pool()
        if pool is None:
            return
        new_selected = _build_selected_gpus(settings)
        pool.reconcile_gpu_workers(new_selected)
    except Exception:
        logger.warning(
            "Could not reconcile the live worker pool with the new GPU settings. "
            "The settings were saved, but you may need to restart the app for the GPU changes to fully "
            "take effect. Currently-running jobs are unaffected. "
            "See the traceback below for the underlying cause.",
            exc_info=True,
        )


def _auto_pause_if_needed(settings) -> None:
    """Pause processing when all worker counts drop to zero."""
    if settings.processing_paused:
        return
    settings.processing_paused = True
    logger.info("Processing auto-paused — no workers configured")
    try:
        from ..jobs import get_job_manager

        get_job_manager().emit_processing_paused_changed(True)
    except Exception:
        logger.debug("Could not emit pause event", exc_info=True)


_PER_SERVER_FIELDS = frozenset(
    {
        "plex_url",
        "plex_token",
        "plex_verify_ssl",
        "plex_config_folder",
        "selected_libraries",
        "path_mappings",
        "exclude_paths",
    }
)


def _route_legacy_plex_fields_into_media_servers(settings, updates: dict) -> tuple[dict, list[dict] | None]:
    """Pluck legacy Plex-flavoured fields out of ``updates`` and fold them into ``media_servers[0]``.

    The Setup Wizard and the legacy Settings page both POST flat
    ``plex_url`` / ``plex_token`` / ``selected_libraries`` /
    ``path_mappings`` / ``exclude_paths`` / ``plex_config_folder`` keys.
    Phase 1 of the multi-server refactor stops persisting those at the
    top level and instead writes them into the first Plex entry of the
    ``media_servers`` list — that's the single source of truth Phase 0
    already taught every reader to prefer.

    Returns a tuple of:

    1. A new ``updates`` dict with the per-server fields removed (so the
       caller can persist whatever's left as global settings).
    2. The new ``media_servers`` list to persist (or ``None`` if no
       per-server fields were touched).

    The caller is responsible for adding the returned ``media_servers``
    list back into ``updates`` when persisting.

    Behaviour:

    - The token field of value ``"****"`` (four asterisks) means
      "the user didn't change it" — the existing token is preserved.
    - When no Plex entry exists yet (fresh install, completing the
      Setup Wizard), a new ``plex-default`` entry is created.
    - Per-library toggles in the existing ``media_servers[0].libraries``
      list are preserved when ``selected_libraries`` is updated; only
      the ``enabled`` flag is recomputed from the new selection.
    """
    touched = _PER_SERVER_FIELDS & updates.keys()
    if not touched:
        return updates, None

    # Pull the per-server fields out of updates so the caller doesn't
    # write them as global keys too (avoids dual state).
    payload = {k: updates[k] for k in touched}
    new_updates = {k: v for k, v in updates.items() if k not in touched}

    media_servers = list(settings.get("media_servers") or [])
    plex_index = next(
        (i for i, e in enumerate(media_servers) if isinstance(e, dict) and (e.get("type") or "").lower() == "plex"),
        None,
    )
    if plex_index is None:
        # Fresh install: synthesise a new Plex entry. Match the shape
        # produced by upgrade.py::_legacy_plex_to_media_server so old
        # and new flow result in identical settings.json.
        plex_entry: dict = {
            "id": "plex-default",
            "type": "plex",
            "name": "Plex",
            "enabled": True,
            "url": "",
            "auth": {},
            "verify_ssl": True,
            "timeout": 60,
            "libraries": [],
            "path_mappings": [],
            "exclude_paths": [],
            "output": {"adapter": "plex_bundle", "plex_config_folder": "", "frame_interval": 10},
        }
        media_servers.append(plex_entry)
        plex_index = len(media_servers) - 1
    plex_entry = dict(media_servers[plex_index])  # shallow copy so we don't mutate input

    if "plex_url" in payload:
        plex_entry["url"] = str(payload["plex_url"] or "").strip()
    if "plex_verify_ssl" in payload:
        plex_entry["verify_ssl"] = bool(payload["plex_verify_ssl"])
    if "plex_token" in payload:
        token_val = payload["plex_token"]
        # The GET endpoint returns "****" as a placeholder; ignore that
        # so the user's existing token isn't wiped by re-saving the form.
        if token_val and token_val != "****":
            auth = dict(plex_entry.get("auth") or {})
            auth.setdefault("method", "token")
            auth["token"] = token_val
            plex_entry["auth"] = auth
    if "plex_config_folder" in payload:
        output = dict(plex_entry.get("output") or {})
        output.setdefault("adapter", "plex_bundle")
        output.setdefault("frame_interval", 10)
        output["plex_config_folder"] = str(payload["plex_config_folder"] or "").strip()
        plex_entry["output"] = output
    if "selected_libraries" in payload:
        new_selected = payload["selected_libraries"] or []
        if not isinstance(new_selected, list):
            new_selected = []
        new_selected_set = {str(x) for x in new_selected if x}
        existing_libs = list(plex_entry.get("libraries") or [])
        if existing_libs:
            # Re-flag existing entries; new ids that aren't in the cached
            # library list get appended as minimal stubs (a Refresh
            # Libraries call will fill in the names).
            updated: list[dict] = []
            seen_ids: set[str] = set()
            for lib in existing_libs:
                if not isinstance(lib, dict):
                    continue
                lib_id = str(lib.get("id") or lib.get("name") or "")
                seen_ids.add(lib_id)
                lib_copy = dict(lib)
                lib_copy["enabled"] = lib_id in new_selected_set
                updated.append(lib_copy)
            for lib_id in new_selected_set - seen_ids:
                updated.append({"id": lib_id, "name": lib_id, "remote_paths": [], "enabled": True})
            plex_entry["libraries"] = updated
        else:
            # No cached libraries yet — wizard provided the IDs only.
            plex_entry["libraries"] = [
                {"id": lib_id, "name": lib_id, "remote_paths": [], "enabled": True}
                for lib_id in sorted(new_selected_set)
            ]
    if "path_mappings" in payload:
        pm = payload["path_mappings"] or []
        plex_entry["path_mappings"] = list(pm) if isinstance(pm, list) else []
    if "exclude_paths" in payload:
        ep = payload["exclude_paths"] or []
        plex_entry["exclude_paths"] = list(ep) if isinstance(ep, list) else []

    media_servers[plex_index] = plex_entry
    return new_updates, media_servers


def _auto_resume_if_needed(settings) -> None:
    """Resume processing when workers become available again."""
    settings.processing_paused = False
    logger.info("Processing auto-resumed — workers available")
    try:
        from ..jobs import get_job_manager
        from .api_jobs import _start_job_async

        jm = get_job_manager()
        jm.emit_processing_paused_changed(False)
        for running in jm.get_running_jobs():
            jm.request_resume(running.id)
        pending = sorted(
            jm.get_pending_jobs(),
            key=lambda j: (j.priority, j.created_at or ""),
        )
        for pj in pending:
            _start_job_async(pj.id, pj.config or {})
    except Exception:
        logger.debug("Could not emit resume event", exc_info=True)


# ============================================================================
# Settings
# ============================================================================


@api.route("/settings")
@setup_or_auth_required
def get_settings():
    """Get all settings.

    Per-server Plex fields (URL, token, libraries, path mappings,
    exclude paths, config folder) are projected from ``media_servers[0]``
    when present so the legacy Settings UI keeps working through the
    Phase 1 migration. Legacy global keys remain as a fallback.
    """
    from ...config import derive_legacy_plex_view
    from ..settings_manager import get_settings_manager

    settings = get_settings_manager()
    plex_view = derive_legacy_plex_view(settings.get("media_servers") or [])

    def _from_view(view_key, fallback):
        val = plex_view.get(view_key)
        return val if val not in (None, "", []) else fallback

    return jsonify(
        {
            "plex_url": _from_view("plex_url", settings.plex_url or ""),
            "plex_token": "****" if (plex_view.get("plex_token") or settings.plex_token) else "",
            "plex_name": settings.plex_name or "",
            "plex_verify_ssl": plex_view.get("plex_verify_ssl", settings.plex_verify_ssl),
            "plex_config_folder": _from_view("plex_config_folder", settings.plex_config_folder or "/plex"),
            "selected_libraries": _from_view("selected_libraries", settings.selected_libraries),
            "media_path": settings.media_path or "",
            "plex_videos_path_mapping": settings.get("plex_videos_path_mapping", ""),
            "plex_local_videos_path_mapping": settings.get("plex_local_videos_path_mapping", ""),
            "path_mappings": _from_view("path_mappings", settings.get("path_mappings", [])),
            "exclude_paths": _from_view("exclude_paths", settings.get("exclude_paths", [])),
            "gpu_config": settings.gpu_config,
            "gpu_threads": settings.gpu_threads,
            "cpu_threads": settings.cpu_threads,
            "ffmpeg_threads": settings.get("ffmpeg_threads", 2),
            "thumbnail_interval": settings.thumbnail_interval,
            "thumbnail_quality": settings.thumbnail_quality,
            "tonemap_algorithm": settings.tonemap_algorithm,
            "log_level": settings.get("log_level", "INFO"),
            "log_rotation_size": settings.get("log_rotation_size", "10 MB"),
            "log_retention_count": settings.get("log_retention_count", 5),
            "job_history_days": settings.get("job_history_days", 30),
            "webhook_enabled": settings.get("webhook_enabled", True),
            "webhook_delay": settings.get("webhook_delay", 60),
            "webhook_retry_count": settings.get("webhook_retry_count", 3),
            "webhook_retry_delay": settings.get("webhook_retry_delay", 30),
            "webhook_secret": "****" if settings.get("webhook_secret") else "",
            "auto_requeue_on_restart": settings.get("auto_requeue_on_restart", True),
            "requeue_max_age_minutes": settings.get("requeue_max_age_minutes", 720),
        }
    )


@api.route("/settings", methods=["POST"])
@setup_or_auth_required
def save_settings():
    """Save settings."""
    from ..settings_manager import get_settings_manager

    settings = get_settings_manager()
    data = request.get_json() or {}

    allowed_fields = [
        "plex_url",
        "plex_token",
        "plex_name",
        "plex_verify_ssl",
        "plex_config_folder",
        "selected_libraries",
        "media_path",
        "plex_videos_path_mapping",
        "plex_local_videos_path_mapping",
        "path_mappings",
        "exclude_paths",
        "gpu_config",
        "gpu_threads",
        "cpu_threads",
        "ffmpeg_threads",
        "thumbnail_interval",
        "thumbnail_quality",
        "tonemap_algorithm",
        "log_level",
        "log_rotation_size",
        "log_retention_count",
        "job_history_days",
        "webhook_enabled",
        "webhook_delay",
        "webhook_retry_count",
        "webhook_retry_delay",
        "webhook_secret",
        "auto_requeue_on_restart",
        "requeue_max_age_minutes",
    ]

    updates = {k: v for k, v in data.items() if k in allowed_fields}

    # Sanitize gpu_config: must be a list of dicts with a device key.
    # Normalize: workers <= 0 forces enabled=false (contradictory state).
    if "gpu_config" in updates:
        raw = updates["gpu_config"]
        if not isinstance(raw, list):
            return jsonify({"error": "gpu_config must be a list"}), 400
        cleaned = []
        for entry in raw:
            if not isinstance(entry, dict) or not entry.get("device"):
                continue
            if entry.get("enabled") and (entry.get("workers") or 0) <= 0:
                entry["enabled"] = False
                entry["workers"] = 0
            cleaned.append(entry)
        updates["gpu_config"] = cleaned

    # Route Plex-flavoured legacy fields into media_servers[0] instead of
    # writing them as top-level keys. Phase 0 already taught readers to
    # prefer the per-server view; this is the write half of the same flip.
    # Capture which fields the *caller* tried to update before we strip
    # them, so the post-save hooks (cache invalidation, webhook re-register)
    # still fire even though the keys ended up nested under media_servers.
    incoming_field_keys = set(updates.keys())
    updates, new_media_servers = _route_legacy_plex_fields_into_media_servers(settings, updates)
    if new_media_servers is not None:
        updates["media_servers"] = new_media_servers

    thread_warning = ""
    if updates:
        settings.update(updates)
        logger.info("Settings updated: {}", list(updates.keys()))

        if "gpu_config" in updates:
            _reconcile_live_gpu_workers(settings)

        ok, thread_warning = validate_processing_thread_totals(settings.get_all())
        if not ok:
            logger.warning(
                "Worker count check: {}. "
                "Processing has been auto-paused so no new jobs run until you increase a worker count. "
                "Open Settings → Workers and set GPU and/or CPU workers > 0, then save.",
                thread_warning,
            )
            _auto_pause_if_needed(settings)
        elif settings.processing_paused:
            _auto_resume_if_needed(settings)

        # Invalidate the Plex library cache when connection details change
        # so the next libraries request fetches fresh data.
        # Use incoming_field_keys (pre-routing) so this hook still fires
        # when the user changes plex_url / plex_token / plex_verify_ssl —
        # even though those keys now end up inside media_servers[0].
        plex_fields = {"plex_url", "plex_token", "plex_verify_ssl"}
        if plex_fields & incoming_field_keys:
            from .api_system import clear_library_cache

            clear_library_cache()

        # Rotating the webhook secret invalidates the token embedded in any
        # registered Plex webhook URL. Re-register every Plex server that has
        # a stored webhook URL so Plex picks up the new auth token. Best-effort
        # per server — one failure shouldn't block the others or the save.
        if "webhook_secret" in updates:
            try:
                from .. import plex_webhook_registration as pwh

                for entry in settings.get("media_servers") or []:
                    if not isinstance(entry, dict) or (entry.get("type") or "").lower() != "plex":
                        continue
                    token = (entry.get("auth") or {}).get("token") or entry.get("token") or ""
                    token = str(token).strip()
                    public_url = ((entry.get("output") or {}).get("webhook_public_url") or "").strip()
                    if not token or not public_url:
                        continue
                    new_auth = _plex_webhook_auth_token()
                    if not new_auth:
                        continue
                    try:
                        pwh.register(token, public_url, auth_token=new_auth, server_id=entry.get("id"))
                        logger.info(
                            "Plex webhook re-registered for {!r} after secret rotation",
                            entry.get("name") or entry.get("id"),
                        )
                    except Exception:
                        logger.warning(
                            "Could not re-register Plex webhook for {!r} after secret rotation. "
                            "Plex will keep posting with the OLD token until you re-register from "
                            "Servers → Edit → Webhook & Scanner.",
                            entry.get("name") or entry.get("id"),
                            exc_info=True,
                        )
            except Exception:
                logger.warning(
                    "Webhook secret rotation re-registration failed unexpectedly.",
                    exc_info=True,
                )

        log_fields = {"log_level", "log_rotation_size", "log_retention_count"}
        if log_fields & updates.keys():
            from ...logging_config import setup_logging

            setup_logging(
                log_level=settings.get("log_level", "INFO"),
                rotation=settings.get("log_rotation_size", "10 MB"),
                retention=settings.get("log_retention_count", 5),
            )

    result = {"success": True}
    if thread_warning:
        result["warning"] = thread_warning
    return jsonify(result)


@api.route("/settings/log-level", methods=["PUT"])
@api_token_required
def update_log_level():
    """Hot-reload log level at runtime."""
    from ...logging_config import setup_logging
    from ..settings_manager import get_settings_manager

    data = request.get_json() or {}
    level = (data.get("log_level") or "INFO").upper()

    valid_levels = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
    if level not in valid_levels:
        return jsonify({"error": f"Invalid log level. Must be one of {valid_levels}"}), 400

    sm = get_settings_manager()
    sm.set("log_level", level)

    rotation = sm.get("log_rotation_size", "10 MB")
    retention = sm.get("log_retention_count", 5)
    setup_logging(log_level=level, rotation=rotation, retention=retention)
    logger.info("Log level changed to {}", level)

    return jsonify({"success": True, "log_level": level})


@api.route("/settings/validate-local-path", methods=["POST"])
@setup_or_auth_required
def validate_local_path():
    """Check whether a single local path exists and is readable.

    Used for inline validation of the 'Path in this app' field in
    path mapping rows.  Only the local_prefix is validated because
    it is the only path the app can verify on disk.

    Request JSON: ``{"path": "/mnt/data"}``

    Returns JSON:
        ``{"exists": bool, "readable": bool, "error": str|null}``

    """
    data = request.get_json() or {}
    raw_path = (data.get("path") or "").strip()
    if not raw_path:
        return jsonify({"exists": False, "readable": False, "error": None})
    if "\x00" in raw_path:
        return jsonify({"exists": False, "readable": False, "error": "Invalid path"})

    resolved = _safe_resolve_within(raw_path, MEDIA_ROOT)
    if resolved is None:
        return jsonify(
            {
                "exists": False,
                "readable": False,
                "error": "Path is outside the allowed media root",
            }
        )
    if not os.path.exists(resolved):
        return jsonify({"exists": False, "readable": False, "error": None})
    if not os.path.isdir(resolved):
        return jsonify(
            {
                "exists": True,
                "readable": False,
                "error": "Path exists but is not a directory",
            }
        )
    if not os.access(resolved, os.R_OK):
        return jsonify(
            {
                "exists": True,
                "readable": False,
                "error": "Directory exists but is not readable",
            }
        )
    return jsonify({"exists": True, "readable": True, "error": None})


@api.route("/settings/validate-plex-config-folder", methods=["POST"])
@setup_or_auth_required
def validate_plex_config_folder():
    """Inline check that a path looks like a real Plex config folder.

    Used by the Servers > Edit Plex > General tab to give the user a confident
    "yes this is the right folder" message, instead of just "directory exists".
    Mirrors the structural checks in /setup/validate-paths but returns a tight,
    boolean-friendly shape suitable for live form validation.

    Request JSON: ``{"path": "/plex"}``

    Returns JSON:
        ``{"exists": bool, "valid_plex_structure": bool, "shard_count": int,
           "writable": bool, "detail": str, "error": str|null}``
    """
    data = request.get_json() or {}
    raw_path = (data.get("path") or "").strip()
    if not raw_path:
        return jsonify(
            {
                "exists": False,
                "valid_plex_structure": False,
                "shard_count": 0,
                "writable": False,
                "detail": "",
                "error": None,
            }
        )
    if "\x00" in raw_path:
        return jsonify(
            {
                "exists": False,
                "valid_plex_structure": False,
                "shard_count": 0,
                "writable": False,
                "detail": "",
                "error": "Invalid path",
            }
        )

    resolved = _safe_resolve_within(raw_path, PLEX_DATA_ROOT)
    if resolved is None:
        # Path is outside PLEX_DATA_ROOT (defaults to /plex). The previous
        # message ("Path is outside the allowed Plex data root") was
        # confusing — users on /setup don't know what the "allowed root"
        # is or how to fix it. Disambiguate based on whether the typed
        # path actually exists on disk so we can suggest the right Docker
        # bind, and tell them they need to mount the folder at /plex.
        canonical_root = os.path.realpath(PLEX_DATA_ROOT)
        try:
            probe_resolved = os.path.realpath(os.path.normpath(raw_path))
            exists_outside_root = os.path.isdir(probe_resolved)
        except OSError:
            exists_outside_root = False
        if exists_outside_root:
            msg = (
                f"Folder found at {raw_path}, but this app can only write to "
                f"{canonical_root} from inside the container. Mount your Plex "
                f"config folder there with: -v {raw_path}:{canonical_root}, "
                f"then enter {canonical_root} here."
            )
        else:
            msg = (
                f"Path must be inside {canonical_root} (the container's Plex data "
                f"mount). Mount your host Plex config folder there with "
                f"-v /your/host/path:{canonical_root}, then enter {canonical_root} here."
            )
        return jsonify(
            {
                "exists": exists_outside_root,
                "valid_plex_structure": False,
                "shard_count": 0,
                "writable": False,
                "detail": "",
                "error": msg,
            }
        )
    if not os.path.exists(resolved):
        return jsonify(
            {
                "exists": False,
                "valid_plex_structure": False,
                "shard_count": 0,
                "writable": False,
                "detail": "",
                "error": "Folder not found",
            }
        )

    media_path = os.path.join(resolved, "Media")
    localhost_path = os.path.join(media_path, "localhost")

    if not os.path.exists(media_path):
        return jsonify(
            {
                "exists": True,
                "valid_plex_structure": False,
                "shard_count": 0,
                "writable": os.access(resolved, os.W_OK),
                "detail": "",
                "error": 'Missing "Media" subfolder — this does not look like a Plex config folder',
            }
        )
    if not os.path.exists(localhost_path):
        return jsonify(
            {
                "exists": True,
                "valid_plex_structure": False,
                "shard_count": 0,
                "writable": os.access(resolved, os.W_OK),
                "detail": "",
                "error": 'Missing "Media/localhost" subfolder — this does not look like a Plex config folder',
            }
        )

    try:
        contents = os.listdir(localhost_path)
        shard_count = sum(1 for d in contents if len(d) == 1 and d in "0123456789abcdef")
    except OSError as exc:
        logger.warning(
            "validate-plex-config-folder: could not enumerate {} ({}: {})",
            localhost_path,
            type(exc).__name__,
            exc,
        )
        shard_count = 0

    writable = os.access(resolved, os.W_OK)
    if not writable:
        return jsonify(
            {
                "exists": True,
                "valid_plex_structure": True,
                "shard_count": shard_count,
                "writable": False,
                "detail": "",
                "error": "Folder is not writable — check PUID/PGID on the container",
            }
        )

    if shard_count >= 10:
        detail = f"valid Plex structure ({shard_count}/16 hash shards under Media/localhost)"
    elif shard_count > 0:
        detail = (
            f"Plex structure looks new — only {shard_count}/16 hash shards present (will populate as previews generate)"
        )
    else:
        detail = "Media/localhost exists but has no hash shards yet (will populate as previews generate)"

    return jsonify(
        {
            "exists": True,
            "valid_plex_structure": True,
            "shard_count": shard_count,
            "writable": True,
            "detail": detail,
            "error": None,
        }
    )


# ============================================================================
# Auto-trigger sources (Plex direct webhook + Recently Added scanner)
# ============================================================================


def _default_plex_webhook_url() -> str:
    """Build the default webhook URL Plex should POST to.

    Uses the request's effective host/scheme so the same browser
    session that's looking at the Settings page can register a URL
    Plex Media Server is likely to be able to reach (typical
    same-host or same-LAN setups).  Users on reverse proxies / split
    networks override this manually.
    """
    base = request.host_url.rstrip("/")
    return f"{base}/api/webhooks/incoming"


def _resolve_plex_server_for_webhook(server_id: str | None) -> tuple[dict | None, str | None, int | None]:
    """Look up the Plex server entry the webhook endpoint should operate on.

    Returns (server_entry, error_message, status_code). On success the second
    and third members are None. When ``server_id`` is provided we require an
    exact match in ``media_servers``; when it's omitted we fall back to the
    first Plex entry (handles the setup-wizard / single-server case).
    """
    from ..settings_manager import get_settings_manager

    settings = get_settings_manager()
    media_servers = settings.get("media_servers") or []
    if server_id:
        match = next(
            (s for s in media_servers if isinstance(s, dict) and s.get("id") == server_id),
            None,
        )
        if not match:
            return None, f"Server {server_id!r} not configured", 404
        if (match.get("type") or "").lower() != "plex":
            return None, "Plex Direct webhook is Plex-only", 400
        return match, None, None
    plex_entry = next(
        (s for s in media_servers if isinstance(s, dict) and (s.get("type") or "").lower() == "plex"),
        None,
    )
    return plex_entry, None, None


def _server_token(server_entry: dict | None) -> str:
    """Extract the Plex token from a server entry, falling back to the legacy global one.

    Server entries store the token under ``auth.token`` (matching the
    multi-server schema). A flat ``token`` key is also accepted for
    forward-compat with any future re-shape.
    """
    from ..settings_manager import get_settings_manager

    if server_entry:
        token = (server_entry.get("auth") or {}).get("token") or server_entry.get("token") or ""
        token = str(token).strip()
        if token:
            return token
    return (get_settings_manager().plex_token or "").strip()


def _server_webhook_url(server_entry: dict | None) -> str:
    """Stored public URL for the given Plex server, or the per-request default."""
    if server_entry:
        url = ((server_entry.get("output") or {}).get("webhook_public_url") or "").strip()
        if url:
            return url
    return _default_plex_webhook_url()


def _persist_server_webhook_url(server_entry: dict | None, public_url: str) -> None:
    """Write the public URL back onto the server entry's ``output``."""
    from ..settings_manager import get_settings_manager

    if not server_entry:
        return
    settings = get_settings_manager()
    media_servers = list(settings.get("media_servers") or [])
    for i, s in enumerate(media_servers):
        if isinstance(s, dict) and s.get("id") == server_entry.get("id"):
            entry = dict(s)
            output = dict(entry.get("output") or {})
            output["webhook_public_url"] = public_url
            entry["output"] = output
            media_servers[i] = entry
            break
    settings.update({"media_servers": media_servers})


@api.route("/settings/plex_webhook/status")
@setup_or_auth_required
def plex_webhook_status():
    """Return the live registration state of the Plex direct webhook.

    Probes plex.tv on every call so the UI reflects reality (e.g. the
    user revoked the webhook in Plex Web Settings).  Returns Plex Pass
    detection so the UI can disable the toggle when unsupported.

    Accepts ``?server_id=<id>`` to scope the check to one specific Plex
    server (each Plex server has its own token + URL). Without server_id,
    falls back to the first configured Plex server.
    """
    from .. import plex_webhook_registration as pwh

    server_id = (request.args.get("server_id") or "").strip() or None
    server_entry, err, status = _resolve_plex_server_for_webhook(server_id)
    if err:
        return jsonify({"error": err, "error_reason": "server_not_found"}), status

    token = _server_token(server_entry)
    public_url = _server_webhook_url(server_entry)

    has_pass: bool | None
    registered = False
    error: str | None = None
    error_reason: str | None = None

    if not token:
        has_pass = None
        error = "Plex token not configured"
        error_reason = "missing_token"
    else:
        try:
            registered = pwh.is_registered(token, public_url)
            has_pass = True
        except pwh.PlexWebhookError as exc:
            registered = False
            has_pass = False if exc.reason == "plex_pass_required" else None
            error = str(exc)
            error_reason = exc.reason
        except Exception:
            try:
                has_pass = pwh.has_plex_pass(token)
            except Exception:
                has_pass = None
            registered = False

    return jsonify(
        {
            "server_id": server_entry.get("id") if server_entry else None,
            "server_name": server_entry.get("name") if server_entry else None,
            "registered_in_plex": registered,
            "public_url": public_url,
            "default_url": _default_plex_webhook_url(),
            "has_plex_pass": has_pass,
            "error": error,
            "error_reason": error_reason,
            "warning": _loopback_in_docker_warning(public_url),
        }
    )


def _plex_webhook_auth_token() -> str:
    """Return the secret to embed in the registered Plex webhook URL.

    Plex's webhook UI offers no way to set headers or HTTP Basic
    credentials, so the only way for Plex Media Server to authenticate
    against this app's webhook endpoint is via a ``?token=`` query
    parameter. The canonical inbound URL is ``/api/webhooks/incoming``;
    the legacy ``/api/webhooks/plex`` endpoint is kept around for
    installs that registered before the unified router landed.

    Returns the global ``webhook_secret`` (or the API auth token as a
    fallback). Per-server secrets were removed — every Plex server
    in a multi-Plex install shares the same URL token, rotated by
    changing the global secret.
    """
    from ..auth import get_auth_token
    from ..settings_manager import get_settings_manager

    settings = get_settings_manager()
    secret = (settings.get("webhook_secret") or "").strip()
    if secret:
        return secret
    return get_auth_token() or ""


@api.route("/settings/plex_webhook/register", methods=["POST"])
@setup_or_auth_required
def plex_webhook_register():
    """Register the Plex direct webhook with the user's plex.tv account.

    The auth secret is embedded in the URL Plex stores (as a ``?token=``
    query parameter) because Plex's webhook UI doesn't allow custom
    headers or credentials — that's the only way for Plex Media Server
    to authenticate against the receiving endpoint.
    """
    from .. import plex_webhook_registration as pwh

    data = request.get_json() or {}
    server_id = (data.get("server_id") or request.args.get("server_id") or "").strip() or None
    server_entry, err, status = _resolve_plex_server_for_webhook(server_id)
    if err:
        return jsonify({"success": False, "error": err, "reason": "server_not_found"}), status

    token = _server_token(server_entry)
    if not token:
        return (
            jsonify(
                {
                    "success": False,
                    "error": "Plex token not configured for this server. Re-authenticate from the Edit modal.",
                    "reason": "missing_token",
                }
            ),
            400,
        )

    auth_token = _plex_webhook_auth_token()
    if not auth_token:
        return (
            jsonify(
                {
                    "success": False,
                    "error": (
                        "No webhook secret or API token available to embed in the "
                        "Plex webhook URL.  Generate a webhook secret on this page "
                        "or set an API token, then try again."
                    ),
                    "reason": "missing_auth_token",
                }
            ),
            400,
        )

    raw_url = (data.get("public_url") or "").strip()
    public_url = raw_url or _server_webhook_url(server_entry)

    try:
        # K6: pass server_id so the registered URL embeds it. Inbound Plex
        # POSTs then arrive with `?server_id=<id>` and _authenticate_webhook
        # can validate against that server's per-server secret.
        registered_server_id = server_entry.get("id") if server_entry else None
        pwh.register(token, public_url, auth_token=auth_token, server_id=registered_server_id)
    except pwh.PlexWebhookError as exc:
        status_code = 400 if exc.reason in ("missing_url", "missing_token") else 502
        if exc.reason == "plex_pass_required":
            status_code = 403
        return (
            jsonify({"success": False, "error": str(exc), "reason": exc.reason}),
            status_code,
        )

    _persist_server_webhook_url(server_entry, public_url)

    return jsonify(
        {
            "success": True,
            "server_id": server_entry.get("id") if server_entry else None,
            "registered_in_plex": True,
            "public_url": public_url,
        }
    )


@api.route("/settings/plex_webhook/unregister", methods=["POST"])
@setup_or_auth_required
def plex_webhook_unregister():
    """Remove the Plex direct webhook from the user's plex.tv account."""
    from .. import plex_webhook_registration as pwh

    data = request.get_json() or {}
    server_id = (data.get("server_id") or request.args.get("server_id") or "").strip() or None
    server_entry, err, status = _resolve_plex_server_for_webhook(server_id)
    if err:
        return jsonify({"success": False, "error": err, "reason": "server_not_found"}), status

    token = _server_token(server_entry)
    public_url = _server_webhook_url(server_entry)

    if token:
        try:
            pwh.unregister(token, public_url)
        except pwh.PlexWebhookError as exc:
            logger.warning(
                "Could not remove the Plex webhook registration on plex.tv ({}). "
                "Plex may keep firing webhooks at us until you remove the entry manually at "
                "https://app.plex.tv/desktop#!/account → Webhooks. "
                "Check your Plex token is still valid for this server.",
                exc,
            )
            return (
                jsonify({"success": False, "error": str(exc), "reason": exc.reason}),
                502,
            )

    return jsonify(
        {
            "success": True,
            "server_id": server_entry.get("id") if server_entry else None,
            "registered_in_plex": False,
        }
    )


@api.route("/settings/plex_webhook/test", methods=["POST"])
@setup_or_auth_required
def plex_webhook_test_reachability():
    """Self-POST a synthetic ping to the configured public URL.

    Mirrors what real Plex POSTs look like as closely as possible —
    multipart/form-data, ``payload`` part with JSON, and the auth token
    in a ``?token=`` query parameter rather than a header.  Success
    means the URL is routable from this app's process *and* the
    auth token works, which is a strong proxy for whether Plex Media
    Server will actually be able to deliver events.
    """
    import json as _json

    import requests

    from .. import plex_webhook_registration as pwh

    data = request.get_json() or {}
    server_id = (data.get("server_id") or request.args.get("server_id") or "").strip() or None
    server_entry, err, status = _resolve_plex_server_for_webhook(server_id)
    if err:
        return jsonify({"success": False, "error": err}), status

    raw_url = (data.get("public_url") or "").strip()
    public_url = raw_url or _server_webhook_url(server_entry)

    auth_token = _plex_webhook_auth_token()
    if not auth_token:
        return (
            jsonify(
                {
                    "success": False,
                    "error": ("No webhook secret or API token available to authenticate the test request."),
                }
            ),
            400,
        )

    loopback_warning = _loopback_in_docker_warning(public_url)
    if loopback_warning:
        return jsonify(
            {
                "success": False,
                "error": loopback_warning,
                "public_url": public_url,
            }
        )

    test_url = pwh._build_authenticated_url(public_url, auth_token)
    payload = {"event": "test.ping", "source": "plex-previews-self-test"}
    multipart = {"payload": (None, _json.dumps(payload), "application/json")}

    try:
        # verify=False: this is a webhook self-test POST back to the user's
        # own public_url. Home-lab users commonly front the app with
        # self-signed TLS, so strict verification would fail the test for
        # exactly the valid deployments it's meant to check.
        response = requests.post(
            test_url,
            files=multipart,
            timeout=10,
            verify=False,  # nosec B501
        )
    except requests.exceptions.RequestException as exc:
        return (
            jsonify(
                {
                    "success": False,
                    "error": f"Could not reach {public_url}: {exc}",
                    "public_url": public_url,
                }
            ),
            200,
        )

    ok = 200 <= response.status_code < 300
    return jsonify(
        {
            "success": ok,
            "status_code": response.status_code,
            "public_url": public_url,
            "response_excerpt": (response.text or "")[:200],
        }
    )


# ============================================================================
# Setup Wizard
# ============================================================================


@api.route("/setup/status")
def get_setup_status():
    """Check if setup is complete (no auth required for setup check)."""
    from ..settings_manager import get_settings_manager

    settings = get_settings_manager()

    return jsonify(
        {
            "configured": settings.is_configured(),
            "setup_complete": settings.is_setup_complete(),
            "current_step": settings.get_setup_step(),
            "plex_authenticated": settings.is_plex_authenticated(),
        }
    )


@api.route("/setup/state")
@setup_or_auth_required
def get_setup_state():
    """Get current setup wizard state."""
    from ..settings_manager import get_settings_manager

    settings = get_settings_manager()
    state = settings.get_setup_state()

    return jsonify(
        {
            "step": state.get("step", 1),
            "data": state.get("data", {}),
        }
    )


@api.route("/setup/state", methods=["POST"])
@setup_or_auth_required
def save_setup_state():
    """Save setup wizard progress."""
    from ..settings_manager import get_settings_manager

    settings = get_settings_manager()
    data = request.get_json() or {}

    step = data.get("step", 1)
    step_data = data.get("data", {})

    settings.set_setup_state(step, step_data)

    return jsonify({"success": True})


@api.route("/setup/complete", methods=["POST"])
@setup_or_auth_required
def complete_setup():
    """Mark setup as complete."""
    from ..settings_manager import get_settings_manager

    settings = get_settings_manager()
    settings.complete_setup()

    return jsonify({"success": True, "redirect": "/"})


@api.route("/setup/token-info", methods=["GET"])
@setup_or_auth_required
def get_setup_token_info():
    """Get information about the current authentication token for setup wizard."""
    from ..auth import get_token_info

    return jsonify(get_token_info())


@api.route("/setup/skip", methods=["POST"])
@setup_or_auth_required
def skip_setup():
    """Mark setup as complete without configuring any media server.

    Used by the "Skip setup — I'll add my server later" link on Step 1 of the
    setup wizard (Phase H8). Lets Emby/Jellyfin users bypass the Plex-first
    flow and add their server from the Servers page on the dashboard.
    """
    from ..settings_manager import get_settings_manager

    settings = get_settings_manager()
    settings.complete_setup()
    return jsonify({"success": True})


@api.route("/setup/set-token", methods=["POST"])
@setup_or_auth_required
def set_setup_token():
    """Set a custom authentication token during setup."""
    from ..auth import set_auth_token

    data = request.get_json() or {}
    new_token = data.get("token", "")
    confirm_token = data.get("confirm_token", "")

    if new_token != confirm_token:
        return jsonify({"success": False, "error": "Tokens do not match."}), 400

    result = set_auth_token(new_token)

    if not result["success"]:
        return jsonify(result), 400

    return jsonify(result)


@api.route("/setup/validate-paths", methods=["POST"])
@setup_or_auth_required
def validate_paths():
    """Validate path configuration (path_mappings or legacy plex/local pair)."""
    from ...config import normalize_path_mappings

    data = request.get_json() or {}
    plex_data_path = data.get("plex_config_folder", "/plex")
    path_mappings = normalize_path_mappings(data)
    plex_media_path = data.get("plex_videos_path_mapping", "")
    local_media_path = data.get("plex_local_videos_path_mapping", "")

    result = {"valid": True, "errors": [], "warnings": [], "info": []}

    # Validate Plex Data Path
    if not plex_data_path:
        result["errors"].append("Plex Data Path is required")
        result["valid"] = False
    else:
        if "\x00" in plex_data_path:
            result["errors"].append("Invalid Plex Data Path")
            result["valid"] = False
            return jsonify(result)

        resolved_plex_data_path = _safe_resolve_within(plex_data_path, PLEX_DATA_ROOT)

        if resolved_plex_data_path is None:
            canonical_root = os.path.realpath(PLEX_DATA_ROOT)
            result["errors"].append(f"Plex Data Path must be within the configured root: {canonical_root}")
            result["valid"] = False
            return jsonify(result)

        if not os.path.exists(resolved_plex_data_path):
            result["errors"].append(f"Plex data folder not found: {resolved_plex_data_path}")
            result["valid"] = False
        else:
            media_path = os.path.join(resolved_plex_data_path, "Media")
            localhost_path = os.path.join(media_path, "localhost")

            if not os.path.exists(media_path):
                result["errors"].append(f'Plex data folder ({resolved_plex_data_path}): missing "Media" subfolder')
                result["valid"] = False
            elif not os.path.exists(localhost_path):
                result["errors"].append(
                    f'Plex data folder ({resolved_plex_data_path}): missing "Media/localhost" subfolder'
                )
                result["valid"] = False
            else:
                try:
                    contents = os.listdir(localhost_path)
                    hex_dirs = [d for d in contents if len(d) == 1 and d in "0123456789abcdef"]
                    if len(hex_dirs) >= 10:
                        result["info"].append(
                            f"✓ Plex data folder ({resolved_plex_data_path}): valid structure ({len(hex_dirs)} hash directories)"
                        )
                    else:
                        result["warnings"].append(
                            f"Plex data folder ({resolved_plex_data_path}): structure looks incomplete ({len(hex_dirs)}/16 hash directories)"
                        )
                except Exception as e:
                    logger.warning(
                        "Setup Wizard: could not verify the Plex data folder's internal structure ({}: {}). "
                        "The wizard will continue with a 'structure unverified' note instead of blocking. "
                        "Check the folder is the correct Plex 'Media' parent (it should contain a 'localhost' "
                        "subfolder with single-character hex directories like 0/1/2/.../f).",
                        type(e).__name__,
                        e,
                    )
                    result["warnings"].append(
                        f"Plex data folder ({resolved_plex_data_path}): could not verify structure"
                    )

            if os.access(resolved_plex_data_path, os.W_OK):
                result["info"].append(f"✓ Plex data folder ({resolved_plex_data_path}): write permissions OK")
            else:
                result["errors"].append(
                    f"Plex data folder ({resolved_plex_data_path}): no write permission — check PUID/PGID"
                )
                result["valid"] = False

    # Validate Path Mapping (path_mappings rows or legacy pair)
    if path_mappings:
        for i, row in enumerate(path_mappings):
            plex_prefix = (row.get("plex_prefix") or "").strip()
            local_prefix = (row.get("local_prefix") or "").strip()
            row_label = f"Row {i + 1}"
            path_desc = f"{plex_prefix} → {local_prefix}" if plex_prefix else local_prefix
            if "\x00" in local_prefix:
                result["errors"].append(f"{row_label} ({path_desc}): invalid path")
                result["valid"] = False
                continue
            if not local_prefix:
                continue
            resolved = _safe_resolve_within(local_prefix, MEDIA_ROOT)
            if resolved is None:
                result["errors"].append(f"{row_label} ({path_desc}): path must be inside the allowed media folder")
                result["valid"] = False
            elif not os.path.exists(resolved):
                result["errors"].append(f"{row_label} ({path_desc}): folder not found")
                result["valid"] = False
            else:
                try:
                    contents = os.listdir(resolved)
                    result["info"].append(f"✓ {row_label} ({path_desc}): accessible ({len(contents)} items)")
                except Exception as e:
                    logger.error(
                        "Setup Wizard: cannot read the local path for one of the path-mapping rows ({}: {}). "
                        "The wizard's validation will flag this row in the UI. "
                        "Check the folder exists, is readable by the app's user (Docker: PUID/PGID), "
                        "and is inside the configured media root.",
                        type(e).__name__,
                        e,
                    )
                    result["errors"].append(f"{row_label} ({path_desc}): cannot read folder")
                    result["valid"] = False
    elif plex_media_path or local_media_path:
        if plex_media_path and not local_media_path:
            result["errors"].append("Local Media Path is required when Plex Media Path is set")
            result["valid"] = False
        elif local_media_path and not plex_media_path:
            result["errors"].append("Plex Media Path is required when Local Media Path is set")
            result["valid"] = False
        elif local_media_path:
            if "\x00" in local_media_path:
                result["errors"].append("Invalid Local Media Path")
                result["valid"] = False
                return jsonify(result)
            resolved_local_media = _safe_resolve_within(local_media_path, MEDIA_ROOT)
            if resolved_local_media is None:
                result["errors"].append("Invalid Local Media Path (must be within the configured media root)")
                result["valid"] = False
                return jsonify(result)
            if not os.path.exists(resolved_local_media):
                result["errors"].append(f"Local media path ({resolved_local_media}): folder not found")
                result["valid"] = False
            else:
                try:
                    contents = os.listdir(resolved_local_media)
                    if len(contents) == 0:
                        result["warnings"].append(f"Local media path ({resolved_local_media}): folder is empty")
                    else:
                        result["info"].append(
                            f"✓ Local media path ({resolved_local_media}): accessible ({len(contents)} items)"
                        )
                except Exception as e:
                    logger.error(
                        "Setup Wizard: cannot read the configured Local Media Path ({}: {}). "
                        "The wizard's validation will flag this in the UI. "
                        "Check the folder exists, is readable by the app's user (Docker: PUID/PGID), "
                        "and is inside the configured media root.",
                        type(e).__name__,
                        e,
                    )
                    result["errors"].append(f"Local media path ({resolved_local_media}): cannot read folder")
                    result["valid"] = False
    else:
        result["info"].append("No path mapping configured (media mounted at same path as Plex)")

    return jsonify(result)


# ============================================================================
# J6 — Backup recovery (settings.json / schedules.json / webhook_history.json)
# ============================================================================
#
# Each writer that uses ``atomic_json_save_with_backup`` (J1+J2) leaves a
# rolling ``.bak`` next to the live file. These endpoints surface that on the
# Settings page so a clobbered live file is one click away from recovery.
# Job history (jobs.db) is not included — see the panel description.

_BACKUP_FILES = ("settings.json", "schedules.json", "webhook_history.json", "setup_state.json")


def _list_backups_for(live_path: str) -> list[dict]:
    """Return all backup snapshots for a single live file, newest first.

    Recognises both the new ``filepath.{YYYYMMDD-HHMMSS}.bak`` form and the
    legacy single ``filepath.bak`` left over from older app versions.
    """
    import glob

    out: list[dict] = []
    # New timestamped backups.
    for path in glob.glob(live_path + ".*.bak"):
        # Skip the legacy form that happens to also match (e.g. backup.bak)
        # — it would have no timestamp segment between the dots.
        suffix = path[len(live_path) :]  # e.g. ".20260429-211544.bak"
        parts = suffix.split(".")
        if len(parts) != 3 or parts[0] != "" or parts[2] != "bak":
            continue
        ts_raw = parts[1]
        # Validate timestamp shape; skip if malformed.
        if len(ts_raw) != 15 or ts_raw[8] != "-" or not (ts_raw[:8] + ts_raw[9:]).isdigit():
            continue
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        out.append(
            {
                "filename": os.path.basename(path),
                "path": path,
                "timestamp": ts_raw,
                "mtime": mtime,
                "legacy": False,
            }
        )
    # Legacy single rolling backup.
    legacy = live_path + ".bak"
    if os.path.exists(legacy):
        try:
            out.append(
                {
                    "filename": os.path.basename(legacy),
                    "path": legacy,
                    "timestamp": None,
                    "mtime": os.path.getmtime(legacy),
                    "legacy": True,
                }
            )
        except OSError:
            pass
    # Newest first by mtime so the UI shows recent snapshots at the top.
    out.sort(key=lambda r: r["mtime"], reverse=True)
    return out


def _backup_inventory() -> list[dict]:
    """Inspect the config directory and report all backups per managed file."""
    from ..settings_manager import get_settings_manager

    cfg_dir = str(get_settings_manager().config_dir)
    rows = []
    for name in _BACKUP_FILES:
        live = os.path.join(cfg_dir, name)
        live_mtime = os.path.getmtime(live) if os.path.exists(live) else None
        backups = _list_backups_for(live)
        # If any backup is newer than the live file the user has likely been
        # clobbered (e.g. by an old container truncating settings.json) — UI
        # surfaces this as a "restore me" highlight.
        newest_bak_mtime = backups[0]["mtime"] if backups else None
        bak_newer = live_mtime is not None and newest_bak_mtime is not None and newest_bak_mtime > live_mtime
        rows.append(
            {
                "name": name,
                "live_path": live,
                "live_mtime": live_mtime,
                "backups": backups,
                "has_bak": bool(backups),
                "bak_newer": bak_newer,
            }
        )
    return rows


@api.route("/settings/backups")
@setup_or_auth_required
def list_backups():
    """List per-file backup status for the Settings > Backups panel."""
    return jsonify({"files": _backup_inventory()})


@api.route("/settings/backups/restore", methods=["POST"])
@api_token_required
def restore_backup():
    """Restore a specific backup snapshot for a managed file.

    Body: ``{"file": "settings.json", "backup": "settings.json.20260429-211544.bak"}``.
    The named backup is COPIED over the live file (the backup itself is
    preserved so the user can restore the same point-in-time again).
    Before overwriting, the current live contents are saved as a fresh
    timestamped backup so a misclick is recoverable via a second restore.

    For backwards compatibility, ``backup`` is optional — if omitted, the
    newest available backup (timestamped or legacy) is restored.
    """
    import shutil

    data = request.get_json() or {}
    name = (data.get("file") or "").strip()
    backup_filename = (data.get("backup") or "").strip()

    if name not in _BACKUP_FILES:
        return jsonify({"success": False, "error": f"Refusing to restore unknown file {name!r}"}), 400

    from ..settings_manager import get_settings_manager

    cfg_dir = str(get_settings_manager().config_dir)
    live = os.path.join(cfg_dir, name)

    available = _list_backups_for(live)
    if not available:
        return jsonify({"success": False, "error": f"No backups available for {name}"}), 404

    if backup_filename:
        # Resolve by basename; reject anything that isn't one of ours
        # so we can't be talked into copying /etc/passwd over settings.json.
        match = next((b for b in available if b["filename"] == backup_filename), None)
        if match is None:
            return jsonify({"success": False, "error": f"No backup named {backup_filename!r} for {name}"}), 404
        bak_path = match["path"]
    else:
        bak_path = available[0]["path"]

    # Snapshot the current live contents into a fresh timestamped backup
    # before overwriting. Lets the user undo a restore by restoring this
    # snapshot in turn. Best-effort — never blocks the primary restore.
    if os.path.exists(live):
        try:
            from datetime import datetime, timezone

            from ...utils import _backup_retention, _prune_old_backups

            ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            shutil.copy2(live, f"{live}.{ts}.bak")
            _prune_old_backups(live, _backup_retention())
        except OSError as exc:
            logger.debug("Pre-restore snapshot of {} failed: {}", live, exc)

    try:
        shutil.copy2(bak_path, live)
    except OSError as exc:
        return jsonify({"success": False, "error": str(exc)}), 500

    logger.info("Restored {} from {}", live, bak_path)
    return jsonify(
        {
            "success": True,
            "file": name,
            "backup": os.path.basename(bak_path),
            "note": "Reload the app (or click Refresh) for in-memory caches to pick up the restored content.",
        }
    )
