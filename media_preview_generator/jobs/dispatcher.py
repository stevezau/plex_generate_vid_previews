"""Multi-job dispatcher for concurrent job processing.

Provides a persistent dispatch loop and shared worker pool so multiple jobs
can run simultaneously.  Items are dispatched using priority-aware drain-first
scheduling: workers focus on the highest-priority active job and only spill
over to the next job when the current job's queue is empty.
"""

import threading
import time
from collections import deque
from collections.abc import Callable
from functools import partial
from typing import Any

from loguru import logger

from ..config import Config
from ..processing.generator import ProcessingResult
from ..web.jobs import PRIORITY_NORMAL
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
        items: List of :class:`ProcessableItem` instances.
        config: Config object for this job's processing.
        registry: Live :class:`ServerRegistry` — publishers fan out via this.
        title_max_width: Max display width for titles.
        library_name: Library name for log prefixes.
        callbacks: Dict of per-job callback functions.
        priority: Dispatch priority (1=high, 2=normal, 3=low).
    """

    def __init__(
        self,
        job_id: str,
        items: list,
        config: Config,
        registry,
        title_max_width: int = 20,
        library_name: str = "",
        callbacks: dict[str, Any] | None = None,
        priority: int = PRIORITY_NORMAL,
    ):
        """Initialize tracker for a single job."""
        self.job_id = job_id
        self.priority = priority
        self.submission_order = _next_submission_order()
        self.config = config
        self.registry = registry
        self.title_max_width = title_max_width
        self.library_name = library_name
        self.library_prefix = f"[{library_name}] " if library_name else ""

        self.item_queue: deque = deque(items)
        self.total_items = len(items)
        self.successful = 0
        self.failed = 0
        self.cancelled = False
        self.outcome_counts: dict[str, int] = {r.value: 0 for r in ProcessingResult}
        self.done_event = threading.Event()

        cbs = callbacks or {}
        self.progress_callback: Callable | None = cbs.get("progress_callback")
        self.worker_callback: Callable | None = cbs.get("worker_callback")
        self.on_item_complete: Callable | None = cbs.get("on_item_complete")
        self.cancel_check: Callable | None = cbs.get("cancel_check")
        self.pause_check: Callable | None = cbs.get("pause_check")

        # Throttle timestamps for callbacks
        self._last_progress_update = 0.0
        self._last_worker_update = 0.0
        # Set when all items are done; used by _cleanup_done_trackers
        self._done_at: float | None = None

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

    def wait(self, timeout: float | None = None) -> bool:
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
        self._trackers: dict[str, JobTracker] = {}
        self._trackers_lock = threading.RLock()
        self._dispatch_thread: threading.Thread | None = None
        self._has_work = threading.Event()
        self._shutdown = False
        # Signalled by worker threads on completion so the dispatch loop
        # wakes immediately instead of sleeping through the full cycle.
        self._worker_done = threading.Event()
        worker_pool._worker_done_event = self._worker_done
        # Backfill existing workers that were created before this event existed
        for w in worker_pool._snapshot_workers():
            w._done_event = self._worker_done

    def submit_items(
        self,
        job_id: str,
        items: list,
        config: Config,
        registry,
        title_max_width: int = 20,
        library_name: str = "",
        callbacks: dict[str, Any] | None = None,
        priority: int = PRIORITY_NORMAL,
    ) -> JobTracker:
        """Submit items for a job to the shared dispatch queue.

        Args:
            job_id: Unique job identifier.
            items: List of :class:`ProcessableItem` instances.
            config: Configuration for processing these items.
            registry: Live :class:`ServerRegistry` — publishers fan out via this.
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
            registry=registry,
            title_max_width=title_max_width,
            library_name=library_name,
            callbacks=callbacks,
            priority=priority,
        )
        with self._trackers_lock:
            self._trackers[job_id] = tracker
        logger.info(
            "Dispatcher: submitted {} items for job {} ({})", len(items), job_id[:8], library_name or "no library"
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
            logger.info("Dispatcher: cancelled job {}", job_id[:8])

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
        self._dispatch_thread = threading.Thread(target=self._dispatch_loop, daemon=True, name="job-dispatcher")
        self._dispatch_thread.start()

    def _dispatch_loop(self) -> None:
        """Persistent loop: check completions, assign tasks, sleep adaptively.

        Uses ``_worker_done`` to wake immediately when a worker thread
        finishes, which is critical for fast-completing tasks like
        BIF-exists skips that would otherwise sit idle for the full
        sleep duration (~5 ms) per item.
        """
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

            # 6. Periodic progress logging
            now = time.time()
            if now - last_progress_log >= 5.0:
                self._log_progress()
                last_progress_log = now

            # 7. Clean up completed trackers and check if loop can idle
            self._cleanup_done_trackers()
            if self._all_idle():
                self._has_work.clear()

            # Event-based sleep: wake immediately when any worker
            # completes instead of burning a fixed 5 ms.  Falls back
            # to a longer idle sleep when no workers are active.
            if self.worker_pool.has_busy_workers():
                self._worker_done.wait(timeout=0.005)
                self._worker_done.clear()
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
                    "Dispatcher: job {} cancelled ({}/{} done)",
                    tracker.job_id[:8],
                    tracker.completed,
                    tracker.total_items,
                )

    def _check_completions(self) -> int:
        """Check all workers for completed tasks and update the owning tracker.

        Returns:
            Number of workers that completed since last check.
        """
        reaped = 0
        for worker in self.worker_pool._snapshot_workers():
            if not worker.check_completion():
                continue

            reaped += 1
            title = worker.media_title or "(unknown)"
            job_id = worker.current_job_id

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
                    "Dispatcher: {} completed {} ({}) — no active tracker for job_id={}",
                    worker.display_name,
                    title,
                    outcome,
                    job_id,
                )

            # Retire deferred-removal workers
            self.worker_pool._retire_idle_worker_if_scheduled(worker)
        return reaped

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
            # Atomic claim closes the race vs. _process_items_loop
            # (the worker pool's own consumer) — both used to find the
            # same idle worker and the loser tripped "already busy".
            worker = self.worker_pool._find_available_worker(claim=True)
            if not worker:
                break

            # Pull next item (highest priority, then oldest submission).
            picked = self._get_next_item()
            if not picked:
                # Nothing to do — release the pre-claim.
                worker.is_busy = False
                break

            job_id, item, library_name = picked
            with self._trackers_lock:
                tracker = self._trackers.get(job_id)
            if not tracker:
                # Tracker disappeared between pick and lookup — release the
                # pre-claim and try again with the next available worker.
                worker.is_busy = False
                continue

            progress_callback = partial(self.worker_pool._update_worker_progress, worker)
            worker.assign_task(
                item,
                tracker.config,
                tracker.registry,
                progress_callback=progress_callback,
                title_max_width=tracker.title_max_width,
                job_id=job_id,
                library_name=library_name,
                cancel_check=tracker.cancel_check,
            )
            logger.info(
                "Dispatch: assigned canonical item {!r} (job {}) to {}",
                item.canonical_path,
                job_id[:8],
                worker.display_name,
            )

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

    def _get_next_item(self) -> tuple[str, Any, str] | None:
        """Get the next item using priority-aware drain-first scheduling.

        Picks from the highest-priority active job first (lowest number).
        Within the same priority, earlier submissions are preferred.

        Returns:
            ``(job_id, item, library_name)`` or ``None``. ``item`` is a
            :class:`ProcessableItem`; ``library_name`` always blank for now
            (the canonical-path flow doesn't carry a per-item library tag at
            dispatch time).
        """
        with self._trackers_lock:
            eligible = [
                t
                for t in self._trackers.values()
                if not t.done_event.is_set() and not t.is_paused() and not t.is_cancelled() and t.item_queue
            ]
            eligible.sort(key=lambda t: (t.priority, t.submission_order))
            for tracker in eligible:
                item = tracker.item_queue.popleft()
                return (tracker.job_id, item, "")
        return None

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
                percent = (effective / tracker.total_items * 100) if tracker.total_items > 0 else 0
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

    def _build_worker_statuses(self) -> list[dict]:
        """Build the worker status list for the worker_callback."""
        all_workers = self.worker_pool._snapshot_workers()

        type_counters: dict[str, int] = {}
        worker_type_index: dict[int, int] = {}
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
                    "progress_percent": (progress_data["progress_percent"] if is_busy else 0),
                    "speed": progress_data["speed"] if is_busy else "0.0x",
                    "remaining_time": (progress_data["remaining_time"] if is_busy else 0.0),
                    "fallback_active": bool(getattr(worker, "fallback_active", False)),
                    "fallback_reason": getattr(worker, "fallback_reason", None),
                }
            )
        return statuses

    def _cleanup_done_trackers(self) -> None:
        """Remove trackers that have been done for a while to free memory.

        Keeps done trackers for 60 seconds so callers can still read
        results via ``get_result()`` before they are garbage-collected.
        """
        with self._trackers_lock:
            done_ids = [jid for jid, t in self._trackers.items() if t.done_event.is_set()]
            for jid in done_ids:
                still_referenced = any(
                    w.current_job_id == jid and w.is_busy for w in self.worker_pool._snapshot_workers()
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
            pct = int(tracker.completed / tracker.total_items * 100) if tracker.total_items > 0 else 0
            logger.info(
                "Dispatcher progress: job {} {}/{} ({}%)",
                tracker.job_id[:8],
                tracker.completed,
                tracker.total_items,
                pct,
            )


# ---------------------------------------------------------------------------
# Singleton management
# ---------------------------------------------------------------------------

_dispatcher: JobDispatcher | None = None
_dispatcher_lock = threading.Lock()


def get_dispatcher(worker_pool: WorkerPool | None = None) -> JobDispatcher | None:
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
