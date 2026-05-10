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

import os
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
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


def _upsert_retry_chain_job(
    *,
    canonical_path: str,
    attempt: int,
    outcome: str,
    wait_seconds: int | None = None,
    server_id: str | None = None,
    server_name: str | None = None,
    server_type: str | None = None,
    reason: str | None = None,
    display_name: str | None = None,
    source: str | None = None,
) -> None:
    """Best-effort upsert of the user-visible retry-chain Job row.

    Wraps :meth:`JobManager.upsert_retry_chain_job` so failures inside
    the retry callback path don't propagate (the JobManager singleton
    is set up by the web app — when retries fire from a non-web context
    like a CLI smoke test or a test harness, the import or the call
    might fail; the retry itself must keep going regardless).

    ``display_name`` is the cleaned title the originating webhook job
    used (e.g. ``"Deadliest Catch S22E01"``) — without it the retry-
    chain row falls back to the raw filename
    (``Deadliest Catch (2005) - S22E01 - Kings of the Frozen North
    [WEBDL-1080p]…SNAKE.mkv``), which the user reads as a different
    item from its parent dispatch row. ``source`` is the trigger pill
    label (``"sonarr"``, ``"radarr"``, ``"sportarr"``, ``"plex"``, …)
    and feeds the same UI fallback ``_serverBadge()`` uses when
    ``server_type`` is empty.
    """
    try:
        import os as _os
        from datetime import datetime as _dt
        from datetime import timedelta as _td
        from datetime import timezone as _tz

        from ..web.jobs import get_job_manager

        next_run_at: str | None = None
        if outcome == "scheduled" and wait_seconds is not None:
            next_run_at = (_dt.now(_tz.utc) + _td(seconds=int(wait_seconds))).isoformat()
        get_job_manager().upsert_retry_chain_job(
            canonical_path=canonical_path,
            basename=display_name or _os.path.basename(canonical_path) or canonical_path,
            attempt=attempt,
            max_attempts=len(_BACKOFF),
            next_run_at=next_run_at,
            wait_seconds=wait_seconds,
            outcome=outcome,
            server_id=server_id,
            server_name=server_name,
            server_type=server_type,
            reason=reason,
            source=source,
        )
    except Exception as exc:
        logger.debug("Retry-chain Job upsert failed for {!r}: {}", canonical_path, exc)


def _create_retry_attempt_job(
    *,
    canonical_path: str,
    chain_id: str,
    attempt: int,
    max_attempts: int,
    server_id: str | None,
    server_name: str | None,
    server_type: str | None,
    display_name: str | None,
    source: str | None,
) -> str | None:
    """Create a real per-attempt Job for one retry firing.

    Each retry firing used to run ``process_canonical_path`` directly
    inside the timer thread with no Job context — meaning the only
    "log" the user could see for that attempt was the synthesized
    status text on the parent retry-chain row, which had no
    DEBUG/INFO/WARNING prefixes and so rendered without the colour
    coding ``colorizeLogLine`` applies to every other job's logs.

    By creating a real Job here (and attaching a loguru sink filtered
    to the firing thread, see :func:`_capture_attempt_logs`), every
    ``logger.info`` call inside ``process_canonical_path`` lands in the
    Job's log file with a proper level prefix — so the per-attempt
    log looks identical to a webhook-dispatch job's log.

    Returns the new Job's UUID, or ``None`` when the JobManager isn't
    available (CLI smoke tests, unit tests that didn't bootstrap the
    web layer). The retry still proceeds in that case — log capture
    is best-effort and must not break the retry chain.
    """
    try:
        from ..web.jobs import get_job_manager

        basename = display_name or os.path.basename(canonical_path) or canonical_path
        attempt_config: dict[str, Any] = {
            "is_retry_attempt": True,
            "parent_chain_id": chain_id,
            "retry_chain_for": canonical_path,
            "retry_attempt": attempt,
            "retry_max_attempts": int(max_attempts),
            "retry_basename": basename,
        }
        if source:
            attempt_config["source"] = source
        job = get_job_manager().create_job(
            library_name=f"Retry attempt {attempt}/{max_attempts}: {basename}",
            config=attempt_config,
            server_id=server_id,
            server_name=server_name,
            server_type=server_type,
        )
        return job.id
    except Exception as exc:
        logger.debug("Per-attempt Job creation failed for {!r}: {}", canonical_path, exc)
        return None


