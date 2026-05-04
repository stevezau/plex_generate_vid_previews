"""Core processing workflow for video preview generation.

Contains run_processing() which orchestrates Plex library scanning,
media item dispatch, and worker pool management.  Used exclusively
by the web layer (job_runner.py).
"""

import os
import random
import shutil
import time

from loguru import logger

from ..plex_client import get_media_items_by_paths, plex_server
from ..processing.generator import ProcessingResult, clear_failures, log_failure_summary
from ..servers.ownership import apply_path_mappings
from .worker import WorkerPool


def _outcome_for_multi_server_status(status) -> ProcessingResult:
    """Map a :class:`MultiServerStatus` to the legacy ProcessingResult.

    Mirrors ``Worker._record_outcome`` so the multi-server dispatch path
    (which bypasses :class:`Worker` and calls ``process_canonical_path``
    directly) can persist file-result rows with the same outcome strings
    the Files panel filters on. Without this the multi-server scan path
    skipped ``record_file_result`` entirely and the panel stayed empty
    for the duration of the run.
    """
    from ..processing.multi_server import MultiServerStatus

    if status is MultiServerStatus.PUBLISHED:
        return ProcessingResult.GENERATED
    if status is MultiServerStatus.SKIPPED:
        return ProcessingResult.SKIPPED_BIF_EXISTS
    if status is MultiServerStatus.SKIPPED_NOT_INDEXED:
        return ProcessingResult.SKIPPED_NOT_INDEXED
    if status is MultiServerStatus.SKIPPED_FILE_NOT_FOUND:
        return ProcessingResult.SKIPPED_FILE_NOT_FOUND
    if status is MultiServerStatus.NO_OWNERS:
        return ProcessingResult.NO_MEDIA_PARTS
    return ProcessingResult.FAILED


def _publisher_rows_from_result(result, canonical_path: str) -> list[dict]:
    """Flatten a MultiServerResult into wire-friendly publisher rows for Job UI.

    Persisted on Job.publishers so the dashboard can render
    "this file: Plex ✓, Emby ✗" without re-grepping the log stream.
    Also looks up the server type from media_servers so the badge
    palette matches.
    """
    rows = []
    type_by_id: dict[str, str] = {}
    try:
        from ..web.settings_manager import get_settings_manager

        for entry in get_settings_manager().get("media_servers") or []:
            if isinstance(entry, dict) and entry.get("id"):
                type_by_id[str(entry["id"])] = (entry.get("type") or "").lower()
    except Exception:
        pass
    for pub in (result.publishers or []) if result is not None else []:
        status = pub.status.value if hasattr(pub.status, "value") else str(pub.status)
        rows.append(
            {
                "server_id": pub.server_id,
                "server_name": pub.server_name,
                "server_type": type_by_id.get(str(pub.server_id), ""),
                "adapter_name": pub.adapter_name,
                "status": status,
                "message": pub.message or "",
                "canonical_path": canonical_path,
                # Frame provenance ("extracted" | "cache_hit" | "output_existed")
                # so the Job UI can render a distinct badge when frames were
                # reused across a sibling-server webhook.
                "frame_source": getattr(pub, "frame_source", "extracted"),
                # output_paths feeds the BIF-viewer deep-link in the Files
                # panel — see record_file_result + job_modal.js (D34).
                "output_paths": [str(op) for op in (getattr(pub, "output_paths", None) or [])],
            }
        )
    return rows


def _log_webhook_owning_servers(config, paths: list[str]) -> None:
    """Log a one-line summary of which configured servers own the webhook paths.

    Best-effort: any failure resolving ownership is swallowed so a logging
    bug never blocks the actual dispatch. Used purely as a breadcrumb so
    the operator can read the log top-down and see, before any per-server
    work runs, *which* servers will be touched and how many paths each
    owns. Without this line the legacy single-Plex resolver path looks
    indistinguishable from the multi-server fan-out path.
    """
    try:
        from ..servers.ownership import find_owning_servers
        from ..servers.registry import server_config_from_dict
        from ..web.settings_manager import get_settings_manager

        raw = get_settings_manager().get("media_servers") or []
        configs = []
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            if entry.get("enabled") is False:
                continue
            try:
                configs.append(server_config_from_dict(entry))
            except Exception:
                continue

        if not configs:
            logger.info(
                "Resolving {} webhook path(s) — no media servers configured yet, skipping ownership lookup.",
                len(paths),
            )
            return

        from ..servers.ownership import apply_webhook_prefixes

        name_by_id = {cfg.id: (cfg.name or cfg.id) for cfg in configs}
        owners_by_server: dict[str, int] = {}
        unowned = 0
        for path in paths:
            # Try the path AS-IS first, then fall back to candidates produced
            # by translating webhook_prefixes → local_prefix on every server's
            # path_mappings. Sonarr/Radarr send paths in their own view
            # (e.g. /data/Movies/X.mkv) which won't match library remote_paths
            # like /data_16tb/Movies until translated. Without this, the
            # breadcrumb cried "none match" even when path mapping would have
            # resolved everything cleanly downstream.
            candidate_paths = {path}
            for cfg in configs:
                for translated in apply_webhook_prefixes(path, cfg.path_mappings or []):
                    candidate_paths.add(translated)
            path_matches = []
            for cp in candidate_paths:
                path_matches.extend(find_owning_servers(cp, configs))
            # Dedupe per (path, server_id) so a path that matches via two
            # webhook prefixes doesn't double-count.
            seen_servers = set()
            uniq_matches = []
            for m in path_matches:
                if m.server_id in seen_servers:
                    continue
                seen_servers.add(m.server_id)
                uniq_matches.append(m)
            if not uniq_matches:
                unowned += 1
                continue
            for match in uniq_matches:
                key = name_by_id.get(match.server_id, match.server_id)
                owners_by_server[key] = owners_by_server.get(key, 0) + 1

        if not owners_by_server:
            logger.info(
                "Resolving {} webhook path(s) — none match any configured server's enabled libraries yet "
                "(retry queue will keep trying).",
                len(paths),
            )
            return

        ordered = ", ".join(f"{name} ({count} path(s))" for name, count in owners_by_server.items())
        pinned = getattr(config, "server_id_filter", None)
        scope_note = f" (pinned to server_id={pinned!r})" if pinned else ""
        suffix = f"; {unowned} path(s) unowned" if unowned else ""
        logger.info(
            "Resolving {} webhook path(s) across owning server(s): {}{}{}",
            len(paths),
            ordered,
            scope_note,
            suffix,
        )
    except Exception as exc:  # never block dispatch on a logging failure
        logger.debug("owning-servers breadcrumb skipped: {}", exc)


def _enumerate_plex_full_scan_items(
    config,
    registry,
    *,
    cancel_check=None,
    progress_callback=None,
):
    """Yield :class:`ProcessableItem` for the Plex full-library scan flow.

    Pulled out as a module-level function so tests can patch this single
    boundary instead of stubbing PlexProcessor + ServerRegistry +
    get_processor_for separately. Production code in ``run_processing``
    invokes this exactly once per Plex full-scan dispatch.
    """
    from ..processing import get_processor_for
    from ..servers.base import ServerType

    plex_cfg = next((c for c in registry.configs() if c.type is ServerType.PLEX), None)
    if plex_cfg is None:
        return
    plex_processor = get_processor_for(ServerType.PLEX)
    library_ids = list(getattr(config, "plex_library_ids", None) or []) or None
    yield from plex_processor.list_canonical_paths(
        plex_cfg,
        library_ids=library_ids,
        cancel_check=cancel_check,
        progress_callback=progress_callback,
    )


