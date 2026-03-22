"""Multi-job dispatcher for concurrent job processing.

Provides a persistent dispatch loop and shared worker pool so multiple jobs
can run simultaneously.  Items are dispatched using priority-aware drain-first
scheduling: workers focus on the highest-priority active job and only spill
over to the next job when the current job's queue is empty.
"""

import queue
import threading
import time
from collections import deque
from functools import partial
from typing import Any, Callable, Dict, List, Optional, Tuple

from loguru import logger

from .config import Config
from .media_processing import ProcessingResult
from .web.jobs import PRIORITY_NORMAL
from .worker import Worker, WorkerPool

_submission_counter_lock = threading.Lock()
_submission_counter = 0


def _next_submission_order() -> int:
    """Return a monotonically increasing submission sequence number."""
    global _submission_counter
    with _submission_counter_lock:
        _submission_counter += 1
        return _submission_counter


class JobTracker:
    """Tracks progress for items belonging to a single job.

    Each job submitted to the dispatcher gets its own tracker that holds
    the item queue, completion counters, callbacks, and a done event.

    Args:
        job_id: Unique job identifier.
        items: List of (item_key, media_title, media_type) tuples.
        config: Config object for this job's processing.
        plex: Plex server instance for this job.
        title_max_width: Max display width for titles.
        library_name: Library name for log prefixes.
        callbacks: Dict of per-job callback functions.
        priority: Dispatch priority (1=high, 2=normal, 3=low).

    """

    def __init__(
        self,
        job_id: str,
        items: List[tuple],
        config: Config,
        plex,
        title_max_width: int = 20,
        library_name: str = "",
        callbacks: Optional[Dict[str, Any]] = None,
        priority: int = PRIORITY_NORMAL,
    ):
        """Initialize tracker for a single job."""
        self.job_id = job_id
        self.priority = priority
        self.submission_order = _next_submission_order()
        self.config = config
        self.plex = plex
        self.title_max_width = title_max_width
        self.library_name = library_name
        self.library_prefix = f"[{library_name}] " if library_name else ""

        self.item_queue: deque = deque(items)
        self.total_items = len(items)
        self.successful = 0
        self.failed = 0
        self.cancelled = False
        self.outcome_counts: Dict[str, int] = {r.value: 0 for r in ProcessingResult}
        self.done_event = threading.Event()

        cbs = callbacks or {}
        self.progress_callback: Optional[Callable] = cbs.get("progress_callback")
        self.worker_callback: Optional[Callable] = cbs.get("worker_callback")
        self.on_item_complete: Optional[Callable] = cbs.get("on_item_complete")
        self.cancel_check: Optional[Callable] = cbs.get("cancel_check")
        self.pause_check: Optional[Callable] = cbs.get("pause_check")

        # Throttle timestamps for callbacks
        self._last_progress_update = 0.0
        self._last_worker_update = 0.0
        # Set when all items are done; used by _cleanup_done_trackers
        self._done_at: Optional[float] = None

    @property
    def completed(self) -> int:
        """Total items finished (success + failure)."""
        return self.successful + self.failed

    def is_paused(self) -> bool:
        """Check if this job's dispatch is paused."""
        if self.pause_check:
            return self.pause_check()
        return False

    def is_cancelled(self) -> bool:
        """Check if this job has been cancelled."""
        if self.cancel_check:
            return self.cancel_check()
        return False

    def record_completion(
        self,
        success: bool,
        worker_display_name: str = "",
        title: str = "",
    ) -> None:
        """Record a completed item and fire per-job callbacks.

        Args:
            success: Whether the item succeeded.
            worker_display_name: Display name of the worker for logging.
            title: Media title for logging.

        """
        if success:
            self.successful += 1
        else:
            self.failed += 1

        if self.on_item_complete:
            self.on_item_complete(worker_display_name, title, success)

        if self.progress_callback:
            now = time.time()
            is_final = self.completed >= self.total_items
            if is_final or now - self._last_progress_update >= 0.5:
                self.progress_callback(
                    self.completed,
                    self.total_items,
                    f"{self.library_prefix}{self.completed}/{self.total_items} completed",
                )
                self._last_progress_update = now

        if self.completed >= self.total_items:
            self.done_event.set()

    def cancel(self) -> None:
        """Mark this job cancelled and drain remaining items."""
        self.cancelled = True
        remaining = len(self.item_queue)
        self.item_queue.clear()
        if remaining:
            self.failed += remaining
        self.done_event.set()

    def wait(self, timeout: Optional[float] = None) -> bool:
        """Block until all items are processed.

        Returns:
            True if completed, False if timed out.

        """
        return self.done_event.wait(timeout)

    def get_result(self) -> dict:
        """Return a result dict compatible with WorkerPool.process_items_headless."""
        return {
            "completed": self.successful,
            "failed": self.failed,
            "total": self.total_items,
            "cancelled": self.cancelled,
            "outcome": dict(self.outcome_counts),
        }