@contextmanager
def _capture_attempt_logs(child_job_id: str | None) -> Iterator[None]:
    """Pipe ``logger.*`` calls in this thread into the child Job's log file.

    Mirrors the per-job log capture in ``web/routes/job_runner.py``
    (``log_sink`` + ``job_thread_filter``) so the user opens the
    attempt Job from the Jobs panel and sees the same ``INFO -``,
    ``WARNING -``, ``ERROR -`` level prefixes the dashboard already
    knows how to colourise. Without this, ``process_canonical_path``'s
    INFO output would only land in the global container log, not the
    per-attempt Job log.

    Setup errors (JobManager unavailable in CLI / test contexts,
    ``logger.add`` failing, …) are caught BEFORE the yield so the body
    still runs without log capture. The yield itself is wrapped in a
    bare ``try / finally`` — wrapping it in ``try / except`` would
    swallow exceptions that the contextmanager protocol re-raises at
    the yield point via ``gen.throw``, hiding the body's real failure
    and tripping a "generator didn't stop after throw" RuntimeError.
    """
    if not child_job_id:
        yield
        return

    sink_id: int | None = None
    registered = False

    try:
        from ..jobs.worker import is_job_thread_for, register_job_thread
        from ..web.jobs import get_job_manager

        job_manager = get_job_manager()
        register_job_thread(child_job_id)
        registered = True

        def _sink(message: Any) -> None:
            record = message.record
            log_text = f"{record['level'].name} - {record['message']}"
            try:
                job_manager.add_log(child_job_id, log_text)
            except Exception:
                pass

        def _filter(record: dict) -> bool:
            return is_job_thread_for(record["thread"].id, child_job_id)

        sink_id = logger.add(_sink, level="INFO", format="{message}", filter=_filter, enqueue=True)
    except Exception as exc:
        logger.debug("Attempt log capture setup failed for {!r}: {}", child_job_id, exc)

    try:
        yield
    finally:
        # ``logger.remove`` on an enqueue=True sink synchronously waits
        # for the worker thread to drain its queue, so the closure
        # capturing ``job_manager`` is released before the context
        # manager exits — no per-firing leak.
        if sink_id is not None:
            try:
                logger.remove(sink_id)
            except (ValueError, KeyError):
                pass
        if registered:
            try:
                from ..jobs.worker import unregister_job_thread

                unregister_job_thread()
            except Exception:
                pass


def _complete_retry_attempt_job(
    child_job_id: str | None,
    *,
    error: str | None = None,
    warning: str | None = None,
) -> None:
    """Mark the per-attempt Job done. Best-effort — ignored when missing."""
    if not child_job_id:
        return
    try:
        from ..web.jobs import get_job_manager

        get_job_manager().complete_job(child_job_id, error=error, warning=warning)
    except Exception as exc:
        logger.debug("Per-attempt Job completion failed for {!r}: {}", child_job_id, exc)


def _link_attempt_to_chain(canonical_path: str, attempt_job_id: str | None) -> None:
    """Append ``attempt_job_id`` to the parent chain's ``child_job_ids`` config.

    The chain row's synthesized log uses this list to hand the user
    direct UUIDs they can paste into the Jobs panel filter to find the
    real per-attempt log file. Without it the chain is a dead-end
    summary with no way back to the actual retry execution.
    """
    if not attempt_job_id:
        return
    try:
        import hashlib

        from ..web.jobs import get_job_manager

        chain_id = "retry-" + hashlib.sha256(canonical_path.encode("utf-8")).hexdigest()[:16]
        jm = get_job_manager()
        chain = jm.get_job(chain_id)
        if chain is None:
            return
        existing = list(chain.config.get("child_job_ids") or [])
        if attempt_job_id not in existing:
            existing.append(attempt_job_id)
            new_cfg = dict(chain.config)
            new_cfg["child_job_ids"] = existing
            jm.update_job_config(chain_id, new_cfg)
    except Exception as exc:
        logger.debug("Link attempt {!r} → chain failed: {}", attempt_job_id, exc)


