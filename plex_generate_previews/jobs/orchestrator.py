"""Core processing workflow for video preview generation.

Contains run_processing() which orchestrates Plex library scanning,
media item dispatch, and worker pool management.  Used exclusively
by the web layer (job_runner.py).
"""

import os
import random
import shutil

from loguru import logger

from ..plex_client import get_library_sections, get_media_items_by_paths, plex_server
from ..processing.orchestrator import ProcessingResult, clear_failures, log_failure_summary
from .worker import WorkerPool


def _dispatch_webhook_paths_multi_server(config, *, progress_callback=None, cancel_check=None) -> dict:
    """Dispatch webhook_paths through the multi-server registry without Plex.

    Used when a webhook fires on an Emby/Jellyfin-only install: the legacy
    Plex resolution shortcut is unavailable, but ``process_canonical_path``
    in the multi-server dispatcher walks every owning server in the registry
    directly — Plex is not required.

    Returns the aggregated ProcessingResult counts keyed by enum value.
    """
    from ..processing.multi_server import process_canonical_path
    from ..servers import ServerRegistry
    from ..web.settings_manager import get_settings_manager

    counts = {r.value: 0 for r in ProcessingResult}
    paths = list(config.webhook_paths or [])
    if not paths:
        return counts

    raw_servers = []
    try:
        raw_servers = list(get_settings_manager().get("media_servers") or [])
    except Exception as exc:
        logger.warning(
            "Could not read media_servers when dispatching webhook paths ({}: {}). "
            "These paths will not be processed — verify the Servers page lists at least one enabled server.",
            type(exc).__name__,
            exc,
        )
        return counts

    try:
        registry = ServerRegistry.from_settings(raw_servers, legacy_config=config)
    except Exception as exc:
        logger.warning(
            "Could not build the media-server registry for webhook dispatch ({}: {}). "
            "These paths will not be processed — open the Servers page and verify each server "
            "has valid auth and a reachable URL.",
            type(exc).__name__,
            exc,
        )
        return counts

    sid_filter_raw = getattr(config, "server_id_filter", None)
    sid_filter = sid_filter_raw if isinstance(sid_filter_raw, str) and sid_filter_raw else None

    for idx, p in enumerate(paths, 1):
        if cancel_check and cancel_check():
            logger.info("Webhook dispatch cancelled after {} of {} path(s)", idx - 1, len(paths))
            break
        if progress_callback:
            try:
                progress_callback(idx - 1, len(paths), f"Dispatching {os.path.basename(p)}")
            except Exception:
                pass
        try:
            result = process_canonical_path(
                canonical_path=p,
                registry=registry,
                config=config,
                cancel_check=cancel_check,
                server_id_filter=sid_filter,
            )
            for pub in result.publishers or []:
                key = (pub.status.value if hasattr(pub.status, "value") else str(pub.status)).lower()
                counts[key] = counts.get(key, 0) + 1
        except Exception as exc:
            logger.warning(
                "Multi-server dispatch failed for {} ({}: {}). Other paths in this batch are still being processed.",
                p,
                type(exc).__name__,
                exc,
            )

    if progress_callback:
        try:
            progress_callback(len(paths), len(paths), "Done")
        except Exception:
            pass
    return counts