class JobDispatcher:
    """Coordinates item dispatch across multiple concurrent jobs.

    Owns a persistent WorkerPool and runs a background dispatch loop.
    Uses priority-aware drain-first scheduling: workers focus on the
    highest-priority active job and spill over to the next only when idle.

    Args:
        worker_pool: The shared WorkerPool instance.

    """

    def __init__(self, worker_pool: WorkerPool):
        """Initialize dispatcher with shared worker pool."""
        self.worker_pool = worker_pool
        self._trackers: Dict[str, JobTracker] = {}
        self._trackers_lock = threading.RLock()
        self._dispatch_thread: Optional[threading.Thread] = None
        self._has_work = threading.Event()
        self._shutdown = False

    def submit_items(
        self,
        job_id: str,
        items: List[tuple],
        config: Config,
        plex,
        title_max_width: int = 20,
        library_name: str = "",
        callbacks: Optional[Dict[str, Any]] = None,
        priority: int = PRIORITY_NORMAL,
    ) -> JobTracker:
        """Submit items for a job to the shared dispatch queue.

        Args:
            job_id: Unique job identifier.
            items: List of (item_key, media_title, media_type) tuples.
            config: Configuration for processing these items.
            plex: Plex server instance.
            title_max_width: Max title display width.
            library_name: Library name for log prefixes.
            callbacks: Dict with keys: progress_callback, worker_callback,
                on_item_complete, cancel_check, pause_check.
            priority: Dispatch priority (1=high, 2=normal, 3=low).

        Returns:
            JobTracker that callers can wait() on for completion.

        """
        tracker = JobTracker(
            job_id=job_id,
            items=items,
            config=config,
            plex=plex,
            title_max_width=title_max_width,
            library_name=library_name,
            callbacks=callbacks,
            priority=priority,
        )
        with self._trackers_lock:
            self._trackers[job_id] = tracker
        logger.info(
            f"Dispatcher: submitted {len(items)} items for job {job_id[:8]} "
            f"({library_name or 'no library'})"
        )
        self._has_work.set()
        self._ensure_dispatch_running()
        return tracker

    def cancel_job(self, job_id: str) -> None:
        """Cancel a job's remaining items in the dispatch queue."""
        with self._trackers_lock:
            tracker = self._trackers.get(job_id)
        if tracker:
            tracker.cancel()
            logger.info(f"Dispatcher: cancelled job {job_id[:8]}")

    def get_tracker(self, job_id: str) -> Optional[JobTracker]:
        """Get the tracker for a job, if it exists."""
        with self._trackers_lock:
            return self._trackers.get(job_id)

    def shutdown(self) -> None:
        """Stop the dispatch loop and shut down the worker pool."""
        self._shutdown = True
        self._has_work.set()  # Wake the loop so it can exit
        if self._dispatch_thread and self._dispatch_thread.is_alive():
            self._dispatch_thread.join(timeout=30)
        self.worker_pool.shutdown()

    # ------------------------------------------------------------------
    # Internal dispatch machinery
    # ------------------------------------------------------------------

    def _ensure_dispatch_running(self) -> None:
        """Start the background dispatch thread if not already running."""
        if self._dispatch_thread is not None and self._dispatch_thread.is_alive():
            return
        self._dispatch_thread = threading.Thread(
            target=self._dispatch_loop, daemon=True, name="job-dispatcher"
        )
        self._dispatch_thread.start()

    def _dispatch_loop(self) -> None:
        """Persistent loop: check completions, assign tasks, sleep adaptively."""
        logger.info("Dispatcher: dispatch loop started")
        last_progress_log = time.time()

        while not self._shutdown:
            # Wait until there's work to do (with periodic wake for housekeeping)
            self._has_work.wait(timeout=1.0)

            if self._shutdown:
                break

            # 1. Handle cancelled jobs
            self._handle_cancellations()

            # 2. Check worker completions and route to trackers
            self._check_completions()

            # 3. Assign items to available workers BEFORE emitting
            #    updates so the first status emission reflects the newly
            #    busy worker instead of stale "idle" data.
            self._assign_tasks()

            # 4. Emit periodic worker status updates for all active jobs
            self._emit_worker_updates()

            # 5. Emit periodic progress updates for active jobs
            self._emit_progress_updates()

            # 6. Drain fallback queue if no CPU workers remain
            self._drain_orphaned_fallback_items()

            # 7. Periodic progress logging
            now = time.time()
            if now - last_progress_log >= 5.0:
                self._log_progress()
                last_progress_log = now

            # 8. Clean up completed trackers and check if loop can idle
            self._cleanup_done_trackers()
            if self._all_idle():
                self._has_work.clear()

            # Adaptive sleep
            if self.worker_pool.has_busy_workers():
                time.sleep(0.005)
            else:
                time.sleep(0.01)

        logger.info("Dispatcher: dispatch loop exited")

    def _handle_cancellations(self) -> None:
        """Cancel trackers whose cancel_check returns True."""
        with self._trackers_lock:
            active = [t for t in self._trackers.values() if not t.done_event.is_set()]
        for tracker in active:
            if tracker.is_cancelled() and not tracker.cancelled:
                tracker.cancel()
                logger.info(
                    f"Dispatcher: job {tracker.job_id[:8]} cancelled "
                    f"({tracker.completed}/{tracker.total_items} done)"
                )

    def _check_completions(self) -> None:
        """Check all workers for completed tasks and update the owning tracker."""
        for worker in self.worker_pool._snapshot_workers():
            if not worker.check_completion():
                continue

            title = worker.media_title or "(unknown)"
            job_id = worker.current_job_id

            # GPU->CPU re-queue: the CPU worker will record completion later
            if worker.requeued_to_cpu:
                continue

            with self._trackers_lock:
                tracker = self._trackers.get(job_id) if job_id else None

            if tracker and not tracker.done_event.is_set():
                success = worker.last_task_succeeded()
                # Merge worker outcome counts into tracker
                self._merge_worker_outcome(worker, tracker)
                tracker.record_completion(success, worker.display_name, title)
            else:
                # No tracker for this item — just log
                success = worker.last_task_succeeded()
                outcome = "success" if success else "failed"
                logger.debug(
                    f"Dispatcher: {worker.display_name} completed {title} "
                    f"({outcome}) — no active tracker for job_id={job_id}"
                )

            # Retire deferred-removal workers
            self.worker_pool._retire_idle_worker_if_scheduled(worker)

    def _merge_worker_outcome(self, worker: Worker, tracker: JobTracker) -> None:
        """Merge the latest outcome delta from a worker into the tracker.

        Uses the pre-task baseline snapshot on the worker to compute which
        outcome counters changed during the most recent task.
        """
        delta = worker.last_task_outcome_delta()
        for key, count in delta.items():
            if count > 0 and key in tracker.outcome_counts:
                tracker.outcome_counts[key] += count

    def _assign_tasks(self) -> None:
        """Assign items from active jobs to available workers."""
        self.worker_pool._apply_deferred_removals()

        while True:
            # Determine if we should only look for CPU workers
            has_main_items = self._has_dispatchable_items()
            cpu_only = not has_main_items
            worker = self.worker_pool._find_available_worker(cpu_only=cpu_only)
            if not worker:
                break

            # CPU/CPU_FALLBACK workers: try fallback queue first
            if worker.worker_type in ("CPU", "CPU_FALLBACK"):
                if self._assign_fallback_to_worker(worker):
                    continue
                if worker.worker_type == "CPU_FALLBACK" or not has_main_items:
                    break

            # Pull next item (highest priority, then oldest submission)
            item = self._get_next_item()
            if not item:
                break

            job_id, item_key, media_title, media_type, library_name = item
            with self._trackers_lock:
                tracker = self._trackers.get(job_id)
            if not tracker:
                continue

            progress_callback = partial(
                self.worker_pool._update_worker_progress, worker
            )
            cpu_fallback_queue = (
                self.worker_pool.cpu_fallback_queue
                if worker.worker_type == "GPU"
                else None
            )
            worker.assign_task(
                item_key,
                tracker.config,
                tracker.plex,
                progress_callback=progress_callback,
                media_title=media_title,
                media_type=media_type,
                title_max_width=tracker.title_max_width,
                cpu_fallback_queue=cpu_fallback_queue,
                job_id=job_id,
                library_name=library_name,
                cancel_check=tracker.cancel_check,
            )
            logger.info(
                f"Dispatch: assigned {media_title!r} (job {job_id[:8]}) "
                f"to {worker.display_name}"
            )

    def _assign_fallback_to_worker(self, worker: Worker) -> bool:
        """Try to assign a fallback-queue item to a CPU worker.

        The fallback queue holds items from ALL jobs (tagged with job_id),
        so we look up the correct tracker to get config/plex.

        Returns:
            True if a task was assigned.

        """
        try:
            fallback_item = self.worker_pool.cpu_fallback_queue.get_nowait()
        except queue.Empty:
            return False

        job_id = fallback_item[0] if len(fallback_item) >= 1 else None
        item_key = fallback_item[1] if len(fallback_item) >= 2 else fallback_item
        media_title = fallback_item[2] if len(fallback_item) >= 3 else None
        media_type = fallback_item[3] if len(fallback_item) >= 4 else None
        library_name = fallback_item[4] if len(fallback_item) >= 5 else ""

        with self._trackers_lock:
            tracker = self._trackers.get(job_id) if job_id else None

        # Don't assign fallback items for cancelled jobs
        if tracker and (tracker.cancelled or tracker.is_cancelled()):
            tracker.record_completion(
                success=False,
                worker_display_name="(cancelled)",
                title=str(media_title or item_key),
            )
            logger.info(
                f"Dispatcher: skipped cancelled fallback item {media_title!r} "
                f"(job {job_id[:8] if job_id else 'unknown'})"
            )
            return True

        if tracker:
            config = tracker.config
            plex = tracker.plex
            title_max_width = tracker.title_max_width
        else:
            config, plex, title_max_width = self._fallback_config()
            if config is None:
                logger.warning(
                    f"Dispatcher: no config available for fallback item {item_key}"
                )
                return False

        if media_title is None or media_type is None:
            media_title, media_type = self.worker_pool._get_plex_media_info(
                plex, item_key
            )

        progress_callback = partial(self.worker_pool._update_worker_progress, worker)
        cancel_check = tracker.cancel_check if tracker else None
        worker.assign_task(
            item_key,
            config,
            plex,
            progress_callback=progress_callback,
            media_title=media_title,
            media_type=media_type,
            title_max_width=title_max_width,
            cpu_fallback_queue=None,
            job_id=job_id,
            library_name=library_name,
            cancel_check=cancel_check,
        )
        logger.info(
            f"Dispatch: assigned fallback item {media_title!r} to {worker.display_name}"
        )
        return True

    def _fallback_config(self) -> Tuple[Optional[Config], Any, int]:
        """Return (config, plex, title_max_width) from any active tracker."""
        with self._trackers_lock:
            for tracker in self._trackers.values():
                if not tracker.done_event.is_set():
                    return tracker.config, tracker.plex, tracker.title_max_width
        return None, None, 20

    def _has_dispatchable_items(self) -> bool:
        """Check if any active (non-paused, non-cancelled) tracker has items."""
        with self._trackers_lock:
            for tracker in self._trackers.values():
                if tracker.done_event.is_set():
                    continue
                if tracker.is_paused() or tracker.is_cancelled():
                    continue
                if tracker.item_queue:
                    return True
        return False

    def update_job_priority(self, job_id: str, priority: int) -> None:
        """Update the dispatch priority of a running job's tracker.

        Args:
            job_id: Job identifier.
            priority: New priority (1=high, 2=normal, 3=low).
        """
        with self._trackers_lock:
            tracker = self._trackers.get(job_id)
            if tracker:
                tracker.priority = priority

    def _get_next_item(self) -> Optional[Tuple[str, str, str, str, str]]:
        """Get the next item using priority-aware drain-first scheduling.

        Picks from the highest-priority active job first (lowest number).
        Within the same priority, earlier submissions are preferred.

        Returns:
            (job_id, item_key, media_title, media_type, library_name) or None.

        """
        with self._trackers_lock:
            eligible = [
                t
                for t in self._trackers.values()
                if not t.done_event.is_set()
                and not t.is_paused()
                and not t.is_cancelled()
                and t.item_queue
            ]
            eligible.sort(key=lambda t: (t.priority, t.submission_order))
            for tracker in eligible:
                item = tracker.item_queue.popleft()
                item_key, media_title, media_type = item[0], item[1], item[2]
                library_name = item[3] if len(item) > 3 else ""
                return (tracker.job_id, item_key, media_title, media_type, library_name)
        return None

    def _drain_orphaned_fallback_items(self) -> None:
        """Drain fallback items when no CPU-capable workers exist.

        Unlike the WorkerPool's generic drain, this version extracts the
        job_id from each 4-tuple and attributes the failure to the correct
        JobTracker so per-job counts remain accurate.
        """
        if (
            not self.worker_pool._check_fallback_queue_empty()
            and not self.worker_pool._has_cpu_capable_workers()
            and not self.worker_pool.has_busy_workers()
        ):
            drained = 0
            while not self.worker_pool.cpu_fallback_queue.empty():
                try:
                    item = self.worker_pool.cpu_fallback_queue.get_nowait()
                except queue.Empty:
                    break
                job_id = (
                    item[0]
                    if isinstance(item, (list, tuple)) and len(item) >= 1
                    else None
                )
                item_key = (
                    item[1]
                    if isinstance(item, (list, tuple)) and len(item) >= 2
                    else item
                )
                with self._trackers_lock:
                    tracker = self._trackers.get(job_id) if job_id else None
                if tracker and not tracker.done_event.is_set():
                    tracker.record_completion(
                        success=False,
                        worker_display_name="(drain)",
                        title=str(item_key),
                    )
                logger.warning(
                    f"Drained unreachable fallback item {item_key} as failed "
                    f"(no CPU workers available, job={job_id[:8] if job_id else 'unknown'})"
                )
                drained += 1
            if drained:
                logger.warning(
                    f"Dispatcher: drained {drained} orphaned fallback item(s) — "
                    "add CPU or CPU_FALLBACK workers to process codec-fallback items"
                )

    def _emit_worker_updates(self) -> None:
        """Emit worker status updates for all active trackers (throttled)."""
        now = time.time()
        with self._trackers_lock:
            active = [t for t in self._trackers.values() if not t.done_event.is_set()]
        for tracker in active:
            if tracker.worker_callback and now - tracker._last_worker_update >= 1.0:
                worker_statuses = self._build_worker_statuses()
                tracker.worker_callback(worker_statuses)
                tracker._last_worker_update = now

    def _emit_progress_updates(self) -> None:
        """Emit periodic progress updates for active trackers.

        Factors in per-file progress from busy workers so the job-level
        percentage reflects in-progress work instead of staying at 0%
        until the first file completes.
        """
        now = time.time()
        with self._trackers_lock:
            active = [t for t in self._trackers.values() if not t.done_event.is_set()]
        for tracker in active:
            if tracker.progress_callback and now - tracker._last_progress_update >= 3.0:
                # Include fractional progress from workers actively
                # processing items for this job.
                in_progress_fraction = self._get_in_progress_fraction(tracker.job_id)
                effective = tracker.completed + in_progress_fraction
                percent = (
                    (effective / tracker.total_items * 100)
                    if tracker.total_items > 0
                    else 0
                )
                tracker.progress_callback(
                    tracker.completed,
                    tracker.total_items,
                    f"{tracker.library_prefix}{tracker.completed}/{tracker.total_items} completed",
                    percent_override=percent,
                )
                tracker._last_progress_update = now

    def _get_in_progress_fraction(self, job_id: str) -> float:
        """Sum fractional progress of workers busy on a specific job.

        Each busy worker contributes its per-file progress as a fraction
        of one item (e.g. a worker at 60% contributes 0.6).

        Args:
            job_id: Job identifier to match against workers.

        Returns:
            Sum of fractional item progress across busy workers.
        """
        fraction = 0.0
        for worker in self.worker_pool._snapshot_workers():
            with self.worker_pool._progress_lock:
                is_busy = worker.is_busy
                wjob = worker.current_job_id
                pct = worker.progress_percent
            if is_busy and wjob == job_id and pct > 0:
                fraction += pct / 100.0
        return fraction

    def _build_worker_statuses(self) -> List[dict]:
        """Build the worker status list for the worker_callback."""
        all_workers = self.worker_pool._snapshot_workers()

        type_counters: Dict[str, int] = {}
        worker_type_index: Dict[int, int] = {}
        for w in all_workers:
            type_counters[w.worker_type] = type_counters.get(w.worker_type, 0) + 1
            worker_type_index[w.worker_id] = type_counters[w.worker_type]

        statuses = []
        for worker in all_workers:
            with self.worker_pool._progress_lock:
                progress_data = worker.get_progress_data()
                is_busy = worker.is_busy

            idx = worker_type_index[worker.worker_id]
            gpu_base_name = (worker.gpu_name or "").strip() or f"GPU {worker.gpu_index}"

            if worker.worker_type == "GPU":
                display_name = f"{gpu_base_name} #{idx}"
            elif worker.worker_type == "CPU_FALLBACK":
                display_name = f"CPU Fallback - Worker {idx}"
            else:
                display_name = f"CPU - Worker {idx}"

            statuses.append(
                {
                    "worker_id": worker.worker_id,
                    "worker_type": worker.worker_type,
                    "worker_name": display_name,
                    "status": "processing" if is_busy else "idle",
                    "current_title": worker.media_title if is_busy else "",
                    "library_name": worker.library_name if is_busy else "",
                    "progress_percent": (
                        progress_data["progress_percent"] if is_busy else 0
                    ),
                    "speed": progress_data["speed"] if is_busy else "0.0x",
                    "remaining_time": (
                        progress_data["remaining_time"] if is_busy else 0.0
                    ),
                }
            )
        return statuses

    def _cleanup_done_trackers(self) -> None:
        """Remove trackers that have been done for a while to free memory.

        Keeps done trackers for 60 seconds so callers can still read
        results via ``get_result()`` before they are garbage-collected.
        """
        with self._trackers_lock:
            done_ids = [
                jid for jid, t in self._trackers.items() if t.done_event.is_set()
            ]
            for jid in done_ids:
                still_referenced = any(
                    w.current_job_id == jid and w.is_busy
                    for w in self.worker_pool._snapshot_workers()
                )
                if not still_referenced:
                    tracker = self._trackers[jid]
                    if tracker._done_at is None:
                        tracker._done_at = time.time()
                    elif time.time() - tracker._done_at > 60:
                        del self._trackers[jid]

    def _all_idle(self) -> bool:
        """Check if there is no work left (no items, no busy workers)."""
        if self.worker_pool.has_busy_workers():
            return False
        if not self.worker_pool._check_fallback_queue_empty():
            return False
        with self._trackers_lock:
            for tracker in self._trackers.values():
                if not tracker.done_event.is_set():
                    return False
        return True

    def _log_progress(self) -> None:
        """Log aggregate progress across all active jobs."""
        with self._trackers_lock:
            active = [t for t in self._trackers.values() if not t.done_event.is_set()]
        if not active:
            return
        for tracker in active:
            pct = (
                int(tracker.completed / tracker.total_items * 100)
                if tracker.total_items > 0
                else 0
            )
            logger.info(
                f"Dispatcher progress: job {tracker.job_id[:8]} "
                f"{tracker.completed}/{tracker.total_items} ({pct}%)"
            )


# ---------------------------------------------------------------------------
# Singleton management
# ---------------------------------------------------------------------------

_dispatcher: Optional[JobDispatcher] = None
_dispatcher_lock = threading.Lock()


def get_dispatcher(worker_pool: Optional[WorkerPool] = None) -> Optional[JobDispatcher]:
    """Get or create the global JobDispatcher singleton.

    Args:
        worker_pool: Required on first call to create the dispatcher.
            Subsequent calls ignore this argument.

    Returns:
        The global JobDispatcher, or None if no pool has been provided yet.

    """
    global _dispatcher
    with _dispatcher_lock:
        if _dispatcher is None:
            if worker_pool is None:
                return None
            _dispatcher = JobDispatcher(worker_pool)
            logger.info("Created global JobDispatcher")
        return _dispatcher


def reset_dispatcher() -> None:
    """Reset the global dispatcher (for testing)."""
    global _dispatcher
    with _dispatcher_lock:
        if _dispatcher is not None:
            _dispatcher.shutdown()
        _dispatcher = None
