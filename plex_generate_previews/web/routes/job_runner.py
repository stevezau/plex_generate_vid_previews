"""Background job execution for the web interface.

Contains _start_job_async which runs processing jobs in background threads.
Separated from route handlers for clarity -- this is the bridge between
the web layer and the CLI processing pipeline.
"""

import threading

from loguru import logger

from ..jobs import get_job_manager


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
        entry["device"]: entry
        for entry in gpu_config
        if isinstance(entry, dict) and entry.get("device")
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
            info = dict(g)
            info["ffmpeg_threads"] = entry.get("ffmpeg_threads", 2)
            info["workers"] = entry.get("workers", 1)
            selected.append((g["type"], device, info))
        else:
            # GPU not in config yet (newly detected); include with defaults
            info = dict(g)
            info["ffmpeg_threads"] = 2
            info["workers"] = 1
            selected.append((g["type"], device, info))

    return selected


def _start_job_async(job_id: str, config_overrides: dict = None):
    """Start job execution in a background thread."""

    def run_job():
        log_handler_id = None
        job_manager = None
        try:
            import os

            from loguru import logger as loguru_logger

            from ...processing import run_processing
            from ...config import ConfigValidationError, load_config
            from ...media_processing import (
                _verify_tmp_folder_health,
                clear_failures,
                get_failures,
                log_failure_summary,
            )
            from ...utils import setup_working_directory as create_working_directory
            from ...worker import is_job_thread, register_job_thread
            from ..settings_manager import get_settings_manager

            register_job_thread()

            job_manager = get_job_manager()
            job = job_manager.get_job(job_id)
            if not job:
                return

            if get_settings_manager().processing_paused:
                merged = {**(job.config or {}), **(config_overrides or {})}
                job_manager.update_job_config(job_id, merged)
                logger.info(
                    f"Job {job_id} not started — global processing paused; job remains pending"
                )
                return

            def log_sink(message):
                """Capture log messages for this job."""
                record = message.record
                log_text = f"{record['level'].name} - {record['message']}"
                job_manager.add_log(job_id, log_text)

            def job_thread_filter(record: dict) -> bool:
                """Capture messages from the job thread and its worker threads."""
                return is_job_thread(record["thread"].id)

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

            job_manager.update_progress(
                job_id,
                percent=0,
                processed_items=0,
                total_items=0,
                current_item="Initializing...",
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
            if settings.plex_url:
                config.plex_url = settings.plex_url
            if settings.plex_token:
                config.plex_token = settings.plex_token
            if settings.plex_config_folder:
                config.plex_config_folder = settings.plex_config_folder

            from ...config import (
                normalize_exclude_paths,
                normalize_path_mappings,
                split_library_selectors,
            )

            selected_libs = settings.get("selected_libraries", [])
            if selected_libs:
                selected_ids, selected_titles = split_library_selectors(selected_libs)
                config.plex_library_ids = selected_ids or None
                config.plex_libraries = selected_titles

            path_mappings = normalize_path_mappings(settings)
            if path_mappings:
                config.path_mappings = path_mappings
            config.exclude_paths = normalize_exclude_paths(
                settings.get("exclude_paths")
            )
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
                        config.webhook_paths = [
                            str(path).strip() for path in value if str(path).strip()
                        ]
                    elif hasattr(config, key):
                        setattr(config, key, value)

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
                percent_override: float = None,
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
                    worker_key = (
                        f"{worker_data['worker_type']}_{worker_data['worker_id']}"
                    )
                    active_worker_keys.add(worker_key)
                    remaining_time = worker_data.get("remaining_time")
                    worker_eta = ""
                    if isinstance(remaining_time, (int, float)) and remaining_time > 0:
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

            try:
                if _retry_cancelled:
                    job_manager.add_log(
                        job_id, "WARNING - Retry cancelled by user during wait"
                    )
                    job_manager.cancel_job(job_id)
                else:
                    clear_failures()

                    def _on_item_complete(display_name, title, success):
                        outcome = "success" if success else "failed"
                        logger.info(f"{display_name} completed: {title!r} ({outcome})")

                    def _on_dispatch_start():
                        """Transition PENDING -> RUNNING when items are dispatched."""
                        job_manager.start_job(job_id)
                        job_manager.add_log(job_id, "INFO - Job started")

                    result = run_processing(
                        config,
                        selected_gpus,
                        progress_callback=progress_callback,
                        worker_callback=worker_callback,
                        item_complete_callback=_on_item_complete,
                        cancel_check=lambda: job_manager.is_cancellation_requested(
                            job_id
                        ),
                        pause_check=lambda: (
                            job_manager.is_pause_requested(job_id)
                            or get_settings_manager().processing_paused
                        ),
                        worker_pool_callback=lambda pool: (
                            job_manager.set_active_worker_pool(job_id, pool)
                            if pool is not None
                            else job_manager.clear_active_worker_pool(job_id)
                        ),
                        job_id=job_id,
                        on_dispatch_start=_on_dispatch_start,
                        priority=job.priority,
                    )
                    log_failure_summary()

                    result = result or {}
                    failures = get_failures()

                    outcome = result.get("outcome")
                    if outcome:
                        job_manager.set_job_outcome(job_id, outcome)

                    current_job = job_manager.get_job(job_id)
                    status_value = (
                        getattr(current_job.status, "value", current_job.status)
                        if current_job
                        else None
                    )
                    job_config = (current_job.config or {}) if current_job else {}

                    resolution = result.get("webhook_resolution", {})
                    unresolved_paths = resolution.get("unresolved_paths") or []
                    total_paths = resolution.get("total_paths", 0)
                    resolved_count = resolution.get("resolved_count", 0)
                    is_retry = job_config.get("is_retry", False)
                    retry_attempt = int(job_config.get("retry_attempt", 0))
                    max_retries = int(job_config.get("max_retries", 0))
                    retry_count = max(0, int(job_config.get("webhook_retry_count", 0)))
                    retry_delay_sec = max(
                        10, min(300, int(job_config.get("webhook_retry_delay", 30)))
                    )
                    effective_max = max_retries or retry_count

                    def _spawn_retry_job(paths, attempt):
                        """Create and start a retry job for unresolved webhook paths."""
                        import os as _os
                        from datetime import datetime, timedelta, timezone

                        basenames = [_os.path.basename(p) for p in paths]
                        parent_lib = current_job.library_name if current_job else ""
                        if parent_lib.startswith("Retry: "):
                            parent_lib = parent_lib[len("Retry: ") :]
                        retry_library_name = (
                            f"Retry: {parent_lib}"
                            if parent_lib
                            else f"Retry: {basenames[0]}"
                        )
                        parent_id = job_config.get("parent_job_id") or job_id
                        backoff_delay = min(300, retry_delay_sec * (2 ** (attempt - 1)))
                        scheduled_at = (
                            datetime.now(timezone.utc)
                            + timedelta(seconds=backoff_delay)
                        ).isoformat()
                        parent_priority = current_job.priority if current_job else 2
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
                        )
                        selected_libs = settings.get("selected_libraries", []) or []
                        if not isinstance(selected_libs, list):
                            selected_libs = []
                        selected_libs = [
                            str(x).strip() for x in selected_libs if str(x).strip()
                        ]
                        _start_job_async(
                            rj.id,
                            {
                                "selected_libraries": selected_libs,
                                "sort_by": "newest",
                                "webhook_paths": paths,
                                "webhook_retry_count": effective_max,
                                "webhook_retry_delay": retry_delay_sec,
                            },
                        )
                        return rj.id

                    if unresolved_paths and not (
                        result.get("cancelled") or status_value == "cancelled"
                    ):
                        try:
                            from ...plex_client import trigger_plex_partial_scan

                            scan_results = trigger_plex_partial_scan(
                                plex_url=config.plex_url,
                                plex_token=config.plex_token,
                                unresolved_paths=unresolved_paths,
                                path_mappings=config.path_mappings,
                                verify_ssl=config.plex_verify_ssl,
                            )
                            if scan_results:
                                job_manager.add_log(
                                    job_id,
                                    f"INFO - Triggered Plex scan for "
                                    f"{len(scan_results)} unresolved path(s)",
                                )
                        except Exception as scan_exc:  # noqa: BLE001
                            logger.debug(
                                f"Plex partial scan attempt failed (non-fatal): {scan_exc}"
                            )

                    spawned_retry_id = None
                    if unresolved_paths and not (
                        result.get("cancelled") or status_value == "cancelled"
                    ):
                        if is_retry and retry_attempt < effective_max:
                            next_attempt = retry_attempt + 1
                            spawned_retry_id = _spawn_retry_job(
                                unresolved_paths, next_attempt
                            )
                            next_delay = min(
                                300, retry_delay_sec * (2 ** (next_attempt - 1))
                            )
                            job_manager.add_log(
                                job_id,
                                f"INFO - Retry {retry_attempt}/{effective_max}: "
                                f"{len(unresolved_paths)} still unresolved, "
                                f"next retry in {next_delay}s",
                            )
                        elif not is_retry and effective_max > 0:
                            spawned_retry_id = _spawn_retry_job(unresolved_paths, 1)
                            job_manager.add_log(
                                job_id,
                                f"INFO - {len(unresolved_paths)} item(s) not found in Plex, "
                                f"retry scheduled in {retry_delay_sec}s",
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
                                wt = (
                                    f"[{f['worker_type']}] "
                                    if f.get("worker_type")
                                    else ""
                                )
                                job_manager.add_log(
                                    job_id,
                                    f"ERROR - {i}. {wt}exit={f['exit_code']} | {f['reason']} | {f['file']}",
                                )
                            error_parts.append(f"{len(failures)} failed file(s)")

                        if outcome:
                            not_found = outcome.get("skipped_file_not_found", 0)
                            generated = outcome.get("generated", 0)
                            total_outcome = sum(outcome.values())
                            if total_outcome > 0 and not_found > 0 and generated == 0:
                                msg = (
                                    f"{not_found} of {total_outcome} items skipped "
                                    "(file not found locally) — check path mapping configuration"
                                )
                                job_manager.add_log(job_id, f"WARNING - {msg}")
                                error_parts.append(msg)

                        if spawned_retry_id:
                            error_parts.append(
                                f"{len(unresolved_paths)} sent for retry"
                            )
                            summary = dict(job_config)
                            summary["resolution_summary"] = {
                                "total": total_paths,
                                "resolved": resolved_count,
                                "unresolved": len(unresolved_paths),
                                "retry_job_ids": [spawned_retry_id],
                            }
                            job_manager.update_job_config(job_id, summary)
                        elif unresolved_paths:
                            if is_retry:
                                error_parts.append(
                                    f"Could not find in Plex after {effective_max} attempt(s)"
                                )
                            else:
                                error_parts.append(
                                    f"{len(unresolved_paths)} item(s) not found in Plex"
                                )

                        if error_parts:
                            if total_paths > 0 and resolved_count < total_paths:
                                error_msg = (
                                    f"{resolved_count}/{total_paths} processed; "
                                    + ", ".join(error_parts)
                                )
                            else:
                                error_msg = "; ".join(error_parts)
                            job_manager.add_log(job_id, f"WARNING - {error_msg}")
                            all_not_found = (
                                outcome
                                and outcome.get("generated", 0) == 0
                                and outcome.get("skipped_file_not_found", 0) > 0
                            )
                            nothing_resolved = (
                                total_paths > 0
                                and resolved_count == 0
                                and not spawned_retry_id
                            )
                            is_hard_failure = (
                                bool(failures)
                                or (
                                    is_retry
                                    and unresolved_paths
                                    and not spawned_retry_id
                                )
                                or all_not_found
                                or nothing_resolved
                            )
                            if is_hard_failure:
                                job_manager.complete_job(job_id, error=error_msg)
                            else:
                                job_manager.complete_job(job_id, warning=error_msg)
                        else:
                            if is_retry:
                                job_manager.add_log(
                                    job_id, "INFO - Retry job completed successfully"
                                )
                            else:
                                job_manager.add_log(
                                    job_id, "INFO - Job completed successfully"
                                )
                            job_manager.complete_job(job_id)
            finally:
                job_manager.clear_pause_flag(job_id)
                job_manager.clear_cancellation_flag(job_id)
                job_manager.clear_active_worker_pool(job_id)
                if not job_manager.get_running_jobs():
                    job_manager.clear_worker_statuses()

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
            if job_manager is None:
                job_manager = get_job_manager()
            job_manager.add_log(job_id, f"ERROR - Job failed: {e}")
            job_manager.complete_job(job_id, error=str(e))
        finally:
            from ...worker import unregister_job_thread

            unregister_job_thread()
            if log_handler_id is not None:
                try:
                    from loguru import logger as loguru_logger

                    loguru_logger.complete()
                    loguru_logger.remove(log_handler_id)
                except (ValueError, TypeError):
                    logger.debug("Could not remove job log handler", exc_info=True)

    thread = threading.Thread(target=run_job, daemon=True)
    thread.start()
