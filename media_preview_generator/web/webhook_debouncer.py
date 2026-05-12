"""Per-app webhook debounce + dedup state.

Encapsulates three pieces of state that were previously module-level
globals in ``webhooks.py``:

* ``pending_timers`` — :class:`threading.Timer` instances keyed by
  debounce key (``"sonarr"``, ``"radarr"``, ``"plex:server_id"``, …).
* ``pending_batches`` — payload-aggregation dicts keyed by the same
  debounce key. Each batch carries the file paths, source title, server
  pin, and the Job id created at batch-open.
* ``recent_dispatches`` — TTL'd dedup table keyed by
  ``(source, server_id, path)``. Prevents duplicate Plex
  ``library.new`` events from spawning two jobs for the same file.

Migration rationale (architectural refactor, 2026-05-12)
--------------------------------------------------------

Pre-refactor: these three dicts lived as module-level globals in
``webhooks.py`` with a single shared ``threading.Lock``. The test
suite had **19 separate ``_reset_singletons`` fixtures** that each
manually cleared the globals — the test code shouting that the
production code had hidden coupling. Under
``pytest-xdist -n auto`` the coupling surfaced as the canary
``test_run_now_creates_job_in_active_panel`` flaking 1/3 runs:
Timer threads scheduled by one test fired into the next test's
JobManager, batches survived ``/api/__test/reset``, and the
dedup table dropped follow-on webhooks as duplicates.

Post-refactor: one instance per :class:`flask.Flask` app, stored at
``app.extensions["webhook_debouncer"]``. New Flask app → new
instance → guaranteed clean state. The 19 reset fixtures collapse
to a single conftest fixture that constructs a fresh Flask app
per test (the standard pytest-flask pattern).

Threading model
---------------

The debouncer's :class:`threading.Lock` protects the three dicts.
Callers can either:

* Call a high-level method (``check_and_record_dedup``,
  ``fire_now``, ``cancel_all``, ``pending_snapshot``) that acquires
  the lock internally, OR
* Use the debouncer as a context manager
  (``with current_app.extensions["webhook_debouncer"] as d:``)
  and access ``d.pending_timers`` / ``d.pending_batches`` /
  ``d.recent_dispatches`` directly. This is the compound-operation
  path used by ``_schedule_webhook_job`` where dedup + timer-cancel +
  batch-update + timer-schedule must happen atomically.

Background-thread access (from :class:`threading.Timer` callbacks)
must wrap the lookup in ``with app.app_context():`` because Flask's
``current_app`` is a request-bound proxy. See ``_execute_webhook_job``
in ``webhooks.py`` for the pattern.
"""

from __future__ import annotations

import threading
from typing import Any

# How long a (source, server_id, path) dispatch is remembered as
# "recent" for dedup. Plex's ``library.new`` re-fires after metadata
# refreshes — anything inside this window is dropped.
RECENT_DISPATCH_TTL_SECONDS = 600


