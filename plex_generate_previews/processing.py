"""Core processing workflow for video preview generation.

Contains run_processing() which orchestrates Plex library scanning,
media item dispatch, and worker pool management.  Used exclusively
by the web layer (job_runner.py).
"""

import os
import shutil

from loguru import logger

from .media_processing import ProcessingResult, clear_failures, log_failure_summary
from .plex_client import get_library_sections, get_media_items_by_paths, plex_server
from .worker import WorkerPool


def run_processing(
    config: dict,
    selected_gpus,
    progress_callback=None,
    worker_callback=None,
    item_complete_callback=None,
    cancel_check=None,
    pause_check=None,
    worker_pool_callback=None,
    job_id: int=None,
    on_dispatch_start=None,
    priority: int=None,
) -> None:
    """Run the main processing workflow.

    Args:
        config: Configuration object.
        selected_gpus: List of (gpu_type, gpu_device, gpu_info) tuples
            for enabled GPUs.
        progress_callback: Optional callback(current, total, message)
            for progress updates.
        worker_callback: Optional callback(workers_list) for worker
            status updates.
        item_complete_callback: Optional callback(display_name, title,
            success) when a worker finishes an item.
        cancel_check: Optional callable returning True when processing
            should stop.
        pause_check: Optional callable returning True when processing
            should pause dispatch.
        worker_pool_callback: Optional callable receiving WorkerPool on
            create/cleanup.
        job_id: Optional job identifier for multi-job dispatch.
        on_dispatch_start: Optional callable invoked once before the
            first batch of items is dispatched.
        priority: Optional dispatch priority (1=high, 2=normal, 3=low).

    Returns:
        Dict with outcome counts and optional webhook resolution info,
        or None on fatal error.

    """
    return_data = None
    worker_pool = None
    try:
        plex = plex_server(config)
        clear_failures()

        title_max_width = 200

        fallback_cpu_workers = (
            config.fallback_cpu_threads
            if config.cpu_threads == 0 and config.fallback_cpu_threads > 0
            else 0
        )

        def _create_worker_pool():
            pool = WorkerPool(
                gpu_workers=config.gpu_threads,
                cpu_workers=config.cpu_threads,
                selected_gpus=selected_gpus,
                fallback_cpu_workers=fallback_cpu_workers,
            )
            if worker_pool_callback:
                worker_pool_callback(pool)
            return pool

        total_processed = 0
        total_successful = 0
        total_failed = 0
        cancellation_requested = False
        aggregate_outcome = {r.value: 0 for r in ProcessingResult}

        def _merge_outcome(result_dict):
            """Merge outcome counts from a worker pool result into the aggregate."""
            outcome = result_dict.get("outcome")
            if outcome:
                for key, count in outcome.items():
                    aggregate_outcome[key] = aggregate_outcome.get(key, 0) + count

        logger.info("Running in headless mode (no console display)")

        _dispatch_started = False

        def _dispatch_items(items, library_name):
            """Dispatch items via shared dispatcher or local pool."""
            nonlocal worker_pool, _dispatch_started
            if job_id:
                from .job_dispatcher import get_dispatcher

                existing = get_dispatcher()
                if existing is not None:
                    worker_pool = existing.worker_pool
                elif worker_pool is None:
                    worker_pool = _create_worker_pool()
                dispatcher = get_dispatcher(worker_pool)

                # Reconcile the pool with the latest settings.  The pool
                # may have been created minutes ago with stale config
                # (e.g. 0 workers because the user hadn't configured GPUs
                # yet at startup).  The callback re-reads current settings
                # and calls reconcile_gpu_workers so the pool matches.
                if worker_pool_callback:
                    worker_pool_callback(worker_pool)

                if not _dispatch_started and on_dispatch_start:
                    on_dispatch_start()
                    _dispatch_started = True
                    # Emit the initial 0% progress AFTER the job
                    # transitions to RUNNING so the frontend's
                    # active-job DOM elements exist before the
                    # job_progress SocketIO event arrives.
                    if progress_callback:
                        progress_callback(0, len(items), f"Starting {library_name}")

                callbacks = {
                    "progress_callback": progress_callback,
                    "worker_callback": worker_callback,
                    "on_item_complete": item_complete_callback,
                    "cancel_check": cancel_check,
                    "pause_check": pause_check,
                }
                from .web.jobs import PRIORITY_NORMAL

                tracker = dispatcher.submit_items(
                    job_id=job_id,
                    items=items,
                    config=config,
                    plex=plex,
                    title_max_width=title_max_width,
                    library_name=library_name,
                    callbacks=callbacks,
                    priority=priority if priority is not None else PRIORITY_NORMAL,
                )
                tracker.wait()
                return tracker.get_result()
            else:
                # Local pool mode (no dispatcher) — emit initial progress
                # before starting the pool.
                if progress_callback:
                    progress_callback(0, len(items), f"Starting {library_name}")
                if worker_pool is None:
                    worker_pool = _create_worker_pool()
                return worker_pool.process_items_headless(
                    items,
                    config,
                    plex,
                    title_max_width,
                    library_name=library_name,
                    progress_callback=progress_callback,
                    worker_callback=worker_callback,
                    on_item_complete=item_complete_callback,
                    cancel_check=cancel_check,
                    pause_check=pause_check,
                )

        if getattr(config, "webhook_paths", None):
            webhook_resolution = get_media_items_by_paths(
                plex, config, config.webhook_paths
            )
            return_data = {
                "webhook_resolution": {
                    "unresolved_paths": list(webhook_resolution.unresolved_paths),
                    "skipped_paths": list(webhook_resolution.skipped_paths),
                    "resolved_count": len(webhook_resolution.items),
                    "total_paths": len(config.webhook_paths),
                    "path_hints": list(webhook_resolution.path_hints),
                }
            }
            if not webhook_resolution.items:
                logger.warning(
                    "No Plex items matched webhook file paths; skipping processing"
                )
            else:
                result = _dispatch_items(webhook_resolution.items, "Webhook Targets")
                total_successful += result["completed"]
                total_failed += result["failed"]
                total_processed += result["completed"] + result["failed"]
                cancellation_requested = cancellation_requested or result["cancelled"]
                _merge_outcome(result)
        else:
            all_media_items = []
            library_item_counts = []
            for section, media_items in get_library_sections(
                plex, config, cancel_check=cancel_check
            ):
                if cancel_check and cancel_check():
                    logger.info(
                        "Cancellation requested during library enumeration "
                        "— skipping remaining libraries"
                    )
                    cancellation_requested = True
                    break
                count = len(media_items)
                if count <= 0:
                    logger.info(
                        f"No media items found in library '{section.title}', skipping"
                    )
                    continue
                logger.info(f"Queued library '{section.title}' with {count} items")
                all_media_items.extend(media_items)
                library_item_counts.append((section.title, count))

            if cancel_check and cancel_check():
                logger.info(
                    "Cancellation requested before dispatch — skipping processing"
                )
                cancellation_requested = True
            elif not all_media_items:
                logger.info("No media items found across selected libraries")
            else:
                total_items = len(all_media_items)
                logger.info(
                    f"Processing {total_items} items across "
                    f"{len(library_item_counts)} libraries in a shared queue"
                )
                for library_name, count in library_item_counts:
                    logger.info(f"Library queued: {library_name} ({count} items)")

                result = _dispatch_items(all_media_items, "All Libraries")
                total_successful += result["completed"]
                total_failed += result["failed"]
                total_processed += result["completed"] + result["failed"]
                cancellation_requested = cancellation_requested or result["cancelled"]
                _merge_outcome(result)

        generated = aggregate_outcome.get("generated", 0)
        bif_exists = aggregate_outcome.get("skipped_bif_exists", 0)
        not_found = aggregate_outcome.get("skipped_file_not_found", 0)
        excluded = aggregate_outcome.get("skipped_excluded", 0)
        invalid_hash = aggregate_outcome.get("skipped_invalid_hash", 0)
        failed_count = aggregate_outcome.get("failed", 0)
        no_parts = aggregate_outcome.get("no_media_parts", 0)

        parts = []
        if generated:
            parts.append(f"{generated} generated")
        if bif_exists:
            parts.append(f"{bif_exists} already existed")
        if not_found:
            parts.append(f"{not_found} not found")
        if excluded:
            parts.append(f"{excluded} excluded")
        if invalid_hash:
            parts.append(f"{invalid_hash} invalid hash")
        if failed_count:
            parts.append(f"{failed_count} failed")
        if no_parts:
            parts.append(f"{no_parts} no media parts")

        summary = ", ".join(parts) if parts else "no items processed"

        if cancellation_requested:
            logger.info(f"Processing stopped by cancellation: {summary}")
        else:
            logger.info(f"Processing complete: {summary}")

        if total_processed > 0 and not_found > 0 and generated == 0:
            logger.warning("=" * 80)
            logger.warning(
                f"WARNING: {not_found} of {total_processed} items were skipped "
                "because the media file was not found locally."
            )
            logger.warning(
                "This usually means your path mappings are incorrect. "
                "Check your path mapping settings in the web UI."
            )
            logger.warning("=" * 80)

        log_failure_summary()

        return_data = return_data or {}
        return_data["outcome"] = aggregate_outcome

        return return_data

    except KeyboardInterrupt:
        logger.info("Received interrupt signal, shutting down gracefully...")
    except ConnectionError as e:
        logger.error(f"Connection failed: {e}")
        logger.error("Please fix the connection issue and try again.")
        return None
    except Exception as e:
        logger.error(f"Unexpected error in main execution: {e}")
        raise
    finally:
        try:
            if worker_pool is not None and not job_id:
                worker_pool.shutdown()
        except Exception as worker_error:
            logger.warning(f"Failed to shutdown worker pool: {worker_error}")
        finally:
            if not job_id and worker_pool_callback:
                worker_pool_callback(None)

        try:
            if os.path.isdir(config.working_tmp_folder):
                shutil.rmtree(config.working_tmp_folder)
                logger.debug(
                    f"Cleaned up working temp folder: {config.working_tmp_folder}"
                )
        except Exception as cleanup_error:
            logger.warning(
                f"Failed to clean up working temp folder "
                f"{config.working_tmp_folder}: {cleanup_error}"
            )