def _dispatch_webhook_paths_multi_server(
    config,
    *,
    progress_callback=None,
    cancel_check=None,
    pause_check=None,
    job_id: str | None = None,
    paths: list[str] | None = None,
    item_id_hints: dict[str, dict[str, str]] | None = None,
) -> dict:
    """Dispatch webhook_paths through the multi-server registry without Plex.

    Used when a webhook fires on an Emby/Jellyfin-only install: the legacy
    Plex resolution shortcut is unavailable, but ``process_canonical_path``
    in the multi-server dispatcher walks every owning server in the registry
    directly — Plex is not required.

    When ``job_id`` is supplied, per-publisher outcomes are appended to that
    job's ``publishers`` field so the Jobs UI can show per-server status.

    K4: ``paths`` may be provided to dispatch a *subset* of the job's
    webhook_paths (e.g. the unresolved-by-Plex paths) so the fallback
    multi-server flow only runs for those Plex couldn't claim. When None,
    falls back to ``config.webhook_paths`` for backward compat with the
    no-Plex code path that originally introduced this helper.

    Returns the aggregated ProcessingResult counts keyed by enum value.
    """
    from ..processing.multi_server import process_canonical_path
    from ..servers import ServerRegistry
    from ..web.settings_manager import get_settings_manager

    counts = {r.value: 0 for r in ProcessingResult}
    if paths is None:
        paths = list(config.webhook_paths or [])
    else:
        paths = list(paths)
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

    job_manager = None
    if job_id:
        try:
            from ..web.jobs import get_job_manager

            job_manager = get_job_manager()
        except Exception:
            job_manager = None

    for idx, p in enumerate(paths, 1):
        if cancel_check and cancel_check():
            logger.info("Webhook dispatch cancelled after {} of {} path(s)", idx - 1, len(paths))
            break
        # Pause gate — block (don't bail) so the loop resumes from the
        # current path when the user un-pauses, instead of dropping the
        # remaining webhook paths on the floor.
        while pause_check and pause_check():
            if cancel_check and cancel_check():
                logger.info("Webhook dispatch cancelled while paused after {} of {} path(s)", idx - 1, len(paths))
                return counts
            time.sleep(0.25)
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
                regenerate=bool(getattr(config, "regenerate_thumbnails", False)),
                item_id_by_server=(item_id_hints or {}).get(p),
            )
            for pub in result.publishers or []:
                key = (pub.status.value if hasattr(pub.status, "value") else str(pub.status)).lower()
                counts[key] = counts.get(key, 0) + 1
            if job_manager is not None:
                try:
                    job_manager.append_publishers(job_id, _publisher_rows_from_result(result, p))
                except Exception as exc:
                    logger.debug("Could not append publisher rows for job {}: {}", job_id, exc)
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


