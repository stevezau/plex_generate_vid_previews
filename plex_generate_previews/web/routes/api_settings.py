"""Settings and setup wizard API routes."""

import os

from flask import jsonify, request
from loguru import logger

from ...config import validate_processing_thread_totals
from ..auth import api_token_required, setup_or_auth_required
from . import api
from ._helpers import (
    MEDIA_ROOT,
    PLEX_DATA_ROOT,
    _safe_resolve_within,
)


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
        logger.warning("Failed to reconcile GPU workers", exc_info=True)


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
    """Get all settings."""
    from ..settings_manager import get_settings_manager

    settings = get_settings_manager()

    return jsonify(
        {
            "plex_url": settings.plex_url or "",
            "plex_token": "****" if settings.plex_token else "",
            "plex_name": settings.plex_name or "",
            "plex_verify_ssl": settings.plex_verify_ssl,
            "plex_config_folder": settings.plex_config_folder or "/plex",
            "selected_libraries": settings.selected_libraries,
            "media_path": settings.media_path or "",
            "plex_videos_path_mapping": settings.get("plex_videos_path_mapping", ""),
            "plex_local_videos_path_mapping": settings.get("plex_local_videos_path_mapping", ""),
            "path_mappings": settings.get("path_mappings", []),
            "exclude_paths": settings.get("exclude_paths", []),
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
            "plex_webhook_enabled": bool(settings.get("plex_webhook_enabled", False)),
            "plex_webhook_public_url": settings.get("plex_webhook_public_url", "") or "",
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
        "plex_webhook_enabled",
        "plex_webhook_public_url",
    ]

    updates = {k: v for k, v in data.items() if k in allowed_fields}

    if "plex_webhook_public_url" in updates:
        updates["plex_webhook_public_url"] = str(updates.get("plex_webhook_public_url") or "").strip()
    if "plex_webhook_enabled" in updates:
        updates["plex_webhook_enabled"] = bool(updates["plex_webhook_enabled"])

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

    thread_warning = ""
    if updates:
        settings.update(updates)
        logger.info(f"Settings updated: {list(updates.keys())}")

        if "gpu_config" in updates:
            _reconcile_live_gpu_workers(settings)

        ok, thread_warning = validate_processing_thread_totals(settings.get_all())
        if not ok:
            logger.warning(thread_warning)
            _auto_pause_if_needed(settings)
        elif settings.processing_paused:
            _auto_resume_if_needed(settings)

        # Invalidate the Plex library cache when connection details change
        # so the next libraries request fetches fresh data.
        plex_fields = {"plex_url", "plex_token", "plex_verify_ssl"}
        if plex_fields & updates.keys():
            from .api_system import clear_library_cache

            clear_library_cache()

        # Rotating the webhook secret invalidates the token embedded in
        # the registered Plex webhook URL — re-register so Plex picks up
        # the new value.  Best-effort; failures are logged but don't
        # block the settings save.
        if "webhook_secret" in updates and settings.get("plex_webhook_enabled"):
            try:
                from .. import plex_webhook_registration as pwh

                plex_token = settings.plex_token or ""
                public_url = (settings.get("plex_webhook_public_url") or "").strip() or _default_plex_webhook_url()
                new_auth = _plex_webhook_auth_token()
                if plex_token and new_auth:
                    pwh.register(plex_token, public_url, auth_token=new_auth)
                    logger.info("Plex webhook re-registered with new auth token after secret rotation")
            except Exception:
                logger.warning(
                    "Failed to re-register Plex webhook after secret change",
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
    logger.info(f"Log level changed to {level}")

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
    return f"{base}/api/webhooks/plex"


@api.route("/settings/plex_webhook/status")
@setup_or_auth_required
def plex_webhook_status():
    """Return the live registration state of the Plex direct webhook.

    Probes plex.tv on every call so the UI reflects reality (e.g. the
    user revoked the webhook in Plex Web Settings).  Returns Plex Pass
    detection so the UI can disable the toggle when unsupported.
    """
    from .. import plex_webhook_registration as pwh
    from ..settings_manager import get_settings_manager

    settings = get_settings_manager()
    token = settings.plex_token or ""
    public_url = (settings.get("plex_webhook_public_url") or "").strip()
    if not public_url:
        public_url = _default_plex_webhook_url()

    enabled_in_settings = bool(settings.get("plex_webhook_enabled", False))

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
            "enabled_in_settings": enabled_in_settings,
            "registered_in_plex": registered,
            "public_url": public_url,
            "default_url": _default_plex_webhook_url(),
            "has_plex_pass": has_pass,
            "error": error,
            "error_reason": error_reason,
        }
    )


