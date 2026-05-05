"""Slow-backoff retry scheduler for ``SKIPPED_NOT_INDEXED`` publishers.

Real-world scenario this handles:

A webhook arrives (typically from Sonarr/Radarr) immediately after the
file lands on disk. We try to publish to every owning server, but
**Plex** can't accept the publish yet — its bundle hash comes from
``GET /library/metadata/{id}/tree``, which only exists *after* Plex
has scanned the file. The Plex publisher raises
:class:`~media_preview_generator.servers.LibraryNotYetIndexedError`,
which the dispatcher converts into a ``SKIPPED_NOT_INDEXED`` result.

Without this module, that's where the story ends — the Emby + Jellyfin
publishers succeed, the Plex one is forever stuck waiting for Plex's
own webhook to re-fire (which the user might have disabled). This
scheduler re-runs :func:`~media_preview_generator.processing.multi_server.process_canonical_path`
on a slow backoff so the moment Plex finishes its scan, we publish.

Design choices:

* In-process ``threading.Timer`` rather than APScheduler. Keeps the
  retry self-contained — no jobstore migrations, no persistence
  surprises across restarts. A retry pending at process-restart time
  is dropped; the next webhook for that file (or scheduled scan) will
  pick it up.
* One pending retry per canonical path. Subsequent webhooks for the
  same file while a retry is pending coalesce into the existing timer
  rather than piling up.
* Backoff schedule: 30s, 2m, 5m, 15m, 60m. Five attempts, then give up
  and log. Caps at ~80 minutes which covers slow Plex scans without
  turning into a runaway loop.
* The retry callback calls back into ``process_canonical_path`` —
  which means the journal short-circuit, frame cache, owning-server
  resolution, and per-publisher skip-if-exists all still apply on
  retry, so retries are cheap when the publish has already happened
  through some other channel.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from loguru import logger

#: Backoff schedule in seconds for each attempt (1-indexed: ``BACKOFF_SCHEDULE[0]``
#: is the delay before attempt #2). Five entries → up to five retries
#: before giving up. Total wall time is ~82 minutes, deliberately past
#: typical Plex full-scan duration on a small library.
#:
#: Public so the resolution-step retry-job spawner in
#: ``web/routes/job_runner.py`` can match this cadence (D15) — both code
#: paths are fundamentally "wait for Plex to finish indexing", so they
#: should pace identically and not race each other.
BACKOFF_SCHEDULE: tuple[int, ...] = (30, 120, 300, 900, 3600)
_BACKOFF = BACKOFF_SCHEDULE  # backwards-compat alias


class RetryScheduler:
    """In-process timer-based retry queue keyed by canonical path."""

    def __init__(self) -> None:
        self._timers: dict[str, threading.Timer] = {}
        self._attempts: dict[str, int] = {}
        self._lock = threading.RLock()

    def schedule(
        self,
        canonical_path: str,
        callback: Callable[[str, int], None],
        *,
        attempt: int = 1,
    ) -> bool:
        """Schedule (or replace) a retry for ``canonical_path``.

        ``attempt`` is the *upcoming* attempt number — ``1`` means the
        first retry after the initial publish. Returns ``True`` if a
        retry was scheduled, ``False`` if the max attempt count is
        already exhausted (caller should log + give up).
        """
        if attempt < 1 or attempt > len(_BACKOFF):
            logger.info(
                "Giving up on retry for {} after {} attempt(s)",
                canonical_path,
                attempt - 1,
            )
            return False

        delay = _BACKOFF[attempt - 1]
        with self._lock:
            existing = self._timers.pop(canonical_path, None)
            if existing is not None:
                existing.cancel()

            timer = threading.Timer(delay, self._fire, args=(canonical_path, callback, attempt))
            timer.daemon = True
            self._timers[canonical_path] = timer
            self._attempts[canonical_path] = attempt
            timer.start()

        logger.info(
            "Scheduled retry #{} for {} in {}s",
            attempt,
            canonical_path,
            delay,
        )
        return True

    def cancel(self, canonical_path: str) -> bool:
        """Cancel any pending retry for ``canonical_path``.

        Returns ``True`` when there was a timer to cancel. Used when a
        subsequent successful publish (perhaps via a different webhook
        source) makes a pending retry redundant.
        """
        with self._lock:
            timer = self._timers.pop(canonical_path, None)
            self._attempts.pop(canonical_path, None)
        if timer is None:
            return False
        timer.cancel()
        return True

    def pending_count(self) -> int:
        """Number of canonical paths with a retry currently pending."""
        with self._lock:
            return len(self._timers)

    def _fire(self, canonical_path: str, callback: Callable[[str, int], None], attempt: int) -> None:
        """Timer thread entry point. Cleans up state then runs the callback.

        Exceptions in the callback are caught + logged so a buggy
        publisher can't kill the retry timer thread.
        """
        with self._lock:
            self._timers.pop(canonical_path, None)
            self._attempts.pop(canonical_path, None)
        try:
            callback(canonical_path, attempt)
        except Exception as exc:
            logger.exception(
                "Retry #{} for {} hit an unexpected error and was abandoned ({}: {}). "
                "This file won't be retried again automatically — re-trigger it via webhook or by re-running "
                "the relevant library job. Please report this with the traceback above as it indicates a bug.",
                attempt,
                canonical_path,
                type(exc).__name__,
                exc,
            )


_singleton: RetryScheduler | None = None
_singleton_lock = threading.Lock()


def get_retry_scheduler() -> RetryScheduler:
    """Return the process-wide :class:`RetryScheduler` (lazily constructed)."""
    global _singleton
    with _singleton_lock:
        if _singleton is None:
            _singleton = RetryScheduler()
        return _singleton


def reset_retry_scheduler() -> None:
    """Drop the singleton — used by tests to start with a clean slate."""
    global _singleton
    with _singleton_lock:
        if _singleton is not None:
            # Snapshot the timer keys under the singleton's own lock so
            # an in-flight _fire callback mutating the dict can't trip
            # "dictionary changed size during iteration" on .keys().
            with _singleton._lock:  # noqa: SLF001
                paths = list(_singleton._timers.keys())  # noqa: SLF001
            for path in paths:
                _singleton.cancel(path)
        _singleton = None


def schedule_retry_for_unindexed(
    canonical_path: str,
    *,
    registry: Any,
    config: Any,
    item_id_by_server: dict[str, str] | None = None,
    attempt: int = 1,
    server_id_filter: str | None = None,
) -> bool:
    """Convenience wrapper that schedules a retry calling back into process_canonical_path.

    Lives at module scope so callers don't have to plumb the callback
    through. ``registry`` and ``config`` are captured by the closure;
    avoid passing live objects whose state matters at retry time —
    fresh registry/config snapshots are taken each retry firing by the
    caller's normal path.

    ``server_id_filter`` MUST match the value that was passed to the
    original :func:`process_canonical_path` invocation that hit
    ``SKIPPED_NOT_INDEXED``. The dispatch pin is derived from two
    sources (final-audit MED finding):

    * ``config.server_id_filter`` — set when the job-level config
      pinned to one server (vendor webhooks always hit this path).
    * Worker / orchestrator originator-derived pin — set when the
      caller routed an item via its non-Plex originator's id (worker.py
      "case 2" — ``per_item_pin = item.server_id``).

    Reading the pin off ``config`` alone misses the second case; the
    retry would then fan out to every owning server, defeating the
    M4 contract that originator-pinned webhooks publish only to the
    originator. Pass it explicitly here so retries inherit it.

    Returns the underlying scheduler's :meth:`RetryScheduler.schedule`
    result.
    """
    scheduler = get_retry_scheduler()

    def _callback(path: str, fired_attempt: int) -> None:
        # Imported lazily to break the import cycle:
        # multi_server -> retry_queue (here) -> multi_server.
        from .generator import failure_scope
        from .multi_server import MultiServerStatus, PublisherStatus, process_canonical_path

        # K2: include server context. ``server_id_filter`` is the
        # authoritative pin for this retry chain — prefer it over the
        # legacy config.server_display_name when emitting the log tag.
        _retry_pin_id = server_id_filter or None
        _retry_server_tag = _retry_pin_id or getattr(config, "server_display_name", None)
        if _retry_server_tag:
            logger.info("Retry #{} firing for {} (server={})", fired_attempt, path, _retry_server_tag)
        else:
            logger.info("Retry #{} firing for {}", fired_attempt, path)

        # Bind a synthetic failure scope for the retry. The original
        # job has long completed by the time this APScheduler timer
        # fires, so there's no active job_id to attribute failures to.
        # A synthetic ``retry:<path>`` scope keeps ``record_failure``
        # from logging the "Internal bookkeeping bug" warning every
        # time a retry hits an FFmpeg failure (e.g. file deleted
        # between scan and retry, codec gone unsupported after a
        # driver update). The recorded failures are detached from
        # any user-visible job summary — that's correct: retries
        # are headless from the JobManager's perspective.
        retry_scope_id = f"retry:{path}"
        with failure_scope(retry_scope_id):
            try:
                result = process_canonical_path(
                    canonical_path=path,
                    registry=registry,
                    config=config,
                    item_id_by_server=item_id_by_server,
                    # The retry callback manages its own scheduling — don't
                    # let process_canonical_path spawn yet another timer
                    # alongside ours.
                    schedule_retry_on_not_indexed=False,
                    retry_attempt=fired_attempt,
                    # Use the explicit pin captured at schedule time
                    # — covers BOTH config-pinned (vendor webhook)
                    # and originator-pinned (worker case 2) dispatches.
                    server_id_filter=server_id_filter,
                )
            except Exception as exc:
                logger.exception(
                    "Retry #{} for {} could not be dispatched ({}: {}). "
                    "Scheduling another retry — if this keeps happening, the underlying error needs investigation; "
                    "share the traceback above when reporting.",
                    fired_attempt,
                    path,
                    type(exc).__name__,
                    exc,
                )
                schedule_retry_for_unindexed(
                    path,
                    registry=registry,
                    config=config,
                    item_id_by_server=item_id_by_server,
                    attempt=fired_attempt + 1,
                    server_id_filter=server_id_filter,
                )
                return

            # Did any publisher remain SKIPPED_NOT_INDEXED? If so, schedule
            # another retry; otherwise we're done. PUBLISHED, FAILED, and
            # SKIPPED_OUTPUT_EXISTS all terminate the retry chain.
            still_unindexed = any(p.status is PublisherStatus.SKIPPED_NOT_INDEXED for p in result.publishers)
            if still_unindexed and result.status is not MultiServerStatus.FAILED:
                schedule_retry_for_unindexed(
                    path,
                    registry=registry,
                    config=config,
                    item_id_by_server=item_id_by_server,
                    attempt=fired_attempt + 1,
                    server_id_filter=server_id_filter,
                )
            else:
                logger.info("Retry chain for {} complete on attempt #{}", path, fired_attempt)

    return scheduler.schedule(canonical_path, _callback, attempt=attempt)
