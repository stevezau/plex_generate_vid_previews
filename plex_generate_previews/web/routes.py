"""Flask routes for the web interface.

Provides both page routes and API endpoints for the dashboard,
settings, schedules, and job management.

Rate limiting is applied to authentication endpoints:
- /login: 5 requests per minute (brute force protection)
- /api/auth/login: 10 requests per minute (API access)

For multi-worker deployments (e.g., gunicorn with multiple workers),
configure Redis storage via RATELIMIT_STORAGE_URL environment variable.
"""

import os
import threading

from flask import (
    Blueprint,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_socketio import join_room, leave_room
from loguru import logger

from ..media_processing import (
    _verify_tmp_folder_health,
    clear_failures,
    get_failures,
    log_failure_summary,
)
from .auth import (
    api_token_required,
    is_authenticated,
    login_required,
    regenerate_token,
    setup_or_auth_required,
    validate_token,
)
from .jobs import get_job_manager
from .scheduler import get_schedule_manager

# Define safe root directories for user-provided paths. All user-supplied
# paths must resolve within these directories before any filesystem
# operations are performed. Override via environment variables.
PLEX_DATA_ROOT = os.path.realpath(os.environ.get("PLEX_DATA_ROOT", "/plex"))
MEDIA_ROOT = os.path.realpath(os.environ.get("MEDIA_ROOT", "/"))


def _is_within_base(base_path: str, candidate_path: str) -> bool:
    """Return True if candidate_path is inside (or equal to) base_path.

    Both paths are resolved via os.path.realpath before comparison.
    Uses a trailing-separator check to avoid prefix collisions
    (e.g. /plex2 should not match /plex).
    """
    base_real = os.path.realpath(base_path)
    candidate_real = os.path.realpath(candidate_path)
    if base_real == candidate_real:
        return True
    base_with_sep = base_real if base_real.endswith(os.sep) else base_real + os.sep
    return candidate_real.startswith(base_with_sep)


def _safe_resolve_within(user_path: str, allowed_root: str) -> str | None:
    """Resolve a user-provided path and verify it stays within *allowed_root*.

    Returns the canonical absolute path on success, or ``None`` when the
    path contains null bytes or escapes the allowed root directory.

    The implementation uses ``os.path.normpath`` followed by a
    ``str.startswith`` guard, which is the pattern recognised by CodeQL
    as path-traversal sanitisation (py/path-injection).
    """
    if "\x00" in user_path:
        return None

    # Normalise (collapse ../ segments) before resolving
    normalized = os.path.normpath(user_path)

    # Resolve relative paths against the allowed root
    if not os.path.isabs(normalized):
        normalized = os.path.normpath(os.path.join(allowed_root, normalized))

    # Canonicalise both paths (follows symlinks)
    resolved = os.path.realpath(normalized)
    root_real = os.path.realpath(allowed_root)

    # Containment check: resolved must be root or a child of root
    if resolved == root_real:
        return resolved
    if not resolved.startswith(root_real + os.sep):
        return None

    return resolved


# Create blueprints
main = Blueprint("main", __name__)
api = Blueprint("api", __name__, url_prefix="/api")

# Cache for GPU detection results (GPUs don't change at runtime)
_gpu_cache: dict = {"result": None, "timestamp": 0.0}
_GPU_CACHE_TTL = 300  # 5 minutes


def clear_gpu_cache() -> None:
    """Reset the GPU detection cache.

    Useful for tests and when the user explicitly requests a re-scan.
    """
    _gpu_cache["result"] = None
    _gpu_cache["timestamp"] = 0.0


# Initialize rate limiter
# Uses in-memory storage by default (suitable for single-worker deployments)
# For multi-worker deployments, set RATELIMIT_STORAGE_URL=redis://localhost:6379
# Note: Rate limiting is only applied to specific endpoints (login, auth)
# Dashboard APIs are exempt since they poll frequently
limiter = Limiter(
    key_func=get_remote_address,
    default_limits=[],  # No default limits - only apply to specific endpoints
    storage_uri=os.environ.get("RATELIMIT_STORAGE_URL", "memory://"),
)


# ============================================================================
# Page Routes
# ============================================================================


@main.route("/")
@login_required
def index():
    """Dashboard page."""
    return render_template("index.html")


@main.route("/login", methods=["GET", "POST"])
@limiter.limit("5 per minute", methods=["POST"])
def login():
    """Login page. Rate limited to 5 POST requests per minute."""
    if request.method == "POST":
        token = request.form.get("token", "")
        if validate_token(token):
            session["authenticated"] = True
            session.permanent = True
            logger.info("User logged in successfully")
            return redirect(url_for("main.index"))
        return render_template("login.html", error="Invalid token")

    if is_authenticated():
        return redirect(url_for("main.index"))
    return render_template("login.html")


@main.route("/logout")
def logout():
    """Logout and clear session."""
    session.clear()
    return redirect(url_for("main.login"))


@main.route("/settings")
@login_required
def settings():
    """Settings page."""
    return render_template("settings.html")


@main.route("/webhooks")
@login_required
def webhooks_page():
    """Webhooks configuration page."""
    return render_template("webhooks.html")


# ============================================================================
# API Routes - Authentication
# ============================================================================


@api.route("/auth/status")
def auth_status():
    """Check authentication status."""
    return jsonify({"authenticated": is_authenticated()})


@api.route("/auth/login", methods=["POST"])
@limiter.limit("10 per minute")
def api_login():
    """API login endpoint. Rate limited to 10 requests per minute."""
    data = request.get_json() or {}
    token = data.get("token", "")

    if validate_token(token):
        session["authenticated"] = True
        session.permanent = True
        return jsonify({"success": True})

    return jsonify({"success": False, "error": "Invalid token"}), 401


@api.route("/auth/logout", methods=["POST"])
def api_logout():
    """API logout endpoint."""
    session.clear()
    return jsonify({"success": True})


@api.route("/token/regenerate", methods=["POST"])
@api_token_required
def api_regenerate_token():
    """Regenerate authentication token."""
    new_token = regenerate_token()
    # Clear session to require re-authentication
    session.clear()
    masked = "****" + new_token[-4:] if len(new_token) > 4 else "****"
    return jsonify({"success": True, "token": masked})


# ============================================================================
# API Routes - Jobs
# ============================================================================


@api.route("/jobs")
@api_token_required
def get_jobs():
    """Get all jobs."""
    try:
        job_manager = get_job_manager()
        jobs = [job.to_dict() for job in job_manager.get_all_jobs()]
        return jsonify({"jobs": jobs})
    except Exception as e:
        logger.error(f"Failed to get jobs: {e}")
        return jsonify({"error": "Failed to retrieve jobs", "jobs": []}), 500


@api.route("/jobs/<job_id>")
@api_token_required
def get_job(job_id):
    """Get a specific job."""
    job_manager = get_job_manager()
    job = job_manager.get_job(job_id)
    if job:
        return jsonify(job.to_dict())
    return jsonify({"error": "Job not found"}), 404


@api.route("/jobs", methods=["POST"])
@api_token_required
def create_job():
    """Create a new job."""
    data = request.get_json() or {}

    # Support library_names (array of lowercase names) or library_ids (for backward compat)
    library_names = data.get("library_names") or []
    library_ids = data.get("library_ids") or []
    if not library_names and data.get("library_id"):
        library_ids = [data.get("library_id")]

    job_manager = get_job_manager()
    job = job_manager.create_job(
        library_id=",".join(library_names)
        if library_names
        else (",".join(library_ids) if library_ids else None),
        library_name=data.get("library_name", ""),
        config=data.get("config", {}),
    )

    # Start job execution in background with selected libraries
    config_overrides = data.get("config", {})
    if library_names:
        config_overrides["selected_libraries"] = library_names
    elif library_ids:
        config_overrides["selected_library_ids"] = library_ids
    else:
        # "All Libraries" selected - explicitly clear to override any saved settings
        config_overrides["selected_libraries"] = []

    _start_job_async(job.id, config_overrides)

    return jsonify(job.to_dict()), 201


@api.route("/jobs/<job_id>/cancel", methods=["POST"])
@api_token_required
def cancel_job(job_id):
    """Cancel a job."""
    job_manager = get_job_manager()
    # Request cancellation (for running jobs)
    job_manager.request_cancellation(job_id)
    job = job_manager.cancel_job(job_id)
    if job:
        return jsonify(job.to_dict())
    return jsonify({"error": "Job not found"}), 404


@api.route("/jobs/<job_id>/logs", methods=["GET"])
@api_token_required
def get_job_logs(job_id):
    """Get logs for a specific job."""
    job_manager = get_job_manager()
    job = job_manager.get_job(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404

    last_n = request.args.get("last", type=int)
    logs = job_manager.get_logs(job_id, last_n)

    return jsonify({"job_id": job_id, "logs": logs, "count": len(logs)})


@api.route("/jobs/workers", methods=["GET"])
@api_token_required
def get_worker_statuses():
    """Get status of all workers."""
    try:
        job_manager = get_job_manager()
        workers = job_manager.get_worker_statuses()

        return jsonify(
            {"workers": [w.to_dict() if hasattr(w, "to_dict") else w for w in workers]}
        )
    except Exception as e:
        logger.error(f"Failed to get worker statuses: {e}")
        return jsonify(
            {"error": "Failed to retrieve worker statuses", "workers": []}
        ), 500


@api.route("/jobs/<job_id>", methods=["DELETE"])
@api_token_required
def delete_job(job_id):
    """Delete a job."""
    job_manager = get_job_manager()
    if job_manager.delete_job(job_id):
        return jsonify({"success": True})
    return jsonify({"error": "Job not found or is running"}), 404


@api.route("/jobs/clear", methods=["POST"])
@api_token_required
def clear_jobs():
    """Clear jobs by status.

    Accepts optional JSON body: {"statuses": ["completed", "failed", "cancelled"]}
    Defaults to clearing all three if omitted.
    """
    job_manager = get_job_manager()
    data = request.get_json(silent=True) or {}
    statuses = data.get("statuses")  # None means clear all terminal
    count = job_manager.clear_completed_jobs(statuses=statuses)
    return jsonify({"success": True, "cleared": count})


@api.route("/jobs/stats")
@api_token_required
def get_job_stats():
    """Get job statistics."""
    try:
        job_manager = get_job_manager()
        return jsonify(job_manager.get_stats())
    except Exception as e:
        logger.error(f"Failed to get job stats: {e}")
        return jsonify(
            {
                "error": "Failed to retrieve job statistics",
                "pending": 0,
                "running": 0,
                "completed": 0,
                "failed": 0,
                "cancelled": 0,
                "total": 0,
            }
        ), 500


# ============================================================================
# API Routes - Schedules
# ============================================================================


@api.route("/schedules")
@api_token_required
def get_schedules():
    """Get all schedules."""
    schedule_manager = get_schedule_manager()
    return jsonify({"schedules": schedule_manager.get_all_schedules()})


@api.route("/schedules/<schedule_id>")
@api_token_required
def get_schedule(schedule_id):
    """Get a specific schedule."""
    schedule_manager = get_schedule_manager()
    schedule = schedule_manager.get_schedule(schedule_id)
    if schedule:
        return jsonify(schedule)
    return jsonify({"error": "Schedule not found"}), 404


@api.route("/schedules", methods=["POST"])
@api_token_required
def create_schedule():
    """Create a new schedule."""
    data = request.get_json() or {}

    if not data.get("name"):
        return jsonify({"error": "Name is required"}), 400

    if not data.get("cron_expression") and not data.get("interval_minutes"):
        return jsonify(
            {"error": "Either cron_expression or interval_minutes is required"}
        ), 400

    try:
        schedule_manager = get_schedule_manager()
        schedule = schedule_manager.create_schedule(
            name=data["name"],
            cron_expression=data.get("cron_expression"),
            interval_minutes=data.get("interval_minutes"),
            library_id=data.get("library_id"),
            library_name=data.get("library_name", ""),
            config=data.get("config", {}),
            enabled=data.get("enabled", True),
        )
        return jsonify(schedule), 201
    except ValueError as e:
        logger.warning(f"Schedule validation error: {e}")
        return jsonify({"error": "Invalid schedule parameters"}), 400
    except Exception as e:
        import traceback

        logger.error(f"Failed to create schedule: {e}\n{traceback.format_exc()}")
        return jsonify({"error": "Failed to create schedule"}), 500


@api.route("/schedules/<schedule_id>", methods=["PUT"])
@api_token_required
def update_schedule(schedule_id):
    """Update a schedule."""
    data = request.get_json() or {}

    schedule_manager = get_schedule_manager()
    schedule = schedule_manager.update_schedule(
        schedule_id=schedule_id,
        name=data.get("name"),
        cron_expression=data.get("cron_expression"),
        interval_minutes=data.get("interval_minutes"),
        library_id=data.get("library_id"),
        library_name=data.get("library_name"),
        config=data.get("config"),
        enabled=data.get("enabled"),
    )

    if schedule:
        return jsonify(schedule)
    return jsonify({"error": "Schedule not found"}), 404


@api.route("/schedules/<schedule_id>", methods=["DELETE"])
@api_token_required
def delete_schedule(schedule_id):
    """Delete a schedule."""
    schedule_manager = get_schedule_manager()
    if schedule_manager.delete_schedule(schedule_id):
        return jsonify({"success": True})
    return jsonify({"error": "Schedule not found"}), 404


@api.route("/schedules/<schedule_id>/enable", methods=["POST"])
@api_token_required
def enable_schedule(schedule_id):
    """Enable a schedule."""
    schedule_manager = get_schedule_manager()
    schedule = schedule_manager.enable_schedule(schedule_id)
    if schedule:
        return jsonify(schedule)
    return jsonify({"error": "Schedule not found"}), 404


@api.route("/schedules/<schedule_id>/disable", methods=["POST"])
@api_token_required
def disable_schedule(schedule_id):
    """Disable a schedule."""
    schedule_manager = get_schedule_manager()
    schedule = schedule_manager.disable_schedule(schedule_id)
    if schedule:
        return jsonify(schedule)
    return jsonify({"error": "Schedule not found"}), 404


@api.route("/schedules/<schedule_id>/run", methods=["POST"])
@api_token_required
def run_schedule_now(schedule_id):
    """Run a schedule immediately."""
    schedule_manager = get_schedule_manager()
    if schedule_manager.run_now(schedule_id):
        return jsonify({"success": True})
    return jsonify({"error": "Schedule not found"}), 404


# ============================================================================
# API Routes - Libraries
# ============================================================================


def _fetch_libraries_via_http(
    plex_url: str, plex_token: str, include_count: bool = False
) -> list:
    """Fetch Plex libraries via direct HTTP request.

    Args:
        plex_url: Plex server URL
        plex_token: Plex authentication token
        include_count: Whether to include totalSize count

    Returns:
        List of library dicts with id, name, type (and optionally count)
    """
    import requests

    response = requests.get(
        f"{plex_url.rstrip('/')}/library/sections",
        headers={"X-Plex-Token": plex_token, "Accept": "application/json"},
        timeout=10,
    )
    response.raise_for_status()
    data = response.json()

    libraries = []
    for section in data.get("MediaContainer", {}).get("Directory", []):
        if section.get("type") in ("movie", "show"):
            lib = {
                "id": str(section.get("key")),
                "name": section.get("title"),
                "type": section.get("type"),
            }
            if include_count:
                lib["count"] = section.get("totalSize", 0)
            libraries.append(lib)
    return libraries


@api.route("/libraries")
@api_token_required
def get_libraries():
    """Get available Plex libraries."""
    try:
        from .settings_manager import get_settings_manager

        settings = get_settings_manager()
        plex_url = settings.plex_url
        plex_token = settings.plex_token

        if not plex_url or not plex_token:
            # Fall back to load_config for env var based config
            try:
                from ..config import load_config
                from ..plex_client import get_plex_client

                config = load_config()
                if config is None:
                    return jsonify(
                        {
                            "error": "Plex not configured. Complete setup in Settings.",
                            "libraries": [],
                        }
                    ), 400

                plex = get_plex_client(config)

                libraries = []
                for section in plex.library.sections():
                    if section.type in ("movie", "show"):
                        libraries.append(
                            {
                                "id": str(section.key),
                                "name": section.title,
                                "type": section.type,
                                "count": section.totalSize,
                            }
                        )

                return jsonify({"libraries": libraries})
            except Exception as e:
                logger.error(f"Failed to get libraries via config: {e}")
                return jsonify(
                    {
                        "error": "Plex not configured. Complete setup in Settings.",
                        "libraries": [],
                    }
                ), 400

        # Use settings.json values
        libraries = _fetch_libraries_via_http(plex_url, plex_token, include_count=True)

        return jsonify({"libraries": libraries})
    except Exception as e:
        logger.error(f"Failed to get libraries: {e}")
        return jsonify({"error": "Failed to retrieve libraries", "libraries": []}), 500


# ============================================================================
# API Routes - System
# ============================================================================


@api.route("/system/status")
@setup_or_auth_required
def get_system_status():
    """Get system status including GPU info.

    GPU detection results are cached for ``_GPU_CACHE_TTL`` seconds because
    the detection runs FFmpeg sub-processes that are expensive and hardware
    does not change at runtime.
    """
    import time

    try:
        now = time.monotonic()
        if (
            _gpu_cache["result"] is None
            or (now - _gpu_cache["timestamp"]) > _GPU_CACHE_TTL
        ):
            from ..gpu_detection import detect_all_gpus

            gpus_raw = detect_all_gpus()
            gpus = []
            for gpu_type, gpu_device, gpu_info in gpus_raw:
                gpus.append(
                    {
                        "name": gpu_info.get("name", gpu_type),
                        "type": gpu_type,
                        "device": gpu_device,
                    }
                )
            _gpu_cache["result"] = gpus
            _gpu_cache["timestamp"] = now
            logger.debug(f"GPU detection cache refreshed: {len(gpus)} GPU(s)")
        else:
            gpus = _gpu_cache["result"]

        job_manager = get_job_manager()
        running_job = job_manager.get_running_job()

        return jsonify(
            {
                "gpus": gpus,
                "gpu_stats": [],
                "running_job": running_job.to_dict() if running_job else None,
                "pending_jobs": len(job_manager.get_pending_jobs()),
            }
        )
    except Exception as e:
        logger.error(f"Failed to get system status: {e}")
        return jsonify({"error": "Failed to retrieve system status"}), 500


@api.route("/system/config")
@api_token_required
def get_config():
    """Get current configuration."""
    try:
        import os

        from ..config import load_config

        config = load_config()
        if config is None:
            # Return what we can from environment variables
            return jsonify(
                {
                    "plex_url": os.environ.get("PLEX_URL", ""),
                    "plex_token": "****" if os.environ.get("PLEX_TOKEN") else "",
                    "plex_config_folder": os.environ.get("PLEX_CONFIG_FOLDER", ""),
                    "config_error": "Configuration incomplete. Check required environment variables.",
                    "gpu_threads": int(os.environ.get("GPU_THREADS", 1)),
                    "cpu_threads": int(os.environ.get("CPU_THREADS", 1)),
                }
            )

        return jsonify(
            {
                "plex_url": config.plex_url or "",
                "plex_token": "****" if config.plex_token else "",
                "plex_config_folder": config.plex_config_folder or "",
                "plex_local_videos_path_mapping": config.plex_local_videos_path_mapping
                or "",
                "plex_videos_path_mapping": config.plex_videos_path_mapping or "",
                "thumbnail_interval": config.plex_bif_frame_interval,
                "thumbnail_quality": config.thumbnail_quality,
                "regenerate_thumbnails": config.regenerate_thumbnails,
                "gpu_threads": config.gpu_threads,
                "cpu_threads": config.cpu_threads,
                "log_level": config.log_level,
            }
        )
    except Exception as e:
        logger.error(f"Failed to get config: {e}")
        return jsonify({"error": "Failed to retrieve configuration"}), 500


@api.route("/health")
def health_check():
    """Health check endpoint (no auth required)."""
    return jsonify({"status": "healthy"})


# ============================================================================
# API Routes - Plex OAuth
# ============================================================================

PLEX_HEADERS = {
    "X-Plex-Product": "Plex Preview Generator",
    "X-Plex-Version": "1.0.0",
    "X-Plex-Platform": "Web",
    "Accept": "application/json",
}


@api.route("/plex/auth/pin", methods=["POST"])
@setup_or_auth_required
def create_plex_pin():
    """Create a new PIN for Plex OAuth authentication."""
    import requests

    from .settings_manager import get_settings_manager

    settings = get_settings_manager()
    client_id = settings.get_client_identifier()

    headers = {
        **PLEX_HEADERS,
        "X-Plex-Client-Identifier": client_id,
    }

    try:
        response = requests.post(
            "https://plex.tv/api/v2/pins",
            headers=headers,
            data={"strong": "true"},
            timeout=10,
        )
        response.raise_for_status()
        pin_data = response.json()

        # Return the PIN info including auth URL
        auth_url = f"https://app.plex.tv/auth#?clientID={client_id}&code={pin_data['code']}&context%5Bdevice%5D%5Bproduct%5D=Plex%20Preview%20Generator"

        return jsonify(
            {
                "id": pin_data["id"],
                "code": pin_data["code"],
                "auth_url": auth_url,
            }
        )
    except requests.RequestException as e:
        logger.error(f"Failed to create Plex PIN: {e}")
        return jsonify({"error": "Failed to create PIN"}), 500


@api.route("/plex/auth/pin/<int:pin_id>")
@setup_or_auth_required
def check_plex_pin(pin_id: int):
    """Check if a PIN has been authenticated."""
    import requests

    from .settings_manager import get_settings_manager

    settings = get_settings_manager()
    client_id = settings.get_client_identifier()

    headers = {
        **PLEX_HEADERS,
        "X-Plex-Client-Identifier": client_id,
    }

    try:
        response = requests.get(
            f"https://plex.tv/api/v2/pins/{pin_id}",
            headers=headers,
            timeout=10,
        )
        response.raise_for_status()
        pin_data = response.json()

        auth_token = pin_data.get("authToken")

        if auth_token:
            # Save the token server-side only
            settings.plex_token = auth_token
            logger.info("Plex authentication successful, token saved")

        return jsonify(
            {
                "authenticated": bool(auth_token),
            }
        )
    except requests.RequestException as e:
        logger.error(f"Failed to check Plex PIN: {e}")
        return jsonify({"error": "Failed to check PIN"}), 500


@api.route("/plex/servers")
@setup_or_auth_required
def get_plex_servers():
    """Get user's Plex servers."""
    import requests

    from .settings_manager import get_settings_manager

    settings = get_settings_manager()
    client_id = settings.get_client_identifier()

    # Use provided token or saved token
    token = request.headers.get("X-Plex-Token") or settings.plex_token
    if not token:
        return jsonify({"error": "No Plex token available", "servers": []}), 401

    headers = {
        **PLEX_HEADERS,
        "X-Plex-Client-Identifier": client_id,
        "X-Plex-Token": token,
    }

    try:
        response = requests.get(
            "https://plex.tv/api/v2/resources",
            headers=headers,
            params={"includeHttps": "1", "includeRelay": "1"},
            timeout=15,
        )
        response.raise_for_status()
        resources = response.json()

        # Filter to only Plex Media Servers
        servers = []
        for resource in resources:
            if resource.get("provides") == "server":
                # Find best connection
                connections = resource.get("connections", [])
                local_conn = next((c for c in connections if c.get("local")), None)
                any_conn = connections[0] if connections else None
                best_conn = local_conn or any_conn

                if best_conn:
                    servers.append(
                        {
                            "name": resource.get("name"),
                            "machine_id": resource.get("clientIdentifier"),
                            "host": best_conn.get("address"),
                            "port": best_conn.get("port", 32400),
                            "ssl": best_conn.get("protocol") == "https",
                            "uri": best_conn.get("uri"),
                            "owned": resource.get("owned", False),
                            "local": best_conn.get("local", False),
                        }
                    )

        return jsonify({"servers": servers})
    except requests.RequestException as e:
        logger.error(f"Failed to get Plex servers: {e}")
        return jsonify({"error": "Failed to get servers", "servers": []}), 500


@api.route("/plex/libraries")
@setup_or_auth_required
def get_plex_libraries():
    """Get libraries from a Plex server."""
    import requests

    from .settings_manager import get_settings_manager

    settings = get_settings_manager()

    # Use provided URL/token or saved values
    plex_url = request.args.get("url") or settings.plex_url
    plex_token = request.args.get("token") or settings.plex_token

    if not plex_url or not plex_token:
        return jsonify({"error": "Plex URL and token required", "libraries": []}), 400

    try:
        libraries = _fetch_libraries_via_http(plex_url, plex_token)
        return jsonify({"libraries": libraries})
    except requests.RequestException as e:
        logger.error(f"Failed to get Plex libraries: {e}")
        return jsonify({"error": "Failed to get libraries", "libraries": []}), 500


@api.route("/plex/test", methods=["POST"])
@setup_or_auth_required
def test_plex_connection():
    """Test connection to a Plex server."""
    import requests

    from .settings_manager import get_settings_manager

    settings = get_settings_manager()
    data = request.get_json() or {}

    plex_url = data.get("url") or settings.plex_url
    plex_token = data.get("token") or settings.plex_token

    if not plex_url or not plex_token:
        return jsonify({"success": False, "error": "URL and token required"}), 400

    try:
        response = requests.get(
            f"{plex_url.rstrip('/')}/",
            headers={"X-Plex-Token": plex_token, "Accept": "application/json"},
            timeout=10,
        )
        response.raise_for_status()
        data = response.json()

        server_name = data.get("MediaContainer", {}).get(
            "friendlyName", "Unknown Server"
        )

        return jsonify(
            {
                "success": True,
                "server_name": server_name,
                "error": None,
            }
        )
    except requests.RequestException as e:
        logger.error(f"Plex connection test failed: {e}")
        return jsonify(
            {
                "success": False,
                "server_name": None,
                "error": "Connection test failed",
            }
        )


# ============================================================================
# API Routes - Settings
# ============================================================================


@api.route("/settings")
@setup_or_auth_required
def get_settings():
    """Get all settings."""
    from .settings_manager import get_settings_manager

    settings = get_settings_manager()

    return jsonify(
        {
            "plex_url": settings.plex_url or "",
            "plex_token": "****" if settings.plex_token else "",
            "plex_name": settings.plex_name or "",
            "plex_config_folder": settings.plex_config_folder or "/plex",
            "selected_libraries": settings.selected_libraries,
            "media_path": settings.media_path or "",
            "plex_videos_path_mapping": settings.plex_videos_path_mapping or "",
            "plex_local_videos_path_mapping": settings.plex_local_videos_path_mapping
            or "",
            "gpu_threads": settings.gpu_threads,
            "cpu_threads": settings.cpu_threads,
            "thumbnail_interval": settings.thumbnail_interval,
            "thumbnail_quality": settings.thumbnail_quality,
            "log_level": settings.get("log_level", "INFO"),
            "log_rotation_size": settings.get("log_rotation_size", "10 MB"),
            "log_retention_count": settings.get("log_retention_count", 5),
            "webhook_enabled": settings.get("webhook_enabled", True),
            "webhook_delay": settings.get("webhook_delay", 60),
            "webhook_secret": "****" if settings.get("webhook_secret") else "",
            "webhook_radarr_library": settings.get("webhook_radarr_library", ""),
            "webhook_sonarr_library": settings.get("webhook_sonarr_library", ""),
        }
    )


@api.route("/settings", methods=["POST"])
@setup_or_auth_required
def save_settings():
    """Save settings."""
    from .settings_manager import get_settings_manager

    settings = get_settings_manager()
    data = request.get_json() or {}

    # Update settings (only update provided fields)
    allowed_fields = [
        "plex_url",
        "plex_token",
        "plex_name",
        "plex_config_folder",
        "selected_libraries",
        "media_path",
        "plex_videos_path_mapping",
        "plex_local_videos_path_mapping",
        "gpu_threads",
        "cpu_threads",
        "thumbnail_interval",
        "thumbnail_quality",
        "log_level",
        "log_rotation_size",
        "log_retention_count",
        "webhook_enabled",
        "webhook_delay",
        "webhook_secret",
        "webhook_radarr_library",
        "webhook_sonarr_library",
    ]

    updates = {k: v for k, v in data.items() if k in allowed_fields}

    if updates:
        settings.update(updates)
        logger.info(f"Settings updated: {list(updates.keys())}")

        # Hot-reload logging config when any log setting changes
        log_fields = {"log_level", "log_rotation_size", "log_retention_count"}
        if log_fields & updates.keys():
            from ..logging_config import setup_logging

            setup_logging(
                log_level=settings.get("log_level", "INFO"),
                rotation=settings.get("log_rotation_size", "10 MB"),
                retention=settings.get("log_retention_count", 5),
            )

    return jsonify({"success": True})


@api.route("/settings/log-level", methods=["PUT"])
@api_token_required
def update_log_level():
    """Hot-reload log level at runtime."""
    from ..logging_config import setup_logging
    from .settings_manager import get_settings_manager

    data = request.get_json() or {}
    level = (data.get("log_level") or "INFO").upper()

    valid_levels = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
    if level not in valid_levels:
        return jsonify(
            {"error": f"Invalid log level. Must be one of {valid_levels}"}
        ), 400

    sm = get_settings_manager()
    sm.set("log_level", level)

    rotation = sm.get("log_rotation_size", "10 MB")
    retention = sm.get("log_retention_count", 5)
    setup_logging(log_level=level, rotation=rotation, retention=retention)
    logger.info(f"Log level changed to {level}")

    return jsonify({"success": True, "log_level": level})


# ============================================================================
# API Routes - Setup Wizard
# ============================================================================


@api.route("/setup/status")
def get_setup_status():
    """Check if setup is complete (no auth required for setup check)."""
    from .settings_manager import get_settings_manager

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
    from .settings_manager import get_settings_manager

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
    from .settings_manager import get_settings_manager

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
    from .settings_manager import get_settings_manager

    settings = get_settings_manager()
    settings.complete_setup()

    return jsonify({"success": True, "redirect": "/"})


@api.route("/setup/token-info", methods=["GET"])
@setup_or_auth_required
def get_setup_token_info():
    """Get information about the current authentication token for setup wizard."""
    from .auth import get_token_info

    return jsonify(get_token_info())


@api.route("/setup/set-token", methods=["POST"])
@setup_or_auth_required
def set_setup_token():
    """Set a custom authentication token during setup."""
    from .auth import set_auth_token

    data = request.get_json() or {}
    new_token = data.get("token", "")
    confirm_token = data.get("confirm_token", "")

    # Validate tokens match
    if new_token != confirm_token:
        return jsonify({"success": False, "error": "Tokens do not match."}), 400

    result = set_auth_token(new_token)

    if not result["success"]:
        return jsonify(result), 400

    return jsonify(result)


@api.route("/setup/validate-paths", methods=["POST"])
def validate_paths():
    """Validate path configuration."""
    import os

    data = request.get_json() or {}
    plex_data_path = data.get("plex_config_folder", "/plex")
    plex_media_path = data.get("plex_videos_path_mapping", "")
    local_media_path = data.get("plex_local_videos_path_mapping", "")

    result = {"valid": True, "errors": [], "warnings": [], "info": []}

    # Validate Plex Data Path
    if not plex_data_path:
        result["errors"].append("Plex Data Path is required")
        result["valid"] = False
    else:
        # Reject null bytes explicitly (for a clear error message)
        if "\x00" in plex_data_path:
            result["errors"].append("Invalid Plex Data Path")
            result["valid"] = False
            return jsonify(result)

        # Resolve and confine the path within PLEX_DATA_ROOT.
        # _safe_resolve_within performs normalisation, symlink resolution,
        # and a startswith containment check.
        resolved_plex_data_path = _safe_resolve_within(plex_data_path, PLEX_DATA_ROOT)

        if resolved_plex_data_path is None:
            canonical_root = os.path.realpath(PLEX_DATA_ROOT)
            result["errors"].append(
                f"Plex Data Path must be within the configured root: {canonical_root}"
            )
            result["valid"] = False
            return jsonify(result)

        if not os.path.exists(resolved_plex_data_path):
            result["errors"].append(
                f"Plex Data Path does not exist: {resolved_plex_data_path}"
            )
            result["valid"] = False
        else:
            # Check for expected Plex structure
            media_path = os.path.join(resolved_plex_data_path, "Media")
            localhost_path = os.path.join(media_path, "localhost")

            if not os.path.exists(media_path):
                result["errors"].append(
                    f'Missing "Media" folder in Plex Data Path. Expected: {media_path}'
                )
                result["valid"] = False
            elif not os.path.exists(localhost_path):
                result["errors"].append(
                    f'Missing "Media/localhost" folder. Expected: {localhost_path}'
                )
                result["valid"] = False
            else:
                # Check for Plex database structure (hex directories)
                try:
                    contents = os.listdir(localhost_path)
                    hex_dirs = [
                        d for d in contents if len(d) == 1 and d in "0123456789abcdef"
                    ]
                    if len(hex_dirs) >= 10:
                        result["info"].append(
                            f"✓ Valid Plex database structure found ({len(hex_dirs)} hash directories)"
                        )
                    else:
                        result["warnings"].append(
                            f"Plex database structure looks incomplete. Found {len(hex_dirs)}/16 hash directories."
                        )
                except Exception as e:
                    logger.warning(f"Could not verify Plex structure: {e}")
                    result["warnings"].append("Could not verify Plex structure")

            # Check write permissions (non-destructive)
            if os.access(resolved_plex_data_path, os.W_OK):
                result["info"].append("✓ Write permissions OK")
            else:
                result["errors"].append(
                    "Cannot write to Plex Data Path. Check permissions (PUID/PGID)."
                )
                result["valid"] = False

    # Validate Path Mapping (if provided)
    if plex_media_path or local_media_path:
        if plex_media_path and not local_media_path:
            result["errors"].append(
                "Local Media Path is required when Plex Media Path is set"
            )
            result["valid"] = False
        elif local_media_path and not plex_media_path:
            result["errors"].append(
                "Plex Media Path is required when Local Media Path is set"
            )
            result["valid"] = False
        elif local_media_path:
            # Reject null bytes explicitly (for a clear error message)
            if "\x00" in local_media_path:
                result["errors"].append("Invalid Local Media Path")
                result["valid"] = False
                return jsonify(result)

            # Resolve and confine the path within MEDIA_ROOT.
            resolved_local_media = _safe_resolve_within(local_media_path, MEDIA_ROOT)

            if resolved_local_media is None:
                result["errors"].append(
                    "Invalid Local Media Path (must be within the configured media root)"
                )
                result["valid"] = False
                return jsonify(result)

            if not os.path.exists(resolved_local_media):
                result["errors"].append(
                    f"Local Media Path does not exist: {resolved_local_media}"
                )
                result["valid"] = False
            else:
                # Check if it contains media files/folders
                try:
                    contents = os.listdir(resolved_local_media)
                    if len(contents) == 0:
                        result["warnings"].append("Local Media Path is empty")
                    else:
                        result["info"].append(
                            f"✓ Local Media Path accessible ({len(contents)} items)"
                        )
                except Exception as e:
                    logger.error(f"Cannot read Local Media Path: {e}")
                    result["errors"].append("Cannot read Local Media Path")
                    result["valid"] = False
    else:
        result["info"].append(
            "No path mapping configured (media mounted at same path as Plex)"
        )

    return jsonify(result)


# ============================================================================
# Page Route - Setup Wizard
# ============================================================================


@main.route("/setup")
def setup_wizard():
    """Setup wizard page."""
    from .settings_manager import get_settings_manager

    settings = get_settings_manager()

    # If already configured, redirect to dashboard
    if settings.is_setup_complete() and is_authenticated():
        return redirect(url_for("main.index"))

    # If not authenticated and setup not complete, show setup
    # (setup page handles its own auth via the web token)
    return render_template("setup.html")


# ============================================================================
# Helper Functions
# ============================================================================


def _start_job_async(job_id: str, config_overrides: dict = None):
    """Start job execution in a background thread."""

    def run_job():
        log_handler_id = None
        try:
            import os

            from loguru import logger as loguru_logger

            from ..cli import run_processing
            from ..config import load_config
            from ..utils import setup_working_directory as create_working_directory
            from .settings_manager import get_settings_manager

            job_thread_id = threading.current_thread().ident

            job_manager = get_job_manager()
            job = job_manager.get_job(job_id)
            if not job:
                return

            # Set up log capture for this job
            def log_sink(message):
                """Capture log messages for this job."""
                record = message.record
                log_text = f"{record['level'].name} - {record['message']}"
                job_manager.add_log(job_id, log_text)

            def job_thread_filter(record: dict) -> bool:
                """Only capture messages from this job's thread."""
                return record["thread"].id == job_thread_id

            # Read the configured log level so job logs respect it
            sm = get_settings_manager()
            job_log_level = sm.get("log_level", "INFO").upper()

            log_handler_id = loguru_logger.add(
                log_sink,
                level=job_log_level,
                format="{message}",
                filter=job_thread_filter,
            )

            job_manager.start_job(job_id)
            job_manager.add_log(job_id, "INFO - Job started")

            # Send initial progress update to show job is initializing
            job_manager.update_progress(
                job_id,
                percent=0,
                processed_items=0,
                total_items=0,
                current_item="Querying Plex libraries...",
                eta="",
            )

            # Create config with overrides
            config = load_config()
            if config is None:
                job_manager.complete_job(
                    job_id,
                    error="Configuration incomplete. Check PLEX_URL and PLEX_TOKEN.",
                )
                return

            # Apply settings from settings.json
            settings = get_settings_manager()
            if settings.plex_url:
                config.plex_url = settings.plex_url
            if settings.plex_token:
                config.plex_token = settings.plex_token
            if settings.plex_config_folder:
                config.plex_config_folder = settings.plex_config_folder

            # Apply selected libraries (empty list = all libraries)
            selected_libs = settings.get("selected_libraries", [])
            if selected_libs:
                config.plex_libraries = [lib.lower() for lib in selected_libs]

            # Apply path mappings
            if settings.get("plex_videos_path_mapping"):
                config.plex_videos_path_mapping = settings.get(
                    "plex_videos_path_mapping"
                )
            if settings.get("plex_local_videos_path_mapping"):
                config.plex_local_videos_path_mapping = settings.get(
                    "plex_local_videos_path_mapping"
                )

            if config_overrides:
                for key, value in config_overrides.items():
                    # Map selected_libraries to plex_libraries
                    if key == "selected_libraries":
                        # Empty list means "all libraries" - set to empty to skip filtering
                        config.plex_libraries = (
                            [v.lower() for v in value] if value else []
                        )
                    elif key == "force_generate":
                        # Web UI sends "force_generate"; Config uses "regenerate_thumbnails"
                        config.regenerate_thumbnails = bool(value)
                    elif hasattr(config, key):
                        setattr(config, key, value)

            # Initialize working_tmp_folder (normally done in CLI main())
            config.working_tmp_folder = create_working_directory(config.tmp_folder)
            logger.debug(f"Created working temp folder: {config.working_tmp_folder}")

            tmp_ok, tmp_messages = _verify_tmp_folder_health(config.working_tmp_folder)
            for message in tmp_messages:
                logger.warning(message)
                job_manager.add_log(job_id, f"WARNING - {message}")
            if not tmp_ok:
                raise RuntimeError(
                    f"Working temp folder is not healthy: {config.working_tmp_folder}"
                )

            # Run processing — use cached GPU results when available
            # to avoid expensive FFmpeg subprocess calls on every job.
            if _gpu_cache["result"] is not None:
                # Reconstruct the tuple format that run_processing expects
                selected_gpus = [
                    (g["type"], g["device"], g) for g in _gpu_cache["result"]
                ]
            else:
                from ..gpu_detection import detect_all_gpus

                selected_gpus = detect_all_gpus()

            # ===================================================================
            # ETA Calculation — Dual-Track Algorithm
            # ===================================================================
            # Problem: Items that already have BIF files complete in
            # milliseconds, which corrupts any simple rate average.
            #
            # Solution: Two complementary rate estimators run in parallel.
            #
            # Track 1 — Burst-filtered rate (most accurate when available)
            #   Uses the average wall-clock rate *since the first item that
            #   actually took real work*.  A "real work" transition is
            #   detected when the overall average time per item (wall-clock
            #   elapsed / completed) exceeds _SKIP_THRESHOLD.  This avoids
            #   the old inter-call per_item metric that broke with parallel
            #   workers completing in the same poll cycle.
            #
            # Track 2 — Simple elapsed rate (fallback)
            #   After a short warmup (≥20 s elapsed, ≥2 items), uses
            #   completed / elapsed as a fallback.  This guarantees the
            #   user sees *some* ETA even if burst detection has not yet
            #   resolved (e.g. many tiny libraries).
            #
            # The burst-filtered rate is preferred when available; the
            # simple rate is used otherwise.

            import time as _time  # local alias (time also imported at module level)

            _last_total: int = 0
            _processing_start_time: float = 0.0  # wall-clock of first callback
            _last_completed: int = 0
            _last_completion_time: float = 0.0  # wall-clock when last item completed
            # Burst-filtered tracking
            _real_work_start_time: float = 0.0
            _real_work_start_count: int = 0
            _burst_resolved: bool = False
            _SKIP_THRESHOLD: float = 2.0  # avg secs/item below this → burst
            _STALL_THRESHOLD: float = 5.0  # seconds without completions → stall
            # Warmup thresholds for simple-rate fallback
            _SIMPLE_MIN_ELAPSED: float = 20.0
            _SIMPLE_MIN_ITEMS: int = 2

            def _format_eta(seconds: float) -> str:
                """Format seconds into human-readable ETA string."""
                if seconds < 60:
                    return f"{int(seconds)}s"
                elif seconds < 3600:
                    return f"{int(seconds // 60)}m {int(seconds % 60)}s"
                else:
                    return f"{int(seconds // 3600)}h {int((seconds % 3600) // 60)}m"

            # Create progress callback
            def progress_callback(current: int, total: int, message: str):
                """Update job progress from processing."""
                nonlocal _last_total, _last_completed, _processing_start_time
                nonlocal _last_completion_time
                nonlocal _burst_resolved, _real_work_start_time, _real_work_start_count

                now = _time.time()
                percent = (current / total * 100) if total > 0 else 0

                # Reset tracking when a new library starts (total changes)
                if total != _last_total:
                    _last_total = total
                    _last_completed = 0
                    _last_completion_time = 0.0
                    _processing_start_time = now
                    _burst_resolved = False
                    _real_work_start_time = 0.0
                    _real_work_start_count = 0

                new_items = current - _last_completed
                if new_items > 0:
                    _last_completed = current
                    _last_completion_time = now

                remaining = total - current

                # Stall detection: seconds since last item completed
                stall_time = 0.0
                if _last_completion_time > 0 and remaining > 0:
                    stall_time = now - _last_completion_time

                # ----- Track 1: burst-filtered rate --------------------------
                # Detect burst→real transition using the *overall* average
                # time per completed item.  This is immune to the parallel-
                # worker batching problem because it divides total elapsed by
                # total completed, not inter-call gaps.
                if not _burst_resolved and _processing_start_time > 0 and current >= 2:
                    overall_elapsed = now - _processing_start_time
                    avg_per_item = overall_elapsed / current
                    if avg_per_item >= _SKIP_THRESHOLD:
                        _burst_resolved = True
                        _real_work_start_time = _processing_start_time
                        _real_work_start_count = 0

                # Stall-based burst resolution: if many items were skipped
                # instantly the overall average stays low forever, but a
                # stall proves the remaining items need real work.
                if not _burst_resolved and stall_time >= _STALL_THRESHOLD:
                    _burst_resolved = True
                    _real_work_start_time = _last_completion_time
                    _real_work_start_count = _last_completed

                # ----- Compute ETA -------------------------------------------
                eta = ""
                if remaining > 0:
                    # Prefer burst-filtered rate when available
                    if _burst_resolved and _real_work_start_time > 0:
                        real_elapsed = now - _real_work_start_time
                        real_items = current - _real_work_start_count
                        if real_elapsed > 0 and real_items >= 1:
                            rate = real_items / real_elapsed
                            eta = _format_eta(remaining / rate)

                    # Fallback: simple elapsed rate after warmup.
                    # Suppress when stalling — the fast-skip rate is
                    # misleading for items that need real processing.
                    if (
                        not eta
                        and _processing_start_time > 0
                        and current >= _SIMPLE_MIN_ITEMS
                        and stall_time < _STALL_THRESHOLD
                    ):
                        elapsed = now - _processing_start_time
                        if elapsed >= _SIMPLE_MIN_ELAPSED:
                            rate = current / elapsed
                            if rate > 0:
                                eta = _format_eta(remaining / rate)

                job_manager.update_progress(
                    job_id,
                    percent=percent,
                    processed_items=current,
                    total_items=total,
                    current_item=message,
                    eta=eta,
                )

            # Create worker status callback
            def worker_callback(workers_list):
                """Update worker statuses from processing."""
                from .jobs import WorkerStatus

                for worker_data in workers_list:
                    worker_key = (
                        f"{worker_data['worker_type']}_{worker_data['worker_id']}"
                    )
                    status = WorkerStatus(
                        worker_id=worker_data["worker_id"],
                        worker_type=worker_data["worker_type"],
                        worker_name=worker_data["worker_name"],
                        status=worker_data["status"],
                        current_title=worker_data.get("current_title", ""),
                        progress_percent=worker_data.get("progress_percent", 0),
                        speed=worker_data.get("speed", "0.0x"),
                    )
                    job_manager.update_worker_status(worker_key, status)

            try:
                clear_failures()
                # Run in headless mode with progress and worker callbacks
                run_processing(
                    config,
                    selected_gpus,
                    headless=True,
                    progress_callback=progress_callback,
                    worker_callback=worker_callback,
                    cancel_check=lambda: job_manager.is_cancellation_requested(job_id),
                )
                log_failure_summary()

                # Surface per-item failures into the job log and status
                failures = get_failures()
                if failures:
                    job_manager.add_log(
                        job_id,
                        f"WARNING - {len(failures)} file(s) failed during processing",
                    )
                    for i, f in enumerate(failures, 1):
                        wt = f"[{f['worker_type']}] " if f.get("worker_type") else ""
                        job_manager.add_log(
                            job_id,
                            f"ERROR - {i}. {wt}exit={f['exit_code']} | {f['reason']} | {f['file']}",
                        )
                    error_msg = f"Completed with {len(failures)} failed file(s)"
                    job_manager.add_log(job_id, f"WARNING - {error_msg}")
                    job_manager.complete_job(job_id, error=error_msg)
                else:
                    job_manager.add_log(job_id, "INFO - Job completed successfully")
                    job_manager.complete_job(job_id)
            finally:
                # Clear worker statuses when job ends
                job_manager.clear_worker_statuses()

                # Cleanup working folder
                import shutil

                if config.working_tmp_folder and os.path.isdir(
                    config.working_tmp_folder
                ):
                    try:
                        logger.debug(
                            f"Cleaning up working temp folder: {config.working_tmp_folder}"
                        )
                        shutil.rmtree(config.working_tmp_folder)
                        logger.debug(
                            f"Cleaned up working temp folder: {config.working_tmp_folder}"
                        )
                    except Exception as cleanup_error:
                        logger.warning(f"Failed to clean up: {cleanup_error}")
                elif config.working_tmp_folder:
                    logger.debug(
                        "Working temp folder already absent, skipping cleanup: "
                        f"{config.working_tmp_folder}"
                    )

        except Exception as e:
            logger.error(f"Job {job_id} failed: {e}")
            job_manager = get_job_manager()
            job_manager.add_log(job_id, f"ERROR - Job failed: {e}")
            job_manager.complete_job(job_id, error=str(e))
        finally:
            # Remove the log handler when job is done
            if log_handler_id is not None:
                try:
                    from loguru import logger as loguru_logger

                    loguru_logger.remove(log_handler_id)
                except Exception:
                    pass

    thread = threading.Thread(target=run_job, daemon=True)
    thread.start()


# ============================================================================
# SocketIO Event Handlers
# ============================================================================


def register_socketio_handlers(socketio):
    """Register SocketIO event handlers."""

    @socketio.on("connect", namespace="/jobs")
    def handle_connect():
        """Handle client connection."""
        if not is_authenticated():
            from flask_socketio import disconnect

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
            from flask_socketio import disconnect

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
            from flask_socketio import disconnect

            disconnect()
            return
        job_id = data.get("job_id")
        if job_id:
            leave_room(job_id)
            logger.debug(f"Client unsubscribed from job {job_id}")
