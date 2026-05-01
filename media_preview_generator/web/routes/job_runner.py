"""Background job execution for the web interface.

Contains _start_job_async which runs processing jobs in background threads.
Separated from route handlers for clarity -- this is the bridge between
the web layer and the CLI processing pipeline.
"""

import threading
from contextlib import ExitStack

from loguru import logger

from ..jobs import get_job_manager

# Tracks job IDs that already have a run_job thread in flight so that
# resume / auto-resume calls during the long library scan don't spawn
# duplicate threads for the same job.
_inflight_jobs: set = set()
_inflight_lock = threading.Lock()


def _build_selected_gpus(settings) -> list:
    """Build the selected_gpus list from gpu_config and GPU cache.

    Merges persisted gpu_config (enabled/workers/ffmpeg_threads per GPU)
    with the live GPU detection cache.  Only enabled GPUs are returned.
    The ``ffmpeg_threads`` value from gpu_config is attached to each
    gpu_info dict so WorkerPool._create_worker can pick it up.

    Args:
        settings: SettingsManager instance.

    Returns:
        List of (gpu_type, gpu_device, gpu_info) tuples for enabled GPUs.

    """
    from ._helpers import _ensure_gpu_cache, _gpu_cache, _gpu_cache_lock

    _ensure_gpu_cache()
    with _gpu_cache_lock:
        cached_gpus = _gpu_cache["result"] or []

    gpu_config = settings.gpu_config  # list of per-GPU config dicts

    # Build a lookup from device path -> gpu_config entry
    config_by_device = {
        entry["device"]: entry for entry in gpu_config if isinstance(entry, dict) and entry.get("device")
    }

    selected = []
    for g in cached_gpus:
        if g.get("status") == "failed":
            continue
        device = g.get("device", "")
        entry = config_by_device.get(device)
        if entry is not None:
            if not entry.get("enabled", True):
                continue
            workers = entry.get("workers", 1)
            if workers <= 0:
                continue
            info = dict(g)
            info["ffmpeg_threads"] = entry.get("ffmpeg_threads", 2)
            info["workers"] = workers
            selected.append((g["type"], device, info))
        else:
            # GPU not in config yet (newly detected); include with defaults
            info = dict(g)
            info["ffmpeg_threads"] = 2
            info["workers"] = 1
            selected.append((g["type"], device, info))

    return selected


