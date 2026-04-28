"""Slow-backoff retry scheduler for ``SKIPPED_NOT_INDEXED`` publishers.

Real-world scenario this handles:

A webhook arrives (typically from Sonarr/Radarr) immediately after the
file lands on disk. We try to publish to every owning server, but
**Plex** can't accept the publish yet — its bundle hash comes from
``GET /library/metadata/{id}/tree``, which only exists *after* Plex
has scanned the file. The Plex publisher raises
:class:`~plex_generate_previews.servers.LibraryNotYetIndexedError`,
which the dispatcher converts into a ``SKIPPED_NOT_INDEXED`` result.

Without this module, that's where the story ends — the Emby + Jellyfin
publishers succeed, the Plex one is forever stuck waiting for Plex's
own webhook to re-fire (which the user might have disabled). This
scheduler re-runs :func:`~plex_generate_previews.processing.multi_server.process_canonical_path`
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

#: Backoff schedule in seconds for each attempt (1-indexed: ``_BACKOFF[0]``
#: is the delay before attempt #2). Five entries → up to five retries
#: before giving up. Total wall time is ~82 minutes, deliberately past
#: typical Plex full-scan duration on a small library.
_BACKOFF: tuple[int, ...] = (30, 120, 300, 900, 3600)


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
            for path in list(_singleton._timers.keys()):  # noqa: SLF001
                _singleton.cancel(path)
        _singleton = None


def schedule_retry_for_unindexed(
    canonical_path: str,
    *,
    registry: Any,
    config: Any,
    item_id_by_server: dict[str, str] | None = None,
    attempt: int = 1,
) -> bool:
    """Convenience wrapper that schedules a retry calling back into process_canonical_path.

    Lives at module scope so callers don't have to plumb the callback
    through. ``registry`` and ``config`` are captured by the closure;
    avoid passing live objects whose state matters at retry time —
    fresh registry/config snapshots are taken each retry firing by the
    caller's normal path.

    Returns the underlying scheduler's :meth:`RetryScheduler.schedule`
    result.
    """
    scheduler = get_retry_scheduler()

    def _callback(path: str, fired_attempt: int) -> None:
        # Imported lazily to break the import cycle:
        # multi_server -> retry_queue (here) -> multi_server.
        from .multi_server import MultiServerStatus, PublisherStatus, process_canonical_path

        logger.info("Retry #{} firing for {}", fired_attempt, path)
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
            )
        else:
            logger.info("Retry chain for {} complete on attempt #{}", path, fired_attempt)

    return scheduler.schedule(canonical_path, _callback, attempt=attempt)
