"""Cross-source webhook echo suppression.

When Sonarr/Radarr imports a new file, this app generates tiles and
triggers a Plex partial scan. Plex auto-scans, finds the file, and
fires its own ``library.new`` webhook back at the app 30s-7m later —
a "Plex echo" that carries no new information. The existing
``(source, server_id, path)`` dedup table treats this as a distinct
event (different source) so the echo creates a SECOND Job for the
same file.

Production fallout (job-i0sses session, 2026-05-12): nine Job rows
sat ``pending`` for 1-2h each, each with a sibling completed Job for
the same path created 60s-7m earlier from a different source. The
RetryScheduler is path-keyed so only ONE chain Timer can be armed per
canonical path; whichever publish-run was LAST to call
``schedule_retry_for_unindexed`` captured the Timer slot, and the
other Job's chain was silently orphaned.

These tests pin the cross-source echo suppression: when a webhook
arrives for a path that an active (PENDING or RUNNING) Job already
covers, the new webhook is dropped and history records the
suppression.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from media_preview_generator.web.settings_manager import reset_settings_manager


@pytest.fixture(autouse=True)
def _reset_singletons():
    """Same singleton reset the early-scan tests use — without it, state
    from a prior test's debounce batch / job manager leaks into this one.
    """
    reset_settings_manager()
    import media_preview_generator.web.jobs as jobs_mod

    with jobs_mod._job_lock:
        jobs_mod._job_manager = None
    import media_preview_generator.web.webhooks as wh

    wh._webhook_history.clear()
    with wh._pending_lock:
        for t in wh._pending_timers.values():
            t.cancel()
        wh._pending_timers.clear()
        wh._pending_batches.clear()
        wh._recent_dispatches.clear()
    yield
    reset_settings_manager()
    with jobs_mod._job_lock:
        jobs_mod._job_manager = None
    wh._webhook_history.clear()
    with wh._pending_lock:
        for t in wh._pending_timers.values():
            t.cancel()
        wh._pending_timers.clear()
        wh._pending_batches.clear()
        wh._recent_dispatches.clear()


def _make_active_job(job_manager_mock, *, job_id: str, webhook_paths: list[str], status: str = "pending"):
    """Wire up the job_manager mock so ``get_all_jobs`` returns a job with
    the given config. Used to simulate an in-flight sibling Job that
    should suppress an incoming cross-source echo.
    """
    from media_preview_generator.web.jobs import JobStatus

    status_enum = {
        "pending": JobStatus.PENDING,
        "running": JobStatus.RUNNING,
        "completed": JobStatus.COMPLETED,
        "failed": JobStatus.FAILED,
    }[status]
    job = MagicMock()
    job.id = job_id
    job.status = status_enum
    job.config = {"source": "sonarr", "webhook_paths": list(webhook_paths)}
    # Route the job through the right status-specific accessor — the
    # SUT now calls get_pending_jobs() + get_running_jobs() instead of
    # the all-jobs walk (avoids scanning terminal rows under the lock).
    if status == "pending":
        job_manager_mock.get_pending_jobs.return_value = [job]
        job_manager_mock.get_running_jobs.return_value = []
    elif status == "running":
        job_manager_mock.get_pending_jobs.return_value = []
        job_manager_mock.get_running_jobs.return_value = [job]
    else:
        # Terminal (completed/failed/cancelled): not returned by either
        # active-getter — the SUT's scan won't see the job at all.
        job_manager_mock.get_pending_jobs.return_value = []
        job_manager_mock.get_running_jobs.return_value = []
    return job


@patch("media_preview_generator.web.webhooks._kick_early_scan")
@patch("media_preview_generator.web.webhooks.get_job_manager")
@patch("media_preview_generator.web.webhooks.threading.Timer")
def test_plex_echo_suppressed_when_sibling_job_is_pending(mock_timer_cls, mock_job_mgr, mock_kick):
    """A Plex webhook for a path that a PENDING sonarr Job already
    covers must NOT create a second Job.

    This is the exact regression that produced the 9 stuck jobs in the
    job-i0sses incident: the sonarr Job was in debounce-window PENDING
    when Plex's echo arrived ~60s later.
    """
    from media_preview_generator.web import webhooks as wh

    mock_timer = MagicMock()
    mock_timer.daemon = True
    mock_timer_cls.return_value = mock_timer

    path = "/data/Movies/Example (2026)/Example.mkv"
    _make_active_job(
        mock_job_mgr.return_value,
        job_id="sib-sonarr-job",
        webhook_paths=[path],
        status="pending",
    )

    accepted = wh._schedule_webhook_job("plex", "Example", path)

    assert accepted is False, (
        "Cross-source Plex echo for a path covered by an active sibling "
        "MUST be suppressed (otherwise the duplicate chain orphans — see "
        "job-i0sses incident, 2026-05-12)."
    )
    # No new Job should have been created.
    mock_job_mgr.return_value.create_job.assert_not_called()


@patch("media_preview_generator.web.webhooks._kick_early_scan")
@patch("media_preview_generator.web.webhooks.get_job_manager")
@patch("media_preview_generator.web.webhooks.threading.Timer")
def test_plex_echo_suppressed_when_sibling_job_is_running(mock_timer_cls, mock_job_mgr, mock_kick):
    """Same suppression must fire while sibling is mid-processing."""
    from media_preview_generator.web import webhooks as wh

    mock_timer = MagicMock()
    mock_timer.daemon = True
    mock_timer_cls.return_value = mock_timer

    path = "/data/TV Shows/Show/S01E01.mkv"
    _make_active_job(
        mock_job_mgr.return_value,
        job_id="sib-running",
        webhook_paths=[path],
        status="running",
    )

    accepted = wh._schedule_webhook_job("plex", "Show S01E01", path)

    assert accepted is False
    mock_job_mgr.return_value.create_job.assert_not_called()


@patch("media_preview_generator.web.webhooks._kick_early_scan")
@patch("media_preview_generator.web.webhooks.get_job_manager")
@patch("media_preview_generator.web.webhooks.threading.Timer")
def test_new_webhook_after_sibling_completed_still_creates_job(mock_timer_cls, mock_job_mgr, mock_kick):
    """A COMPLETED sibling MUST NOT suppress a new webhook — the user
    may legitimately want to re-process (e.g., after fixing a bad
    metadata match)."""
    from media_preview_generator.web import webhooks as wh

    mock_timer = MagicMock()
    mock_timer.daemon = True
    mock_timer_cls.return_value = mock_timer

    path = "/data/Movies/Done.mkv"
    _make_active_job(
        mock_job_mgr.return_value,
        job_id="sib-done",
        webhook_paths=[path],
        status="completed",
    )

    new_job = MagicMock()
    new_job.id = "fresh-job"
    mock_job_mgr.return_value.create_job.return_value = new_job
    mock_job_mgr.return_value.get_job.return_value = new_job

    accepted = wh._schedule_webhook_job("plex", "Done", path)

    assert accepted is True
    mock_job_mgr.return_value.create_job.assert_called_once()


@patch("media_preview_generator.web.webhooks._kick_early_scan")
@patch("media_preview_generator.web.webhooks.get_job_manager")
@patch("media_preview_generator.web.webhooks.threading.Timer")
def test_new_webhook_after_sibling_failed_still_creates_job(mock_timer_cls, mock_job_mgr, mock_kick):
    """A FAILED/exhausted sibling MUST NOT suppress a new webhook — the
    user re-triggers precisely to recover from the failure."""
    from media_preview_generator.web import webhooks as wh

    mock_timer = MagicMock()
    mock_timer.daemon = True
    mock_timer_cls.return_value = mock_timer

    path = "/data/Movies/Failed.mkv"
    _make_active_job(
        mock_job_mgr.return_value,
        job_id="sib-failed",
        webhook_paths=[path],
        status="failed",
    )

    new_job = MagicMock()
    new_job.id = "fresh-job"
    mock_job_mgr.return_value.create_job.return_value = new_job
    mock_job_mgr.return_value.get_job.return_value = new_job

    accepted = wh._schedule_webhook_job("plex", "Failed", path)

    assert accepted is True
    mock_job_mgr.return_value.create_job.assert_called_once()


@patch("media_preview_generator.web.webhooks._kick_early_scan")
@patch("media_preview_generator.web.webhooks.get_job_manager")
@patch("media_preview_generator.web.webhooks.threading.Timer")
def test_webhook_for_different_path_not_suppressed(mock_timer_cls, mock_job_mgr, mock_kick):
    """An active sibling for DIFFERENT path must not trip the suppression."""
    from media_preview_generator.web import webhooks as wh

    mock_timer = MagicMock()
    mock_timer.daemon = True
    mock_timer_cls.return_value = mock_timer

    _make_active_job(
        mock_job_mgr.return_value,
        job_id="sib-other-path",
        webhook_paths=["/data/Movies/OtherFile.mkv"],
        status="pending",
    )

    new_job = MagicMock()
    new_job.id = "fresh"
    mock_job_mgr.return_value.create_job.return_value = new_job
    mock_job_mgr.return_value.get_job.return_value = new_job

    accepted = wh._schedule_webhook_job("plex", "Different", "/data/Movies/Different.mkv")

    assert accepted is True
    mock_job_mgr.return_value.create_job.assert_called_once()


@patch("media_preview_generator.web.webhooks._kick_early_scan")
@patch("media_preview_generator.web.webhooks.get_job_manager")
@patch("media_preview_generator.web.webhooks.threading.Timer")
def test_suppression_writes_history_entry(mock_timer_cls, mock_job_mgr, mock_kick):
    """The user-visible webhook history MUST show the suppression event so
    operators can tell echoes from genuine duplicates. ``"deduped"`` is
    used by same-source TTL dedup — echoes need their own label so the
    UI can render them differently."""
    from media_preview_generator.web import webhooks as wh

    mock_timer = MagicMock()
    mock_timer.daemon = True
    mock_timer_cls.return_value = mock_timer

    path = "/data/Movies/EchoMe.mkv"
    _make_active_job(
        mock_job_mgr.return_value,
        job_id="active-sib",
        webhook_paths=[path],
        status="pending",
    )

    wh._schedule_webhook_job("plex", "EchoMe", path)

    # History must record the suppression with a distinct status so the
    # UI / debug page can tell echoes apart from same-source dedupes.
    history = list(wh._webhook_history)
    suppressed = [e for e in history if e.get("status") == "echo_suppressed"]
    assert suppressed, f"history must record echo_suppressed entry; got {history}"
    # Pin the sibling Job's ID on the entry — without this, a regression
    # where ``_find_active_job_for_path`` returns the WRONG job_id (or
    # any non-None sentinel) would still satisfy the
    # ``status == 'echo_suppressed'`` check. The job_id MUST match the
    # suppressing sibling so operators can navigate to the in-flight Job
    # from the history view.
    assert suppressed[0].get("job_id") == "active-sib", suppressed[0]


@patch("media_preview_generator.web.webhooks._kick_early_scan")
@patch("media_preview_generator.web.webhooks.get_job_manager")
@patch("media_preview_generator.web.webhooks.threading.Timer")
def test_no_active_jobs_does_not_suppress(mock_timer_cls, mock_job_mgr, mock_kick):
    """When JobManager has no jobs at all, a fresh webhook proceeds normally."""
    from media_preview_generator.web import webhooks as wh

    mock_timer = MagicMock()
    mock_timer.daemon = True
    mock_timer_cls.return_value = mock_timer

    mock_job_mgr.return_value.get_pending_jobs.return_value = []
    mock_job_mgr.return_value.get_running_jobs.return_value = []
    new_job = MagicMock()
    new_job.id = "fresh"
    mock_job_mgr.return_value.create_job.return_value = new_job
    mock_job_mgr.return_value.get_job.return_value = new_job

    accepted = wh._schedule_webhook_job("sonarr", "Fresh", "/data/x.mkv")

    assert accepted is True
    mock_job_mgr.return_value.create_job.assert_called_once()


@patch("media_preview_generator.web.webhooks._kick_early_scan")
@patch("media_preview_generator.web.webhooks.get_job_manager")
@patch("media_preview_generator.web.webhooks.threading.Timer")
def test_same_source_different_server_not_suppressed(mock_timer_cls, mock_job_mgr, mock_kick):
    """Multi-server fan-out: same source (e.g., two Plex installs sharing
    storage, both emit library.new for the same file) MUST NOT be
    suppressed. The user wants separate Jobs per destination server.
    Suppression is for CROSS-SOURCE echoes only — same-source dedup is
    handled by ``_check_and_record_dedup`` (TTL window) and same-source
    multi-server is legitimate work.

    Regression guard for
    ``test_schedule_webhook_job_per_server_keeps_separate_batches``.
    """
    from media_preview_generator.web import webhooks as wh
    from media_preview_generator.web.jobs import JobStatus

    mock_timer = MagicMock()
    mock_timer.daemon = True
    mock_timer_cls.return_value = mock_timer

    path = "/data/Movies/MultiServer.mkv"
    # Sibling Job has SAME source as incoming. The new webhook targets
    # a different server (multi-Plex install) — that's legitimate,
    # don't suppress.
    job = MagicMock()
    job.id = "sib-same-source"
    job.status = JobStatus.PENDING
    job.config = {"source": "sonarr", "webhook_paths": [path]}
    mock_job_mgr.return_value.get_pending_jobs.return_value = [job]
    mock_job_mgr.return_value.get_running_jobs.return_value = []

    new_job = MagicMock()
    new_job.id = "fresh"
    mock_job_mgr.return_value.create_job.return_value = new_job
    mock_job_mgr.return_value.get_job.return_value = new_job

    accepted = wh._schedule_webhook_job("sonarr", "MultiServer", path, server_id="other-server")

    assert accepted is True, (
        "Same-source multi-server fan-out MUST NOT be suppressed — "
        "the user expects per-server Jobs for path-shared multi-Plex installs."
    )
    mock_job_mgr.return_value.create_job.assert_called_once()


@patch("media_preview_generator.web.webhooks._kick_early_scan")
@patch("media_preview_generator.web.webhooks.get_job_manager")
@patch("media_preview_generator.web.webhooks.threading.Timer")
def test_sibling_matched_via_retry_chain_for_field(mock_timer_cls, mock_job_mgr, mock_kick):
    """Match must also work when the sibling is deep in a retry chain
    where its config carries ``retry_chain_for`` (some chain rows
    pre-rewrite did not carry the original ``webhook_paths`` — defense
    in depth)."""
    from media_preview_generator.web import webhooks as wh
    from media_preview_generator.web.jobs import JobStatus

    mock_timer = MagicMock()
    mock_timer.daemon = True
    mock_timer_cls.return_value = mock_timer

    path = "/data/Movies/Chained.mkv"
    job = MagicMock()
    job.id = "deep-chain"
    job.status = JobStatus.PENDING
    # Simulated legacy chain row that has retry_chain_for but EMPTY
    # webhook_paths — match must still fire on retry_chain_for.
    job.config = {"source": "sonarr", "retry_chain_for": path, "is_retry_chain": True}
    mock_job_mgr.return_value.get_pending_jobs.return_value = [job]
    mock_job_mgr.return_value.get_running_jobs.return_value = []

    accepted = wh._schedule_webhook_job("plex", "Chained", path)

    assert accepted is False
    mock_job_mgr.return_value.create_job.assert_not_called()


@patch("media_preview_generator.web.webhooks._kick_early_scan")
@patch("media_preview_generator.web.webhooks.get_job_manager")
@patch("media_preview_generator.web.webhooks.threading.Timer")
def test_cancelled_sibling_does_not_suppress(mock_timer_cls, mock_job_mgr, mock_kick):
    """JobStatus matrix coverage: CANCELLED is a terminal state — the
    user explicitly stopped that Job, so a new webhook for the same
    path is a legitimate fresh request and MUST proceed.

    The SUT achieves this by querying only ``get_pending_jobs()`` +
    ``get_running_jobs()``; CANCELLED jobs appear in neither list. This
    test pins that contract so a future refactor that switches back to
    ``get_all_jobs()`` (and forgets to status-filter) fails loudly.
    """
    from media_preview_generator.web import webhooks as wh

    mock_timer = MagicMock()
    mock_timer.daemon = True
    mock_timer_cls.return_value = mock_timer

    # No pending or running siblings — the CANCELLED job is in neither
    # active-getter's return value.
    mock_job_mgr.return_value.get_pending_jobs.return_value = []
    mock_job_mgr.return_value.get_running_jobs.return_value = []

    new_job = MagicMock()
    new_job.id = "fresh"
    mock_job_mgr.return_value.create_job.return_value = new_job
    mock_job_mgr.return_value.get_job.return_value = new_job

    accepted = wh._schedule_webhook_job("plex", "WasCancelled", "/data/Movies/Cancelled.mkv")

    assert accepted is True
    mock_job_mgr.return_value.create_job.assert_called_once()
    # The active-getters must have been queried — proves the SUT didn't
    # accidentally fall back to get_all_jobs().
    mock_job_mgr.return_value.get_pending_jobs.assert_called()
    mock_job_mgr.return_value.get_running_jobs.assert_called()
