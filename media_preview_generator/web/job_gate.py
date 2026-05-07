"""Priority-aware concurrency gate for job activation.

Caps how many jobs can be "actively working" (config-loading, enumerating
paths, submitting items to the dispatcher) at once. The worker pool
below is sized for FFmpeg parallelism; this gate sits above it and
prevents N+1 jobs from stampeding config/API/registry init when a
webhook burst or auto-requeue fires many jobs at once.

The gate sits between ``job_runner._start_job_async``'s daemon-thread
spawn and the ``run_processing(...)`` call — every job thread acquires
a slot before ``run_processing`` and releases in the outer finally.
Jobs that can't acquire stay in ``JobStatus.PENDING`` with a visible
``current_item="Queued — waiting for active slot (X of Y busy)"`` until
a peer finishes.

Design choices (see /home/data/.claude/plans/piped-humming-flame.md for
the full library-review):

* **threading.Condition + priority heap** over BoundedSemaphore (FIFO,
  uninterruptible, immutable cap), over ThreadPoolExecutor (no priority,
  no waiter visibility, immutable max_workers), over external brokers
  (requires Redis/RabbitMQ — overkill for a single-container app).
* **Priority by ``job.priority``** (1=high, 2=normal, 3=low) — matches
  the dispatcher's existing priority semantics so a Sonarr-webhook job
  (pri=normal) can jump past a scheduled full-scan (pri=normal, earlier
  submission) only if promoted to pri=high, while the cap still bounds
  the total concurrency.
* **Cap read on every wake** via ``cap_provider`` callable — the user
  can change ``max_concurrent_jobs`` in Settings and the new value
  takes effect immediately, no restart.
"""

from __future__ import annotations

import heapq
import itertools
import threading
from collections.abc import Callable


class JobGate:
    """Priority-aware concurrency gate with runtime-adjustable cap.

    ``acquire(priority, cancel_check, on_wait)`` blocks until the caller
    is admitted (returns True) or ``cancel_check()`` returns True
    (returns False, no slot consumed). ``release()`` admits the next
    waiter by priority, then FIFO within the same priority.

    The cap is read from ``cap_provider`` on every wake, so setting
    changes take effect within one poll tick (1s) without a restart.
    Values outside ``[1, 10]`` are clamped; non-int values fall back
    to 3. Three layers of defense (this clamp, the settings POST
    validator, the settings GET response clamp) keep the runtime
    value in sync with the UI.
    """

    _CAP_MIN = 1
    _CAP_MAX = 10
    _CAP_DEFAULT = 3
    _POLL_SECONDS = 1.0  # How often a waiter re-checks cancel_check.

    def __init__(self, cap_provider: Callable[[], int]) -> None:
        self._cap_provider = cap_provider
        self._cond = threading.Condition()
        self._active = 0
        self._heap: list[tuple[int, int, object]] = []  # (priority, seq, token)
        self._seq = itertools.count()

    def _cap(self) -> int:
        try:
            return max(self._CAP_MIN, min(self._CAP_MAX, int(self._cap_provider())))
        except (TypeError, ValueError):
            return self._CAP_DEFAULT

    def acquire(
        self,
        priority: int,
        cancel_check: Callable[[], bool],
        on_wait: Callable[[int, int], None] | None = None,
    ) -> bool:
        """Block until admitted or cancelled.

        Args:
            priority: Lower int = higher precedence (1=high, 2=normal,
                3=low). Matches ``job.priority``.
            cancel_check: Called on every poll tick. Returning True
                makes this acquire return False without consuming a
                slot and wakes peers so the next eligible waiter can
                reconsider.
            on_wait: Optional ``(active_count, cap)`` callback fired
                before each ``Condition.wait`` (including the very
                first one) while the waiter is queued. Used by
                ``job_runner`` to update the job's ``current_item``
                so the dashboard shows a live "Queued — X of Y busy"
                message. Fires at most once per wake tick.

        Returns:
            True if admitted (caller must eventually call ``release``),
            False if cancelled (no slot consumed, no release needed).
        """
        token = object()
        with self._cond:
            heapq.heappush(self._heap, (priority, next(self._seq), token))
            while True:
                cap = self._cap()
                # Admission: we're at the head of the priority heap AND
                # a slot is free. Pop under the lock so a concurrent
                # acquire/release can't race us onto the wrong side of
                # the _active counter.
                if self._heap and self._heap[0][2] is token and self._active < cap:
                    heapq.heappop(self._heap)
                    self._active += 1
                    return True
                if cancel_check():
                    # Remove our token from the heap so peers don't
                    # spin waking up trying to admit a no-longer-
                    # present waiter. O(n) but n is bounded by the
                    # number of queued jobs (<100 realistically).
                    self._heap = [entry for entry in self._heap if entry[2] is not token]
                    heapq.heapify(self._heap)
                    self._cond.notify_all()
                    return False
                if on_wait is not None:
                    # Fires BEFORE every wait (including the first) so
                    # the dashboard flips to "Queued — …" the moment a
                    # waiter realises it can't admit, instead of after
                    # the first 1s poll tick. The callback may take
                    # job_manager's lock; safe because we release
                    # _cond during Condition.wait below. No cycle:
                    # no job_manager codepath ever acquires _cond.
                    on_wait(self._active, cap)
                # The 1s poll is our cancel-responsiveness budget.
                # notify_all from release() wakes us sooner — this is
                # the belt-and-braces path.
                self._cond.wait(timeout=self._POLL_SECONDS)

    def release(self) -> None:
        """Release a slot and wake every waiter so the priority-heap
        head can admit itself.

        ``notify_all`` (rather than ``notify``) is required because the
        heap head may have cancelled between our notify and its wake —
        we can't pick "the right waiter" ourselves; each waiter has to
        re-evaluate its own eligibility.
        """
        with self._cond:
            self._active = max(0, self._active - 1)
            self._cond.notify_all()

    def snapshot(self) -> tuple[int, int, int]:
        """Return ``(active_count, waiting_count, cap)`` for observability.

        All three fields are captured under the same lock so the UI
        never sees a torn read where ``active + waiting`` briefly
        exceeds the total job count.
        """
        with self._cond:
            return (self._active, len(self._heap), self._cap())


_gate: JobGate | None = None
_gate_lock = threading.Lock()


def get_job_gate() -> JobGate:
    """Return the process-wide ``JobGate`` singleton.

    Lazy-initialised so tests that never spin up the web app don't
    pay for gate construction. The cap is pulled from
    ``settings_manager`` via a closure so every ``_cap()`` call reads
    the user's current setting — no reload, no restart.
    """
    global _gate
    with _gate_lock:
        if _gate is None:
            from .settings_manager import get_settings_manager

            _gate = JobGate(lambda: get_settings_manager().get("max_concurrent_jobs", 3))
        return _gate


def reset_job_gate() -> None:
    """Drop the singleton. Tests only.

    Required because the gate holds a settings-manager closure; a test
    that swaps the settings manager singleton needs a fresh gate so
    cap reads see the new manager.
    """
    global _gate
    with _gate_lock:
        _gate = None