class WebhookDebouncer:
    """Per-Flask-app webhook batching + dedup state.

    See module docstring for the architectural context. Each Flask
    app constructs exactly one instance via ``WebhookDebouncer()``
    and stores it at ``app.extensions["webhook_debouncer"]``.

    Thread safety: every access (read or write) must hold
    :attr:`_lock` (acquired via ``with debouncer:`` for compound
    operations, or via the high-level methods which acquire
    internally).
    """

    def __init__(self) -> None:
        # Each dict is fresh per instance. No module-global state.
        self.pending_timers: dict[str, threading.Timer] = {}
        self.pending_batches: dict[str, dict[str, Any]] = {}
        # Dedup key shape: (source, server_id_or_empty, normalized_path).
        # server_id is "" rather than None so the key is hashable + the
        # dict's __eq__ short-circuits cleanly.
        self.recent_dispatches: dict[tuple[str, str, str], float] = {}
        self._lock = threading.Lock()

    # -- context-manager: compound-operation atomicity ------------------

    def __enter__(self) -> WebhookDebouncer:
        """Acquire the internal lock for a compound operation.

        Use when multiple reads + writes must happen atomically — e.g.
        the ``_schedule_webhook_job`` flow that checks dedup, cancels
        an existing timer, creates-or-extends a batch, and schedules
        a new timer all under one lock acquisition. Mirrors the
        pre-refactor ``with _pending_lock:`` pattern.
        """
        self._lock.acquire()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self._lock.release()

    # -- dedup ----------------------------------------------------------

    def check_and_record_dedup(self, source: str, server_id: str | None, path: str, now_ts: float) -> int | None:
        """Atomic: prune-expired, check-recent, record-now.

        Returns ``None`` when ``(source, server_id, path)`` was NOT
        recently dispatched (i.e. the caller should process the
        webhook). Returns the age in seconds when it WAS — caller
        should drop the webhook as a duplicate.

        Caller MUST NOT hold ``self._lock`` — this method acquires
        it. Callers inside a ``with debouncer:`` block must use
        :meth:`_check_and_record_dedup_locked` instead; calling this
        method while holding the lock will deadlock (the lock is
        non-reentrant :class:`threading.Lock`, by design — we want a
        single shared lock for the compound-operation path in
        ``_schedule_webhook_job``, and ``RLock`` would mask
        accidental nested-acquire bugs).
        """
        with self._lock:
            return self._check_and_record_dedup_locked(source, server_id, path, now_ts)

    def _check_and_record_dedup_locked(
        self, source: str, server_id: str | None, path: str, now_ts: float
    ) -> int | None:
        """Same as :meth:`check_and_record_dedup` but assumes the caller
        already holds ``self._lock`` (e.g. inside a ``with debouncer:``
        block).

        Splitting the locking from the logic lets the compound-operation
        path in ``_schedule_webhook_job`` keep its single-lock atomicity
        without re-acquiring (which would deadlock against a non-RLock).
        """
        # Opportunistic TTL eviction. Done here (not on a timer) so the
        # table stays bounded on long-running installs without any
        # background thread overhead.
        expired = [k for k, ts in self.recent_dispatches.items() if now_ts - ts >= RECENT_DISPATCH_TTL_SECONDS]
        for k in expired:
            self.recent_dispatches.pop(k, None)
        dedup_key = (source, server_id or "", path)
        last = self.recent_dispatches.get(dedup_key)
        if last is not None and (now_ts - last) < RECENT_DISPATCH_TTL_SECONDS:
            return int(now_ts - last)
        self.recent_dispatches[dedup_key] = now_ts
        return None

    # -- pending-state snapshot for /api/webhooks/pending ---------------

    def pending_snapshot(self) -> list[dict[str, Any]]:
        """Read-only snapshot of every pending batch. Used by the
        ``/api/webhooks/pending`` endpoint for the Webhooks page.

        Returns a list of dicts (one per batch) with: ``key``,
        ``source``, ``file_count``, ``fire_at``, ``first_title``.
        Snapshot is taken under the lock; the returned list is safe
        to inspect outside.
        """
        from datetime import datetime, timezone

        with self._lock:
            now_ts = datetime.now(timezone.utc).timestamp()
            out: list[dict[str, Any]] = []
            for key, batch in self.pending_batches.items():
                fire_at = batch.get("fire_at")
                fire_at_iso: str | None = None
                remaining: float | None = None
                if isinstance(fire_at, int | float):
                    fire_at_iso = datetime.fromtimestamp(fire_at, tz=timezone.utc).isoformat()
                    remaining = round(fire_at - now_ts, 1)
                titles = batch.get("titles") or []
                first_title = titles[0] if titles else None
                out.append(
                    {
                        "key": key,
                        "source": batch.get("source"),
                        "file_count": len(batch.get("file_paths", ())),
                        "fire_at": fire_at_iso,
                        "remaining_seconds": remaining,
                        "first_title": first_title,
                    }
                )
            return out

    # -- fire-now (operator action) -------------------------------------

    def fire_now(self, debounce_key: str) -> dict[str, Any] | None:
        """Cancel the pending timer + atomically pop the batch.

        Returns the popped batch dict (caller dispatches it on a
        fresh thread), or ``None`` when nothing is pending for
        ``debounce_key``. Idempotent: a second call returns ``None``.
        """
        with self._lock:
            timer = self.pending_timers.pop(debounce_key, None)
            batch = self.pending_batches.pop(debounce_key, None)
        if timer is not None:
            # Cancel outside the lock — Timer.cancel() is safe to call
            # without the debouncer lock; it acquires its own internal
            # state lock.
            timer.cancel()
        return batch

    # -- timer-callback path (atomic pop, no cancel) --------------------

    def pop_for_fire(self, debounce_key: str) -> dict[str, Any] | None:
        """Atomic: pop the batch + pending-timer entries.

        Called from inside the Timer's own callback — the caller IS the
        timer, so there's nothing to cancel. Returns the batch dict to
        process, or ``None`` if the entry was already gone (race with
        a concurrent :meth:`fire_now` for the same key).
        """
        with self._lock:
            batch = self.pending_batches.pop(debounce_key, None)
            self.pending_timers.pop(debounce_key, None)
        return batch

    # -- bulk teardown for tests + graceful shutdown --------------------

    def cancel_all(self) -> None:
        """Cancel every pending Timer + wipe all state.

        Test-only / shutdown-only. The webhook flow never calls this —
        a fresh batch should never need to nuke prior batches because
        per-app isolation already guarantees clean state at boot.

        Used by:
        * The ``/api/__test/reset`` endpoint (post-refactor: this is
          the only test-only entry point).
        * App teardown if a graceful-shutdown handler is wired.

        Best-effort: a Timer mid-execution may raise on cancel; we
        swallow so the rest of the cleanup still runs.
        """
        with self._lock:
            timers = list(self.pending_timers.values())
            self.pending_timers.clear()
            self.pending_batches.clear()
            self.recent_dispatches.clear()
        for timer in timers:
            try:
                timer.cancel()
            except Exception:
                # Mid-execution Timer can raise; the cancel was best-
                # effort anyway, and the dict references are already
                # gone so the Timer can't re-enter the debouncer.
                pass


def get_webhook_debouncer() -> WebhookDebouncer:
    """Convenience accessor: ``current_app.extensions["webhook_debouncer"]``.

    Lookups via ``current_app`` require an active Flask request
    context. Background threads (e.g. :class:`threading.Timer`
    callbacks scheduled by ``_schedule_webhook_job``) must wrap the
    call in ``with app.app_context():`` first — see the helper
    ``_execute_webhook_job`` in ``webhooks.py`` for the pattern.

    Raises :class:`KeyError` if no debouncer is registered. That
    indicates a misconfigured Flask app — ``create_app()`` is
    supposed to register one at boot.
    """
    from flask import current_app

    debouncer = current_app.extensions.get("webhook_debouncer")
    if debouncer is None:
        raise KeyError(
            "No 'webhook_debouncer' registered on the Flask app. "
            "create_app() is supposed to construct WebhookDebouncer() "
            "and store it on app.extensions — this lookup failed because "
            "either app.extensions is empty or the app was constructed "
            "by a path that bypassed wiring (e.g. a test using a "
            "hand-built Flask without calling create_app())."
        )
    return debouncer