def _dispatch_processable_items(
    items,
    *,
    config,
    registry,
    selected_gpus,
    progress_callback=None,
    cancel_check=None,
    pause_check=None,
    job_id: str | None = None,
    label: str = "scan",
    server_id_filter: str | None = None,
    worker_callback=None,
) -> dict:
    """Run a list of ``(server_config, ProcessableItem)`` pairs in parallel.

    Shared dispatch loop used by :func:`_run_full_scan_multi_server` and
    :func:`_run_recently_added_multi_server`. Pulled out so adding new
    enumeration sources doesn't mean copying ~80 lines of GPU rotation +
    progress-callback + per-publisher aggregation.

    Args:
        items: Pre-collected list of ``(server_config, ProcessableItem)``.
        config: Job-wide :class:`Config` used by FFmpeg + frame extraction.
        registry: Live :class:`ServerRegistry` (publishers fan out via this).
        selected_gpus: ``[(gpu_type, gpu_device, gpu_info), ...]`` from the
            UI's GPU selection. Workers use this round-robin.
        progress_callback: Optional ``(processed, total, msg)`` callback
            forwarded to the UI's progress widget.
        cancel_check: Optional callable returning True when the caller wants
            the dispatch to stop.
        job_id: Optional job identifier; per-publisher rows get appended to
            this job for the dashboard's per-server status view.
        label: Free-text identifier used in info logs ("full scan",
            "recently-added scan", etc.) so log lines stay grep-friendly.

    Returns:
        Aggregated PublisherStatus counts keyed by enum value
        (``published``/``failed``/``skipped_*``) — same shape every
        existing caller already depends on.
    """
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from ..processing.generator import _notify_file_result
    from ..processing.multi_server import process_canonical_path
    from ..servers.base import ServerType

    counts = {r.value: 0 for r in ProcessingResult}
    total = len(items)
    if total == 0:
        return counts

    gpu_devices = list(selected_gpus or [])
    cpu_workers = max(0, int(getattr(config, "cpu_threads", 1) or 0))

    def _read_workers_count(gpu_info) -> int:
        # Previously ``getattr(g[2], "workers", 1)`` — but ``g[2]`` is the
        # dict that ``_build_selected_gpus`` constructs with
        # ``info["workers"] = workers``. ``getattr`` on a dict returns
        # the default unless the dict also has an attribute by that
        # name (it doesn't), so a user with two GPUs each configured for
        # 2 workers got parallelism=2 instead of 4. Use the right
        # accessor + clamp to ≥1 so a workers=0 typo can't silently
        # hide a device.
        if isinstance(gpu_info, dict):
            try:
                return max(1, int(gpu_info.get("workers", 1) or 1))
            except (TypeError, ValueError):
                return 1
        try:
            return max(1, int(getattr(gpu_info, "workers", 1) or 1))
        except (TypeError, ValueError):
            return 1

    from .worker_naming import (
        cpu_worker_label as _cpu_worker_label,
    )
    from .worker_naming import (
        friendly_device_label as _device_label,
    )
    from .worker_naming import (
        gpu_worker_label as _gpu_worker_label,
    )

    # Pre-allocate one stable slot per concurrent worker. The slot is
    # alive for the entire dispatch; only ``status`` and ``current_title``
    # mutate as items pass through. Two payoffs:
    #   1. The Workers panel shows N persistent rows (matches the legacy
    #      WorkerPool model). No more rows flashing on/off as items
    #      complete and the next thread picks up a microsecond later.
    #   2. Each ThreadPool thread persistently binds to ONE slot, which
    #      means its GPU assignment is also stable — no per-item
    #      round-robin churn.
    # Per-type counter so labels match the legacy WorkerPool format
    # ("GPU Worker 1 (NVIDIA TITAN RTX)") that users already recognise
    # — avoids two different label conventions side-by-side when a job
    # mixes the legacy and multi-server paths.
    # Slot identity is decoupled from thread identity so the settings
    # poller (further down) can add/remove rows mid-job without
    # disturbing in-flight work — the dispatcher used to bake the slot
    # count at job start, so a user bumping NVIDIA workers 2→3 saw no
    # change until the next job. With the decoupling: a thread acquires
    # the semaphore, claims any free slot at runtime, releases it on
    # finish. New slots become claimable the moment the poller appends
    # them.
    gpu_slots: list[dict] = []
    _seq_state = {"slot": 0, "gpu": 0, "cpu": 0}

    def _build_gpu_slot(gpu_type: str, gpu_device: str | None, gpu_info: dict | object) -> dict:
        _seq_state["slot"] += 1
        _seq_state["gpu"] += 1
        return {
            "worker_id": _seq_state["slot"],
            "worker_type": "GPU",
            "worker_name": _gpu_worker_label(_seq_state["gpu"], _device_label(gpu_info, gpu_device, gpu_type)),
            "gpu_type": gpu_type,
            "gpu_device": gpu_device,
            "_gpu_info": gpu_info,  # kept so the poller can build matching new slots
            "status": "idle",
            "current_title": "",
            "library_name": "",
            "progress_percent": 0,
            "speed": "0.0x",
            "remaining_time": 0,
            "_claimed_by": None,
            "_pending_removal": False,
        }

    def _build_cpu_slot() -> dict:
        _seq_state["slot"] += 1
        _seq_state["cpu"] += 1
        return {
            "worker_id": _seq_state["slot"],
            "worker_type": "CPU",
            "worker_name": _cpu_worker_label(_seq_state["cpu"]),
            "gpu_type": None,
            "gpu_device": None,
            "_gpu_info": None,
            "status": "idle",
            "current_title": "",
            "library_name": "",
            "progress_percent": 0,
            "speed": "0.0x",
            "remaining_time": 0,
            "_claimed_by": None,
            "_pending_removal": False,
        }

    for gpu_type, gpu_device, gpu_info in gpu_devices:
        for _ in range(_read_workers_count(gpu_info)):
            gpu_slots.append(_build_gpu_slot(gpu_type, gpu_device, gpu_info))
    for _ in range(cpu_workers):
        gpu_slots.append(_build_cpu_slot())

    initial_concurrency = max(1, len(gpu_slots))
    # Generous ThreadPool ceiling so the hot-reload poller can grow the
    # active worker count without bumping into max_workers. Tasks waiting
    # on the concurrency semaphore are cheap (one idle thread each) so
    # 32 is fine; users wanting more can restart.
    pool_max_workers = max(initial_concurrency, 32)

    job_manager = None
    if job_id:
        try:
            from ..web.jobs import get_job_manager

            job_manager = get_job_manager()
        except Exception:
            job_manager = None

    logger.info(
        "Multi-server {}: dispatching {} item(s) with parallelism={} (max pool={})",
        label,
        total,
        initial_concurrency,
        pool_max_workers,
    )
    # Surface "Dispatching N items…" up-front so the progress widget gets
    # a real total + denominator the moment enumeration finishes — without
    # this the bar sits at 0/0 with the stale "Querying…" label until the
    # first item completes (can be 30s+ on the first FFmpeg pass).
    if progress_callback:
        try:
            progress_callback(0, total, f"Dispatching {total} item(s) across {initial_concurrency} worker(s)…")
        except Exception as exc:
            logger.debug("progress_callback raised on dispatch banner: {}", exc)

    # Concurrency gate. Decoupled from ThreadPoolExecutor's max_workers
    # so the hot-reload poller below can grow/shrink concurrency by
    # adjusting the permit count alone.
    _slots_lock = threading.Lock()
    _concurrency_cond = threading.Condition(_slots_lock)
    _concurrency_state = {"target": initial_concurrency, "active": 0}

    def _acquire_concurrency() -> None:
        # Block until the active count is below the dynamic target.
        # If the user shrinks workers mid-job, queued threads back up
        # here until enough active ones finish; if they grow workers
        # the poller's notify_all wakes a queued thread immediately.
        with _concurrency_cond:
            while _concurrency_state["active"] >= _concurrency_state["target"]:
                _concurrency_cond.wait()
            _concurrency_state["active"] += 1

    def _release_concurrency() -> None:
        with _concurrency_cond:
            _concurrency_state["active"] -= 1
            _concurrency_cond.notify_all()

    def _claim_idle_slot(thread_name: str) -> dict | None:
        # Slots are claimed at task start (after concurrency gate),
        # released at task end. A thread is NOT pinned to a slot —
        # different items the same thread processes can land on
        # different slots (whichever is free). That's necessary for
        # hot-reload: when the user adds a new GPU slot, any waiting
        # thread can pick it up immediately rather than only the
        # thread that was bound to that slot at job start.
        with _slots_lock:
            for s in gpu_slots:
                if s["_claimed_by"] is None and not s["_pending_removal"] and s["status"] == "idle":
                    s["_claimed_by"] = thread_name
                    return s
            return None

    def _release_slot(slot: dict) -> None:
        with _slots_lock:
            slot["status"] = "idle"
            slot["current_title"] = ""
            slot["_claimed_by"] = None
            slot["progress_percent"] = 0
            # If the poller marked this slot for removal while it was
            # busy, drop it now that it's free — the panel sees one
            # fewer row on the next snapshot.
            if slot["_pending_removal"]:
                try:
                    gpu_slots.remove(slot)
                except ValueError:
                    pass

    def _snapshot_slots() -> list[dict]:
        # Strip internal bookkeeping ("_claimed_by", "_pending_removal",
        # "_gpu_info") before exposing rows to the worker_callback.
        with _slots_lock:
            return [{k: v for k, v in s.items() if not k.startswith("_")} for s in gpu_slots]

    def _emit_worker_snapshot() -> None:
        if not worker_callback:
            return
        try:
            worker_callback(_snapshot_slots())
        except Exception as exc:
            logger.debug("worker_callback raised: {}", exc)

    # ── Hot-reload settings poller ───────────────────────────────────
    # Reads gpu_config + cpu_threads every ~1.5s and reconciles
    # gpu_slots + concurrency target with the live setting. The user
    # can bump NVIDIA workers from 2→3 in Settings while the job is
    # running and see the third row appear within ~1.5s — without
    # this the slot count was baked at job start.
    _poller_stop = threading.Event()
    _poller_thread: threading.Thread | None = None

    def _reconcile_with_settings() -> None:
        try:
            from ..web.settings_manager import get_settings_manager

            sm = get_settings_manager()
            new_gpu_config = sm.gpu_config or []
            new_cpu = max(0, int(getattr(sm, "cpu_threads", 0) or 0))
        except Exception as exc:
            logger.debug("hot-reload poller: settings read failed ({}); skipping tick", exc)
            return

        # Build a target {device: desired_count} map for GPUs that were
        # in scope at job start. Devices added/removed entirely
        # mid-job are out of scope (next job picks them up).
        gpu_info_by_device: dict[str, tuple[str, object]] = {}
        for gpu_type, gpu_device, gpu_info in gpu_devices:
            if gpu_device:
                gpu_info_by_device[gpu_device] = (gpu_type, gpu_info)

        desired_gpu_per_device: dict[str, int] = {}
        for entry in new_gpu_config:
            if not isinstance(entry, dict):
                continue
            device = entry.get("device")
            if device not in gpu_info_by_device:
                continue
            if not entry.get("enabled", True):
                desired_gpu_per_device[device] = 0
                continue
            try:
                desired_gpu_per_device[device] = max(0, int(entry.get("workers", 1) or 0))
            except (TypeError, ValueError):
                desired_gpu_per_device[device] = 1

        added = 0
        removed = 0
        with _slots_lock:
            # GPU diff per device
            for device, desired in desired_gpu_per_device.items():
                live = [
                    s
                    for s in gpu_slots
                    if s["worker_type"] == "GPU" and s["gpu_device"] == device and not s["_pending_removal"]
                ]
                delta = desired - len(live)
                if delta > 0:
                    gpu_type, gpu_info = gpu_info_by_device[device]
                    for _ in range(delta):
                        gpu_slots.append(_build_gpu_slot(gpu_type, device, gpu_info))
                        added += 1
                elif delta < 0:
                    # Prefer to retire idle slots first (immediate),
                    # mark busy ones as pending so they retire when
                    # they finish their current item.
                    surplus = -delta
                    idle_first = sorted(live, key=lambda s: 0 if s["_claimed_by"] is None else 1)
                    for s in idle_first[:surplus]:
                        if s["_claimed_by"] is None:
                            try:
                                gpu_slots.remove(s)
                                removed += 1
                            except ValueError:
                                pass
                        else:
                            s["_pending_removal"] = True
                            removed += 1
            # CPU diff
            live_cpu = [s for s in gpu_slots if s["worker_type"] == "CPU" and not s["_pending_removal"]]
            delta = new_cpu - len(live_cpu)
            if delta > 0:
                for _ in range(delta):
                    gpu_slots.append(_build_cpu_slot())
                    added += 1
            elif delta < 0:
                surplus = -delta
                idle_first = sorted(live_cpu, key=lambda s: 0 if s["_claimed_by"] is None else 1)
                for s in idle_first[:surplus]:
                    if s["_claimed_by"] is None:
                        try:
                            gpu_slots.remove(s)
                            removed += 1
                        except ValueError:
                            pass
                    else:
                        s["_pending_removal"] = True
                        removed += 1

            new_target = sum(1 for s in gpu_slots if not s["_pending_removal"])
            if new_target != _concurrency_state["target"]:
                _concurrency_state["target"] = max(1, new_target)
                _concurrency_cond.notify_all()

        if added or removed:
            logger.info(
                "Hot-reload: gpu_config changed mid-{} — added {} slot(s), removed {} slot(s); concurrency target now {}",
                label,
                added,
                removed,
                _concurrency_state["target"],
            )
            _emit_worker_snapshot()

    def _poller_loop() -> None:
        while not _poller_stop.wait(1.5):
            _reconcile_with_settings()
            # Periodic snapshot tick so the Workers panel reflects
            # in-flight FFmpeg progress (progress_percent / speed) within
            # ~1.5s. Without this, the snapshot only fires at task
            # start / end and during settings reconciliation, leaving
            # the panel frozen at "0% @ 0.0x" through 30s+ FFmpeg
            # passes for multi-server full scans.
            _emit_worker_snapshot()

    def _process_one(index_and_item):
        # D27 — register the executor's worker thread under this job's
        # id so the per-job log handler captures every per-file
        # Dispatch / Owners-resolved / FFmpeg / Publisher line that
        # process_canonical_path emits. Without this, the Emby/Jellyfin
        # full-scan path (which uses ThreadPoolExecutor directly,
        # bypassing JobDispatcher → Worker.assign_task → register_job_thread)
        # leaves its threads anonymous and the per-job log shows only
        # the lifecycle markers — users see "dispatching 5000 items"
        # then nothing for hours despite continuous activity in app.log.
        # Idempotent re-register per call: the executor pool reuses
        # threads across items, but every call sets the same job_id so
        # there's no churn in _job_thread_to_job_id.
        if job_id:
            from .worker import register_job_thread

            register_job_thread(job_id)

        index, (server_cfg, item) = index_and_item
        thread_name = threading.current_thread().name

        if cancel_check and cancel_check():
            return ("Worker", None)

        # Pause gate — block (don't bail) while the queue is paused so
        # NEW FFmpegs don't spawn after the user clicks Pause All. The
        # dispatcher path (job_runner → JobDispatcher) already honours
        # pause via tracker.is_paused() in _get_next_item, but the
        # multi-server full-scan / webhook ThreadPoolExecutor path used
        # to ignore it — pausing only halted in-flight FFmpegs (via
        # SIGSTOP from commit 6d812ad) while the executor kept pulling
        # the next item and launching fresh subprocesses for ~6 minutes
        # until the queue drained. Cancel takes precedence over pause
        # so a user who pauses then cancels isn't stuck waiting.
        while pause_check and pause_check():
            if cancel_check and cancel_check():
                return ("Worker", None)
            time.sleep(0.25)

        # Two-step acquisition: concurrency permit (gates how many
        # threads run real work simultaneously) and slot claim (which
        # row in the Workers panel represents this thread). The poller
        # adjusts both atomically.
        _acquire_concurrency()
        slot = _claim_idle_slot(thread_name)
        # Edge case: poller marked all live slots for removal between
        # the acquire and the claim. Release the permit so others can
        # proceed and bail this item.
        if slot is None:
            _release_concurrency()
            return ("Worker", None)

        # GPU assignment is *per slot*, not per item — the slot's
        # gpu_type/gpu_device were set at slot creation so concurrency
        # is correctly distributed across physical devices even after
        # hot-reload reshuffles slots.
        gpu_type = slot["gpu_type"]
        gpu_device = slot["gpu_device"]
        worker_label = slot["worker_name"]

        # Flip the slot to "processing X" in place — never pop it.
        # Rows persist for the whole dispatch (legacy WorkerPool
        # model) so the Workers panel doesn't flash entries on/off.
        with _slots_lock:
            slot["status"] = "processing"
            slot["current_title"] = item.title or os.path.basename(item.canonical_path)
        # Push the snapshot the moment the thread picks up the item so
        # the Workers panel shows activity within ~1 frame of dispatch
        # (otherwise the panel would only update on completion — for
        # FFmpeg passes that take 30s+ that's a very long blank stare).
        _emit_worker_snapshot()

        # Pin precedence:
        #   1. Caller-supplied ``server_id_filter`` always wins — that's
        #      the user's explicit "scan Movies on plex-default" pin from
        #      the job config. Without this we'd fan out to Jellyfin/Emby
        #      on a Plex-pinned job (job d9918149 reproducer: every Plex
        #      file got a JellyTest publisher attempt that failed because
        #      no Jellyfin item_id existed).
        #   2. No caller pin + non-Plex originator → scope to that
        #      originator. Plex isn't reachable on this install.
        #   3. No caller pin + Plex originator → fan out to every
        #      owning server (the original cross-vendor publish path
        #      that benefits multi-vendor installs).
        if server_id_filter:
            per_item_pin = server_id_filter
        elif server_cfg.type is not ServerType.PLEX:
            per_item_pin = server_cfg.id
        else:
            per_item_pin = None

        # Slot progress callback — updates the slot's progress_percent /
        # speed / remaining_time fields in place during FFmpeg so the
        # Workers panel rows show live "<progress>% @ <speed>" instead
        # of a frozen 0.0x. Mirrors what worker._update_worker_progress
        # does for the JobDispatcher path; without this the multi-server
        # full-scan path (which uses ThreadPoolExecutor + the slot dict
        # rather than the dispatcher's WorkerPool) emitted only the
        # title, status changes, and 0% / 0.0x defaults.
        def _slot_progress_callback(
            progress_percent,
            current_duration,
            total_duration,
            speed=None,
            remaining_time=None,
            frame=0,
            fps=0,
            q=0,
            size=0,
            time_str="00:00:00.00",
            bitrate=0,
            media_file=None,
        ):
            with _slots_lock:
                slot["progress_percent"] = progress_percent
                if speed:
                    slot["speed"] = speed
                if remaining_time is not None:
                    slot["remaining_time"] = remaining_time
            # Don't emit a snapshot per progress tick — that'd drown
            # the SocketIO emit queue at 5+ updates/sec/worker. The
            # _emit_worker_snapshot at the top + bottom of this task
            # captures status changes; periodic snapshots come from the
            # dispatcher poll thread (1Hz throttled).

        try:
            result = process_canonical_path(
                canonical_path=item.canonical_path,
                registry=registry,
                config=config,
                item_id_by_server=item.item_id_by_server or None,
                bundle_metadata_by_server=item.bundle_metadata_by_server or None,
                gpu=gpu_type,
                gpu_device_path=gpu_device,
                progress_callback=_slot_progress_callback,
                cancel_check=cancel_check,
                server_id_filter=per_item_pin,
                regenerate=bool(getattr(config, "regenerate_thumbnails", False)),
            )
            return (worker_label, result)
        except Exception as exc:
            logger.warning(
                "Multi-server {}: per-item processing failed for {!r} ({}: {}). "
                "Other items in this run will still be processed.",
                label,
                item.canonical_path,
                type(exc).__name__,
                exc,
            )
            return (worker_label, None)
        finally:
            # Release the slot (auto-removes if poller flagged it) and
            # the concurrency permit. Order matters: free the slot
            # first so the snapshot reflects "idle" before another
            # thread grabs it.
            _release_slot(slot)
            _emit_worker_snapshot()
            _release_concurrency()

    completed = 0
    # Index inputs alongside their outcomes so the per-item record can
    # surface the canonical path even when ``result is None`` (the
    # exception-swallowed branch). Without this the Files panel showed
    # only the surviving rows; the failures were just a number on the
    # summary chip with no per-file attribution.
    indexed_items = list(enumerate(items))
    _poller_thread = threading.Thread(
        target=_poller_loop,
        name=f"multi-server-poller-{label}",
        daemon=True,
    )
    _poller_thread.start()
    try:
        pool_ctx = ThreadPoolExecutor(max_workers=pool_max_workers)
    except Exception:
        _poller_stop.set()
        raise
    with pool_ctx as pool:
        for index_and_pair in zip(indexed_items, pool.map(_process_one, indexed_items), strict=True):
            (idx, (_server_cfg, item)), (worker_label, result) = index_and_pair
            completed += 1
            if progress_callback:
                try:
                    progress_callback(completed, total, f"Processed {completed}/{total}")
                except Exception:
                    pass
            # ``worker_label`` already came back from _process_one as the
            # actual stable slot name (e.g. "NVIDIA TITAN RTX #1") that
            # processed this item — so the Files panel's Worker column
            # shows the same identity as the Workers panel's row.
            if result is None:
                # _process_one swallowed an exception (FFmpeg crash, codec
                # not supported, etc.). Count it as a failed item so the
                # outcome counter — and the Job UI badge — surface it
                # instead of silently reporting "Completed".
                counts["failed"] = counts.get("failed", 0) + 1
                # Persist a Files-panel row so the user can see *which*
                # file failed without grepping the log. Without this, a
                # batch with 50 failures showed "0 file(s)" in the panel
                # and the failures only existed as a counter on the chip.
                try:
                    _notify_file_result(
                        item.canonical_path,
                        ProcessingResult.FAILED,
                        "process_canonical_path raised — see app log for traceback",
                        worker_label,
                        servers=[],
                    )
                except Exception as exc:
                    logger.debug("Could not notify failed file result for {}: {}", item.canonical_path, exc)
                continue
            for pub in result.publishers or []:
                key = (pub.status.value if hasattr(pub.status, "value") else str(pub.status)).lower()
                counts[key] = counts.get(key, 0) + 1
            rows = _publisher_rows_from_result(result, result.canonical_path)
            if job_manager is not None:
                try:
                    job_manager.append_publishers(job_id, rows)
                except Exception as exc:
                    logger.debug("Could not append publisher rows for job {}: {}", job_id, exc)
            # Live Files-panel row. The multi-server dispatch path
            # bypasses Worker → _persist (it calls process_canonical_path
            # directly via the ThreadPoolExecutor), so without this hook
            # the JSONL file stays empty for the entire run and users
            # see "0 file(s)" mid-job despite continuous activity.
            try:
                outcome = _outcome_for_multi_server_status(result.status)
                _notify_file_result(
                    result.canonical_path,
                    outcome,
                    (result.message or "").strip(),
                    worker_label,
                    servers=rows,
                )
            except Exception as exc:
                logger.debug("Could not notify file result for {}: {}", result.canonical_path, exc)

    # Stop the hot-reload poller and clear any persistent slot rows
    # from the panel snapshot. Daemon thread, but explicit shutdown
    # avoids a 1.5s tail of poller activity after the dispatch
    # returns.
    _poller_stop.set()
    try:
        _poller_thread.join(timeout=2.0)
    except Exception:
        pass
    logger.info("Multi-server {} complete: {} item(s) processed.", label, completed)
    return counts