def run_processing(
    config,
    selected_gpus,
    progress_callback=None,
    worker_callback=None,
    item_complete_callback=None,
    cancel_check=None,
    pause_check=None,
    worker_pool_callback=None,
    job_id=None,
    on_dispatch_start=None,
    priority=None,
):
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
        # Multi-server guard: when this job is pinned to a non-Plex server, or
        # when no Plex is configured at all, the legacy Plex orchestrator can't
        # do anything useful — full-library enumeration uses the Plex API.
        # Honest no-op: log clearly and return so the job ends cleanly instead
        # of crashing with a Plex connection error.
        sid_filter = getattr(config, "server_id_filter", None)
        if sid_filter and isinstance(sid_filter, str) and sid_filter:
            try:
                from ..web.settings_manager import get_settings_manager

                raw = get_settings_manager().get("media_servers") or []
                pinned_entry = next((e for e in raw if isinstance(e, dict) and e.get("id") == sid_filter), None)
            except Exception:
                pinned_entry = None
            pinned_type = (pinned_entry or {}).get("type") or ""
            if pinned_entry and pinned_type and pinned_type.lower() != "plex":
                if not getattr(config, "webhook_paths", None):
                    logger.warning(
                        "Job is pinned to {} server {!r} but full-library scans currently only support Plex. "
                        "This job ended without processing anything. "
                        "For Emby/Jellyfin, use the Sonarr/Radarr or Custom webhook on the Triggers tab — "
                        "those publish previews to any configured server. "
                        "Multi-server full-scan support is tracked as a follow-up.",
                        pinned_type,
                        sid_filter,
                    )
                    return {
                        "outcome": {r.value: 0 for r in ProcessingResult},
                        "skipped_reason": f"full-library scan not supported for {pinned_type} servers yet",
                    }
        if not (config.plex_url and config.plex_token):
            if getattr(config, "webhook_paths", None):
                # Webhook-driven jobs CAN still run on a non-Plex install via
                # the multi-server dispatcher — it walks every owning server
                # in the registry directly without needing a Plex connection.
                logger.info(
                    "No Plex configured — webhook job ({} path(s)) will be dispatched directly to "
                    "owning media servers via the multi-server registry.",
                    len(config.webhook_paths),
                )
                outcome_counts = _dispatch_webhook_paths_multi_server(
                    config, progress_callback=progress_callback, cancel_check=cancel_check
                )
                return {"outcome": outcome_counts}
            logger.warning(
                "Full-library scan was requested but no Plex server is configured. "
                "Full-scan currently only walks Plex libraries — for Emby/Jellyfin, "
                "use the Sonarr/Radarr or Custom webhook on the Triggers tab. "
                "This job ended without processing anything."
            )
            return {
                "outcome": {r.value: 0 for r in ProcessingResult},
                "skipped_reason": "no Plex server configured for full-library scan",
            }
        if progress_callback:
            progress_callback(0, 0, "Connecting to Plex...")
        plex = plex_server(config)
        clear_failures()

        title_max_width = 200

        def _create_worker_pool():
            pool = WorkerPool(
                gpu_workers=config.gpu_threads,
                cpu_workers=config.cpu_threads,
                selected_gpus=selected_gpus,
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
                from .dispatcher import get_dispatcher

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
                from ..web.jobs import PRIORITY_NORMAL

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
            if progress_callback:
                path_count = len(config.webhook_paths)
                progress_callback(
                    0,
                    0,
                    f"Looking up {path_count} file path(s) in Plex — this can take a while...",
                )
            webhook_resolution = get_media_items_by_paths(plex, config, config.webhook_paths)
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
                    "Webhook arrived with {} file path(s) but Plex doesn't have any of them indexed yet — "
                    "nothing to process. The retry queue will keep checking; if this persists, verify "
                    "Plex's library scan is finishing and the path mappings under Settings line up between "
                    "the source (e.g. Sonarr/Radarr) and Plex.",
                    len(config.webhook_paths or []),
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
                plex,
                config,
                cancel_check=cancel_check,
                progress_callback=progress_callback,
            ):
                if cancel_check and cancel_check():
                    logger.info("Cancellation requested during library enumeration — skipping remaining libraries")
                    cancellation_requested = True
                    break
                count = len(media_items)
                if count <= 0:
                    logger.info("No media items found in library '{}', skipping", section.title)
                    continue
                logger.info("Queued library '{}' with {} items", section.title, count)
                all_media_items.extend(media_items)
                library_item_counts.append((section.title, count))

            if cancel_check and cancel_check():
                logger.info("Cancellation requested before dispatch — skipping processing")
                cancellation_requested = True
            elif not all_media_items:
                logger.info("No media items found across selected libraries")
            else:
                # When sort_by is "random", shuffle the combined cross-library list
                # so parallel workers statistically pull from multiple physical disks
                # at once (big win on unraid shfs / mergerfs / JBOD setups).
                if config.sort_by == "random":
                    random.Random().shuffle(all_media_items)
                    logger.info("Shuffled {} items for random processing order", len(all_media_items))

                total_items = len(all_media_items)
                logger.info(
                    "Processing {} items across {} libraries in a shared queue", total_items, len(library_item_counts)
                )
                for library_name, count in library_item_counts:
                    logger.info("Library queued: {} ({} items)", library_name, count)

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
            logger.info("Processing stopped by cancellation: {}", summary)
        else:
            logger.info("Processing complete: {}", summary)

        if total_processed > 0 and not_found > 0 and generated == 0:
            logger.warning(
                "All {} item(s) finished with the file not found locally — no previews were generated this run. "
                "This almost always means your path mappings are wrong: Plex reports the file at one path, but this "
                "app can't see it at that path. Open Settings → Path mappings and add a row that translates Plex's "
                "path to the local path this app sees. The Plex server itself is fine — only file access is broken.",
                not_found,
            )

        log_failure_summary()

        return_data = return_data or {}
        return_data["outcome"] = aggregate_outcome

        return return_data

    except KeyboardInterrupt:
        logger.info("Received interrupt signal, shutting down gracefully...")
    except ConnectionError as e:
        logger.error(
            "Could not reach Plex while running this job ({}). "
            "Job aborted — verify the Plex URL and token in Settings, that the Plex server is running, "
            "and that there's no firewall between the two. Re-run the job once Plex is reachable.",
            e,
        )
        return None
    except Exception as e:
        logger.error(
            "Unexpected error during the preview-generation job — aborting this job. Underlying cause: {}. "
            "This is likely a bug. The web UI and other jobs keep running. "
            "Enable Debug logging under Settings → Logging, re-run the job to capture the full traceback, "
            "then report it at https://github.com/stevezau/plex_generate_vid_previews/issues.",
            e,
        )
        raise
    finally:
        try:
            if worker_pool is not None and not job_id:
                worker_pool.shutdown()
        except Exception as worker_error:
            logger.warning(
                "Worker pool didn't shut down cleanly: {}. "
                "Background threads may still be running — usually harmless, but if you see orphan FFmpeg "
                "processes after the job ends, restart the container.",
                worker_error,
            )
        finally:
            if not job_id and worker_pool_callback:
                worker_pool_callback(None)

        try:
            if os.path.isdir(config.working_tmp_folder):
                shutil.rmtree(config.working_tmp_folder)
                logger.debug("Cleaned up working temp folder: {}", config.working_tmp_folder)
        except Exception as cleanup_error:
            logger.warning(
                "Could not delete the working temp folder at {}: {}. "
                "This won't break future runs but the folder will accumulate data over time — "
                "watch your disk and manually clear it if it grows large.",
                config.working_tmp_folder,
                cleanup_error,
            )