def _plex_webhook_auth_token() -> str:
    """Return the secret to embed in the registered Plex webhook URL.

    Plex's webhook UI offers no way to set headers or HTTP Basic
    credentials, so the only way for Plex Media Server to authenticate
    against this app's ``/api/webhooks/plex`` endpoint is via a
    ``?token=`` query parameter.  We pick the dedicated webhook secret
    when configured, otherwise fall back to the main API auth token —
    matching the order ``_authenticate_webhook`` checks them in.
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
    from ..settings_manager import get_settings_manager

    settings = get_settings_manager()
    token = settings.plex_token or ""
    if not token:
        return (
            jsonify(
                {
                    "success": False,
                    "error": "Plex token not configured. Re-run the Setup Wizard.",
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

    data = request.get_json() or {}
    raw_url = (data.get("public_url") or "").strip()
    public_url = raw_url or _default_plex_webhook_url()

    try:
        pwh.register(token, public_url, auth_token=auth_token)
    except pwh.PlexWebhookError as exc:
        status_code = 400 if exc.reason in ("missing_url", "missing_token") else 502
        if exc.reason == "plex_pass_required":
            status_code = 403
        return (
            jsonify({"success": False, "error": str(exc), "reason": exc.reason}),
            status_code,
        )

    settings.update(
        {
            "plex_webhook_enabled": True,
            "plex_webhook_public_url": public_url,
        }
    )

    return jsonify(
        {
            "success": True,
            "registered_in_plex": True,
            "public_url": public_url,
        }
    )


@api.route("/settings/plex_webhook/unregister", methods=["POST"])
@setup_or_auth_required
def plex_webhook_unregister():
    """Remove the Plex direct webhook from the user's plex.tv account."""
    from .. import plex_webhook_registration as pwh
    from ..settings_manager import get_settings_manager

    settings = get_settings_manager()
    token = settings.plex_token or ""
    public_url = (settings.get("plex_webhook_public_url") or "").strip()
    if not public_url:
        public_url = _default_plex_webhook_url()

    if token:
        try:
            pwh.unregister(token, public_url)
        except pwh.PlexWebhookError as exc:
            # Surface the error but still flip the local toggle off so
            # the UI doesn't get stuck in a confusing in-between state.
            logger.warning("Plex webhook unregister failed: {}", exc)
            settings.update({"plex_webhook_enabled": False})
            return (
                jsonify(
                    {
                        "success": False,
                        "error": str(exc),
                        "reason": exc.reason,
                        "enabled_in_settings": False,
                    }
                ),
                502,
            )

    settings.update({"plex_webhook_enabled": False})
    return jsonify(
        {
            "success": True,
            "registered_in_plex": False,
            "enabled_in_settings": False,
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
    from ..settings_manager import get_settings_manager

    settings = get_settings_manager()
    data = request.get_json() or {}
    raw_url = (data.get("public_url") or "").strip()
    public_url = raw_url or ((settings.get("plex_webhook_public_url") or "").strip() or _default_plex_webhook_url())

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
                    logger.warning(f"Could not verify Plex structure: {e}")
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
                    logger.error(f"Cannot read mapping local path: {e}")
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
                    logger.error(f"Cannot read Local Media Path: {e}")
                    result["errors"].append(f"Local media path ({resolved_local_media}): cannot read folder")
                    result["valid"] = False
    else:
        result["info"].append("No path mapping configured (media mounted at same path as Plex)")

    return jsonify(result)