def schedule_retry_for_unindexed(
    canonical_path: str,
    *,
    registry: Any,
    config: Any,
    item_id_by_server: dict[str, str] | None = None,
    attempt: int = 1,
    server_id_filter: str | None = None,
    display_name: str | None = None,
    source: str | None = None,
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
        import hashlib

        # Imported lazily to break the import cycle:
        # multi_server -> retry_queue (here) -> multi_server.
        from .generator import failure_scope
        from .multi_server import MultiServerStatus, PublisherStatus, process_canonical_path

        # K2: include server context. ``server_id_filter`` is the
        # authoritative pin for this retry chain — prefer it over the
        # legacy config.server_display_name when emitting the log tag.
        _retry_pin_id = server_id_filter or None
        _retry_server_tag = _retry_pin_id or getattr(config, "server_display_name", None)

        # Surface the retry to the user-visible Jobs panel — countdown
        # ends, status flips to "running" so the row no longer shows
        # "next in Xs" while the dispatch is actually executing.
        _upsert_retry_chain_job(
            canonical_path=path,
            attempt=fired_attempt,
            outcome="running",
            server_id=_retry_pin_id,
            display_name=display_name,
            source=source,
        )

        # Spawn a real per-attempt Job so the user sees a normal job
        # row (with proper INFO/WARNING-coloured logs) for THIS firing
        # — instead of the synthesized status text on the parent chain
        # row, which had no level prefixes and so rendered without the
        # log-line colour every other job has. Best-effort: when the
        # JobManager isn't available (CLI / test contexts) the firing
        # still proceeds with no per-attempt Job, matching the legacy
        # headless behaviour.
        chain_id = "retry-" + hashlib.sha256(path.encode("utf-8")).hexdigest()[:16]
        attempt_job_id = _create_retry_attempt_job(
            canonical_path=path,
            chain_id=chain_id,
            attempt=fired_attempt,
            max_attempts=len(_BACKOFF),
            server_id=_retry_pin_id,
            server_name=getattr(config, "server_display_name", None),
            server_type=None,
            display_name=display_name,
            source=source,
        )
        _link_attempt_to_chain(path, attempt_job_id)

        # Bind a synthetic failure scope for the retry. The ORIGINATING
        # job (the dispatch row that first hit SKIPPED_NOT_INDEXED) has
        # long completed by the time this Timer fires, so its job_id is
        # gone from the live failure_scope stack. The per-attempt Job
        # we just spawned IS user-visible, but ``record_failure`` keys
        # on the active scope rather than the per-attempt Job — without
        # a synthetic scope it would log the "Internal bookkeeping bug"
        # warning every time a retry hits an FFmpeg failure (file
        # deleted between scan and retry, codec gone unsupported after
        # a driver update). The synthetic ``retry:<path>`` scope makes
        # those failures attributable to "the retry chain for this
        # path" without polluting ``record_failure``'s warning channel.
        retry_scope_id = f"retry:{path}"
        with _capture_attempt_logs(attempt_job_id), failure_scope(retry_scope_id):
            # First user-visible line in the per-attempt Job log so the
            # operator opening it sees what triggered the firing — the
            # rest comes from process_canonical_path's own INFO calls.
            if _retry_server_tag:
                logger.info("Retry #{} firing for {} (server={})", fired_attempt, path, _retry_server_tag)
            else:
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
                    # Use the explicit pin captured at schedule time
                    # — covers BOTH config-pinned (vendor webhook)
                    # and originator-pinned (worker case 2) dispatches.
                    server_id_filter=server_id_filter,
                    display_name=display_name,
                    source=source,
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
                _complete_retry_attempt_job(
                    attempt_job_id,
                    error=f"Retry firing crashed: {type(exc).__name__}: {exc}",
                )
                schedule_retry_for_unindexed(
                    path,
                    registry=registry,
                    config=config,
                    item_id_by_server=item_id_by_server,
                    attempt=fired_attempt + 1,
                    server_id_filter=server_id_filter,
                    display_name=display_name,
                    source=source,
                )
                return

            # Did any publisher need another shot? If so, schedule
            # another retry; otherwise the chain is complete. The three
            # "needs more time" statuses:
            #   * SKIPPED_NOT_INDEXED — Plex hasn't analysed the file yet
            #     (no bundle hash → can't write the BIF)
            #   * SKIPPED_NOT_IN_LIBRARY — server doesn't know the file
            #     exists yet (resolve_remote_path_to_item_id returned None)
            #   * PUBLISHED_PENDING_REGISTRATION — tiles/sidecar are on
            #     disk but Jellyfin/Emby item_id wasn't resolved at
            #     publish time, so the per-item registration calls
            #     (Media Preview Bridge plugin + /Items/{id}/Refresh)
            #     never fired. The retry re-resolves the item id so the
            #     registration can complete.
            #
            # PUBLISHED, FAILED, and SKIPPED_OUTPUT_EXISTS all terminate
            # the retry chain.
            #
            # Live Homebodies S01E01 (2026-05-09) regression: pre-fix the
            # check only looked for SKIPPED_NOT_INDEXED, so a JellyTest
            # PENDING_REGISTRATION result on attempt #1 silently terminated
            # the chain — the trickplay tiles never got registered.
            needs_retry = any(
                p.status
                in (
                    PublisherStatus.SKIPPED_NOT_INDEXED,
                    PublisherStatus.SKIPPED_NOT_IN_LIBRARY,
                    PublisherStatus.PUBLISHED_PENDING_REGISTRATION,
                )
                for p in result.publishers
            )
            if needs_retry and result.status is not MultiServerStatus.FAILED:
                next_attempt = fired_attempt + 1
                # ``schedule_retry_for_unindexed`` will upsert the row
                # to "scheduled" if the next attempt is within
                # BACKOFF_SCHEDULE; otherwise it returns False and we
                # surface the chain as exhausted below.
                rescheduled = schedule_retry_for_unindexed(
                    path,
                    registry=registry,
                    config=config,
                    item_id_by_server=item_id_by_server,
                    attempt=next_attempt,
                    server_id_filter=server_id_filter,
                    display_name=display_name,
                    source=source,
                )
                if not rescheduled:
                    exhausted_reason = (
                        f"Server still hasn't indexed this file after "
                        f"{fired_attempt} retry attempt(s). The publisher's "
                        f"output is on disk but the server-side trickplay "
                        f"row never registered — likely the file is outside "
                        f"every configured library root, or the server's "
                        f"realtime monitor is disabled."
                    )
                    logger.warning("Retry chain exhausted for {}: {}", path, exhausted_reason)
                    _upsert_retry_chain_job(
                        canonical_path=path,
                        attempt=fired_attempt,
                        outcome="exhausted",
                        server_id=_retry_pin_id,
                        display_name=display_name,
                        source=source,
                        reason=exhausted_reason,
                    )
                    _complete_retry_attempt_job(attempt_job_id, error=exhausted_reason)
                else:
                    # Mark the firing job as done-with-warning so the
                    # row reads "completed (amber)" rather than green —
                    # this attempt didn't finish the chain, more work
                    # is queued.
                    _complete_retry_attempt_job(
                        attempt_job_id,
                        warning=f"Attempt {fired_attempt} still pending; next retry queued",
                    )
            else:
                logger.info("Retry chain for {} complete on attempt #{}", path, fired_attempt)
                _upsert_retry_chain_job(
                    canonical_path=path,
                    attempt=fired_attempt,
                    outcome="completed",
                    server_id=_retry_pin_id,
                    display_name=display_name,
                    source=source,
                )
                _complete_retry_attempt_job(attempt_job_id)

    scheduled = scheduler.schedule(canonical_path, _callback, attempt=attempt)
    if scheduled:
        # Surface the pending retry to the user-visible Jobs panel.
        # ``BACKOFF_SCHEDULE`` indexes are 0-based; attempt is 1-based.
        wait_seconds = _BACKOFF[attempt - 1]
        _upsert_retry_chain_job(
            canonical_path=canonical_path,
            attempt=attempt,
            outcome="scheduled",
            wait_seconds=wait_seconds,
            server_id=server_id_filter,
        )
    return scheduled
