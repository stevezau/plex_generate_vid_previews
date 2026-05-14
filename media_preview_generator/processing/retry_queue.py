"""Backoff schedule for the job-level retry path.

This module used to contain a per-file ``threading.Timer``-based retry
scheduler that fired one Timer per canonical path. When a Sonarr/Radarr
batch arrived with N files and several came back ``PUBLISHED_PENDING_REGISTRATION``,
the scheduler scheduled N independent retries — each spawning a per-attempt
child Job collapsed onto the originating dispatcher row. For a 333-file
batch with 61 pending files, the dispatcher accumulated 191 attempt rows
(61 + 54 + 47 + 29) and the modal Attempts dropdown rendered them as
duplicate ``1, 2, 3, 4`` pills (job ``756255aa``, 2026-05-12).

The 2026-05-13 refactor routes all retry-eligible outcomes (PENDING_REGISTRATION,
NOT_INDEXED, NOT_IN_LIBRARY, unresolved, not-found-on-disk) through the
existing per-Job retry pattern in ``web/routes/job_runner.py``
(``_spawn_retry_job``). One retry Job per attempt, scoped to the still-
pending paths only, with proper gate admission, log isolation, and parent
chain-state mutation via ``upsert_retry_chain_job``. The per-file
``RetryScheduler`` and its supporting plumbing are gone.

What remains:

* :data:`BACKOFF_SCHEDULE` — the (60s, 2m, 5m, 15m, 1h) cadence consumed
  by ``_spawn_retry_job``. Public so any caller that wants to display the
  "next retry in Xs" countdown shares the canonical timing.
* :data:`PENDING_PUBLISHER_STATUSES` — the per-publisher status values
  that flag a file for retry. Shared between the retry-decision scan in
  ``web/routes/job_runner.py`` and the ``/api/jobs/<chain_id>/attempts``
  response helper in ``web/routes/api_jobs.py`` so the two code paths
  can't drift.
"""

from __future__ import annotations

#: Backoff schedule in seconds for each attempt (1-indexed:
#: ``BACKOFF_SCHEDULE[0]`` is the delay before attempt #2). Five entries
#: → up to five retries before giving up. Total wall time is ~83 minutes,
#: deliberately past typical Plex full-scan duration on a small library.
#:
#: First attempt is 60s — Jellyfin's ``LibraryMonitor`` has a hard-coded
#: ~45s file-event settle delay before processing the refresh, so anything
#: under 45s is a guaranteed miss. Starting at 60s gives Jellyfin a real
#: chance to have indexed the file on attempt 1 instead of wasting it.
#: Subsequent gaps (2m / 5m / 15m / 1h) cover Plex's typical scan latency
#: window without turning into a runaway loop.
BACKOFF_SCHEDULE: tuple[int, ...] = (60, 120, 300, 900, 3600)

#: Per-publisher status values (as ``.value`` strings of
#: :class:`PublisherStatus`) that flag a file as "still needs another
#: attempt because the destination server isn't ready yet."
#:
#: Two consumers must agree on this set:
#:
#: * ``web/routes/job_runner.py`` — the retry-decision scan that walks
#:   the per-file JSONL after each dispatch to decide whether to spawn
#:   another retry Job.
#: * ``web/routes/api_jobs.py`` — the ``_pending_servers`` helper that
#:   computes the ``pending_servers`` field on each ``/attempts`` entry
#:   so the modal can render per-pill vendor chips.
#:
#: Adding a fourth status (e.g. ``"skipped_metadata_unavailable"``) only
#: at one call site would silently desync the retry decision from the
#: UI rendering — both must reference this single source of truth.
PENDING_PUBLISHER_STATUSES: frozenset[str] = frozenset(
    {
        "published_pending_registration",
        "skipped_not_indexed",
        "skipped_not_in_library",
    }
)