def _start_job_async(job_id: str, config_overrides: dict | None = None):
    """Start job execution in a background thread.

    If a thread is already in-flight for *job_id* (e.g. still scanning
    libraries after a revive), the call is silently skipped to avoid
    duplicate work.
    """
    with _inflight_lock:
        if job_id in _inflight_jobs:
            logger.info("Skipping duplicate _start_job_async for {} — already in flight", job_id)
            return
        _inflight_jobs.add(job_id)

    def run_job():
        log_handler_id = None
        job_manager = None
        try:
            import os

            from loguru import logger as loguru_logger

            from ...config import ConfigValidationError, load_config
            from ...jobs.orchestrator import run_processing
            from ...jobs.worker import is_job_thread_for, register_job_thread
            from ...processing.generator import (
                _verify_tmp_folder_health,
                clear_failures,
                failure_scope,
                get_failures,
                set_file_result_callback,
            )
            from ...utils import setup_working_directory as create_working_directory
            from ..settings_manager import get_settings_manager

            # Register THIS job's main thread under THIS job's id so the
            # per-job log handler captures only this job's messages, not a
            # concurrently-running sibling job's (D5).
            register_job_thread(job_id)

            job_manager = get_job_manager()
            job = job_manager.get_job(job_id)
            if not job:
                return

            if get_settings_manager().processing_paused:
                merged = {**(job.config or {}), **(config_overrides or {})}
                wp = merged.get("webhook_paths")
                if wp and not merged.get("webhook_basenames"):
                    merged["webhook_basenames"] = [os.path.basename(p) for p in wp][:20]
                    if not merged.get("path_count"):
                        merged["path_count"] = len(wp)
                job_manager.update_job_config(job_id, merged)
                logger.info("Job {} not started — global processing paused; job remains pending", job_id)
                return

            def log_sink(message):
                """Capture log messages for this job."""
                record = message.record
                log_text = f"{record['level'].name} - {record['message']}"
                job_manager.add_log(job_id, log_text)

            def job_thread_filter(record: dict) -> bool:
                """Capture only THIS job's thread messages.

                The thread→job_id mapping in worker.py is keyed per job, so
                a sibling job's worker threads (e.g. a Radarr webhook
                completing while a manual library scan is winding down)
                won't leak into this job's log buffer (D5).
                """
                return is_job_thread_for(record["thread"].id, job_id)

            sm = get_settings_manager()
            job_log_level = sm.get("log_level", "INFO").upper()

            log_handler_id = loguru_logger.add(
                log_sink,
                level=job_log_level,
                format="{message}",
                filter=job_thread_filter,
                enqueue=True,
            )

            job = job_manager.get_job(job_id)
            if job and config_overrides:
                merged = {**(job.config or {}), **(config_overrides or {})}
                job_manager.update_job_config(job_id, merged)

            # Ensure webhook_basenames is populated for UI file display.
            # Some code paths (resume from pause, requeue after restart)
            # pass config_overrides without basenames, so derive them from
            # webhook_paths when missing.
            job = job_manager.get_job(job_id)
            if job:
                cfg = job.config or {}
                wp = cfg.get("webhook_paths")
                if wp and not cfg.get("webhook_basenames"):
                    cfg["webhook_basenames"] = [os.path.basename(p) for p in wp][:20]
                    if not cfg.get("path_count"):
                        cfg["path_count"] = len(wp)
                    job_manager.update_job_config(job_id, cfg)

            # Transition PENDING -> RUNNING as soon as the worker thread
            # begins, not after path resolution.  Previously the status
            # flipped only at dispatch time (via _on_dispatch_start),
            # which meant jobs with a slow Plex resolution phase (e.g.
            # Recently Added scanner runs with 100+ paths) looked stuck
            # in Pending even though they were actively querying Plex.
            # start_job() is idempotent, so the later _on_dispatch_start
            # call is a safe no-op.
            job_manager.start_job(job_id)

            job_manager.update_progress(
                job_id,
                percent=0,
                processed_items=0,
                total_items=0,
                current_item="Starting — loading configuration...",
            )

            try:
                config = load_config()
            except ConfigValidationError as exc:
                detail = "; ".join(exc.errors)
                job_manager.complete_job(
                    job_id,
                    error=f"Configuration validation failed: {detail}",
                )
                return

            settings = get_settings_manager()

            from ...config import (
                derive_legacy_plex_view,
                normalize_exclude_paths,
                normalize_path_mappings,
                split_library_selectors,
            )

            # Layer the per-server Plex view over legacy global keys so reads
            # here match what load_config does. Without this, a settings.json
            # that only has media_servers[N] (no legacy plex_url/plex_token/etc)
            # would miss them at job-start time.
            #
            # Multi-Plex support: when this job is pinned to a specific server
            # via config_overrides["server_id"], project from THAT entry so the
            # legacy orchestrator path uses the right Plex URL, token, config
            # folder, and path mappings — not whatever media_servers[0] has.
            pinned_server_id = (config_overrides or {}).get("server_id") or None
            plex_view = derive_legacy_plex_view(
                settings.get("media_servers") or [],
                server_id=pinned_server_id,
            )
            effective = {**settings.get_all(), **plex_view}

            plex_url = effective.get("plex_url")
            plex_token = effective.get("plex_token")
            plex_config_folder = effective.get("plex_config_folder")
            server_display_name = effective.get("server_display_name")
            if plex_url:
                config.plex_url = plex_url
            if plex_token:
                config.plex_token = plex_token
            if plex_config_folder:
                config.plex_config_folder = plex_config_folder
            # K3: thread the per-server display name so log emitters in the
            # legacy resolver/worker path prefix lines as "[<name>] ...".
            # Critical when this job is pinned to a specific server in a
            # multi-Plex install — without this every log line would just
            # say "Plex" with no disambiguation.
            if server_display_name:
                config.server_display_name = server_display_name
            elif pinned_server_id and not server_display_name:
                # Caller asked for a specific server but derive_legacy_plex_view
                # didn't find it — log a WARN so misuse is easy to spot.
                logger.warning(
                    "Job pinned to server_id={!r} but no matching Plex entry was found; "
                    "falling back to the first-enabled Plex view. "
                    "Check that the configured server still exists.",
                    pinned_server_id,
                )

            selected_libs = effective.get("selected_libraries", [])
            if selected_libs:
                selected_ids, selected_titles = split_library_selectors(selected_libs)
                config.plex_library_ids = selected_ids or None
                config.plex_libraries = selected_titles

            path_mappings = normalize_path_mappings(effective)
            if path_mappings:
                config.path_mappings = path_mappings
            config.exclude_paths = normalize_exclude_paths(effective.get("exclude_paths"))
            if effective.get("plex_videos_path_mapping"):
                config.plex_videos_path_mapping = effective.get("plex_videos_path_mapping")
            if effective.get("plex_local_videos_path_mapping"):
                config.plex_local_videos_path_mapping = effective.get("plex_local_videos_path_mapping")

            if config_overrides:
                for key, value in config_overrides.items():
                    if key == "selected_libraries":
                        selected_ids, selected_titles = split_library_selectors(value)
                        config.plex_library_ids = selected_ids or None
                        config.plex_libraries = selected_titles
                    elif key == "selected_library_ids":
                        selected_ids, _ = split_library_selectors(value)
                        config.plex_library_ids = selected_ids or None
                    elif key == "force_generate":
                        config.regenerate_thumbnails = bool(value)
                    elif key == "webhook_paths":
                        config.webhook_paths = [str(path).strip() for path in value if str(path).strip()]
                    elif key == "server_id":
                        # Pin downstream dispatchers to publish for this server only.
                        config.server_id_filter = str(value) if value else None
                    elif hasattr(config, key):
                        setattr(config, key, value)

            config.working_tmp_folder = create_working_directory(config.tmp_folder)
            logger.debug("Created working temp folder: {}", config.working_tmp_folder)

            tmp_ok, tmp_messages = _verify_tmp_folder_health(config.working_tmp_folder)
            for message in tmp_messages:
                logger.warning(
                    "Working temp folder health check on {}: {}. "
                    "If the message above mentions disk space or permissions, fix that and the next job "
                    "should succeed; otherwise jobs may fail. The current job will still attempt to run.",
                    config.working_tmp_folder,
                    message,
                )
                job_manager.add_log(job_id, f"WARNING - {message}")
            if not tmp_ok:
                raise RuntimeError(f"Working temp folder is not healthy: {config.working_tmp_folder}")

            selected_gpus = _build_selected_gpus(settings)

            def _format_eta(seconds: float) -> str:
                """Format seconds into human-readable ETA string."""
                if seconds < 60:
                    return f"{int(seconds)}s"
                elif seconds < 3600:
                    return f"{int(seconds // 60)}m {int(seconds % 60)}s"
                else:
                    return f"{int(seconds // 3600)}h {int((seconds % 3600) // 60)}m"

            def progress_callback(
                current: int,
                total: int,
                message: str,
                percent_override: float | None = None,
            ):
                """Update job progress from processing."""
                if percent_override is not None:
                    percent = percent_override
                else:
                    percent = (current / total * 100) if total > 0 else 0
                job_manager.update_progress(
                    job_id,
                    percent=percent,
                    processed_items=current,
                    total_items=total,
                    current_item=message,
                )

            def worker_callback(workers_list):
                """Update worker statuses from processing."""
                from ..jobs import WorkerStatus

                active_worker_keys = set()
                for worker_data in workers_list:
                    worker_key = f"{worker_data['worker_type']}_{worker_data['worker_id']}"
                    active_worker_keys.add(worker_key)
                    remaining_time = worker_data.get("remaining_time")
                    worker_eta = ""
                    if isinstance(remaining_time, int | float) and remaining_time > 0:
                        worker_eta = _format_eta(float(remaining_time))
                    status = WorkerStatus(
                        worker_id=worker_data["worker_id"],
                        worker_type=worker_data["worker_type"],
                        worker_name=worker_data["worker_name"],
                        status=worker_data["status"],
                        current_title=worker_data.get("current_title", ""),
                        library_name=worker_data.get("library_name", ""),
                        progress_percent=worker_data.get("progress_percent", 0),
                        speed=worker_data.get("speed", "0.0x"),
                        eta=worker_eta,
                    )
                    job_manager.update_worker_status(worker_key, status)
                job_manager.prune_worker_statuses(active_worker_keys)
                job_manager.emit_worker_statuses()

            _retry_cancelled = False
            run_job_config = job_manager.get_job(job_id)
            if run_job_config and run_job_config.config.get("is_retry"):
                import time as _time

                delay_sec = max(1, int(run_job_config.config.get("retry_delay", 30)))
                job_manager.add_log(
                    job_id,
                    f"INFO - Waiting {delay_sec}s before retry (Plex may still be indexing)",
                )
                job_manager.update_progress(
                    job_id,
                    percent=0,
                    processed_items=0,
                    total_items=0,
                    current_item=f"Retry starting in {delay_sec}s...",
                )
                elapsed = 0
                while elapsed < delay_sec:
                    if job_manager.is_cancellation_requested(job_id):
                        _retry_cancelled = True
                        break
                    if get_settings_manager().processing_paused:
                        _time.sleep(0.5)
                        continue
                    sleep_chunk = min(2, delay_sec - elapsed)
                    _time.sleep(sleep_chunk)
                    elapsed += sleep_chunk
                    remaining = max(0, int(delay_sec - elapsed))
                    job_manager.update_progress(
                        job_id,
                        percent=0,
                        processed_items=0,
                        total_items=0,
                        current_item=f"Retry starting in {remaining}s...",
                    )

            # Per-job failure scope — isolates this job's failure records
            # from any concurrent job in the same process.  Workers running
            # on behalf of this job re-enter the same scope on their own
            # threads (see worker._process_item) so record_failure() calls
            # deep inside the FFmpeg path land in this job's bucket.
            _job_scope = ExitStack()
            _job_scope.enter_context(failure_scope(job_id))
            try:
                if _retry_cancelled:
                    job_manager.add_log(job_id, "WARNING - Retry cancelled by user during wait")
                    job_manager.cancel_job(job_id)
                else:
                    clear_failures()

                    def _file_result_cb(file_path, outcome_str, reason, worker, servers=None):
                        job_manager.record_file_result(
                            job_id,
                            file_path,
                            outcome_str,
                            reason,
                            worker,
                            servers=servers,
                        )

                    set_file_result_callback(_file_result_cb)

                    def _on_item_complete(display_name, title, success):
                        outcome = "success" if success else "failed"
                        logger.info("{} completed: {!r} ({})", display_name, title, outcome)

                    def _on_dispatch_start():
                        """Transition PENDING -> RUNNING when items are dispatched."""
                        job_manager.start_job(job_id)
                        job_manager.add_log(job_id, "INFO - Job started")

                    def _on_pool_available(pool):
                        """Register pool and reconcile workers with current settings.

                        The pool may have been created with stale config (e.g. 0
                        workers because the user hadn't configured GPUs yet at
                        startup).  Re-reading settings here ensures the pool
                        matches whatever the user configured in the meantime.
                        """
                        if pool is None:
                            job_manager.clear_active_worker_pool(job_id)
                            return
                        job_manager.set_active_worker_pool(job_id, pool)
                        try:
                            fresh_gpus = _build_selected_gpus(get_settings_manager())
                            if fresh_gpus:
                                pool.reconcile_gpu_workers(fresh_gpus)
                        except Exception:
                            logger.debug(
                                "Could not reconcile pool on dispatch",
                                exc_info=True,
                            )

                    result = run_processing(
                        config,
                        selected_gpus,
                        progress_callback=progress_callback,
                        worker_callback=worker_callback,
                        item_complete_callback=_on_item_complete,
                        cancel_check=lambda: job_manager.is_cancellation_requested(job_id),
                        pause_check=lambda: (
                            job_manager.is_pause_requested(job_id) or get_settings_manager().processing_paused
                        ),
                        worker_pool_callback=_on_pool_available,
                        job_id=job_id,
                        on_dispatch_start=_on_dispatch_start,
                        priority=job.priority,
                    )
                    set_file_result_callback(None)

                    # run_processing returns None when it bailed on a
                    # connection / interrupt error (logged with actionable
                    # text by the orchestrator). Without surfacing it here
                    # the job lands as a green "completed" with no output —
                    # the dashboard badge would mask a Plex outage as
                    # success. Mark FAILED so the user sees the red badge
                    # and can re-run once the upstream is back.
                    if result is None:
                        job_manager.add_log(
                            job_id,
                            "ERROR - Job aborted before any items were processed (connection/interrupt). "
                            "See the application log for the specific cause.",
                        )
                        job_manager.complete_job(
                            job_id,
                            error=(
                                "Job aborted before any items were processed — most commonly because Plex "
                                "could not be reached. Check the app log for the specific cause and re-run."
                            ),
                        )
                        return

                    result = result or {}
                    failures = get_failures()

                    outcome = result.get("outcome")
                    if outcome:
                        job_manager.set_job_outcome(job_id, outcome)

                    current_job = job_manager.get_job(job_id)
                    status_value = getattr(current_job.status, "value", current_job.status) if current_job else None
                    job_config = (current_job.config or {}) if current_job else {}

                    resolution = result.get("webhook_resolution", {})
                    unresolved_paths = resolution.get("unresolved_paths") or []
                    total_paths = resolution.get("total_paths", 0)
                    resolved_count = resolution.get("resolved_count", 0)
                    path_hints = resolution.get("path_hints") or []

                    if path_hints:
                        for hint in path_hints:
                            job_manager.add_log(job_id, f"INFO - {hint}")

                    unresolved_detail = (
                        path_hints[0]
                        if path_hints
                        else "file may not be scanned yet, or path mappings in Settings may need adjusting"
                    )
                    for upath in unresolved_paths:
                        job_manager.record_file_result(
                            job_id,
                            upath,
                            "unresolved_plex",
                            f"Not found in Plex \u2014 {unresolved_detail}",
                        )
                    is_retry = job_config.get("is_retry", False)
                    retry_attempt = int(job_config.get("retry_attempt", 0))
                    max_retries = int(job_config.get("max_retries", 0))
                    retry_count = max(0, int(job_config.get("webhook_retry_count", 0)))
                    retry_delay_sec = max(10, min(300, int(job_config.get("webhook_retry_delay", 30))))
                    effective_max = max_retries or retry_count

                    # Plex resolved these items but returned stale file paths
                    # that don't exist on disk.  Collect them so we can trigger
                    # a Plex partial scan and schedule a retry.
                    not_found_on_disk: list[str] = []
                    if outcome and outcome.get("skipped_file_not_found", 0) > 0:
                        for fr in job_manager.get_file_results(job_id):
                            if fr.get("outcome") == "skipped_file_not_found":
                                not_found_on_disk.append(fr["file"])

                    # Combine paths that need a Plex rescan: unresolved
                    # (Plex doesn't know the file) + not-found-on-disk (Plex
                    # returned a stale path).
                    all_scan_paths = list(unresolved_paths) + not_found_on_disk

                    # For retries, start with unresolved webhook paths and add
                    # original webhook paths when files were not found on disk
                    # (we can't reverse-map stale Plex paths back to webhook
                    # paths, so resubmit the originals — already-processed
                    # items will be skipped as bif_exists).
                    #
                    # webhook_paths lives on the JOB's config dict, not on
                    # global settings — older code read settings.get(...)
                    # which would silently inherit stale paths from any
                    # past job that ever wrote the key globally.
                    retry_paths = list(unresolved_paths)
                    if not_found_on_disk:
                        for wp in job_config.get("webhook_paths") or []:
                            if wp not in retry_paths:
                                retry_paths.append(wp)

                    def _spawn_retry_job(paths, attempt):
                        """Create and start a retry job for unresolved webhook paths."""
                        import os as _os
                        from datetime import datetime, timedelta, timezone

                        from media_preview_generator.processing.retry_queue import BACKOFF_SCHEDULE

                        # Drop the legacy "Sonarr: " / "Radarr: " prefix from the
                        # parent name — the source chip on the row carries the
                        # trigger label now, so the prefix is duplicate noise.
                        # The "Retry:" prefix stays — it tells the user this row
                        # is a retry, not the original.
                        basenames = [_os.path.basename(p) for p in paths]
                        if len(paths) == 1:
                            retry_library_name = f"Retry: {basenames[0]}"
                        else:
                            retry_library_name = f"Retry: {len(paths)} files"
                        parent_id = job_config.get("parent_job_id") or job_id
                        # D15 — borrow the slow-backoff schedule from the
                        # publisher-step retry queue (30s, 2m, 5m, 15m, 60m).
                        # The old formula `30 * 2^(n-1)` capped at 5min spaced
                        # 3 attempts inside ~3.5 minutes — far too tight for
                        # "Plex hasn't scanned the file yet", which routinely
                        # takes minutes. Users would see 4 retry jobs in the
                        # History, all failing for the same reason. The user-
                        # tunable webhook_retry_delay (default 30) now scales
                        # the schedule so manual tuning still works.
                        scale = max(0.5, retry_delay_sec / 30.0)
                        slow_idx = min(attempt - 1, len(BACKOFF_SCHEDULE) - 1)
                        backoff_delay = max(1, int(BACKOFF_SCHEDULE[slow_idx] * scale))
                        scheduled_at = (datetime.now(timezone.utc) + timedelta(seconds=backoff_delay)).isoformat()
                        parent_priority = current_job.priority if current_job else 2
                        # K1: preserve the originating server triple so retry
                        # jobs stay scoped to whichever server fired the
                        # webhook. Without this, the Jobs UI renders retry as
                        # `(server=(all))` and re-resolution silently fans out
                        # to every server even though the original was pinned.
                        parent_server_id = current_job.server_id if current_job else None
                        parent_server_name = current_job.server_name if current_job else None
                        parent_server_type = current_job.server_type if current_job else None
                        rj = job_manager.create_job(
                            library_name=retry_library_name,
                            config={
                                "is_retry": True,
                                "parent_job_id": parent_id,
                                "retry_attempt": attempt,
                                "max_retries": effective_max,
                                "retry_delay": backoff_delay,
                                "scheduled_at": scheduled_at,
                                "path_count": len(paths),
                                "webhook_basenames": basenames[:20],
                            },
                            priority=parent_priority,
                            server_id=parent_server_id,
                            server_name=parent_server_name,
                            server_type=parent_server_type,
                        )
                        selected_libs = settings.get("selected_libraries", []) or []
                        if not isinstance(selected_libs, list):
                            selected_libs = []
                        selected_libs = [str(x).strip() for x in selected_libs if str(x).strip()]
                        retry_async_config = {
                            "selected_libraries": selected_libs,
                            "sort_by": "newest",
                            "webhook_paths": paths,
                            "webhook_retry_count": effective_max,
                            "webhook_retry_delay": retry_delay_sec,
                        }
                        # K1: thread server_id through so the retry's worker
                        # builds Config from the right per-server view.
                        if parent_server_id:
                            retry_async_config["server_id"] = parent_server_id
                        _start_job_async(rj.id, retry_async_config)
                        return rj.id

                    if all_scan_paths and not (result.get("cancelled") or status_value == "cancelled"):
                        try:
                            from ...plex_client import trigger_plex_partial_scan

                            scan_results = trigger_plex_partial_scan(
                                plex_url=config.plex_url,
                                plex_token=config.plex_token,
                                unresolved_paths=all_scan_paths,
                                path_mappings=config.path_mappings,
                                verify_ssl=config.plex_verify_ssl,
                                server_display_name=getattr(config, "server_display_name", None),
                            )
                            if scan_results:
                                parts = []
                                if unresolved_paths:
                                    parts.append(f"{len(unresolved_paths)} unresolved")
                                if not_found_on_disk:
                                    parts.append(f"{len(not_found_on_disk)} stale")
                                detail = " + ".join(parts) or "affected"
                                job_manager.add_log(
                                    job_id,
                                    f"INFO - Triggered Plex scan for {len(scan_results)} path(s) ({detail})",
                                )
                        except Exception as scan_exc:  # noqa: BLE001
                            logger.debug("Plex partial scan attempt failed (non-fatal): {}", scan_exc)

                    spawned_retry_id = None
                    if retry_paths and not (result.get("cancelled") or status_value == "cancelled"):
                        if is_retry and retry_attempt < effective_max:
                            next_attempt = retry_attempt + 1
                            spawned_retry_id = _spawn_retry_job(retry_paths, next_attempt)
                            from media_preview_generator.processing.retry_queue import BACKOFF_SCHEDULE

                            scale = max(0.5, retry_delay_sec / 30.0)
                            slow_idx = min(next_attempt - 1, len(BACKOFF_SCHEDULE) - 1)
                            next_delay = max(1, int(BACKOFF_SCHEDULE[slow_idx] * scale))
                            reason_parts = []
                            if unresolved_paths:
                                reason_parts.append(f"{len(unresolved_paths)} unresolved")
                            if not_found_on_disk:
                                reason_parts.append(f"{len(not_found_on_disk)} stale path(s)")
                            reason = " + ".join(reason_parts) or "issues"
                            job_manager.add_log(
                                job_id,
                                f"INFO - Retry {retry_attempt}/{effective_max}: {reason}, next retry in {next_delay}s",
                            )
                        elif not is_retry and effective_max > 0:
                            spawned_retry_id = _spawn_retry_job(retry_paths, 1)
                            reason_parts = []
                            if unresolved_paths:
                                reason_parts.append(f"{len(unresolved_paths)} not found in Plex")
                            if not_found_on_disk:
                                reason_parts.append(f"{len(not_found_on_disk)} stale path(s)")
                            reason = ", ".join(reason_parts) or "issues"
                            job_manager.add_log(
                                job_id,
                                f"INFO - {reason}, retry scheduled in {retry_delay_sec}s",
                            )

                    if result.get("cancelled") or status_value == "cancelled":
                        job_manager.add_log(job_id, "WARNING - Job cancelled by user")
                        job_manager.cancel_job(job_id)
                    else:
                        error_parts = []

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
                            error_parts.append(f"{len(failures)} failed file(s)")

                        if outcome:
                            not_found = outcome.get("skipped_file_not_found", 0)
                            generated = outcome.get("generated", 0)
                            outcome_failed = outcome.get("failed", 0)
                            total_outcome = sum(outcome.values())
                            if not_found > 0 and generated == 0:
                                if spawned_retry_id:
                                    msg = (
                                        f"{not_found} of {total_outcome} items "
                                        "had stale Plex paths — Plex rescan "
                                        "triggered, retry scheduled"
                                    )
                                else:
                                    msg = (
                                        f"{not_found} of {total_outcome} items "
                                        "skipped (file not found locally) — "
                                        "check path mapping configuration"
                                    )
                                job_manager.add_log(job_id, f"WARNING - {msg}")
                                error_parts.append(msg)
                            # Per-item failures (FFmpeg crashes, adapter errors)
                            # leave the job-level result as "completed" but the
                            # item outcome counter records them. Surface them
                            # so the UI badge reflects "all items failed" jobs
                            # as Failed, not green-Completed.
                            #
                            # "Success" includes both legacy ``generated`` AND
                            # multi-server ``published`` / ``skipped_output_exists``
                            # — anything where a publisher actually wrote (or
                            # confirmed) an output counts. Without this, jobs
                            # that ran via the multi-server scan would always
                            # report ``generated == 0`` and trip the all-failed
                            # branch even when most items succeeded.
                            elif outcome_failed > 0:
                                published_total = (
                                    generated
                                    + outcome.get("published", 0)
                                    + outcome.get("skipped_output_exists", 0)
                                    + outcome.get("skipped_bif_exists", 0)
                                    + outcome.get("skipped_not_indexed", 0)
                                )
                                if published_total == 0:
                                    msg = (
                                        f"{outcome_failed} of {total_outcome} item(s) failed; "
                                        "no previews were generated. Check the per-item logs above."
                                    )
                                else:
                                    msg = (
                                        f"{outcome_failed} of {total_outcome} item(s) failed "
                                        f"(but {published_total} succeeded)."
                                    )
                                job_manager.add_log(job_id, f"WARNING - {msg}")
                                error_parts.append(msg)

                        if spawned_retry_id:
                            error_parts.append(f"{len(retry_paths)} path(s) sent for retry")
                            summary = dict(job_config)
                            summary["resolution_summary"] = {
                                "total": total_paths,
                                "resolved": resolved_count,
                                "unresolved": len(unresolved_paths),
                                "not_found_on_disk": len(not_found_on_disk),
                                "retry_job_ids": [spawned_retry_id],
                            }
                            job_manager.update_job_config(job_id, summary)
                        elif unresolved_paths:
                            if is_retry:
                                error_parts.append(f"Could not find in Plex after {effective_max} attempt(s)")
                            else:
                                error_parts.append(f"{len(unresolved_paths)} item(s) not found in Plex")

                        if error_parts:
                            if total_paths > 0 and resolved_count < total_paths:
                                error_msg = f"{resolved_count}/{total_paths} processed; " + ", ".join(error_parts)
                            else:
                                error_msg = "; ".join(error_parts)
                            job_manager.add_log(job_id, f"WARNING - {error_msg}")
                            all_not_found = (
                                outcome
                                and outcome.get("generated", 0) == 0
                                and outcome.get("skipped_file_not_found", 0) > 0
                                and not spawned_retry_id
                            )
                            # Every processed item failed (FFmpeg crashed,
                            # adapter errored, etc.) AND nothing succeeded:
                            # treat as hard failure (red badge). Partial
                            # failures (some items succeeded) drop through
                            # to the warning branch (yellow badge).
                            #
                            # "Success" = legacy ``generated`` OR multi-server
                            # ``published`` / ``skipped_output_exists`` /
                            # ``skipped_bif_exists``. Without counting the
                            # multi-server outcomes, partial-success jobs would
                            # always trip the hard-failure branch.
                            _ms_published = (
                                (outcome.get("generated", 0) if outcome else 0)
                                + (outcome.get("published", 0) if outcome else 0)
                                + (outcome.get("skipped_output_exists", 0) if outcome else 0)
                                + (outcome.get("skipped_bif_exists", 0) if outcome else 0)
                            )
                            all_items_failed = outcome and outcome.get("failed", 0) > 0 and _ms_published == 0
                            nothing_resolved = total_paths > 0 and resolved_count == 0 and not spawned_retry_id
                            is_hard_failure = (
                                bool(failures)
                                or (is_retry and retry_paths and not spawned_retry_id)
                                or all_not_found
                                or all_items_failed
                                or nothing_resolved
                            )
                            if is_hard_failure:
                                job_manager.complete_job(job_id, error=error_msg)
                            else:
                                job_manager.complete_job(job_id, warning=error_msg)
                        else:
                            # D25 — when files ended in skipped_not_indexed,
                            # the JOB has finished dispatching but background
                            # retries are still pending. Marking it plain
                            # "Completed" (green) is misleading because more
                            # work IS scheduled. Surface as a warning so the
                            # user sees an amber badge with a clear message.
                            not_indexed_count = (outcome or {}).get("skipped_not_indexed", 0) if outcome else 0
                            if not_indexed_count > 0:
                                msg = (
                                    f"{not_indexed_count} file(s) waiting for the server to finish indexing — "
                                    "they'll be retried automatically (slow backoff: 30s → 2m → 5m → 15m → 60m)."
                                )
                                job_manager.add_log(job_id, f"INFO - {msg}")
                                job_manager.complete_job(job_id, warning=msg)
                            elif is_retry:
                                job_manager.add_log(job_id, "INFO - Retry job completed successfully")
                                job_manager.complete_job(job_id)
                            else:
                                job_manager.add_log(job_id, "INFO - Job completed successfully")
                                job_manager.complete_job(job_id)
            finally:
                # Release the per-job failure bucket before the scope exits
                # so the dict entry doesn't linger after the job ends.
                clear_failures()
                _job_scope.close()
                job_manager.clear_pause_flag(job_id)
                job_manager.clear_cancellation_flag(job_id)
                job_manager.clear_active_worker_pool(job_id)
                if not job_manager.get_running_jobs():
                    job_manager.clear_worker_statuses()

                import shutil

                if config.working_tmp_folder and os.path.isdir(config.working_tmp_folder):
                    try:
                        logger.debug("Cleaning up working temp folder: {}", config.working_tmp_folder)
                        shutil.rmtree(config.working_tmp_folder)
                        logger.debug("Cleaned up working temp folder: {}", config.working_tmp_folder)
                    except Exception as cleanup_error:
                        logger.warning(
                            "Could not clean up the working temp folder at {} ({}). "
                            "Leftover scratch files won't affect future jobs but will use disk space — "
                            "you can safely delete this folder manually if it grows too large.",
                            config.working_tmp_folder,
                            cleanup_error,
                        )
                elif config.working_tmp_folder:
                    logger.debug("Working temp folder already absent, skipping cleanup: {}", config.working_tmp_folder)

        except Exception as e:
            logger.exception(
                "Job {} failed with an unexpected error. "
                "The job is marked failed in the Jobs page; you can re-run it from there once the cause "
                "is fixed. The full traceback is included above; if the cause isn't obvious, please open "
                "a GitHub issue with these lines.",
                job_id,
            )
            if job_manager is None:
                job_manager = get_job_manager()
            job_manager.add_log(job_id, f"ERROR - Job failed: {e}")
            job_manager.complete_job(job_id, error=str(e))
        finally:
            set_file_result_callback(None)
            from ...jobs.worker import unregister_job_thread

            unregister_job_thread()
            with _inflight_lock:
                _inflight_jobs.discard(job_id)
            if log_handler_id is not None:
                try:
                    from loguru import logger as loguru_logger

                    loguru_logger.complete()
                    loguru_logger.remove(log_handler_id)
                except (ValueError, TypeError):
                    logger.debug("Could not remove job log handler", exc_info=True)

    thread = threading.Thread(target=run_job, daemon=True)
    thread.start()
