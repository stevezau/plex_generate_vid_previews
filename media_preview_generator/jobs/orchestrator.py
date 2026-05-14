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

from ..processing.generator import ProcessingResult, clear_failures, log_failure_summary
from ..servers.ownership import apply_webhook_prefixes, find_owning_servers
from .worker import WorkerPool

# Max cadence for worker-snapshot SocketIO emits during a multi-server
# dispatch. See the long comment in ``_dispatch_processable_items`` —
# 1 Hz matches the legacy ``WorkerPool`` (``worker.py:1245``). Exposed
# as a module-level constant so tests that observe transient
# "processing" snapshots can ``monkeypatch.setattr`` it down to a few
# milliseconds without exercising the production cadence.
_WORKER_EMIT_THROTTLE_S = 1.0


def _resolve_webhook_path_to_canonical(path: str, server_configs: list) -> tuple[str, list]:
    """Resolve a webhook-source path to a canonical server-view path + its owners.

    Sonarr/Radarr emit paths in their own view (e.g. ``/data/TV Shows/X.mkv``)
    which won't match a server's library ``remote_paths`` (e.g.
    ``/data_16tb/TV Shows``) until translated through the server's
    ``path_mappings`` ``webhook_prefixes`` list. Calling
    :func:`find_owning_servers` with the raw webhook path silently
    misses every install where the webhook source and the media-
    server use different mount roots — including the downstream
    ownership check inside ``process_canonical_path``, which uses the
    canonical path stored on the :class:`ProcessableItem` directly.

    Returns ``(canonical_path, matches)``:

    * ``canonical_path`` — the path form to store on the
      ``ProcessableItem``. Picked from the candidates by preferring,
      in order: (1) a candidate that exists on disk via
      :func:`os.path.exists` so frame extraction can read the source,
      (2) any candidate that owners agree on, (3) the raw input.
    * ``matches`` — the **aggregated** deduplicated list of
      :class:`~servers.ownership.OwnershipMatch` across EVERY
      candidate. This is the audit-P2 fix: the previous version
      returned at the first matching candidate, silently dropping
      owners whose libraries matched a different candidate. On a
      heterogeneous-mount install (Plex on ``/data_16tb``, Emby on
      ``/em-media``, both with ``webhook_prefixes=['/data']``), the
      first match would return Plex only and Emby would never publish.
      Now both servers' owners are returned.

    Tries the raw path first (the dominant case for installs without
    webhook_prefixes mappings), then every translated candidate.
    """
    if not path or not server_configs:
        return path, []

    candidate_paths: list[str] = [path]
    seen_candidates: set[str] = {path}
    for cfg in server_configs:
        for translated in apply_webhook_prefixes(path, cfg.path_mappings or []):
            if translated not in seen_candidates:
                seen_candidates.add(translated)
                candidate_paths.append(translated)

    # Aggregate owners across ALL candidates that match. Track which
    # candidate each owner came from so we can pick a canonical path
    # form that at least one owner agrees on.
    seen_servers: set[str] = set()
    aggregated: list = []
    matching_candidates: list[str] = []
    for candidate in candidate_paths:
        owners = find_owning_servers(candidate, server_configs)
        if not owners:
            continue
        if candidate not in matching_candidates:
            matching_candidates.append(candidate)
        for match in owners:
            if match.server_id in seen_servers:
                continue
            seen_servers.add(match.server_id)
            aggregated.append(match)

    if not aggregated:
        return path, []

    # Canonical-path picker:
    #   1. If a matching candidate exists on disk, pick that. Frame
    #      extraction reads from this path, so on multi-disk installs
    #      (file lives on /data_16tb2 but /data_16tb is the first
    #      mapping) we MUST pick the disk that actually has the file
    #      or FFmpeg fails with "no such file or directory".
    #   2. Otherwise pick the first candidate any owner matched. Beats
    #      the raw input because the raw is usually the source-side
    #      view (Sonarr's /data/...) which the publishers' downstream
    #      ownership check (registry.find_owning_servers) doesn't
    #      translate.
    canonical: str | None = None
    for cand in matching_candidates:
        try:
            if os.path.exists(cand):
                canonical = cand
                break
        except OSError:
            # Defensive: a malformed path (super long, weird chars)
            # could raise on some filesystems. Skip it.
            continue
    if canonical is None:
        canonical = matching_candidates[0]
    return canonical, aggregated


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


# Per-(server, path) precedence for picking the "most informative"
# publisher status across all attempts in a retry chain. Lower rank wins.
#
# Background — the chain head's publishers_json was originally written
# using the LATEST status per path on the assumption that a retry's
# outcome always supersedes the head's. That assumption is correct for
# Jellyfin's ``pending_registration → skipped_output_exists`` upgrade
# (bridge plugin registered the row — the file is now fully indexed,
# adapter gate at ``multi_server.py::_publish_all`` line ~1411
# distinguishes ``needs_registration``-still-true from done), but it
# silently overwrote Plex/Emby's ``published`` with the retry's no-op
# ``skipped_output_exists`` for every chain that hit a retry. The
# user-visible bug: jobs that freshly generated previews showed
# "already existed" everywhere in the modal's Servers strip.
#
# Cases preserved by the table:
#   * Plex/Emby ``published → skipped_output_exists`` keeps
#     ``published``. The retry's "BIF exists" observation is a no-op.
#   * Jellyfin ``published_pending_registration → skipped_output_exists``
#     keeps ``skipped_output_exists``. The adapter only emits
#     SKIPPED_OUTPUT_EXISTS for a JF retry once ``item_id`` resolves
#     (i.e., the row IS registered) — so the upgrade is structurally
#     guarded at the source, not just at the merge.
#   * ``failed → published`` (retry recovered) keeps ``published``.
#
# One case the table intentionally hides (with rationale):
#   * ``published → failed`` would keep ``published``, hiding a late
#     failure. This is structurally impossible in the current retry
#     pipeline: retries fire ONLY when at least one publisher returned
#     a PENDING status (see ``retry_queue.PENDING_PUBLISHER_STATUSES``).
#     A ``published`` server's row is never re-evaluated by the retry
#     in a way that could downgrade it to FAILED — at worst the retry
#     re-publishes (status=published again) or sees the BIF on disk
#     (status=skipped_output_exists). If a future refactor lets a retry
#     re-emit FAILED for an already-published server, demote ``published``
#     below ``failed`` in this table. The matching regression test is
#     ``test_published_then_failed_keeps_published_documents_assumption``.
_PUBLISHER_STATUS_PRECEDENCE: dict[str, int] = {
    "published": 0,
    "skipped_output_exists": 1,
    "published_pending_registration": 2,
    "skipped_not_indexed": 3,
    "skipped_not_in_library": 4,
    "failed": 5,
}


