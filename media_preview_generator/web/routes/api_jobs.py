"""Job management and authentication API routes."""

import math
import os
from datetime import datetime

from flask import jsonify, request, session
from loguru import logger

from ..auth import (
    api_token_required,
    get_auth_method,
    is_authenticated,
    regenerate_token,
    validate_token,
)
from ..jobs import PRIORITY_NORMAL, JobStatus, get_job_manager, is_user_visible_job, parse_priority
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


def _parse_worker_request(data: dict) -> tuple[str, int] | tuple[None, tuple]:
    """Parse worker_type + count from a worker-scaling request body.

    Returns ``(worker_type, count)`` on success, or ``(None, (error_body, status))``
    on validation failure. Handles non-string/non-numeric inputs without
    raising — returning a 400 response shape instead so a malformed UI POST
    doesn't surface as an opaque 500.
    """
    raw_type = data.get("worker_type", "CPU")
    raw_count = data.get("count", 1)
    try:
        worker_type = str(raw_type).upper()
    except Exception:
        return None, ({"error": "worker_type must be a string"}, 400)
    try:
        count = int(raw_count)
    except (TypeError, ValueError):
        return None, ({"error": f"count must be an integer (got {raw_count!r})"}, 400)
    if count <= 0:
        return None, ({"error": "count must be greater than 0"}, 400)
    if worker_type not in {"GPU", "CPU"}:
        return None, ({"error": "Invalid worker_type"}, 400)
    return worker_type, count


def _resolve_server_context(server_id: str | None) -> tuple[str | None, str | None, str | None]:
    """Look up a configured media-server by id and return (id, name, type).

    Returns (None, None, None) when ``server_id`` is missing or unknown — the
    job is then treated as "all servers" (legacy behaviour). Used by job
    creation endpoints to attribute the job to the right server.
    """
    if not server_id:
        return None, None, None
    try:
        from ..settings_manager import get_settings_manager

        raw = get_settings_manager().get("media_servers") or []
    except Exception:
        return None, None, None
    if not isinstance(raw, list):
        return None, None, None
    entry = next((e for e in raw if isinstance(e, dict) and e.get("id") == server_id), None)
    if entry is None:
        return None, None, None
    return (
        entry.get("id"),
        entry.get("name") or entry.get("id"),
        (entry.get("type") or "").lower() or None,
    )


def _infer_server_from_library_id(library_id: str) -> tuple[str | None, str | None, str | None]:
    """Find the configured media server that owns ``library_id``.

    Returns (server_id, server_name, server_type) or (None, None, None)
    when the id matches no configured library. Used by the manual job
    creation path to attribute single-library jobs to their server even
    when the caller didn't pass server_id (D2 — older /Start New Job
    submissions and any external API caller that sends only library_ids).
    """
    if not library_id:
        return None, None, None
    needle = str(library_id)
    try:
        from ..settings_manager import get_settings_manager

        raw = get_settings_manager().get("media_servers") or []
    except Exception:
        return None, None, None
    if not isinstance(raw, list):
        return None, None, None
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        for lib in entry.get("libraries") or []:
            if isinstance(lib, dict) and str(lib.get("id") or "") == needle:
                return (
                    entry.get("id"),
                    entry.get("name") or entry.get("id"),
                    (entry.get("type") or "").lower() or None,
                )
    return None, None, None


def _infer_server_from_library_ids(
    library_ids: list[str],
) -> tuple[str | None, str | None, str | None]:
    """Infer a single-server scope from a list of selected library ids.

    If every ``library_id`` in the list maps to the same configured
    server, return that server's attribution tuple so the caller can
    pin the dispatch. Returns ``(None, None, None)`` when the list is
    empty, when any id is unknown, or when the ids span multiple
    servers (mixed selection → unpinned peer-equal dispatch is the
    correct fall-back behaviour).

    This is the multi-library generalisation of
    :func:`_infer_server_from_library_id`. Without it, picking three
    Plex libraries (e.g. Movies + Sports + TV Shows) leaves
    ``server_id`` unset, ``server_id_filter`` propagates as ``None``,
    and the multi-server dispatcher fans out every file to Emby and
    Jellyfin too — which surprised a user who expected
    "selected Plex libraries" to mean "only touch Plex". Reported on
    job c9253a85.
    """
    if not library_ids:
        return None, None, None
    first = _infer_server_from_library_id(library_ids[0])
    if not first[0]:
        return None, None, None
    for lid in library_ids[1:]:
        sid, _, _ = _infer_server_from_library_id(lid)
        if sid != first[0]:
            return None, None, None
    return first


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