def _enumerate_items_for_servers(
    candidates,
    *,
    enumerate_one,
    cancel_check=None,
    label: str,
    progress_callback=None,
):
    """Walk every server in ``candidates`` and collect the items each yields.

    Shared by :func:`_run_full_scan_multi_server` and
    :func:`_run_recently_added_multi_server` — both walk the same list of
    candidate :class:`ServerConfig` objects, look up the right
    :class:`VendorProcessor`, and dispatch to *some* enumeration method
    on it. ``enumerate_one(processor, server_cfg) -> Iterator[ProcessableItem]``
    captures the only thing that actually differs between the two callers
    (``processor.list_canonical_paths`` vs ``processor.scan_recently_added``).

    Returns a list of ``(server_config, ProcessableItem)`` ready to feed
    into :func:`_dispatch_processable_items`.

    De-duping across servers (Phase P4) lives in this helper so it
    applies uniformly to full-scan AND recently-added flows.
    """
    from ..processing import get_processor_for

    all_items: list = []
    by_canonical: dict[str, int] = {}  # canonical_path → index in all_items

    for server_cfg in candidates:
        try:
            processor = get_processor_for(server_cfg.type)
        except KeyError as exc:
            logger.warning(
                "No processor registered for {!r} ({}). Skipping this server.",
                server_cfg.type,
                exc,
            )
            continue
        if cancel_check and cancel_check():
            logger.info("Cancellation requested while enumerating items — aborting {}.", label)
            return all_items
        # Surface a "Querying…" status BEFORE the per-server walk so the UI
        # progress bar shows the system is alive during the slow library
        # enumeration phase (Emby/Jellyfin TV libraries can take 10–60s
        # before the first item is yielded). Without this, the job sits at
        # "0/0" with no message and users assume it's stuck.
        if progress_callback:
            try:
                _server_label = server_cfg.name or server_cfg.id or server_cfg.type.value
                progress_callback(0, 0, f"Querying {_server_label} library…")
            except Exception as exc:
                logger.debug("progress_callback raised during enumeration banner: {}", exc)
        try:
            for item in enumerate_one(processor, server_cfg):
                if cancel_check and cancel_check():
                    logger.info("Cancellation requested mid-enumeration — aborting {}.", label)
                    return all_items

                # Phase P4: when the same canonical_path appears on more
                # than one server (typical: Plex+Jellyfin sharing media,
                # or two Plex servers with shared storage), keep ONE
                # ProcessableItem and merge every server's vendor item-id
                # hint into it. The publish-side fan-out (_resolve_publishers)
                # already targets every owning server; deduping here just
                # avoids dispatching the same path twice.
                existing_index = by_canonical.get(item.canonical_path)
                if existing_index is None:
                    by_canonical[item.canonical_path] = len(all_items)
                    all_items.append((server_cfg, item))
                else:
                    existing_cfg, existing_item = all_items[existing_index]
                    merged_hints = dict(existing_item.item_id_by_server or {})
                    merged_hints.update(item.item_id_by_server or {})
                    if merged_hints != (existing_item.item_id_by_server or {}):
                        from ..processing.types import ProcessableItem

                        all_items[existing_index] = (
                            existing_cfg,
                            ProcessableItem(
                                canonical_path=existing_item.canonical_path,
                                server_id=existing_item.server_id,
                                item_id_by_server=merged_hints,
                                title=existing_item.title or item.title,
                                library_id=existing_item.library_id,
                            ),
                        )
        except Exception as exc:
            logger.warning(
                "Enumeration on {} server {!r} failed ({}: {}). Continuing with the next server in scope.",
                server_cfg.type.value,
                server_cfg.name or server_cfg.id,
                type(exc).__name__,
                exc,
            )

    return all_items


