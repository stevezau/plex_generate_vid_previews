"""Job management and authentication API routes."""

import math
import os

from flask import jsonify, request, session
from loguru import logger

from ..auth import (
    api_token_required,
    get_auth_method,
    is_authenticated,
    regenerate_token,
    validate_token,
)
from ..jobs import PRIORITY_NORMAL, JobStatus, get_job_manager, parse_priority
from . import api
from ._helpers import (
    MEDIA_ROOT,
    _ensure_gpu_cache,
    _gpu_cache,
    _gpu_cache_lock,
    _param_to_bool,
    _safe_resolve_within,
    limiter,
)
from .job_runner import _start_job_async


@api.route("/auth/status")
def auth_status():
    """Check authentication status and auth method."""
    return jsonify(
        {
            "authenticated": is_authenticated(),
            "auth_method": get_auth_method(),
        }
    )


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
            key=lambda j: (j.priority, j.created_at or ""),
        )
        terminal = sorted(
            (j for j in all_jobs if j.status in (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED)),
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

    priority = data.get("priority", PRIORITY_NORMAL)

    job_manager = get_job_manager()
    job = job_manager.create_job(
        library_id=",".join(library_names) if library_names else (",".join(library_ids) if library_ids else None),
        library_name=data.get("library_name", ""),
        config=data.get("config", {}),
        priority=priority,
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
    priority = data.get("priority", PRIORITY_NORMAL)

    if not raw_paths:
        return jsonify({"error": "file_paths is required and must not be empty"}), 400

    resolved_paths: list[str] = []
    for raw in raw_paths:
        path_str = str(raw).strip()
        if not path_str:
            continue
        resolved = _safe_resolve_within(path_str, MEDIA_ROOT)
        if resolved is None:
            return jsonify({"error": f"Path is outside allowed media root: {path_str}"}), 400
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
        priority=priority,
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


@api.route("/jobs/<job_id>/priority", methods=["POST"])
@api_token_required
def set_job_priority(job_id):
    """Update the dispatch priority of a job.

    Accepts JSON body: {"priority": "high"|"normal"|"low"} or {"priority": 1|2|3}.
    Updates the job model and, if the job is running, the dispatcher tracker.
    """
    data = request.get_json() or {}
    raw = data.get("priority")
    if raw is None:
        return jsonify({"error": "priority is required"}), 400

    priority = parse_priority(raw)

    job_manager = get_job_manager()
    job = job_manager.update_job_priority(job_id, priority)
    if not job:
        return jsonify({"error": "Job not found"}), 404

    if job.status == JobStatus.RUNNING:
        try:
            from ...jobs.dispatcher import get_dispatcher

            dispatcher = get_dispatcher()
            if dispatcher is not None:
                dispatcher.update_job_priority(job_id, priority)
        except Exception:
            logger.debug("Could not update dispatcher priority", exc_info=True)

    return jsonify(job.to_dict())


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
        key=lambda j: (j.priority, j.created_at or ""),
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
    if worker_type not in {"GPU", "CPU"}:
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
    if worker_type not in {"GPU", "CPU"}:
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
        from ...jobs.dispatcher import get_dispatcher

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
    if worker_type not in {"GPU", "CPU"}:
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
    if worker_type not in {"GPU", "CPU"}:
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
    """Get logs for a specific job with optional offset/limit pagination.

    Query params:
        offset: 0-based line index to start from (default 0).
        limit: Max lines to return (default: all). Capped at 5000.
        last: Return only the last N lines (legacy; ignored if offset is set).
    """
    from ..jobs import LOG_RETENTION_CLEARED_MESSAGE

    job_manager = get_job_manager()
    job = job_manager.get_job(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404

    offset = request.args.get("offset", type=int)
    limit = request.args.get("limit", type=int)
    last_n = request.args.get("last", type=int)

    if offset is not None or limit is not None:
        result = job_manager.get_logs_paginated(
            job_id,
            offset=offset if offset is not None else 0,
            limit=min(limit, 5000) if limit is not None else None,
        )
        logs = result["lines"]
        total_lines = result["total_lines"]
        actual_offset = result["offset"]
    else:
        logs = job_manager.get_logs(job_id, last_n)
        total_lines = len(logs)
        actual_offset = 0

    log_cleared_by_retention = len(logs) == 1 and logs[0] == LOG_RETENTION_CLEARED_MESSAGE

    return jsonify(
        {
            "job_id": job_id,
            "logs": logs,
            "count": len(logs),
            "total_lines": total_lines,
            "offset": actual_offset,
            "log_cleared_by_retention": log_cleared_by_retention,
        }
    )


@api.route("/jobs/<job_id>/files", methods=["GET"])
@api_token_required
def get_job_file_results(job_id):
    """Get per-file processing results for a job with server-side pagination.

    Query params:
        outcome: Filter by outcome value (e.g. "failed", "generated").
        search: Case-insensitive filename substring search.
        page: 1-based page number (default 1).
        per_page: Results per page (default 100, max 500).
    """
    import math

    job_manager = get_job_manager()
    job = job_manager.get_job(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404

    outcome_filter = request.args.get("outcome", "").strip()
    search = request.args.get("search", "").strip()
    page = max(1, request.args.get("page", 1, type=int))
    per_page = min(500, max(1, request.args.get("per_page", 100, type=int)))

    all_results = job_manager.get_file_results(job_id)

    summary: dict = {}
    for r in all_results:
        key = r.get("outcome", "unknown")
        summary[key] = summary.get(key, 0) + 1

    filtered = all_results
    if outcome_filter:
        filtered = [r for r in filtered if r.get("outcome") == outcome_filter]
    if search:
        search_lower = search.lower()
        filtered = [r for r in filtered if search_lower in r.get("file", "").lower()]

    filtered_count = len(filtered)
    total_pages = max(1, math.ceil(filtered_count / per_page))
    page = min(page, total_pages)
    start = (page - 1) * per_page
    page_slice = filtered[start : start + per_page]

    return jsonify(
        {
            "job_id": job_id,
            "files": page_slice,
            "summary": summary,
            "count": len(page_slice),
            "filtered_count": filtered_count,
            "total": len(all_results),
            "page": page,
            "per_page": per_page,
            "total_pages": total_pages,
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

        return jsonify({"workers": [w.to_dict() if hasattr(w, "to_dict") else w for w in workers]})
    except Exception as e:
        logger.error(f"Failed to get worker statuses: {e}")
        return jsonify({"error": "Failed to retrieve worker statuses", "workers": []}), 500


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
        from ...jobs.dispatcher import get_dispatcher

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
    cached GPU detection results and gpu_config for per-GPU worker counts.

    Returns:
        List of worker status dicts.

    """
    try:
        from ..settings_manager import get_settings_manager

        settings = get_settings_manager()
        gpu_config = settings.gpu_config
        cpu_count = settings.cpu_threads
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

    # Build GPU workers from per-GPU config
    config_by_device = {
        entry["device"]: entry for entry in gpu_config if isinstance(entry, dict) and entry.get("device")
    }

    for gpu_info in gpu_infos:
        device = gpu_info.get("device", "")
        entry = config_by_device.get(device)
        if entry is not None:
            if not entry.get("enabled", True):
                continue
            workers_for_gpu = entry.get("workers", 1)
        elif gpu_config:
            continue
        else:
            workers_for_gpu = 1
        gpu_name = gpu_info.get("name", "GPU")
        for w in range(workers_for_gpu):
            worker_id += 1
            display = f"{gpu_name} #{w + 1}"
            statuses.append(
                {
                    "worker_id": worker_id,
                    "worker_type": "GPU",
                    "worker_name": display,
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
    """Create and start a new job with the same parameters as the given job.

    When reprocessing a retry job, recovers the full file set and library
    name from the original parent job so every file is retried — not just
    the subset that remained unresolved.
    """
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

    # When reprocessing a retry, restore the original job's full file set
    # and library name so all files are processed again.
    parent_id = new_config.get("parent_job_id")
    library_name = job.library_name
    if parent_id:
        parent_job = job_manager.get_job(parent_id)
        if parent_job:
            parent_cfg = parent_job.config or {}
            if parent_cfg.get("webhook_paths"):
                new_config["webhook_paths"] = parent_cfg["webhook_paths"]
            if parent_cfg.get("webhook_basenames"):
                new_config["webhook_basenames"] = parent_cfg["webhook_basenames"]
            if parent_cfg.get("path_count"):
                new_config["path_count"] = parent_cfg["path_count"]
            library_name = parent_job.library_name or library_name

    for key in (
        "is_retry",
        "retry_delay",
        "retry_attempt",
        "max_retries",
        "parent_job_id",
        "webhook_retry_count",
        "webhook_retry_delay",
        "resolution_summary",
        "scheduled_at",
    ):
        new_config.pop(key, None)
    new_job = job_manager.create_job(
        library_id=job.library_id,
        library_name=library_name,
        config=new_config,
        priority=job.priority,
    )
    from ..settings_manager import get_settings_manager

    sm = get_settings_manager()
    if sm.processing_paused:
        sm.processing_paused = False
        job_manager.emit_processing_paused_changed(False)
        logger.info("Processing auto-resumed — user requested reprocess")

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