def _best_publisher_status(statuses: list[str]) -> str:
    """Return the most informative status from a list of attempts.

    Unknown statuses sort to the end (rank 99) so they only win if no
    known status was observed. The list is expected to be non-empty;
    callers should skip empty lists before calling.
    """
    return min(statuses, key=lambda s: _PUBLISHER_STATUS_PRECEDENCE.get(s, 99))


def merge_chain_publishers_best_per_path(file_results: list[dict]) -> list[dict]:
    """Aggregate per-(server, path) publisher outcomes across a chain's attempts.

    ``file_results`` is the JSONL of dispatches recorded by ``_file_result_cb``
    for the chain's head and every retry child — typically obtained from
    ``JobManager.get_file_results(chain_head_id, dedup_by_path=False)``.

    Returns the publisher rows in the same shape as
    ``fold_publisher_rows_into_aggregate`` produces (id/name/type/counts),
    but with one row per (server, path) folded using the
    ``_PUBLISHER_STATUS_PRECEDENCE`` "best wins" rule rather than the
    original "latest dedup wins". See the module-level constant docstring
    for the bug this fixes.
    """
    # (server_id, path) → list of statuses, in observation order
    per_server_path: dict[tuple[str, str], list[str]] = {}
    # server_id → metadata for the result rows (name / type)
    server_meta: dict[str, dict] = {}

    for fr in file_results:
        if not isinstance(fr, dict):
            continue
        path = fr.get("file") or fr.get("canonical_path") or fr.get("path") or ""
        for s in fr.get("servers") or []:
            if not isinstance(s, dict):
                continue
            sid = s.get("id") or s.get("server_id") or ""
            if not sid:
                continue
            status = s.get("status") or ""
            if not status:
                continue
            per_server_path.setdefault((sid, path), []).append(status)
            # Late-arriving name/type wins over an empty one (matches
            # fold_publisher_rows_into_aggregate's protection against
            # the first row carrying only an id).
            meta = server_meta.setdefault(sid, {"server_name": "", "server_type": ""})
            if not meta["server_name"]:
                meta["server_name"] = s.get("name") or s.get("server_name") or ""
            if not meta["server_type"]:
                meta["server_type"] = (s.get("type") or s.get("server_type") or "").lower()

    aggregate: dict[str, dict] = {}
    for (sid, _path), statuses in per_server_path.items():
        best = _best_publisher_status(statuses)
        entry = aggregate.setdefault(
            sid,
            {
                "server_id": sid,
                "server_name": server_meta.get(sid, {}).get("server_name", ""),
                "server_type": server_meta.get(sid, {}).get("server_type", ""),
                "counts": {},
            },
        )
        entry["counts"][best] = entry["counts"].get(best, 0) + 1

    return list(aggregate.values())