def _build_multi_server_registry(config):
    """Load the live :class:`ServerRegistry` for a multi-server scan/dispatch.

    Wraps the pair of ``settings_manager.get + ServerRegistry.from_settings``
    calls every multi-server entry point repeats and surfaces any failure
    as a warning + ``None`` so callers can ``return zero counts`` early.
    """
    from ..servers import ServerRegistry
    from ..web.settings_manager import get_settings_manager

    try:
        raw_servers = list(get_settings_manager().get("media_servers") or [])
    except Exception as exc:
        logger.warning(
            "Could not read media_servers when running multi-server scan ({}: {}). "
            "Open the Servers page and verify at least one enabled server is configured.",
            type(exc).__name__,
            exc,
        )
        return None
    try:
        return ServerRegistry.from_settings(raw_servers, legacy_config=config)
    except Exception as exc:
        logger.warning(
            "Could not build the media-server registry for multi-server scan ({}: {}). "
            "Open the Servers page and verify each server has valid auth and a reachable URL.",
            type(exc).__name__,
            exc,
        )
        return None


def _run_full_scan_multi_server(
    config,
    *,
    selected_gpus,
    server_id_filter: str | None = None,
    library_ids: list[str] | None = None,
    progress_callback=None,
    cancel_check=None,
    pause_check=None,
    job_id: str | None = None,
    worker_callback=None,
) -> dict:
    """Multi-server full-library scan via the per-vendor :class:`VendorProcessor`.

    Walks every enabled server (or just ``server_id_filter`` when set) using
    the right :class:`VendorProcessor` from the registry, then dispatches each
    enumerated :class:`ProcessableItem` through ``process_canonical_path`` in
    parallel via a :class:`ThreadPoolExecutor`. Workers are sized off the
    user's GPU/CPU configuration and items are distributed across GPUs
    round-robin so a single GPU isn't oversubscribed.

    All vendors (Plex, Emby, Jellyfin) flow through this same path now —
    no separate legacy worker pool. The unified :func:`process_canonical_path`
    handles publish-to-every-owner fan-out so a Plex+Jellyfin install
    publishes both bundles from a single FFmpeg pass.

    Returns the aggregated ProcessingResult counts keyed by enum value
    (same shape as :func:`_dispatch_webhook_paths_multi_server`).
    """
    counts = {r.value: 0 for r in ProcessingResult}

    registry = _build_multi_server_registry(config)
    if registry is None:
        return counts

    candidates = [
        cfg for cfg in registry.configs() if cfg.enabled and (not server_id_filter or cfg.id == server_id_filter)
    ]
    if not candidates:
        logger.warning(
            "No enabled servers matched the multi-server scan request (server_id_filter={!r}). Nothing to process.",
            server_id_filter,
        )
        return counts

    all_items = _enumerate_items_for_servers(
        candidates,
        enumerate_one=lambda processor, server_cfg: processor.list_canonical_paths(
            server_cfg,
            library_ids=library_ids,
            cancel_check=cancel_check,
            progress_callback=progress_callback,
        ),
        cancel_check=cancel_check,
        label="full scan",
        progress_callback=progress_callback,
    )

    if not all_items:
        # Was INFO. WARN it: a "successful" scan that processed nothing is
        # the worst-of-both — the job UI shows green, but the user wonders why
        # no previews appeared. Common real causes: a stale library_id (vendor
        # renamed/recreated the library), an auth token scoped away from the
        # library, vendor's background indexer still catching up, or a
        # library type that filters to no Movies/Episodes. Surface it loudly.
        logger.warning(
            "Multi-server scan walked {} server(s) for library_ids={!r} but found "
            "ZERO items to process. Common causes: (a) the library_ids you passed "
            "no longer match a library on the server (try Refresh libraries on the "
            "Servers page), (b) the auth token can't see this library, (c) the "
            "vendor hasn't finished its own library scan yet, or (d) the library "
            "contains no Movie/Episode items. The job will report success but no "
            "work happened.",
            len(candidates),
            library_ids,
        )
        return counts

    return _dispatch_processable_items(
        all_items,
        config=config,
        registry=registry,
        selected_gpus=selected_gpus,
        progress_callback=progress_callback,
        cancel_check=cancel_check,
        pause_check=pause_check,
        job_id=job_id,
        label="full scan",
        server_id_filter=server_id_filter,
        worker_callback=worker_callback,
    )


