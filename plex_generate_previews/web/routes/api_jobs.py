"""Job management and authentication API routes."""

import math
import os

from flask import jsonify, request, session
from loguru import logger

from ..auth import (
    api_token_required,
    is_authenticated,
    regenerate_token,
    validate_token,
)
from ..jobs import JobStatus, get_job_manager
from . import api
from .job_runner import _start_job_async
from ._helpers import (
    MEDIA_ROOT,
    _ensure_gpu_cache,
    _gpu_cache,
    _gpu_cache_lock,
    _param_to_bool,
    _safe_resolve_within,
    limiter,
)


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
    session.clear()
    masked = "****" + new_token[-4:] if len(new_token) > 4 else "****"
    return jsonify({"success": True, "token": masked})


# ============================================================================
# API Routes - Jobs
# ============================================================================


@api.route("/jobs")
@api_token_required
def get_jobs():
    """Get jobs with optional pagination.

    Query params:
        page: Page number (default 1). Use 0 to return all jobs unpaginated.
        per_page: Items per page (default 50, max 200).

    Returns:
        JSON with ``jobs`` list and pagination metadata (``total``, ``page``,
        ``per_page``, ``pages``).

    """
    try:
        job_manager = get_job_manager()
        all_jobs = job_manager.get_all_jobs()

        running = [j for j in all_jobs if j.status == JobStatus.RUNNING]
        pending = sorted(
            (j for j in all_jobs if j.status == JobStatus.PENDING),
            key=lambda j: j.created_at or "",
        )
        terminal = sorted(
            (
                j
                for j in all_jobs
                if j.status
                in (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED)
            ),
            key=lambda j: j.completed_at or j.created_at or "",
            reverse=True,
        )
        sorted_jobs = running + pending + terminal

        page = request.args.get("page", 1, type=int)
        per_page = min(request.args.get("per_page", 50, type=int), 200)
        per_page = max(per_page, 1)

        total = len(sorted_jobs)

        if page == 0:
            return jsonify(
                {
                    "jobs": [j.to_dict() for j in sorted_jobs],
                    "total": total,
                    "page": 0,
                    "per_page": total,
                    "pages": 1,
                }
            )

        page = max(page, 1)
        pages = max(math.ceil(total / per_page), 1)
        start = (page - 1) * per_page
        page_jobs = sorted_jobs[start : start + per_page]

        return jsonify(
            {
                "jobs": [j.to_dict() for j in page_jobs],
                "total": total,
                "page": page,
                "per_page": per_page,
                "pages": pages,
            }
        )
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

    config_overrides = data.get("config", {})
    if library_names:
        config_overrides["selected_libraries"] = library_names
    elif library_ids:
        config_overrides["selected_library_ids"] = library_ids
    else:
        config_overrides["selected_libraries"] = []

    _start_job_async(job.id, config_overrides)

    return jsonify(job.to_dict()), 201


@api.route("/jobs/manual", methods=["POST"])
@api_token_required
def create_manual_job():
    """Create a job that processes specific file paths.

    Accepts a JSON body with ``file_paths`` (list of absolute media paths)
    and an optional ``force_regenerate`` flag.  Paths are validated against
    MEDIA_ROOT to prevent directory traversal.

    Returns:
        201 with job dict on success, 400 on validation failure.

    """
    data = request.get_json() or {}
    raw_paths = data.get("file_paths") or []
    force_regenerate = _param_to_bool(data.get("force_regenerate"), False)

    if not raw_paths:
        return jsonify({"error": "file_paths is required and must not be empty"}), 400

    resolved_paths: list[str] = []
    for raw in raw_paths:
        path_str = str(raw).strip()
        if not path_str:
            continue
        resolved = _safe_resolve_within(path_str, MEDIA_ROOT)
        if resolved is None:
            return jsonify(
                {"error": f"Path is outside allowed media root: {path_str}"}
            ), 400
        resolved_paths.append(resolved)

    if not resolved_paths:
        return jsonify({"error": "No valid file paths provided"}), 400

    if len(resolved_paths) == 1:
        label = f"Manual: {os.path.basename(resolved_paths[0])}"
    else:
        label = f"Manual: {len(resolved_paths)} files"

    job_manager = get_job_manager()
    job = job_manager.create_job(
        library_name=label,
        config={
            "webhook_paths": resolved_paths,
            "force_generate": force_regenerate,
        },
    )

    config_overrides = {
        "webhook_paths": resolved_paths,
        "force_generate": force_regenerate,
    }
    _start_job_async(job.id, config_overrides)

    return jsonify(job.to_dict()), 201