@api.route("/token/set", methods=["POST"])
@api_token_required
def api_set_token():
    """Set a custom authentication token (post-setup).

    Requires the existing valid token in ``X-Auth-Token`` (enforced by
    ``@api_token_required``) so a passive observer can't take over the app
    by hitting this endpoint. Body shape mirrors the setup endpoint:
    ``{token, confirm_token}``. Validates length >=8 and rejects when
    ``WEB_AUTH_TOKEN`` env var is set (env always wins; tell the user to
    remove it first).

    On success the session is cleared so all open browser tabs land on the
    login page and have to re-enter the new token — same behaviour as
    /api/token/regenerate.
    """
    from ..auth import is_token_env_controlled, set_auth_token

    if is_token_env_controlled():
        return jsonify(
            {
                "success": False,
                "error": (
                    "The token is currently set by the WEB_AUTH_TOKEN environment variable. "
                    "Remove that variable from your docker-compose / docker run command and restart "
                    "the container before setting a custom token from the UI."
                ),
            }
        ), 409

    data = request.get_json() or {}
    new_token = str(data.get("token") or "").strip()
    confirm_token = str(data.get("confirm_token") or "").strip()

    if not new_token:
        return jsonify({"success": False, "error": "Token is required."}), 400
    if new_token != confirm_token:
        return jsonify({"success": False, "error": "Tokens do not match."}), 400

    result = set_auth_token(new_token)
    if not result.get("success"):
        return jsonify(result), 400

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
        include_retry_attempts: Set to ``1`` to include per-attempt retry
            Jobs (``config.is_retry_attempt``) in the response. Default
            is to hide them so the dashboard shows ONE row per file
            (the chain row); per-attempt drill-down is accessible from
            the chain row's modal via ``/jobs/<chain_id>/attempts``.

    Returns:
        JSON with ``jobs`` list and pagination metadata (``total``, ``page``,
        ``per_page``, ``pages``).

    """
    try:
        job_manager = get_job_manager()
        all_jobs = job_manager.get_all_jobs()

        include_attempts = request.args.get("include_retry_attempts") == "1"
        if not include_attempts:
            # Hide retry firings — they're visible only via the chain
            # Job's modal Attempts dropdown. The visibility rule lives
            # in ``jobs.is_user_visible_job`` so the Job Statistics KPI
            # tile (``JobManager.get_stats``) and this list stay in lock-step;
            # otherwise discussion #239 repeats — the queue collapses
            # children but the KPI keeps counting them.
            all_jobs = [j for j in all_jobs if is_user_visible_job(j)]

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
    except Exception:
        logger.exception(
            "Could not load the jobs list for the dashboard. "
            "The Jobs page will show empty until this is resolved — running jobs are unaffected. "
            "The traceback above identifies the cause; if it persists, please open a GitHub issue "
            "with these lines."
        )
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


@api.route("/jobs/<chain_id>/attempts")
@api_token_required
def get_chain_attempts(chain_id):
    """Return the full lifecycle of a retry chain for the modal Attempts dropdown.

    The chain row IS the originating dispatch Job (same UUID). Each
    retry attempt is a separate Job with ``is_retry=True`` and
    ``parent_job_id == chain_id``; those Jobs are hidden from /api/jobs
    but surfaced here as the chain's lineage.

    Returns:
        ``{"chain_id": str, "attempts": [{...metadata}], "max_attempts": int}``
        with the originating dispatch as the first entry
        (``retry_attempt: 0``, ``is_originating: true``) followed by
        each retry Job sorted by ``retry_attempt`` ascending.
        404 if ``chain_id`` is not a retry-chain Job.
    """
    job_manager = get_job_manager()
    chain = job_manager.get_job(chain_id)
    if chain is None or not chain.config.get("is_retry_chain"):
        return jsonify({"error": "Not a retry-chain job"}), 404

    # Post-2026-05-13 retry children: ``is_retry=True`` + ``parent_job_id``.
    # Legacy children (created by the deleted per-file retry queue):
    # ``is_retry_attempt=True`` + ``parent_chain_id``. Walk both so the
    # Attempts modal still works for chains created before the refactor.
    # Dedup by Job ID guards against a row that happens to carry both
    # flag pairs (migration path overlap).
    def _is_child(j):
        cfg = j.config or {}
        if cfg.get("is_retry") and cfg.get("parent_job_id") == chain_id:
            return True
        if cfg.get("is_retry_attempt") and cfg.get("parent_chain_id") == chain_id:
            return True
        return False

    seen_child_ids: set[str] = set()
    children = []
    for j in job_manager.get_all_jobs():
        if _is_child(j) and j.id not in seen_child_ids:
            seen_child_ids.add(j.id)
            children.append(j)
    children.sort(key=lambda j: (j.config.get("retry_attempt", 0), j.created_at or ""))

    def _duration_sec(j) -> float | None:
        # Wall-clock time from job creation to terminal state. Used
        # in the dropdown option label so the user can see at a glance
        # which attempt was a 30s nope vs a 5min real publish.
        if not j.completed_at or not j.created_at:
            return None
        try:
            start = datetime.fromisoformat(j.created_at)
            end = datetime.fromisoformat(j.completed_at)
            return max(0.0, (end - start).total_seconds())
        except (TypeError, ValueError):
            return None

    # Single source of truth for "this server is still pending" status
    # values. Shared with the retry-decision scan in
    # ``web/routes/job_runner.py`` so the modal's per-pill chips and
    # the retry-spawn decision can't drift.
    from media_preview_generator.processing.retry_queue import PENDING_PUBLISHER_STATUSES as _PENDING_PUBLISHER_STATUSES

    def _pending_servers(job_obj) -> list[dict]:
        """Derive the list of per-server "still pending" counts from
        the job's ``publishers`` snapshot.

        Returns an empty list when nothing is pending. Each entry:
        ``{"server_id", "server_name", "server_type", "count"}``.
        The frontend renders one badge per entry on the matching
        attempt pill so the user sees "Jellyfin was the holdout"
        without opening per-attempt logs.

        Defensive against malformed ``publishers`` entries — the field
        is typed ``list[dict]`` but persisted to disk, so a partial
        write or hand-edited row could surface non-dict elements that
        would raise ``AttributeError`` on ``.get()`` and 500 the whole
        ``/attempts`` response. Skip those entries quietly.
        """
        rows: list[dict] = []
        for pub in (job_obj.publishers or []) if job_obj else []:
            if not isinstance(pub, dict):
                continue
            counts = pub.get("counts") or {}
            if not isinstance(counts, dict):
                continue
            n = sum(counts.get(s, 0) for s in _PENDING_PUBLISHER_STATUSES)
            if n > 0:
                rows.append(
                    {
                        "server_id": pub.get("server_id"),
                        "server_name": pub.get("server_name"),
                        "server_type": (pub.get("server_type") or "").lower(),
                        "count": int(n),
                    }
                )
        return rows

    attempts: list[dict] = []

    # First entry: the originating dispatch == the chain Job itself.
    # Selecting it in the dropdown loads /api/jobs/<chain_id>/logs,
    # which serves the original dispatch's log file (FFmpeg + Plex/Emby
    # publish lines + the PUBLISHED_PENDING_REGISTRATION message
    # that triggered the chain). The chain's status field reflects
    # the WHOLE lifecycle (PENDING while between firings, RUNNING
    # during a firing, COMPLETED when chain succeeds, FAILED on
    # exhaustion), so the dropdown label shows the right glyph.
    #
    # ``pending_servers`` is unconditionally empty on the originating
    # entry: the chain head's ``publishers`` snapshot is refreshed at
    # terminal (Hook 3 in job_runner.py), so by the time the user
    # looks it reflects post-retry truth, not what was pending after
    # the initial dispatch. The "why" lives on the retry pills and
    # the chain-summary subtitle; reconstructing the initial pending
    # state isn't worth the complexity.
    attempts.append(
        {
            "id": chain.id,
            "retry_attempt": 0,
            "status": chain.status.value if hasattr(chain.status, "value") else str(chain.status),
            "created_at": chain.config.get("retry_started_at") or chain.created_at,
            "started_at": chain.started_at,
            "completed_at": chain.completed_at,
            "error": chain.error,
            "duration_sec": _duration_sec(chain),
            "is_originating": True,
            "pending_servers": [],
        }
    )

    for j in children:
        attempts.append(
            {
                "id": j.id,
                "retry_attempt": int(j.config.get("retry_attempt", 0)),
                "status": j.status.value if hasattr(j.status, "value") else str(j.status),
                "created_at": j.created_at,
                "started_at": j.started_at,
                "completed_at": j.completed_at,
                "error": j.error,
                "duration_sec": _duration_sec(j),
                "is_originating": False,
                "pending_servers": _pending_servers(j),
            }
        )

    return jsonify(
        {
            "chain_id": chain_id,
            "max_attempts": int(chain.config.get("retry_max_attempts") or 0),
            "attempts": attempts,
        }
    )


@api.route("/jobs", methods=["POST"])
@api_token_required
def create_job():
    """Create a new job.

    Accepts either of (in priority order):
    * ``library_ids: list[str]`` — canonical multi-library shape (Phase H6).
    * ``library_names: list[str]`` — back-compat from older clients.
    * ``library_id: str`` — back-compat single-library shape.
    """
    data = request.get_json() or {}

    library_ids = list(data.get("library_ids") or [])
    library_names = list(data.get("library_names") or [])
    if not library_ids and not library_names:
        single = data.get("library_id")
        if single:
            library_ids = [str(single)]

    priority = data.get("priority", PRIORITY_NORMAL)
    server_id, server_name, server_type = _resolve_server_context(data.get("server_id"))

    # D2 — when the caller didn't pass server_id but picked libraries
    # from a single server, infer the server so:
    #   1. The Jobs row gets a server chip (otherwise every "I just
    #      want TV Shows" manual scan renders as an unlabelled
    #      "All Servers" entry a user can't tell apart from any other).
    #   2. ``server_id_filter`` propagates into the dispatcher at
    #      line ~362 below, so publishing is scoped to the selected
    #      server. Without the pin, the multi-server fan-out publishes
    #      to every server that owns the canonical path (found in the
    #      wild on job c9253a85: user selected 3 Plex libraries, got
    #      Emby + Jellyfin bundles too). When library IDs span multiple
    #      servers the pin stays empty — true peer-equal fan-out is
    #      the correct behaviour for a cross-server scan.
    if not server_id and library_ids:
        server_id, server_name, server_type = _infer_server_from_library_ids(library_ids)

    # Job.library_id is a display field — keep it for the single-library case
    # so the existing UI shows the ID, otherwise leave it None and let
    # library_name carry the human label (e.g. "3 Libraries").
    display_library_id = library_ids[0] if len(library_ids) == 1 else None

    job_manager = get_job_manager()
    job = job_manager.create_job(
        library_id=display_library_id,
        library_name=data.get("library_name", ""),
        config=data.get("config", {}),
        priority=priority,
        server_id=server_id,
        server_name=server_name,
        server_type=server_type,
    )

    # Allow-list of config keys the API accepts as job overrides. Anything
    # NOT in this list (notably credentials like ``plex_token`` /
    # ``plex_url`` / ``plex_config_folder``) is silently dropped — an
    # attacker who crafts a request with credential fields would otherwise
    # have those fields overwrite the live Config inside the worker
    # because ``job_runner.py``'s override loop falls through to
    # ``setattr(config, key, value)`` for any matching attribute.
    _ALLOWED_OVERRIDES = {
        "force_generate",
        "regenerate_thumbnails",
        "sort_by",
        "selected_libraries",
        "selected_library_ids",
    }
    raw_config = data.get("config") or {}
    config_overrides = {k: v for k, v in raw_config.items() if k in _ALLOWED_OVERRIDES}
    if library_ids:
        config_overrides["selected_library_ids"] = library_ids
    elif library_names:
        config_overrides["selected_libraries"] = library_names
    else:
        config_overrides["selected_libraries"] = []
    if server_id:
        # Pin the dispatcher to this server only — handled in job_runner.
        config_overrides["server_id"] = server_id

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

    server_id, server_name, server_type = _resolve_server_context(data.get("server_id"))

    job_manager = get_job_manager()
    job = job_manager.create_job(
        library_name=label,
        config={
            "webhook_paths": resolved_paths,
            "force_generate": force_regenerate,
        },
        priority=priority,
        server_id=server_id,
        server_name=server_name,
        server_type=server_type,
    )

    config_overrides = {
        "webhook_paths": resolved_paths,
        "force_generate": force_regenerate,
    }
    if server_id:
        config_overrides["server_id"] = server_id
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


@api.route("/jobs/<job_id>/retry-now", methods=["POST"])
@api_token_required
def retry_now(job_id):
    """Fire the next pending retry attempt immediately.

    Operator action surfaced in the modal footer for chain heads whose
    back-off countdown is currently inflight. Without this the operator
    had to wait out the back-off (which deep in a chain is 15 min / 1 h)
    even when they knew the upstream server had caught up — typical
    case: Jellyfin's library scan completed but the chain is still
    waiting through the next scheduled retry.

    Mechanism: find the chain's pending retry child Job (is_retry=true,
    parent_job_id=chain, status=PENDING) and set ``force_fire_now`` on
    its config. The child's backoff-wait loop (job_runner.py:464-509)
    polls this flag each tick and breaks out early when set.

    Only valid on chain heads (``is_retry_chain``). Returns:
        200 + updated Job dict on successful fire
        404 when ``job_id`` is unknown
        400 when the job is not a chain head
        409 when no retry is currently pending (chain is terminal,
            cancelled, or between attempts but not awaiting a back-off)
    """
    job_manager = get_job_manager()
    job = job_manager.get_job(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    if not job.config.get("is_retry_chain"):
        return jsonify({"error": "Not a retry-chain job"}), 400

    # Find the pending retry child for this chain. There should only
    # be ONE pending child at any time (the next scheduled attempt) —
    # the retry chain is serial.
    pending_child = None
    for j in job_manager.get_all_jobs():
        cfg = j.config or {}
        if cfg.get("is_retry") and cfg.get("parent_job_id") == job_id and j.status == JobStatus.PENDING:
            pending_child = j
            break

    if pending_child is None:
        return (
            jsonify(
                {
                    "error": "No pending retry to fire",
                    "hint": (
                        "The chain may be terminal, cancelled, or currently mid-firing. "
                        "Refresh the modal to see the latest state."
                    ),
                }
            ),
            409,
        )

    # Signal the pending child's backoff-wait loop to skip the rest of
    # its countdown. Merged into existing config so other fields are
    # preserved.
    new_cfg = dict(pending_child.config or {})
    new_cfg["force_fire_now"] = True
    job_manager.update_job_config(pending_child.id, new_cfg)

    job_manager.add_log(job_id, "INFO - Retry forced by operator (Retry now)")
    return jsonify({"fired": True, "job_id": job_id, "retry_job_id": pending_child.id})


@api.route("/jobs/<job_id>/fire-webhook-now", methods=["POST"])
@api_token_required
def fire_webhook_now(job_id):
    """Skip the debounce window for a webhook-batch Job and dispatch
    immediately.

    Per-job equivalent of the legacy
    ``/api/webhooks/pending/<debounce_key>/fire-now`` endpoint. The row
    on the dashboard knows the ``job_id`` but not the in-memory
    ``debounce_key`` (which is a server+source composite), so this
    route does the lookup and delegates to the shared helper. Keeps
    the older debounce-key route working for any external callers.

    Returns:
        202 + ``{"fired": True, "job_id": ...}`` when the batch was
            cancelled-and-dispatched.
        404 when the Job exists but has no live pending batch (already
            fired, never had one, or container restart cleared the
            in-memory dict — the user just needs to refresh).
    """
    from ..webhooks import _fire_pending_batch_now, find_pending_batch_key_for_job

    job_manager = get_job_manager()
    job = job_manager.get_job(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404

    debounce_key = find_pending_batch_key_for_job(job_id)
    if debounce_key is None:
        return (
            jsonify(
                {
                    "error": "No pending webhook batch to fire",
                    "hint": (
                        "The batch may have already fired or the container restarted since the "
                        "webhook arrived. Refresh the dashboard to see the latest state."
                    ),
                }
            ),
            404,
        )

    if not _fire_pending_batch_now(debounce_key):
        # Race: another caller fired between the lookup and the
        # cancel. Return 404 so the frontend re-fetches.
        return jsonify({"error": "Batch already fired"}), 404

    job_manager.add_log(job_id, "INFO - Webhook batch fired by operator (Fire now)")
    return jsonify({"fired": True, "job_id": job_id, "debounce_key": debounce_key}), 202


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
    parsed_type, parsed_count = _parse_worker_request(data)
    if parsed_type is None:
        body, status = parsed_count
        return jsonify(body), status
    worker_type, count = parsed_type, parsed_count

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
    parsed_type, parsed_count = _parse_worker_request(data)
    if parsed_type is None:
        body, status = parsed_count
        return jsonify(body), status
    worker_type, count = parsed_type, parsed_count

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
    parsed_type, parsed_count = _parse_worker_request(data)
    if parsed_type is None:
        body, status = parsed_count
        return jsonify(body), status
    worker_type, count = parsed_type, parsed_count

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
    parsed_type, parsed_count = _parse_worker_request(data)
    if parsed_type is None:
        body, status = parsed_count
        return jsonify(body), status
    worker_type, count = parsed_type, parsed_count

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
    job_manager = get_job_manager()
    job = job_manager.get_job(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404

    outcome_filter = request.args.get("outcome", "").strip()
    search = request.args.get("search", "").strip()
    page = max(1, request.args.get("page", 1, type=int))
    per_page = min(500, max(1, request.args.get("per_page", 100, type=int)))

    all_results = job_manager.get_file_results(job_id)

    # The per-job JSONL is soft-capped per-outcome at
    # ``_FILE_RESULTS_PER_OUTCOME_CAP`` entries (see
    # ``JobManager.record_file_result``). Past that, a one-shot
    # ``"truncated:<outcome>"`` marker row is written and later rows
    # with that outcome are dropped — but aggregate counters on
    # ``job.progress.outcome`` keep counting. The UI needs both numbers
    # to render "Generated: 5,000 of 95,318 shown · Failed: all 47
    # shown" on huge scans.
    #
    # Pre-fix runs wrote a generic ``"truncated"`` marker with no
    # per-outcome suffix; keep matching that too so historical jobs
    # still render the legacy banner.
    truncated_outcomes_set: set[str] = set()
    legacy_truncated = False
    for r in all_results:
        oc = r.get("outcome", "")
        if oc == "truncated":
            legacy_truncated = True
        elif oc.startswith("truncated:"):
            truncated_outcomes_set.add(oc[len("truncated:") :])
    truncated_outcomes = sorted(truncated_outcomes_set)
    list_truncated = legacy_truncated or bool(truncated_outcomes)
    processed_total = sum((job.progress.outcome or {}).values()) if job.progress else 0

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
            "count": len(page_slice),
            "filtered_count": filtered_count,
            "total": len(all_results),
            "processed_total": processed_total,
            "list_truncated": list_truncated,
            # Per-outcome list — which outcome buckets hit their cap on
            # this job. Empty when nothing was truncated. The UI can use
            # this to render bucket-specific "X of Y shown" messaging
            # instead of one global banner.
            "truncated_outcomes": truncated_outcomes,
            "page": page,
            "per_page": per_page,
            "total_pages": total_pages,
        }
    )


@api.route("/jobs/workers", methods=["GET"])
@api_token_required
def get_worker_statuses():
    """Get status of all workers.

    User's mental model: workers are always-present slots that jobs
    feed items into — the panel should ALWAYS show the configured
    workers (idle when no job, transitioning to processing as the
    dispatcher claims them). So the resolution order is:

    1. ``_worker_statuses`` has live state (any dispatcher actively
       emitting) — return that.
    2. A job IS running but ``_worker_statuses`` is empty
       (enumeration phase — the multi-server dispatcher builds slots
       and emits only AFTER enumeration finishes, which can take
       30-60s on a 118k-item Jellyfin library) — synthesise idle
       workers from the saved config using the SAME 1-based ID
       scheme the dispatcher will use. When the real force-emit
       lands, same keys are updated in place; no DOM swap.
    3. No job running — fall back to the legacy dispatcher pool
       (which itself falls through to ``_build_idle_workers_from_config``
       when no pool exists).

    History:
    * Commit 49dcd7b added a "return [] during running job" gate
      to avoid mixing legacy 0-based fallback IDs with multi-server
      1-based IDs at dispatch start (the "cards jumping" symptom).
    * Job b5651c8a follow-up: that gate left the panel BLANK for
      tens of seconds during enumeration of large libraries. The
      user reported "kick off a jelly full lib scan on shows, the
      workers disappear, cancel the job and they come back."
    * This rewrite resolves both: synth idle uses the same 1-based
      IDs the dispatcher emits, so no flip; the panel is never
      blank during a running job; legacy fallback still serves the
      idle-app case identically.
    """
    try:
        job_manager = get_job_manager()
        workers = job_manager.get_worker_statuses()

        if not workers:
            if job_manager.get_running_jobs():
                workers = _build_idle_workers_from_config()
            else:
                workers = _get_dispatcher_worker_statuses()

        return jsonify({"workers": [w.to_dict() if hasattr(w, "to_dict") else w for w in workers]})
    except Exception:
        logger.exception(
            "Could not load the worker statuses for the dashboard. "
            "The 'Workers' panel will be empty until this is resolved — "
            "actual job processing is unaffected. "
            "The traceback above identifies the cause."
        )
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

    # Mirror the dispatcher's _build_worker_statuses() idle-branch contract
    # so the synthesised idle list (used before any pool exists) has the
    # SAME key set as live dispatcher rows. Without this, a regression that
    # drops a field from one path but not the other would silently flip
    # the UI between rendering modes when the dispatcher takes over.
    idle_entry = {
        "status": "idle",
        "current_title": "",
        "library_name": "",
        "progress_percent": 0,
        "speed": "0.0x",
        "remaining_time": 0.0,
        "fallback_active": False,
        "fallback_reason": None,
        "ffmpeg_started": False,
        "current_phase": "",
    }

    # Use the shared label helper so the panel reads identically whether
    # this synthesised idle list is on screen or the live dispatcher's
    # rows are. Without this the row labels visibly flipped between
    # "GPU Worker 1 (NVIDIA TITAN RTX)" (mid-job) and
    # "NVIDIA TITAN RTX #1" (idle, after the job ended).
    from ...jobs.worker_naming import (
        cpu_worker_label,
        friendly_device_label,
        gpu_worker_label,
    )

    statuses = []
    worker_id = 0
    gpu_seq = 0
    cpu_seq = 0

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
        device_label = friendly_device_label(gpu_info, device, gpu_info.get("type"))
        for _ in range(workers_for_gpu):
            worker_id += 1
            gpu_seq += 1
            statuses.append(
                {
                    "worker_id": worker_id,
                    "worker_type": "GPU",
                    "worker_name": gpu_worker_label(gpu_seq, device_label),
                    **idle_entry,
                }
            )

    for _ in range(cpu_count):
        worker_id += 1
        cpu_seq += 1
        statuses.append(
            {
                "worker_id": worker_id,
                "worker_type": "CPU",
                "worker_name": cpu_worker_label(cpu_seq),
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
        server_id=job.server_id,
        server_name=job.server_name,
        server_type=job.server_type,
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
    if statuses is not None:
        if not isinstance(statuses, list):
            return jsonify({"error": "statuses must be a list"}), 400
        valid = {s.value for s in JobStatus}
        bad = [s for s in statuses if not isinstance(s, str) or s not in valid]
        if bad:
            return jsonify({"error": f"unknown status(es): {bad}. Valid statuses: {sorted(valid)}"}), 400
    count = job_manager.clear_completed_jobs(statuses=statuses)
    return jsonify({"success": True, "cleared": count})


@api.route("/jobs/stats")
@api_token_required
def get_job_stats():
    """Get job statistics."""
    try:
        job_manager = get_job_manager()
        return jsonify(job_manager.get_stats())
    except Exception:
        logger.exception(
            "Could not compute job statistics for the dashboard. "
            "The stat counters will show empty until this is resolved — "
            "running jobs are unaffected. "
            "The traceback above identifies the cause."
        )
        return jsonify({"error": "Failed to retrieve job statistics"}), 500