def _run_recently_added_multi_server(
    config,
    *,
    selected_gpus,
    server_id_filter: str | None = None,
    library_ids: list[str] | None = None,
    lookback_hours: float = 1.0,
    progress_callback=None,
    cancel_check=None,
    pause_check=None,
    job_id: str | None = None,
    worker_callback=None,
) -> dict:
    """Recently-added scan for any vendor via :class:`VendorProcessor`.

    Walks every enabled server (or just ``server_id_filter``) calling
    ``processor.scan_recently_added`` for each. Per-vendor processors
    handle the API differences (Plex's ``addedAt>>`` filter vs.
    Emby/Jellyfin's ``DateCreated`` sort) so the orchestrator stays
    vendor-agnostic.

    Returns the aggregated ProcessingResult counts.
    """
    counts = {r.value: 0 for r in ProcessingResult}

    registry = _build_multi_server_registry(config)
    if registry is None:
        return counts

    candidates = [
        cfg for cfg in registry.configs() if cfg.enabled and (not server_id_filter or cfg.id == server_id_filter)
    ]
    if not candidates:
        logger.warning(
            "No enabled servers matched the recently-added scan request (server_id_filter={!r}). Nothing to process.",
            server_id_filter,
        )
        return counts

    lookback_int = int(max(1, lookback_hours))
    all_items = _enumerate_items_for_servers(
        candidates,
        enumerate_one=lambda processor, server_cfg: processor.scan_recently_added(
            server_cfg,
            lookback_hours=lookback_int,
            library_ids=library_ids,
        ),
        cancel_check=cancel_check,
        label="recently-added scan",
        progress_callback=progress_callback,
    )

    if not all_items:
        logger.info(
            "Recently-added scan walked {} server(s) but found no items in the lookback window ({}h).",
            len(candidates),
            lookback_hours,
        )
        return counts

    return _dispatch_processable_items(
        all_items,
        config=config,
        registry=registry,
        selected_gpus=selected_gpus,
        progress_callback=progress_callback,
        cancel_check=cancel_check,
        pause_check=pause_check,
        job_id=job_id,
        label="recently-added scan",
        server_id_filter=server_id_filter,
        worker_callback=worker_callback,
    )


def _resolve_pinned_server(sid_filter: str | None) -> tuple[dict | None, str]:
    """Look up the media_servers entry for ``sid_filter`` and return ``(entry, type)``.

    Returns ``(None, "")`` when ``sid_filter`` is unset, when settings can't
    be loaded, or when no entry matches. ``type`` is the lowercased server
    type string ("plex" / "emby" / "jellyfin" / ""). Used by the dispatch-
    mode selector to detect non-Plex pins.
    """
    if not (isinstance(sid_filter, str) and sid_filter):
        return None, ""
    try:
        from ..web.settings_manager import get_settings_manager

        raw = get_settings_manager().get("media_servers") or []
    except Exception:
        return None, ""
    pinned_entry = next((e for e in raw if isinstance(e, dict) and e.get("id") == sid_filter), None)
    pinned_type = ((pinned_entry or {}).get("type") or "").lower()
    return pinned_entry, pinned_type


def _should_use_multi_server_full_scan(config, pinned_type: str) -> bool:
    """Decide whether the full-library scan should go through the multi-server path.

    Use the multi-server scan when ANY of the following holds (and there are
    no webhook paths — the webhook flow has its own selector):

    * Pinned to a non-Plex server.
    * No Plex configured at all.
    * At least one non-Plex server (Emby / Jellyfin) is enabled.

    The legacy Plex-only branch only fires for the pure single-Plex install.
    Reason: :func:`_run_plex_full_scan_phase` builds its registry via
    ``ServerRegistry.from_legacy_config`` which has empty per-server
    path_mappings — on a multi-server install Plex items come back with
    their remote view paths (``/media/Movies/...``) and every worker fails
    with "source missing" because the registry doesn't know how to
    translate those.
    """
    no_webhook_paths = not getattr(config, "webhook_paths", None)
    if not no_webhook_paths:
        return False
    non_plex_pin = bool(pinned_type) and pinned_type != "plex"
    no_plex_at_all = not (config.plex_url and config.plex_token)
    has_non_plex_server = False
    try:
        from ..web.settings_manager import get_settings_manager

        raw = get_settings_manager().get("media_servers") or []
        has_non_plex_server = any(
            isinstance(e, dict) and (e.get("type") or "").lower() in ("emby", "jellyfin") and e.get("enabled", True)
            for e in raw
        )
    except Exception:
        has_non_plex_server = False
    return non_plex_pin or no_plex_at_all or has_non_plex_server


def _maybe_log_path_mapping_misconfig(aggregate_outcome: dict, processed: int) -> bool:
    """Emit the path-mapping misconfiguration warning when the run looks broken.

    Returns ``True`` when the warning fired so callers and tests can assert on
    the exact predicate (every processed item finished as
    ``skipped_file_not_found`` and zero items were generated). Splitting this
    out lets the rule be unit-tested without exercising the entire
    ``run_processing`` pipeline; before the extraction the only test coverage
    re-implemented dictionary arithmetic in the test file and never ran the
    real predicate.
    """
    not_found = aggregate_outcome.get("skipped_file_not_found", 0)
    generated = aggregate_outcome.get("generated", 0)
    if processed > 0 and not_found > 0 and generated == 0:
        logger.warning(
            "All {} item(s) finished with the file not found locally — no previews were generated this run. "
            "This almost always means your path mappings are wrong: Plex reports the file at one path, but this "
            "app can't see it at that path. Open Settings → Path mappings and add a row that translates Plex's "
            "path to the local path this app sees. The Plex server itself is fine — only file access is broken.",
            not_found,
        )
        return True
    return False