@api.route("/jobs/<job_id>/cancel", methods=["POST"])
@api_token_required
def cancel_job(job_id):
    """Cancel a job."""
    job_manager = get_job_manager()
    job = job_manager.get_job(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404

    job_manager.request_cancellation(job_id)
    job_manager.add_log(job_id, "WARNING - Cancellation requested by user")
    updated = job_manager.cancel_job(job_id)
    return jsonify((updated or job).to_dict())


@api.route("/jobs/<job_id>/pause", methods=["POST"])
@api_token_required
def pause_job(job_id):
    """Pause processing (global). Kept for backward compatibility; delegates to global pause."""
    job_manager = get_job_manager()
    if not job_manager.get_job(job_id):
        return jsonify({"error": "Job not found"}), 404
    return pause_processing()


@api.route("/jobs/<job_id>/resume", methods=["POST"])
@api_token_required
def resume_job(job_id):
    """Resume processing (global). Kept for backward compatibility; delegates to global resume."""
    job_manager = get_job_manager()
    if not job_manager.get_job(job_id):
        return jsonify({"error": "Job not found"}), 404
    return resume_processing()


@api.route("/processing/state", methods=["GET"])
@api_token_required
def get_processing_state():
    """Return global processing pause state."""
    from ..settings_manager import get_settings_manager

    sm = get_settings_manager()
    return jsonify({"paused": sm.processing_paused})


@api.route("/processing/pause", methods=["POST"])
@api_token_required
def pause_processing():
    """Set global processing pause (all running jobs pause dispatch after current tasks)."""
    from ..settings_manager import get_settings_manager

    sm = get_settings_manager()
    job_manager = get_job_manager()
    sm.processing_paused = True
    for running in job_manager.get_running_jobs():
        job_manager.request_pause(running.id)
    job_manager.emit_processing_paused_changed(True)
    logger.info("Global processing paused")
    return jsonify({"paused": True})


@api.route("/processing/resume", methods=["POST"])
@api_token_required
def resume_processing():
    """Clear global processing pause and resume all running jobs."""
    from ..settings_manager import get_settings_manager

    sm = get_settings_manager()
    job_manager = get_job_manager()
    sm.processing_paused = False
    for running in job_manager.get_running_jobs():
        job_manager.request_resume(running.id)
    job_manager.emit_processing_paused_changed(False)
    logger.info("Global processing resumed")
    pending = sorted(
        job_manager.get_pending_jobs(),
        key=lambda j: j.created_at or "",
    )
    for pj in pending:
        _start_job_async(pj.id, pj.config or {})
    return jsonify({"paused": False})


@api.route("/workers/add", methods=["POST"])
@api_token_required
def add_workers_global():
    """Add workers to the shared pool (not scoped to any job)."""
    data = request.get_json(silent=True) or {}
    worker_type = str(data.get("worker_type", "CPU")).upper()
    count = int(data.get("count", 1))
    if count <= 0:
        return jsonify({"error": "count must be greater than 0"}), 400
    if worker_type not in {"GPU", "CPU", "CPU_FALLBACK"}:
        return jsonify({"error": "Invalid worker_type"}), 400

    worker_pool = _get_shared_worker_pool()
    if worker_pool is None:
        return jsonify({"error": "Worker pool is not available"}), 409

    try:
        added = worker_pool.add_workers(worker_type, count)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    return jsonify(
        {
            "success": True,
            "worker_type": worker_type,
            "requested": count,
            "added": added,
        }
    )


@api.route("/workers/remove", methods=["POST"])
@api_token_required
def remove_workers_global():
    """Remove workers from the shared pool (not scoped to any job)."""
    data = request.get_json(silent=True) or {}
    worker_type = str(data.get("worker_type", "CPU")).upper()
    count = int(data.get("count", 1))
    if count <= 0:
        return jsonify({"error": "count must be greater than 0"}), 400
    if worker_type not in {"GPU", "CPU", "CPU_FALLBACK"}:
        return jsonify({"error": "Invalid worker_type"}), 400

    worker_pool = _get_shared_worker_pool()
    if worker_pool is None:
        return jsonify({"error": "Worker pool is not available"}), 409

    result = worker_pool.remove_workers(worker_type, count)
    return jsonify(
        {
            "success": True,
            "worker_type": worker_type,
            "requested": count,
            "removed": result.get("removed", 0),
            "scheduled_removal": result.get("scheduled", 0),
            "unavailable": result.get("unavailable", 0),
        }
    )


def _get_shared_worker_pool():
    """Get the shared worker pool from the job manager or dispatcher.

    Checks the job manager first (active job pools), then falls back
    to the dispatcher's persistent pool.

    Returns:
        WorkerPool or None if no pool exists.

    """
    pool = get_job_manager().get_active_worker_pool()
    if pool is not None:
        return pool
    try:
        from ...job_dispatcher import get_dispatcher

        dispatcher = get_dispatcher()
        if dispatcher is not None:
            return dispatcher.worker_pool
    except Exception:
        logger.debug("Could not retrieve worker pool from dispatcher", exc_info=True)
    return None


@api.route("/jobs/<job_id>/workers/add", methods=["POST"])
@api_token_required
def add_job_workers(job_id):
    """Add workers to the shared pool (scoped by job for API symmetry)."""
    job_manager = get_job_manager()
    job = job_manager.get_job(job_id)
    if not job or job.status != JobStatus.RUNNING:
        return jsonify({"error": "Job is not running"}), 400

    data = request.get_json(silent=True) or {}
    worker_type = str(data.get("worker_type", "CPU")).upper()
    count = int(data.get("count", 1))
    if count <= 0:
        return jsonify({"error": "count must be greater than 0"}), 400
    if worker_type not in {"GPU", "CPU", "CPU_FALLBACK"}:
        return jsonify({"error": "Invalid worker_type"}), 400

    worker_pool = job_manager.get_active_worker_pool()
    if worker_pool is None:
        return jsonify({"error": "Worker pool is not available"}), 409

    try:
        added = worker_pool.add_workers(worker_type, count)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    return jsonify(
        {
            "success": True,
            "worker_type": worker_type,
            "requested": count,
            "added": added,
        }
    )


@api.route("/jobs/<job_id>/workers/remove", methods=["POST"])
@api_token_required
def remove_job_workers(job_id):
    """Remove workers from the shared pool (scoped by job for API symmetry)."""
    job_manager = get_job_manager()
    job = job_manager.get_job(job_id)
    if not job or job.status != JobStatus.RUNNING:
        return jsonify({"error": "Job is not running"}), 400

    data = request.get_json(silent=True) or {}
    worker_type = str(data.get("worker_type", "CPU")).upper()
    count = int(data.get("count", 1))
    if count <= 0:
        return jsonify({"error": "count must be greater than 0"}), 400
    if worker_type not in {"GPU", "CPU", "CPU_FALLBACK"}:
        return jsonify({"error": "Invalid worker_type"}), 400

    worker_pool = job_manager.get_active_worker_pool()
    if worker_pool is None:
        return jsonify({"error": "Worker pool is not available"}), 409

    result = worker_pool.remove_workers(worker_type, count)
    return jsonify(
        {
            "success": True,
            "worker_type": worker_type,
            "requested": count,
            "removed": result.get("removed", 0),
            "scheduled_removal": result.get("scheduled", 0),
            "unavailable": result.get("unavailable", 0),
        }
    )


@api.route("/jobs/<job_id>/logs", methods=["GET"])
@api_token_required
def get_job_logs(job_id):
    """Get logs for a specific job."""
    from ..jobs import LOG_RETENTION_CLEARED_MESSAGE

    job_manager = get_job_manager()
    job = job_manager.get_job(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404

    last_n = request.args.get("last", type=int)
    logs = job_manager.get_logs(job_id, last_n)
    log_cleared_by_retention = (
        len(logs) == 1 and logs[0] == LOG_RETENTION_CLEARED_MESSAGE
    )

    return jsonify(
        {
            "job_id": job_id,
            "logs": logs,
            "count": len(logs),
            "log_cleared_by_retention": log_cleared_by_retention,
        }
    )


@api.route("/jobs/workers", methods=["GET"])
@api_token_required
def get_worker_statuses():
    """Get status of all workers.

    Returns callback-driven statuses when a job is actively pushing
    updates, otherwise falls back to querying the global dispatcher's
    pool directly so workers are visible even when idle.
    """
    try:
        job_manager = get_job_manager()
        workers = job_manager.get_worker_statuses()

        if not workers:
            workers = _get_dispatcher_worker_statuses()

        return jsonify(
            {"workers": [w.to_dict() if hasattr(w, "to_dict") else w for w in workers]}
        )
    except Exception as e:
        logger.error(f"Failed to get worker statuses: {e}")
        return jsonify(
            {"error": "Failed to retrieve worker statuses", "workers": []}
        ), 500


def _get_dispatcher_worker_statuses():
    """Query the global dispatcher's pool for current worker statuses.

    When a dispatcher exists, delegates to its ``_build_worker_statuses()``
    (which acquires the progress lock for thread safety).  When no pool
    exists yet (before the first job), builds synthetic idle entries from
    the saved config so the UI always shows the configured workers.

    Returns:
        List of worker status dicts, or empty list on error.

    """
    try:
        from ...job_dispatcher import get_dispatcher

        dispatcher = get_dispatcher()
        if dispatcher is not None:
            return dispatcher._build_worker_statuses()
    except Exception:
        logger.debug("Could not get dispatcher worker statuses", exc_info=True)
        return []

    return _build_idle_workers_from_config()


def _build_idle_workers_from_config():
    """Build idle worker status dicts from saved settings.

    Used before any job has been submitted (no WorkerPool exists yet)
    so the UI still shows the configured workers as idle.  Uses the
    cached GPU detection results for real hardware names.

    Returns:
        List of worker status dicts.

    """
    try:
        from ..settings_manager import get_settings_manager

        settings = get_settings_manager()
        gpu_count = settings.gpu_threads
        cpu_count = settings.cpu_threads
        cpu_fb_count = settings.cpu_fallback_threads
    except Exception:
        logger.debug("Could not read worker counts from settings", exc_info=True)
        return []

    _ensure_gpu_cache()
    with _gpu_cache_lock:
        gpu_infos = _gpu_cache["result"] or []

    idle_entry = {
        "status": "idle",
        "current_title": "",
        "progress_percent": 0,
        "speed": "0.0x",
        "remaining_time": 0.0,
    }

    statuses = []
    worker_id = 0

    for i in range(gpu_count):
        worker_id += 1
        gpu_name = gpu_infos[i]["name"] if i < len(gpu_infos) else "GPU"
        display_name = f"{gpu_name} #{i + 1}" if gpu_count > 1 else gpu_name
        statuses.append(
            {
                "worker_id": worker_id,
                "worker_type": "GPU",
                "worker_name": display_name,
                **idle_entry,
            }
        )

    for i in range(cpu_count):
        worker_id += 1
        statuses.append(
            {
                "worker_id": worker_id,
                "worker_type": "CPU",
                "worker_name": f"CPU - Worker {i + 1}",
                **idle_entry,
            }
        )

    for i in range(cpu_fb_count):
        worker_id += 1
        statuses.append(
            {
                "worker_id": worker_id,
                "worker_type": "CPU_FALLBACK",
                "worker_name": f"CPU Fallback - Worker {i + 1}",
                **idle_entry,
            }
        )

    return statuses


@api.route("/jobs/<job_id>", methods=["DELETE"])
@api_token_required
def delete_job(job_id):
    """Delete a job."""
    job_manager = get_job_manager()
    if job_manager.delete_job(job_id):
        return jsonify({"success": True})
    return jsonify({"error": "Job not found or is running"}), 404


@api.route("/jobs/<job_id>/reprocess", methods=["POST"])
@api_token_required
def reprocess_job(job_id):
    """Create and start a new job with the same parameters as the given job."""
    job_manager = get_job_manager()
    job = job_manager.get_job(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    if job.status in (JobStatus.RUNNING, JobStatus.PENDING):
        return (
            jsonify({"error": "Cannot reprocess job that is running or pending"}),
            409,
        )
    new_config = dict(job.config or {})
    for key in (
        "is_retry",
        "retry_delay",
        "retry_attempt",
        "max_retries",
        "parent_job_id",
        "webhook_retry_count",
        "webhook_retry_delay",
    ):
        new_config.pop(key, None)
    new_job = job_manager.create_job(
        library_id=job.library_id,
        library_name=job.library_name,
        config=new_config,
    )
    _start_job_async(new_job.id, new_job.config)
    return jsonify(new_job.to_dict()), 201


@api.route("/jobs/clear", methods=["POST"])
@api_token_required
def clear_jobs():
    """Clear jobs by status.

    Accepts optional JSON body: {"statuses": ["completed", "failed", "cancelled"]}
    Defaults to clearing all terminal statuses if omitted.
    """
    job_manager = get_job_manager()
    data = request.get_json(silent=True) or {}
    statuses = data.get("statuses")
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
        return jsonify({"error": "Failed to retrieve job statistics"}), 500