def fold_publisher_rows_into_aggregate(aggregate: dict[str, dict], rows: list[dict]) -> None:
    """Fold per-task publisher rows into a per-server count aggregate.

    ``aggregate`` is keyed by ``server_id`` and shaped::

        {server_id: {"server_id": ..., "server_name": ...,
                     "server_type": ..., "counts": {status: count}}}

    Mutates ``aggregate`` in place. Both job-dispatch paths (legacy
    WorkerPool dispatcher and the multi-server full-scan / webhook
    ThreadPoolExecutor) feed this so they cannot drift again — commit
    1ecf099 ("aggregate per-server, not per-file") patched only the
    dispatcher path. ``_dispatch_processable_items`` was missed and
    kept ``append_publishers``-ing one row per (file × server), which
    on a 117k-item full library scan turned into an O(N²) SQLite write
    storm (publishers_json grew to 11.8 MB and was re-encoded + UPSERTed
    after every item, dropping throughput from ~30 items/sec early on
    to <8 items/sec by minute 28).
    """
    for row in rows:
        server_id = row.get("server_id") or ""
        if not server_id:
            continue
        entry = aggregate.get(server_id)
        if entry is None:
            entry = {
                "server_id": server_id,
                "server_name": row.get("server_name") or "",
                "server_type": (row.get("server_type") or "").lower(),
                "counts": {},
            }
            aggregate[server_id] = entry
        else:
            # Late-arriving name/type wins over an empty one — protects
            # against the first row for a server having only the id
            # (e.g. before settings_manager has loaded the display name).
            if not entry.get("server_name") and row.get("server_name"):
                entry["server_name"] = row["server_name"]
            if not entry.get("server_type") and row.get("server_type"):
                entry["server_type"] = row["server_type"].lower()
        status = row.get("status") or "unknown"
        entry["counts"][status] = entry["counts"].get(status, 0) + 1


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
            _canonical, uniq_matches = _resolve_webhook_path_to_canonical(path, configs)
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
    # Mirror the Emby/Jellyfin multi-server enumerator's "Querying…"
    # banner (see ``_enumerate_items_for_servers``). A full Plex library
    # enumeration on a large TV library can take 30–120s before the
    # first item is yielded via ``list_canonical_paths``; without this
    # the progress bar sits at "0/0" with no message and the job looks
    # frozen — live user report on job 90301a18. Log at INFO so it
    # lands in the per-job log file too (the UI's Job Detail tab reads
    # from that file, not the progress_callback stream).
    _label = plex_cfg.name or plex_cfg.id or plex_cfg.type.value
    logger.info("Querying {} library… (can take 30–120s for large libraries)", _label)
    if progress_callback is not None:
        try:
            progress_callback(0, 0, f"Querying {_label} library…")
        except Exception as exc:
            logger.debug("progress_callback raised during Plex enumeration banner: {}", exc)
    yield from plex_processor.list_canonical_paths(
        plex_cfg,
        library_ids=library_ids,
        cancel_check=cancel_check,
        progress_callback=progress_callback,
    )


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
    on_dispatch_start=None,
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
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from ..processing.generator import (
        CancellationError,
        CodecNotSupportedError,
        _notify_file_result,
        failure_scope,
    )
    from ..processing.multi_server import MultiServerStatus, process_canonical_path
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

    # The dashboard renderer (``app.js:2393-2438``) gates the
    # progress bar / speed / ETA rendering on ``ffmpeg_started``:
    # when ``False`` it hides those elements and shows a "Working…"
    # placeholder (or the ``current_phase`` text when present).
    # Without these fields on the multi-server slot dict,
    # ``WorkerStatus`` falls back to ``False`` / ``""`` and the panel
    # renders "Working…" for the entire run even when speed/ETA are
    # flowing through the API — the symptom job 2395774d reproduced
    # live.
    #
    # ``ffmpeg_started`` mirrors the legacy ``WorkerPool`` field
    # (``worker.py:128`` / flipped in ``worker.py:1624``) and is
    # flipped True by ``_slot_progress_callback`` below on the first
    # progress tick, reset False in ``_release_slot``.
    #
    # ``current_phase`` is a forward-compat placeholder — no callsite
    # writes it yet. The legacy worker populates it from log-line
    # parsing (``worker.py:397``) for nicer pre-FFmpeg labels like
    # "Resolving item id on EmbyTest…" / "Reusing cached frames";
    # threading those through ``process_canonical_path`` is a separate
    # piece of work. Empty string degrades gracefully to "Working…".
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
            "ffmpeg_started": False,
            "current_phase": "",
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
            "ffmpeg_started": False,
            "current_phase": "",
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
    # Fire the "dispatch has really started" hook so the job flips from
    # PENDING to RUNNING. job_runner's _on_dispatch_start calls
    # job_manager.start_job(); without this the multi-server full-scan
    # path leaves the job stuck in PENDING even as items complete.
    # Matches the equivalent call in ``_dispatch_items`` used by the
    # webhook / legacy-plex-phase code paths.
    if on_dispatch_start:
        try:
            on_dispatch_start()
        except Exception as exc:
            logger.debug("on_dispatch_start raised: {}", exc)
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
            # Reset the FFmpeg-phase flags so the NEXT item this slot
            # picks up starts in the pre-FFmpeg "Working…" state until
            # its own progress callback flips them back. Without this
            # reset, a slot that just finished a real FFmpeg pass keeps
            # ``ffmpeg_started=True`` and the UI shows the previous
            # item's stale speed/ETA on the new (pre-FFmpeg) item.
            slot["ffmpeg_started"] = False
            slot["current_phase"] = ""
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

    # ── Worker-snapshot emit throttle ─────────────────────────────────
    # Cap snapshot emits at ~1 Hz. Pre-fix every slot claim + release
    # called ``_emit_worker_snapshot`` immediately, so a full-library
    # scan whose first phase is mostly "already fresh — skip FFmpeg"
    # no-op items (each completing in ~50 ms with 4 workers → ~80 items/sec
    # → ~160 transitions/sec) flooded the SocketIO connection. Each
    # emit also spawned a fresh thread in ``JobManager._emit_event``,
    # so the dashboard's browser-side event loop couldn't keep up and
    # the Workers panel never settled long enough to render — job
    # 8cd02fa6 reproducer.
    #
    # Same pattern the legacy ``WorkerPool`` already uses at
    # ``worker.py:1245`` — "skip if <1 s since the last emit." The
    # poller heartbeat fires every 1.5 s (> throttle) so the panel
    # never stalls for longer than the heartbeat window even on a
    # sustained flood. ``force=True`` bypasses the throttle for the
    # three events the user must see immediately:
    #   * initial dispatch banner — show the configured slots as idle
    #   * final dispatch end — capture the all-idle terminal state
    #   * poller-driven slot-count changes — rows added/removed
    # Forced emits do NOT consume the throttle budget so a genuine
    # transition immediately afterwards still produces its own emit,
    # and the panel shows the very first claim even on sub-second
    # dispatches.
    throttle_s = _WORKER_EMIT_THROTTLE_S
    _emit_state: dict = {"last_non_force_monotonic": 0.0}
    _emit_state_lock = threading.Lock()

    def _emit_worker_snapshot(force: bool = False) -> None:
        if not worker_callback:
            return
        with _emit_state_lock:
            if not force:
                now = time.monotonic()
                if now - _emit_state["last_non_force_monotonic"] < throttle_s:
                    return
                _emit_state["last_non_force_monotonic"] = now
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
            # Slot-count changed — bypass the 1 Hz throttle so the
            # panel reflects the new row count immediately.
            _emit_worker_snapshot(force=True)

    def _poller_loop() -> None:
        while not _poller_stop.wait(1.5):
            _reconcile_with_settings()
            # Periodic snapshot tick so the Workers panel reflects
            # in-flight FFmpeg progress (progress_percent / speed) within
            # ~1.5s. Without this, the snapshot only fires at task
            # start / end and during settings reconciliation, leaving
            # the panel frozen at "0% @ 0.0x" through 30s+ FFmpeg
            # passes for multi-server full scans. Throttled at 1 Hz —
            # 1.5 s ≥ throttle so the heartbeat always passes through.
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
                # First progress tick means FFmpeg is actually running
                # and producing measurable output — flip the UI out of
                # its pre-FFmpeg "Working…" branch so the user sees the
                # real progress bar + speed + ETA. Mirrors the legacy
                # ``WorkerPool._update_worker_progress`` (``worker.py:1624``).
                # Reset on slot release (see ``_release_slot``) so a
                # cache-hit / skipped-FFmpeg item that follows starts
                # back in the "Working…" branch instead of inheriting
                # the previous item's progress.
                slot["ffmpeg_started"] = True
            # Don't emit a snapshot per progress tick — that'd drown
            # the SocketIO emit queue at 5+ updates/sec/worker. Slot
            # state is mutated in place; the panel picks up the
            # latest speed/remaining_time on the next snapshot. All
            # ``_emit_worker_snapshot`` calls (claim, release, poller
            # heartbeat) share the 1 Hz throttle defined inside this
            # function — see the long comment near ``_emit_worker_snapshot``.

        # Bind the worker thread's failure-tracking scope to this
        # job so any FFmpeg failure inside ``process_canonical_path``
        # → ``generate_images`` → ``record_failure`` is attributed
        # to the right job's run summary. Without this scope every
        # FFmpeg failure on the multi-server full-scan path was
        # logged as "Internal bookkeeping bug: failure ... reported
        # outside an active job" — the codebase's self-flagged
        # diagnostic. The Worker dispatch path (worker.py:_process_item)
        # has the equivalent ``with failure_scope(self.current_job_id)``;
        # this scan path was missing it.
        with failure_scope(job_id):
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
            except CodecNotSupportedError as exc:
                # In-place GPU→CPU fallback. Pre-fix the bare except below
                # swallowed CodecNotSupportedError, so the user-visible
                # "retrying on CPU automatically" log line from
                # generator.py / multi_server.py was a lie on full-scan
                # jobs — no CPU retry ever ran. Worker-pool path has the
                # equivalent fallback at jobs/worker.py:557-613; this
                # mirrors it for the orchestrator's executor path.
                # Live regression: 4 Re:ZERO episodes in job a90c9b87
                # (TV Shows full scan, 2026-05-14) hit this and were
                # marked failed despite the announced fallback.
                if cancel_check and cancel_check():
                    logger.info(
                        "Multi-server {}: cancelled before CPU fallback for {!r}",
                        label,
                        item.canonical_path,
                    )
                    return (worker_label, None)
                logger.warning(
                    "Multi-server {}: GPU couldn't process {!r} ({}); retrying on CPU. "
                    "If this happens for many files, your GPU may not support the codec.",
                    label,
                    item.canonical_path,
                    exc,
                )
                try:
                    result = process_canonical_path(
                        canonical_path=item.canonical_path,
                        registry=registry,
                        config=config,
                        item_id_by_server=item.item_id_by_server or None,
                        bundle_metadata_by_server=item.bundle_metadata_by_server or None,
                        gpu=None,
                        gpu_device_path=None,
                        progress_callback=_slot_progress_callback,
                        cancel_check=cancel_check,
                        server_id_filter=per_item_pin,
                        regenerate=bool(getattr(config, "regenerate_thumbnails", False)),
                    )
                    logger.info(
                        "Multi-server {}: completed CPU fallback for {!r}",
                        label,
                        item.canonical_path,
                    )
                    return (worker_label, result)
                except CancellationError:
                    # Distinct branch (mirrors jobs/worker.py:598-602) so the
                    # user-visible log shows "cancelled during fallback" rather
                    # than getting collapsed into the generic "CPU fallback also
                    # failed (CancellationError: …)" message.
                    logger.info(
                        "Multi-server {}: cancelled during CPU fallback for {!r}",
                        label,
                        item.canonical_path,
                    )
                    return (worker_label, None)
                except Exception as fallback_exc:
                    logger.warning(
                        "Multi-server {}: CPU fallback also failed for {!r} ({}: {}). "
                        "Marking this file as failed; other items keep processing.",
                        label,
                        item.canonical_path,
                        type(fallback_exc).__name__,
                        fallback_exc,
                    )
                    return (worker_label, None)
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
                # Release the slot (auto-removes if poller flagged it)
                # and the concurrency permit. Order matters: free the
                # slot first so the snapshot reflects "idle" before
                # another thread grabs it.
                _release_slot(slot)
                _emit_worker_snapshot()
                _release_concurrency()

    completed = 0
    # Per-server publisher tally for this dispatch. Folded after every
    # completed item and mirrored onto the Job via the fixed-size
    # ``set_publishers`` call (one entry per server). Safe to mutate
    # without a lock because ``as_completed`` yields finished futures
    # back to **a single consumer thread** (the for-loop below) — the
    # workers themselves never touch this dict. If this loop ever
    # multiplexes consumption across threads, wrap the fold +
    # set_publishers call in a ``threading.Lock``.
    # Replaces the original
    # per-item ``append_publishers`` (one row per file × server) which
    # made publishers_json grow O(items × servers) — a 117k-item full
    # library scan re-encoded and SQLite-UPSERTed an 11.8 MB blob after
    # every item, throttling sustained throughput to <10 items/sec on a
    # workload that's almost entirely "all fresh" stat() checks. The
    # legacy dispatcher path was already moved to this shape in commit
    # 1ecf099; this catches the full-scan path up.
    publishers_aggregate: dict[str, dict] = {}
    # Index inputs alongside their outcomes so the per-item record can
    # surface the canonical path even when ``result is None`` (the
    # exception-swallowed branch). Without this the Files panel showed
    # only the surviving rows; the failures were just a number on the
    # summary chip with no per-file attribution.
    indexed_items = list(enumerate(items))
    # Force the initial worker snapshot so the dashboard's Workers
    # panel shows the configured slots as "idle" the moment the
    # dispatch starts, instead of staying blank until the first
    # item-claim transition (which the 1 Hz throttle can defer for
    # up to a second after the panel first asks). Bypasses the
    # throttle because there's nothing to coalesce yet.
    _emit_worker_snapshot(force=True)
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
    # Why submit() + as_completed() instead of ``pool.map``:
    # ``ThreadPoolExecutor.map`` returns results in **submission order**,
    # so a slow item near the front of the queue stalls progress for
    # every faster item behind it — workers are completing in parallel
    # but the consumer is blocked at the head, so the
    # ``progress_callback`` (and its SocketIO emit) never fires until
    # the slow item completes. Job 9eb79d9c surfaced this on a Jellyfin
    # full-library scan: numerator stuck at ~5/5000 for minutes, then
    # jumped to ~700/5000 the instant the user cancelled. The
    # ``as_completed`` iterator yields each future the moment its
    # worker is done, so progress reflects real completion order.
    with pool_ctx as pool:
        future_to_input: dict = {
            pool.submit(_process_one, indexed_item): indexed_item for indexed_item in indexed_items
        }
        for future in as_completed(future_to_input):
            idx, (_server_cfg, item) = future_to_input[future]
            try:
                worker_label, result = future.result()
            except Exception as exc:
                # ``_process_one`` already wraps its own work in try/except
                # and returns ``(worker_label, None)`` on failure, so
                # this branch is only reached if _process_one itself
                # raised before its try-block (or the pool surfaced an
                # InterruptedError). Treat it the same as the
                # exception-swallowed path: log, count as failure, no
                # per-file row (canonical_path is still known).
                logger.warning(
                    "Multi-server {}: future.result() raised for {!r} ({}: {}). Treating as a failed item.",
                    label,
                    item.canonical_path,
                    type(exc).__name__,
                    exc,
                )
                worker_label, result = "Worker", None
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
            # Empty-publisher aggregate statuses (SKIPPED_FILE_NOT_FOUND,
            # NO_OWNERS, NO_FRAMES, MultiServerStatus.FAILED when frame
            # generation raised before any publisher ran) still represent
            # a processed item. Without this fold, the item is counted in
            # ``completed`` but disappears from the outcome totals — which
            # is how the deea99db job reported "128007 processed" but
            # only "128002 in outcome". Mirrors _outcome_for_multi_server_status
            # so the counter key and the Files-panel outcome string agree.
            if not (result.publishers or []):
                aggregate_status = getattr(result, "status", None)
                if aggregate_status is MultiServerStatus.SKIPPED_FILE_NOT_FOUND:
                    counts["skipped_file_not_found"] = counts.get("skipped_file_not_found", 0) + 1
                elif aggregate_status is MultiServerStatus.NO_OWNERS:
                    counts["no_owners"] = counts.get("no_owners", 0) + 1
                elif aggregate_status is MultiServerStatus.NO_FRAMES:
                    # NO_FRAMES already invoked ``record_failure`` inside
                    # ``generate_images`` — rolling it into ``failed`` here
                    # keeps the end-of-run "X failed file(s)" list and the
                    # "N of T item(s) failed" summary aligned. Without this
                    # they diverge (deea99db: 1-in-list vs 2-in-summary).
                    counts["failed"] = counts.get("failed", 0) + 1
                elif aggregate_status is MultiServerStatus.FAILED:
                    counts["failed"] = counts.get("failed", 0) + 1
            rows = _publisher_rows_from_result(result, result.canonical_path)
            if job_manager is not None:
                # Fold this item's per-server outcomes into the running
                # aggregate, then mirror the bounded summary onto the
                # Job. Per-file × per-server detail still lands in the
                # Files-panel JSONL via ``_notify_file_result`` below —
                # that's the right home for it (append-only, capped
                # caller-side; see web/jobs.record_file_result).
                fold_publisher_rows_into_aggregate(publishers_aggregate, rows)
                try:
                    job_manager.set_publishers(job_id, list(publishers_aggregate.values()))
                except Exception as exc:
                    logger.debug("Could not set publisher aggregate for job {}: {}", job_id, exc)
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
    # Force one final emit so the panel sees the terminal all-idle
    # state. Without it, a last-item-release transition within the
    # throttle window leaves the panel showing a stale "processing"
    # row until the next job clears it.
    _emit_worker_snapshot(force=True)
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

    Returns ``(all_items, enumeration_errors)``:
    * ``all_items`` — list of ``(server_config, ProcessableItem)`` ready
      to feed into :func:`_dispatch_processable_items`.
    * ``enumeration_errors`` — list of ``(server_label, "ExcName: msg")``
      tuples, one per server whose enumeration raised. Callers use
      this to distinguish "library was empty" (zero items, no errors —
      green badge fine) from "library couldn't be reached" (zero items
      AND error logged — should surface as a job-level warning so the
      user sees the amber badge instead of a misleading green check).
      Job b6deeac3 was the originating regression: Jellyfin's /Items
      timed out, the library was skipped, and the job reported
      "completed" with no indication anything went wrong.

    De-duping across servers (Phase P4) lives in this helper so it
    applies uniformly to full-scan AND recently-added flows.
    """
    from ..processing import get_processor_for

    all_items: list = []
    enumeration_errors: list[tuple[str, str]] = []
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
            enumeration_errors.append((server_cfg.name or server_cfg.id or server_cfg.type.value, f"KeyError: {exc}"))
            continue
        if cancel_check and cancel_check():
            logger.info("Cancellation requested while enumerating items — aborting {}.", label)
            return all_items, enumeration_errors
        # Surface a "Querying…" status BEFORE the per-server walk so the UI
        # progress bar shows the system is alive during the slow library
        # enumeration phase (Emby/Jellyfin TV libraries can take 10–60s
        # before the first item is yielded). Without this, the job sits at
        # "0/0" with no message and users assume it's stuck. Log at INFO
        # so it also lands in the per-job log file (the progress_callback
        # stream is in-memory only — the Job Detail log tab reads the
        # file that the loguru log_sink writes to).
        _server_label = server_cfg.name or server_cfg.id or server_cfg.type.value
        logger.info("Querying {} library… (can take 10–60s for large libraries)", _server_label)
        if progress_callback:
            try:
                progress_callback(0, 0, f"Querying {_server_label} library…")
            except Exception as exc:
                logger.debug("progress_callback raised during enumeration banner: {}", exc)
        try:
            for item in enumerate_one(processor, server_cfg):
                if cancel_check and cancel_check():
                    logger.info("Cancellation requested mid-enumeration — aborting {}.", label)
                    return all_items, enumeration_errors

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
            enumeration_errors.append(
                (
                    server_cfg.name or server_cfg.id or server_cfg.type.value,
                    f"{type(exc).__name__}: {exc}",
                )
            )

    return all_items, enumeration_errors


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
    on_dispatch_start=None,
    warnings_out: list[str] | None = None,
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

    ``warnings_out``: optional list the caller passes in to collect
    user-visible warning strings. The function appends one string per
    server whose enumeration failed AND ended up contributing zero
    items to the scan — these surface in the job UI's amber-badge
    "completed with warning" state via ``complete_job(warning=...)``.
    Job b6deeac3 reproduced the silent-green-badge regression this
    out-parameter addresses: Jellyfin's /Items timed out, the library
    was skipped, and the job ended as "completed successfully" with
    zero items processed.
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

    all_items, enumeration_errors = _enumerate_items_for_servers(
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

    # Surface enumeration failures regardless of whether any items
    # came through from OTHER servers. The 2×2 matrix is
    # ``{items=0, items>0} × {errors=0, errors>0}``. The cell
    # ``items>0 AND errors>0`` (e.g. Plex enumerated fine, Jellyfin
    # timed out) is the multi-server analogue of job b6deeac3 — the
    # job processes some files but silently drops every Jellyfin
    # path. Without this hoist the badge stays green and the user
    # has no signal that Jellyfin's catalogue was missed. The
    # legitimate-empty-library case (no enumeration_errors at all)
    # still falls through to plain green.
    if warnings_out is not None and enumeration_errors:
        servers_with_errors = ", ".join(name for name, _err in enumeration_errors)
        error_detail = "; ".join(f"{name}: {msg}" for name, msg in enumeration_errors)
        if all_items:
            warnings_out.append(
                f"Enumeration failed for {len(enumeration_errors)} server(s) "
                f"({servers_with_errors}) — these libraries were skipped but other "
                f"servers contributed items. {error_detail}. Retry the scan once "
                f"the server is healthy to pick up the missed catalogue."
            )
        else:
            warnings_out.append(
                f"Enumeration failed for {len(enumeration_errors)} server(s) "
                f"({servers_with_errors}) — zero items processed. {error_detail}. "
                f"Retry the scan once the server is healthy."
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
            "contains no Movie/Episode items.",
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
        on_dispatch_start=on_dispatch_start,
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
    on_dispatch_start=None,
    warnings_out: list[str] | None = None,
) -> dict:
    """Recently-added scan for any vendor via :class:`VendorProcessor`.

    Walks every enabled server (or just ``server_id_filter``) calling
    ``processor.scan_recently_added`` for each. Per-vendor processors
    handle the API differences (Plex's ``addedAt>>`` filter vs.
    Emby/Jellyfin's ``DateCreated`` sort) so the orchestrator stays
    vendor-agnostic.

    Returns the aggregated ProcessingResult counts. ``warnings_out``
    mirrors :func:`_run_full_scan_multi_server` — without this plumbing
    a Sonarr/Radarr-driven recently-added scan whose Jellyfin /Items
    times out would silently report "completed" with zero items, the
    same shape of bug as job b6deeac3.
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
    all_items, enumeration_errors = _enumerate_items_for_servers(
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

    # Same warning-on-enumeration-failure contract as the full-scan
    # path — partial-success and zero-success cells both surface a
    # warning when any server's enumeration raised.
    if warnings_out is not None and enumeration_errors:
        servers_with_errors = ", ".join(name for name, _err in enumeration_errors)
        error_detail = "; ".join(f"{name}: {msg}" for name, msg in enumeration_errors)
        if all_items:
            warnings_out.append(
                f"Recently-added enumeration failed for {len(enumeration_errors)} server(s) "
                f"({servers_with_errors}) — those libraries were skipped but other servers "
                f"contributed items. {error_detail}."
            )
        else:
            warnings_out.append(
                f"Recently-added enumeration failed for {len(enumeration_errors)} server(s) "
                f"({servers_with_errors}) — zero items processed. {error_detail}."
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
        on_dispatch_start=on_dispatch_start,
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


def _build_path_mapping_mismatch_hints(unresolved_paths: list[str], server_configs: list) -> dict[str, str]:
    """Detect likely path-mapping mismatches and return per-path hints.

    Audit P4 fix — previously returned ``list[str]``; the consumer
    (``job_runner.py``) used ``hints[0]`` for every unresolved row, so
    a multi-path webhook with different mismatches showed the SAME
    hint on every row (often the wrong one). Returning a dict keyed
    by the originating path lets each row pick its own hint.

    For each unresolved webhook path, walks every configured server's
    library remote_paths and looks for a location that's a
    path-boundary substring of the webhook path — the fingerprint of
    "Sonarr/Radarr is reporting one prefix but the server stores
    another." When found, the dict entry contains a hint string the
    file_result row can show in place of the generic "Not found"
    message. Paths with no detectable mismatch are simply absent from
    the dict (the caller should fall back to a generic message).
    """
    if not unresolved_paths or not server_configs:
        return {}

    from ..plex_client import _mismatch_covered_by_mappings

    locations: list[str] = []
    location_owners: dict[str, list[tuple[str, list[dict]]]] = {}
    for cfg in server_configs:
        if not getattr(cfg, "enabled", True):
            continue
        for lib in cfg.libraries or []:
            if not getattr(lib, "enabled", True):
                continue
            for loc in lib.remote_paths or ():
                if not str(loc).strip():
                    continue
                norm_loc = str(loc).rstrip("/")
                locations.append(norm_loc)
                location_owners.setdefault(norm_loc, []).append((cfg.name or cfg.id, list(cfg.path_mappings or [])))

    if not locations:
        return {}

    # Longest-first so more-specific library locations match before
    # broader ones (e.g. /media/tv before /media so the hint suggests
    # the closer parent).
    norm_locations = sorted({loc for loc in locations}, key=len, reverse=True)
    all_mappings = [mappings for owners_list in location_owners.values() for _, mappings in owners_list]

    hints: dict[str, str] = {}
    for upath in unresolved_paths:
        upath_norm = upath.replace("\\", "/")
        upath_lower = upath_norm.lower()
        for server_loc in norm_locations:
            loc_lower = server_loc.lower()
            idx = upath_lower.find(loc_lower)
            if idx <= 0:
                continue
            # Path-boundary check — /media/tv must not match /media/tv2.
            end_idx = idx + len(loc_lower)
            if end_idx < len(upath_lower) and upath_lower[end_idx] != "/":
                continue

            extra = upath_norm[:idx]
            # Suggest the parent so the mapping covers sibling
            # libraries (e.g. /media covers both /media/tv and
            # /media/movies).
            server_parent = os.path.dirname(server_loc)
            if server_parent and server_parent != "/":
                server_pfx = server_parent
                webhook_pfx = extra.rstrip("/") + server_parent
            else:
                server_pfx = server_loc
                webhook_pfx = extra.rstrip("/") + server_loc

            # Coverage check: is there already a mapping that would
            # have bridged this prefix gap on the owning server?
            candidate_owners: list[tuple[str, list[dict]]] = []
            for loc, owners_list in location_owners.items():
                if loc == server_pfx or loc.startswith(server_pfx.rstrip("/") + "/"):
                    candidate_owners.extend(owners_list)
            owner_covers = any(
                _mismatch_covered_by_mappings(webhook_pfx, server_pfx, mappings) for _, mappings in candidate_owners
            )
            if owner_covers:
                hints[upath] = (
                    f"Path mapping '{webhook_pfx}' → '{server_pfx}' is configured but file not "
                    "found (may not be indexed yet)"
                )
                break

            other_covers = any(_mismatch_covered_by_mappings(webhook_pfx, server_pfx, m) for m in all_mappings)
            if other_covers:
                hints[upath] = (
                    f"Path mapping '{webhook_pfx}' → '{server_pfx}' is configured on a different "
                    "server but the owning server is missing it — add the mapping there too in "
                    "Settings → Path mappings"
                )
            else:
                hints[upath] = (
                    f"Possible prefix mismatch: webhook sends '{webhook_pfx}' but a configured "
                    f"library uses '{server_pfx}'. Add a path mapping in Settings: server path = "
                    f"{server_pfx}, webhook path = {webhook_pfx}"
                )
            break

    return hints


def _classify_processing_mode(config) -> str:
    """Decide which processing phase ``run_processing`` should execute.

    Returns one of:

    * ``"webhook_paths"`` — the job has a concrete path list; dispatch via
      :func:`_run_webhook_paths_phase`.
    * ``"refuse_malformed_webhook"`` — the job is webhook-origin
      (``webhook_source`` set) but ``webhook_paths`` is empty / None.
      The caller MUST NOT fall through to a full library scan: doing so
      attributes a 100k+ item scan to a Job that the UI presents as a
      single-file webhook entry. See Job e7968486 (May 2026): one Sonarr
      webhook for one TV episode triggered eight separate full-library
      scans across eleven container restarts because the original
      "Job-at-batch-open" refactor forgot to persist ``webhook_paths``
      in ``job.config``. The webhook-side fix closes the primary hole;
      this branch is defense in depth against any future code path that
      ships a webhook job without paths.
    * ``"full_scan"`` — no webhook markers at all; legitimate scheduled
      or manual full-library scan.

    Pure function; only reads attributes on ``config``. Tested in
    ``tests/test_orchestrator_webhook_fallthrough.py``.
    """
    if getattr(config, "webhook_paths", None):
        return "webhook_paths"
    if getattr(config, "webhook_source", None):
        return "refuse_malformed_webhook"
    return "full_scan"


def _run_webhook_paths_phase(
    config,
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
    """Dispatch every webhook path through the unified peer-equal fan-out.

    Mutates ``totals`` (keys: ``processed``, ``successful``, ``failed``,
    ``cancelled``) and ``aggregate_outcome`` in place so the caller can
    keep accumulating across phases. Returns the ``webhook_resolution``
    dict that becomes part of the job's return_data.

    Architecture: every webhook path is a :class:`ProcessableItem` and
    runs through ``dispatch_items`` → ``process_canonical_path``. That
    worker handles per-server ownership resolution + parallel fan-out
    so Plex, Emby, and Jellyfin all publish for any path they own.
    There is no Plex-first stage, no fallback, and no K4: every server
    is a peer. Paths owned by no enabled server fast-skip here so a
    worker thread never gets handed a path it can't process.

    Vendor-webhook hints (Plex/Emby/Jellyfin native plugins that
    already named the item id) flow through unchanged via
    ``ProcessableItem.item_id_by_server`` so the relevant adapter
    skips a slow reverse-lookup. The dispatcher's lazy
    ``_make_item_id_resolver`` handles the no-hint case per-server.
    """
    from ..processing.types import ProcessableItem as _PI

    paths = list(config.webhook_paths or [])
    total_paths = len(paths)
    if not paths:
        return {
            "unresolved_paths": [],
            "skipped_paths": [],
            "resolved_count": 0,
            "total_paths": 0,
            "path_hints": [],
        }

    if progress_callback:
        progress_callback(0, total_paths, f"Resolving {total_paths} webhook path(s) across configured servers…")
    _log_webhook_owning_servers(config, paths)

    hints = getattr(config, "webhook_item_id_hints", None) or {}
    server_configs = list(registry.configs())

    webhook_items: list[_PI] = []
    # Audit A3/A4 — keep a parallel canonical→raw-input map so the
    # ``unresolved_paths`` list (consumed by job_runner.py for
    # file_result rows + retry hint lookup keying) can stay in a
    # SINGLE namespace (the raw webhook input) regardless of whether
    # a path was a no_owner skip or a FAILED dispatch outcome.
    # Without this, no_owner paths landed in unresolved_paths as raw
    # webhook strings while FAILED paths landed as server-view
    # canonical strings → the retry job's
    # ``webhook_item_id_hints`` lookup (keyed by raw input) missed
    # FAILED items entirely → retries paid the slow Jellyfin Pass 2
    # cost instead of using the hint short-circuit.
    canonical_to_input: dict[str, str] = {}
    no_owners: list[str] = []
    for path in paths:
        # Resolve the webhook-source path to a server-view canonical
        # path AND its owners in one pass. Sonarr/Radarr emit paths in
        # their own namespace (``/data/TV Shows/...``) and the server
        # libraries report a different one (``/data_16tb/TV Shows``);
        # the helper translates via webhook_prefixes and returns the
        # canonical form that matches the library, so the downstream
        # ``process_canonical_path._resolve_owners`` lookup (which
        # doesn't translate) gets a path it can match. Without this
        # the worker picks up the path, the per-server check fails,
        # and the job lands NO_OWNERS milliseconds later despite
        # multiple servers actually owning the file.
        canonical_path, owners = _resolve_webhook_path_to_canonical(path, server_configs)
        per_path = hints.get(path) or {}
        if not owners:
            # Audit A2 — when no library covers the path BUT the
            # webhook payload supplied a vendor item-id hint (Plex
            # ``library.new``, Emby ``ItemAdded``, Jellyfin plugin
            # webhook all do), the dispatcher's ``_resolve_publishers``
            # would still honour the hint via the hinted server's
            # adapter. The orchestrator gate previously fast-skipped
            # this case → user adds a library, gets a webhook before
            # our cache refreshes, the very webhook that should
            # bootstrap the new library silently does nothing.
            #
            # Library-cache staleness during library-add → first
            # webhook silently dropped. Honour the hint: dispatch the
            # path with the hinted server pinned via item.server_id;
            # downstream resolver looks up the hinted server's id and
            # fans out only there.
            if not per_path:
                no_owners.append(path)
                continue
            logger.debug(
                "Webhook path {} has no library coverage but vendor hint(s) supplied "
                "{!r} — honouring hint and dispatching anyway (library cache may be "
                "stale post library-add).",
                path,
                list(per_path.keys()),
            )
        # Hint dicts always have one entry today (vendor webhooks carry
        # exactly one server hint); a future caller passing multiple
        # gets dict-insertion order with a debug line so it's traceable.
        if len(per_path) > 1:
            logger.debug(
                "ProcessableItem for {} has {} hint server(s); using first ({}). "
                "Other hints still flow into item_id_by_server.",
                path,
                len(per_path),
                next(iter(per_path)),
            )
        server_id = next(iter(per_path), "")
        webhook_items.append(
            _PI(
                canonical_path=canonical_path,
                server_id=server_id,
                item_id_by_server=dict(per_path),
                title=os.path.basename(canonical_path.rstrip("/")) or canonical_path,
                library_id=None,
            )
        )
        # Track canonical → raw input so a FAILED outcome can be
        # surfaced under the original webhook path (audit A3/A4).
        canonical_to_input[canonical_path] = path

    unresolved: list[str] = list(no_owners)
    # Path-keyed mismatch hints (audit P4). Built per-path so a
    # multi-path webhook with N different mismatches displays N
    # different hints — one per file_result row, not one borrowed
    # from slot 0.
    path_hint_map: dict[str, str] = {}

    if no_owners:
        logger.info(
            "Webhook arrived with {} path(s) that no enabled server claims — fast-skipping "
            "(no worker pickup, no retry). Verify path mappings under Settings line up with "
            "what each server reports for its libraries.",
            len(no_owners),
        )
        # When a path is unowned but a configured library's location is
        # a path-boundary substring of the webhook path, the user almost
        # certainly has a path-mapping mismatch (Sonarr/Radarr send
        # ``/data/Movies/X.mkv`` but Plex/Emby/Jellyfin reports
        # ``/media/Movies/X.mkv``, no mapping configured). Surfacing
        # this hint per-row keeps the UX the legacy Plex-first stage
        # gave users — without it, the file_result row just says "Not
        # found", which doesn't tell the user *why*.
        path_hint_map.update(_build_path_mapping_mismatch_hints(no_owners, server_configs))

    if webhook_items:
        result = dispatch_items(webhook_items, "Webhook Targets")
        totals["successful"] += result["completed"]
        totals["failed"] += result["failed"]
        totals["processed"] += result["completed"] + result["failed"]
        totals["cancelled"] = totals["cancelled"] or result["cancelled"]
        for k, v in (result.get("outcome") or {}).items():
            aggregate_outcome[k] = aggregate_outcome.get(k, 0) + v
        # ``dispatch_items`` doesn't tell us WHICH paths failed — only
        # the aggregate count. Single-path batch (the dominant vendor-
        # webhook case) is unambiguous; multi-path batches mark exactly
        # N (count is correct, identity is unknowable, retries on
        # already-succeeded paths short-circuit cheaply via .meta).
        # See audit H2.
        failed_count = result.get("failed", 0)
        if failed_count:
            # Audit A3/A4 — surface the RAW webhook-input path in the
            # unresolved list so the retry job's
            # ``webhook_item_id_hints`` lookup (keyed by raw input)
            # finds its hint. Pre-fix this stored the server-view
            # canonical_path; the retry job's webhook_paths matched,
            # but the hint dict (keyed by raw) didn't → retries paid
            # full reverse-lookup cost on every retry round.
            failed_inputs = [
                canonical_to_input.get(item.canonical_path, item.canonical_path)
                for item in webhook_items[:failed_count]
            ]
            unresolved.extend(failed_inputs)
            # Pass-1 audit #6: also build hints for FAILED items, not
            # only no_owners. A path that owners exist for but every
            # publisher failed (e.g. publisher 5xx, source missing
            # post-rebind) gets the same diagnostic UX as a no-owner
            # path. Hints are best-effort — if no mismatch is detected
            # the path simply doesn't appear in the dict and the
            # consumer falls back to a generic message.
            path_hint_map.update(_build_path_mapping_mismatch_hints(failed_inputs, server_configs))

    return {
        "unresolved_paths": unresolved,
        "skipped_paths": [],
        "resolved_count": total_paths - len(unresolved),
        "total_paths": total_paths,
        # Backwards-compatible: legacy callers that consumed
        # ``path_hints`` as a list still see a flat list of hint
        # strings (the same set, dedup-preserved). New callers read
        # ``path_hint_map`` for per-path correspondence.
        "path_hints": list(dict.fromkeys(path_hint_map.values())),
        "path_hint_map": dict(path_hint_map),
    }


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

        # Defense in depth (BEFORE either full-scan branch): a job marked
        # as webhook-origin but missing webhook_paths is malformed —
        # likely an auto-requeue after restart where the path list got
        # lost. Refuse outright; never let a webhook job degrade into a
        # full-library scan. See Job e7968486 (May 2026) for the
        # regression this guards. Tested in
        # tests/test_orchestrator_webhook_fallthrough.py.
        if _classify_processing_mode(config) == "refuse_malformed_webhook":
            logger.error(
                "Refusing to run job as full library scan: it carries a webhook "
                "source ({!r}) but no webhook_paths. Most likely cause: the job "
                "was created by a webhook, persisted without webhook_paths in "
                "job.config, then revived after a container restart with the path "
                "list lost. Re-trigger the originating webhook to process the "
                "original file. See Job e7968486 (May 2026) for the regression "
                "this guards against.",
                getattr(config, "webhook_source", None),
            )
            empty_outcome = {r.value: 0 for r in ProcessingResult}
            return {"outcome": empty_outcome, "error": "webhook_paths_missing"}

        if _should_use_multi_server_full_scan(config, pinned_type):
            library_ids = list(getattr(config, "plex_library_ids", None) or [])
            scan_warnings: list[str] = []
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
                on_dispatch_start=on_dispatch_start,
                warnings_out=scan_warnings,
            )
            result: dict = {"outcome": outcome_counts}
            if scan_warnings:
                # Joined warning string the job_runner pipes into
                # ``complete_job(warning=...)`` — flips the dashboard
                # badge from green "completed" to amber "completed
                # with warning" so a silently-failed enumeration
                # doesn't masquerade as a successful run.
                result["warning"] = " | ".join(scan_warnings)
            return result

        # Per-server PlexServer instances are established lazily by
        # the dispatch path (`process_canonical_path` → adapter →
        # `_resolve_one_path`) when a path actually needs Plex
        # resolution. The orchestrator no longer pre-connects:
        # * The result was a dead parameter on
        #   ``_run_webhook_paths_phase`` after the K4 → peer-equal
        #   unification (commit 3edd185). The full-scan phase never
        #   took it.
        # * Eagerly opening a Plex session blocked job start by ~300ms
        #   even on jobs whose paths only Emby/Jellyfin own — and
        #   would abort the entire job (ConnectionError) on a Plex
        #   outage that shouldn't have touched non-Plex paths at all.
        # * The "[Plex] Connecting to Plex" log line landing before
        #   the unified-dispatch "Resolving N webhook path(s)…" read
        #   like Plex-first dispatch in the timeline (user-flagged on
        #   job 3b154264).
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

        # ``_classify_processing_mode`` here picks between
        # "webhook_paths" and "full_scan" — the "refuse_malformed_webhook"
        # case was already short-circuited at the top of run_processing
        # (before the multi-server fast path), so the third branch below
        # is intentionally unreachable today. Keeping it as an explicit
        # AssertionError instead of an open ``else`` makes the invariant
        # load-bearing: if some future edit lifts the early refusal,
        # this site will fail loudly instead of silently degrading
        # malformed webhook jobs into a Plex full scan.
        mode = _classify_processing_mode(config)
        if mode == "webhook_paths":
            webhook_resolution_payload = _run_webhook_paths_phase(
                config,
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
        elif mode == "full_scan":
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
        else:
            raise AssertionError(
                f"Unreachable: refuse_malformed_webhook should have been caught "
                f"by the early refusal at the top of run_processing — got mode={mode!r}"
            )

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