def _format_outcome_summary(aggregate_outcome: dict) -> str:
    """Build the one-line "X generated, Y already existed, Z failed" string for the end-of-job log.

    Pure formatter — only counts that fired appear in the output, in a stable
    order. Returns the literal string ``"no items processed"`` when every
    counter is zero so the log line is never empty.
    """
    parts = []
    counters = (
        ("generated", "{n} generated"),
        ("skipped_bif_exists", "{n} already existed"),
        ("skipped_not_indexed", "{n} not indexed yet"),
        ("skipped_file_not_found", "{n} not found"),
        ("skipped_excluded", "{n} excluded"),
        ("skipped_invalid_hash", "{n} invalid hash"),
        ("failed", "{n} failed"),
        ("no_media_parts", "{n} no media parts"),
    )
    for key, template in counters:
        n = aggregate_outcome.get(key, 0)
        if n:
            parts.append(template.format(n=n))
    return ", ".join(parts) if parts else "no items processed"


def _run_webhook_paths_phase(
    config,
    plex,
    registry,
    *,
    dispatch_items,
    progress_callback,
    cancel_check,
    pause_check=None,
    job_id: str | None,
    totals: dict,
    aggregate_outcome: dict,
) -> dict:
    """Resolve webhook paths via Plex, dispatch the resolved items, and run the K4 fallback.

    Mutates ``totals`` (keys: ``processed``, ``successful``, ``failed``,
    ``cancelled``) and ``aggregate_outcome`` in place so the caller can
    keep accumulating across phases. Returns the ``webhook_resolution``
    dict that becomes part of the job's return_data.

    The K4 fallback dispatches any path Plex couldn't claim through the
    multi-server registry so Emby/Jellyfin webhooks resolve via their own
    APIs instead of dying at the Plex resolver. Only fires when at least
    one non-Plex server is configured AND the server_id pin (if any)
    isn't a Plex server itself.
    """
    # Vendor-webhook short-circuit: when the inbound payload supplied
    # ``{server_id: item_id}`` hints (Plex/Emby/Jellyfin native webhooks),
    # the canonical path is already known to belong to those servers —
    # there's nothing to learn from a Plex resolution pass. Build
    # ProcessableItems carrying the hint and run them through the same
    # ``dispatch_items`` worker pool the Plex-resolved path uses, so the
    # job gets real GPU/CPU worker rows in the Jobs UI instead of
    # silently executing on the orchestrator thread.
    hints = getattr(config, "webhook_item_id_hints", None) or None
    if hints:
        from ..processing.types import ProcessableItem as _PI

        if progress_callback:
            path_count = len(config.webhook_paths)
            progress_callback(0, 0, f"Dispatching {path_count} pre-resolved path(s)…")
        _log_webhook_owning_servers(config, config.webhook_paths)

        webhook_items: list[_PI] = []
        for path in config.webhook_paths or []:
            per_path = hints.get(path) or {}
            # Pick a server_id for the ProcessableItem. The first hint key
            # is the originating vendor (Plex/Emby/Jellyfin) — empty when
            # callers pass paths with no hint, in which case the orchestrator
            # will still walk every owning server inside process_canonical_path.
            server_id = next(iter(per_path), "")
            webhook_items.append(
                _PI(
                    canonical_path=path,
                    server_id=server_id,
                    item_id_by_server=dict(per_path),
                    title=os.path.basename(path),
                    library_id=None,
                )
            )

        if not webhook_items:
            logger.info("Vendor webhook short-circuit fired but produced no items — skipping dispatch.")
        else:
            result = dispatch_items(webhook_items, "Webhook Targets")
            totals["successful"] += result["completed"]
            totals["failed"] += result["failed"]
            totals["processed"] += result["completed"] + result["failed"]
            totals["cancelled"] = totals["cancelled"] or result["cancelled"]
            for k, v in (result.get("outcome") or {}).items():
                aggregate_outcome[k] = aggregate_outcome.get(k, 0) + v

        return {
            "unresolved_paths": [],
            "skipped_paths": [],
            "resolved_count": len(config.webhook_paths or []),
            "total_paths": len(config.webhook_paths or []),
            "path_hints": [],
        }

    if progress_callback:
        path_count = len(config.webhook_paths)
        progress_callback(0, 0, f"Looking up {path_count} file path(s) in Plex — this can take a while...")
    _log_webhook_owning_servers(config, config.webhook_paths)
    webhook_resolution = get_media_items_by_paths(plex, config, config.webhook_paths)
    return_payload = {
        "unresolved_paths": list(webhook_resolution.unresolved_paths),
        "skipped_paths": list(webhook_resolution.skipped_paths),
        "resolved_count": len(webhook_resolution.items),
        "total_paths": len(config.webhook_paths),
        "path_hints": list(webhook_resolution.path_hints),
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
        # Convert resolved Plex matches into ProcessableItems with canonical
        # paths already filled in. process_canonical_path then publishes via
        # every owning server's adapter (Plex BIF plus any Emby/Jellyfin
        # sibling that owns the same path).
        from ..processing.types import ProcessableItem as _PI
        from ..servers.base import ServerType as _ST

        plex_cfg_for_webhook = next((c for c in registry.configs() if c.type is _ST.PLEX), None)
        webhook_items: list[_PI] = []
        for key, locations, title, _media_type in webhook_resolution.items_with_locations:
            if not locations:
                continue
            remote_path = str(locations[0])
            canonicals: list[str] = []
            if plex_cfg_for_webhook is not None:
                canonicals = apply_path_mappings(remote_path, plex_cfg_for_webhook.path_mappings or [])
            if not canonicals:
                canonicals = [remote_path]
            canonical = canonicals[0]
            webhook_items.append(
                _PI(
                    canonical_path=canonical,
                    server_id=(plex_cfg_for_webhook.id if plex_cfg_for_webhook else ""),
                    item_id_by_server=({plex_cfg_for_webhook.id: key} if (plex_cfg_for_webhook and key) else {}),
                    title=title or canonical,
                    library_id=None,
                )
            )

        if not webhook_items:
            logger.info(
                "Webhook resolved {} item(s) but no canonical paths were derivable — skipping dispatch.",
                len(webhook_resolution.items),
            )
        else:
            result = dispatch_items(webhook_items, "Webhook Targets")
            totals["successful"] += result["completed"]
            totals["failed"] += result["failed"]
            totals["processed"] += result["completed"] + result["failed"]
            totals["cancelled"] = totals["cancelled"] or result["cancelled"]
            outcome = result.get("outcome") or {}
            for k, v in outcome.items():
                aggregate_outcome[k] = aggregate_outcome.get(k, 0) + v

    # K4 fallback for paths Plex couldn't claim.
    unresolved = list(webhook_resolution.unresolved_paths or [])
    if unresolved:
        try:
            from ..web.settings_manager import get_settings_manager

            raw = get_settings_manager().get("media_servers") or []
            has_non_plex = any(
                isinstance(e, dict) and (e.get("type") or "").lower() in ("emby", "jellyfin") and e.get("enabled", True)
                for e in raw
            )
        except Exception:
            raw = []
            has_non_plex = False
        pinned = getattr(config, "server_id_filter", None)
        pinned_is_non_plex = False
        pinned_entry = None
        if pinned and isinstance(pinned, str):
            try:
                pinned_entry = next((e for e in raw if isinstance(e, dict) and e.get("id") == pinned), None)
                pinned_is_non_plex = bool(
                    pinned_entry and (pinned_entry.get("type") or "").lower() in ("emby", "jellyfin")
                )
            except Exception:
                pinned_is_non_plex = False
        if has_non_plex and (not pinned or pinned_is_non_plex or pinned_entry):
            logger.info(
                "K4 fallback: {} path(s) unresolved by Plex — dispatching through multi-server registry "
                "for Emby/Jellyfin owners.",
                len(unresolved),
            )
            try:
                fallback_counts = _dispatch_webhook_paths_multi_server(
                    config,
                    progress_callback=progress_callback,
                    cancel_check=cancel_check,
                    pause_check=pause_check,
                    job_id=job_id,
                    paths=unresolved,
                )
                for k, v in (fallback_counts or {}).items():
                    aggregate_outcome[k] = aggregate_outcome.get(k, 0) + v
            except Exception:
                logger.warning(
                    "K4 multi-server fallback failed. The unresolved paths will go through the retry queue as usual.",
                    exc_info=True,
                )

    return return_payload


def _run_plex_full_scan_phase(
    config,
    registry,
    *,
    dispatch_items,
    progress_callback,
    cancel_check,
    totals: dict,
    aggregate_outcome: dict,
) -> bool:
    """Enumerate the full Plex library and dispatch every item.

    Mutates ``totals`` (keys: ``processed``, ``successful``, ``failed``,
    ``cancelled``) and ``aggregate_outcome`` in place. Returns ``True`` if
    enumeration completed (even if no items were found); ``False`` if the
    enumeration itself raised — the caller should treat that as a fatal
    job error.

    The dispatch goes through the same unified per-vendor processor →
    ProcessableItem → process_canonical_path path that Emby and Jellyfin
    use. The legacy tuple-shape pump is gone — keep this in mind when
    reading per-item logs (they'll mention the per-vendor adapter).
    """
    all_media_items: list = []
    try:
        for item in _enumerate_plex_full_scan_items(
            config,
            registry,
            cancel_check=cancel_check,
            progress_callback=progress_callback,
        ):
            if cancel_check and cancel_check():
                totals["cancelled"] = True
                break
            all_media_items.append(item)
    except Exception:
        logger.exception(
            "Plex full-scan enumeration failed. Verify Plex is reachable and the access token in Settings is valid."
        )
        return False

    if cancel_check and cancel_check():
        logger.info("Cancellation requested before dispatch — skipping processing")
        totals["cancelled"] = True
        return True

    if not all_media_items:
        logger.info("No media items found across selected libraries")
        return True

    # When sort_by is "random", shuffle the combined cross-library list so
    # parallel workers statistically pull from multiple physical disks at
    # once (big win on unraid shfs / mergerfs / JBOD setups).
    if config.sort_by == "random":
        random.Random().shuffle(all_media_items)
        logger.info("Shuffled {} items for random processing order", len(all_media_items))

    total_items = len(all_media_items)
    logger.info("Processing {} items across selected Plex libraries", total_items)

    result = dispatch_items(all_media_items, "All Libraries")
    totals["successful"] += result["completed"]
    totals["failed"] += result["failed"]
    totals["processed"] += result["completed"] + result["failed"]
    totals["cancelled"] = totals["cancelled"] or result["cancelled"]
    outcome = result.get("outcome") or {}
    for k, v in outcome.items():
        aggregate_outcome[k] = aggregate_outcome.get(k, 0) + v
    return True


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
        sid_filter = sid_filter if isinstance(sid_filter, str) and sid_filter else None
        _pinned_entry, pinned_type = _resolve_pinned_server(sid_filter)

        if _should_use_multi_server_full_scan(config, pinned_type):
            library_ids = list(getattr(config, "plex_library_ids", None) or [])
            outcome_counts = _run_full_scan_multi_server(
                config,
                selected_gpus=selected_gpus,
                server_id_filter=sid_filter,
                library_ids=library_ids or None,
                progress_callback=progress_callback,
                cancel_check=cancel_check,
                pause_check=pause_check,
                job_id=job_id,
                worker_callback=worker_callback,
            )
            return {"outcome": outcome_counts}

        # Webhook-paths jobs on a no-Plex install go through the multi-server
        # dispatcher (path → publishers, no Plex resolution step).
        if not (config.plex_url and config.plex_token):
            logger.info(
                "No Plex configured — webhook job ({} path(s)) will be dispatched directly to "
                "owning media servers via the multi-server registry.",
                len(config.webhook_paths),
            )
            outcome_counts = _dispatch_webhook_paths_multi_server(
                config,
                progress_callback=progress_callback,
                cancel_check=cancel_check,
                pause_check=pause_check,
                job_id=job_id,
            )
            return {"outcome": outcome_counts}
        if progress_callback:
            progress_callback(0, 0, "Connecting to Plex...")
        plex = plex_server(config)
        clear_failures()

        # Build a registry covering EVERY configured media server so the
        # dispatch path can fan out to all owning publishers (Plex + Emby +
        # Jellyfin). Previously this used from_legacy_config which only
        # produced a single-Plex registry — webhook + scheduled jobs then
        # silently dropped fan-out, publishing only to Plex even when the
        # canonical path was also owned by Emby/Jellyfin libraries. Falls
        # back to the legacy single-Plex shim only when the persisted
        # media_servers list is empty (fresh install / pre-migration).
        from ..servers.registry import ServerRegistry as _ServerRegistry
        from ..web.settings_manager import get_settings_manager as _get_sm

        try:
            _media_servers_raw = _get_sm().get("media_servers") or []
        except Exception:
            _media_servers_raw = []
        if _media_servers_raw:
            registry = _ServerRegistry.from_settings(_media_servers_raw, legacy_config=config)
        else:
            registry = _ServerRegistry.from_legacy_config(config)

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

        # Mutable accumulators threaded through the phase helpers. A dict
        # rather than several `nonlocal` ints because the phase helpers
        # are module-level functions, not closures.
        totals = {"processed": 0, "successful": 0, "failed": 0, "cancelled": False}
        aggregate_outcome = {r.value: 0 for r in ProcessingResult}

        # (Headless is the only mode this app runs in — the legacy CLI
        # console-display path was removed when the web UI became the only
        # interface. The "headless mode" wording in worker.process_items_headless
        # remains as a load-bearing API name.)

        _dispatch_started = False

        def _dispatch_items(items, library_name):
            """Dispatch items via shared dispatcher or local pool."""
            nonlocal worker_pool, _dispatch_started
            if job_id:
                from .dispatcher import get_dispatcher

                existing = get_dispatcher()
                if existing is not None:
                    worker_pool = existing.worker_pool
                    # D33 — Surface the reuse so the per-job log doesn't
                    # silently start dispatching with no worker context.
                    # Without this, the absence of "Initialized N workers"
                    # on a reused pool looked like the job was running
                    # without any workers — confusing when comparing
                    # back-to-back job logs.
                    try:
                        worker_count = len(worker_pool._snapshot_workers())
                    except Exception:
                        worker_count = 0
                    logger.info(
                        "Reusing existing worker pool ({} worker(s)) — no fresh init needed",
                        worker_count,
                    )
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
                    registry=registry,
                    title_max_width=title_max_width,
                    library_name=library_name,
                    callbacks=callbacks,
                    priority=priority if priority is not None else PRIORITY_NORMAL,
                )
                tracker.wait()
                # D12 — Dispatcher._merge_worker_outcome maintains a
                # per-server publisher aggregate on the tracker and
                # mirrors it onto the Job (set_publishers) every task.
                # Per-file × per-server detail lives in the Files panel
                # JSONL via record_file_result; nothing to drain here.
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
                    registry,
                    title_max_width,
                    library_name=library_name,
                    progress_callback=progress_callback,
                    worker_callback=worker_callback,
                    on_item_complete=item_complete_callback,
                    cancel_check=cancel_check,
                    pause_check=pause_check,
                )

        if getattr(config, "webhook_paths", None):
            webhook_resolution_payload = _run_webhook_paths_phase(
                config,
                plex,
                registry,
                dispatch_items=_dispatch_items,
                progress_callback=progress_callback,
                cancel_check=cancel_check,
                pause_check=pause_check,
                job_id=job_id,
                totals=totals,
                aggregate_outcome=aggregate_outcome,
            )
            return_data = {"webhook_resolution": webhook_resolution_payload}
        else:
            ok = _run_plex_full_scan_phase(
                config,
                registry,
                dispatch_items=_dispatch_items,
                progress_callback=progress_callback,
                cancel_check=cancel_check,
                totals=totals,
                aggregate_outcome=aggregate_outcome,
            )
            if not ok:
                return {"outcome": aggregate_outcome}

        summary = _format_outcome_summary(aggregate_outcome)
        if totals["cancelled"]:
            logger.info("Processing stopped by cancellation: {}", summary)
        else:
            logger.info("Processing complete: {}", summary)

        _maybe_log_path_mapping_misconfig(aggregate_outcome, totals["processed"])

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
    except Exception:
        logger.exception(
            "Unexpected error during the preview-generation job — aborting this job. "
            "This is likely a bug. The web UI and other jobs keep running. "
            "The full traceback is included above; please report it at "
            "https://github.com/stevezau/media_preview_generator/issues."
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
