"""Core processing workflow for video preview generation.

Contains run_processing() which orchestrates Plex library scanning,
media item dispatch, and worker pool management.  Used exclusively
by the web layer (job_runner.py).
"""

import os
import random
import shutil

from loguru import logger

from ..plex_client import get_media_items_by_paths, plex_server
from ..processing.generator import ProcessingResult, clear_failures, log_failure_summary
from ..servers.ownership import apply_path_mappings
from .worker import WorkerPool


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

        name_by_id = {cfg.id: (cfg.name or cfg.id) for cfg in configs}
        owners_by_server: dict[str, int] = {}
        unowned = 0
        for path in paths:
            matches = find_owning_servers(path, configs)
            if not matches:
                unowned += 1
                continue
            for match in matches:
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
    job_id: str | None = None,
    paths: list[str] | None = None,
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
    job_id: str | None = None,
    label: str = "scan",
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
    from concurrent.futures import ThreadPoolExecutor

    from ..processing.multi_server import process_canonical_path
    from ..servers.base import ServerType

    counts = {r.value: 0 for r in ProcessingResult}
    total = len(items)
    if total == 0:
        return counts

    gpu_devices = list(selected_gpus or [])
    cpu_workers = max(0, int(getattr(config, "cpu_threads", 1) or 0))
    gpu_workers = sum(int(getattr(g[2], "workers", 1) or 1) for g in gpu_devices) if gpu_devices else 0
    parallelism = max(1, gpu_workers + cpu_workers)

    job_manager = None
    if job_id:
        try:
            from ..web.jobs import get_job_manager

            job_manager = get_job_manager()
        except Exception:
            job_manager = None

    logger.info(
        "Multi-server {}: dispatching {} item(s) with parallelism={}",
        label,
        total,
        parallelism,
    )

    def _gpu_for(index: int):
        if not gpu_devices:
            return None, None
        gpu_type, gpu_device, _ = gpu_devices[index % len(gpu_devices)]
        return gpu_type, gpu_device

    def _process_one(index_and_item):
        index, (server_cfg, item) = index_and_item
        if cancel_check and cancel_check():
            return None
        gpu_type, gpu_device = _gpu_for(index)
        try:
            return process_canonical_path(
                canonical_path=item.canonical_path,
                registry=registry,
                config=config,
                item_id_by_server=item.item_id_by_server or None,
                gpu=gpu_type,
                gpu_device_path=gpu_device,
                cancel_check=cancel_check,
                # Scope publishing to the originating server only on
                # non-Plex installs — Plex scans should still fan out
                # to every owning sibling so multi-vendor publishers
                # benefit.
                server_id_filter=(server_cfg.id if server_cfg.type is not ServerType.PLEX else None),
            )
        except Exception as exc:
            logger.warning(
                "Multi-server {}: per-item processing failed for {!r} ({}: {}). "
                "Other items in this run will still be processed.",
                label,
                item.canonical_path,
                type(exc).__name__,
                exc,
            )
            return None

    completed = 0
    with ThreadPoolExecutor(max_workers=parallelism) as pool:
        for result in pool.map(_process_one, enumerate(items)):
            completed += 1
            if progress_callback:
                try:
                    progress_callback(completed, total, f"Processed {completed}/{total}")
                except Exception:
                    pass
            if result is None:
                # _process_one swallowed an exception (FFmpeg crash, codec
                # not supported, etc.). Count it as a failed item so the
                # outcome counter — and the Job UI badge — surface it
                # instead of silently reporting "Completed".
                counts["failed"] = counts.get("failed", 0) + 1
                continue
            for pub in result.publishers or []:
                key = (pub.status.value if hasattr(pub.status, "value") else str(pub.status)).lower()
                counts[key] = counts.get(key, 0) + 1
            if job_manager is not None:
                try:
                    job_manager.append_publishers(
                        job_id,
                        _publisher_rows_from_result(result, result.canonical_path),
                    )
                except Exception as exc:
                    logger.debug("Could not append publisher rows for job {}: {}", job_id, exc)

    logger.info("Multi-server {} complete: {} item(s) processed.", label, completed)
    return counts


def _enumerate_items_for_servers(
    candidates,
    *,
    enumerate_one,
    cancel_check=None,
    label: str,
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
    job_id: str | None = None,
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
        job_id=job_id,
        label="full scan",
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
    job_id: str | None = None,
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
        job_id=job_id,
        label="recently-added scan",
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
                job_id=job_id,
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
                job_id=job_id,
            )
            return {"outcome": outcome_counts}
        if progress_callback:
            progress_callback(0, 0, "Connecting to Plex...")
        plex = plex_server(config)
        clear_failures()

        # Build a registry from the legacy Config so the dispatch path can
        # publish via process_canonical_path's per-vendor adapters when items
        # arrive as ProcessableItems (the post-Phase-C unified path).
        from ..servers.registry import ServerRegistry as _ServerRegistry

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

        not_found = aggregate_outcome.get("skipped_file_not_found", 0)
        if totals["processed"] > 0 and not_found > 0 and aggregate_outcome.get("generated", 0) == 0:
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
