"""Tests for retry-queue → JobManager.upsert_retry_chain_job hooks.

The retry queue is "headless" by design — it lives in the processing
package and runs background timers. To surface progress to the user,
``schedule_retry_for_unindexed`` upserts a retry-chain Job at three
points:

  1. Schedule time → outcome="scheduled" with countdown
  2. Callback fire (timer expired, dispatch about to run) → outcome="running"
  3. After dispatch returns →
       - "scheduled" again if more retries needed
       - "completed" if PUBLISHED / SKIPPED_OUTPUT_EXISTS / etc
       - "exhausted" if BACKOFF_SCHEDULE ran out

These tests pin those hooks per .claude/rules/testing.md branch matrix.
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from media_preview_generator.processing.retry_queue import (
    _BACKOFF,
    schedule_retry_for_unindexed,
)


@pytest.fixture(autouse=True)
def _reset_retry_scheduler():
    from media_preview_generator.processing.retry_queue import reset_retry_scheduler

    reset_retry_scheduler()
    yield
    reset_retry_scheduler()


@pytest.fixture(autouse=True)
def _reset_job_manager():
    import media_preview_generator.web.jobs as jobs_mod

    with jobs_mod._job_lock:
        jobs_mod._job_manager = None
    yield
    with jobs_mod._job_lock:
        jobs_mod._job_manager = None


class TestUpsertHookAtSchedule:
    def test_schedule_upserts_retry_chain_job_with_outcome_scheduled(self, tmp_path):
        """Calling schedule_retry_for_unindexed creates a user-visible
        retry-chain Job before the timer even fires — operators see
        'this is queued, next in 30s' immediately, not just in the
        logs."""
        # Use the real JobManager singleton with a tmp config dir so we
        # can read the state back via get_all_jobs().
        import media_preview_generator.web.jobs as jobs_mod
        from media_preview_generator.web.jobs import JobManager

        with jobs_mod._job_lock:
            jobs_mod._job_manager = JobManager(config_dir=str(tmp_path / "config"))

        registry = MagicMock()
        config = MagicMock()
        with patch(
            "media_preview_generator.processing.retry_queue._BACKOFF",
            (3600,) + tuple([3600] * (len(_BACKOFF) - 1)),
        ):
            scheduled = schedule_retry_for_unindexed(
                "/data/Foo.mkv",
                registry=registry,
                config=config,
                item_id_by_server=None,
                attempt=1,
            )
        assert scheduled is True

        all_jobs = jobs_mod._job_manager.get_all_jobs()
        chain_jobs = [j for j in all_jobs if j.id.startswith("retry-")]
        assert len(chain_jobs) == 1, (
            "Schedule must create a retry-chain Job — without it, the "
            "user can't see anything is happening until the timer fires."
        )
        chain = chain_jobs[0]
        assert chain.config["last_outcome"] == "scheduled"
        assert chain.config["retry_attempt"] == 1
        assert chain.progress.retry_eta is not None
        assert chain.progress.retry_wait_total == 3600

    def test_failed_schedule_does_not_create_job(self, tmp_path):
        """When BACKOFF is exhausted (attempt > len(BACKOFF)),
        scheduler.schedule returns False — no retry-chain row should
        be spawned for an attempt that won't fire."""
        import media_preview_generator.web.jobs as jobs_mod
        from media_preview_generator.web.jobs import JobManager

        with jobs_mod._job_lock:
            jobs_mod._job_manager = JobManager(config_dir=str(tmp_path / "config"))

        registry = MagicMock()
        config = MagicMock()
        scheduled = schedule_retry_for_unindexed(
            "/data/Foo.mkv",
            registry=registry,
            config=config,
            item_id_by_server=None,
            attempt=len(_BACKOFF) + 1,  # past the end of the schedule
        )
        assert scheduled is False
        chain_jobs = [j for j in jobs_mod._job_manager.get_all_jobs() if j.id.startswith("retry-")]
        assert chain_jobs == []


class TestUpsertHookAtCallbackFire:
    def test_callback_upserts_to_running_and_completed_on_published(self, tmp_path):
        """Walk a complete chain: schedule → fire → dispatch returns
        PUBLISHED → chain marked completed in JobManager."""
        import media_preview_generator.web.jobs as jobs_mod
        from media_preview_generator.processing.multi_server import (
            MultiServerResult,
            MultiServerStatus,
            PublisherResult,
            PublisherStatus,
        )
        from media_preview_generator.web.jobs import JobManager

        with jobs_mod._job_lock:
            jobs_mod._job_manager = JobManager(config_dir=str(tmp_path / "config"))

        chain_complete = threading.Event()

        def fake_process(**kwargs):
            chain_complete.set()
            return MultiServerResult(
                canonical_path=kwargs["canonical_path"],
                status=MultiServerStatus.PUBLISHED,
                publishers=[
                    PublisherResult(
                        server_id="jelly-1",
                        server_name="jelly-1",
                        adapter_name="jellyfin_trickplay",
                        status=PublisherStatus.PUBLISHED,
                        message="ok",
                    )
                ],
                frame_count=6,
                message="ok",
            )

        with (
            patch(
                "media_preview_generator.processing.retry_queue._BACKOFF",
                (0.05,) + tuple([0.05] * (len(_BACKOFF) - 1)),
            ),
            patch(
                "media_preview_generator.processing.multi_server.process_canonical_path",
                side_effect=fake_process,
            ),
        ):
            schedule_retry_for_unindexed(
                "/data/Foo.mkv",
                registry=MagicMock(),
                config=MagicMock(),
                item_id_by_server=None,
                attempt=1,
            )
            assert chain_complete.wait(timeout=2)
            time.sleep(0.05)

        chain_jobs = [j for j in jobs_mod._job_manager.get_all_jobs() if j.id.startswith("retry-")]
        assert len(chain_jobs) == 1
        chain = chain_jobs[0]
        assert chain.config["last_outcome"] == "completed", (
            f"Chain final outcome must be 'completed' when dispatch returns "
            f"PUBLISHED — got {chain.config.get('last_outcome')!r}"
        )

    def test_callback_re_arms_and_keeps_chain_pending_when_still_pending(self, tmp_path):
        """When the dispatch keeps returning PENDING, the retry-chain
        Job should keep updating in place with incrementing attempt
        counter — not spawn new rows."""
        import media_preview_generator.web.jobs as jobs_mod
        from media_preview_generator.processing.multi_server import (
            MultiServerResult,
            MultiServerStatus,
            PublisherResult,
            PublisherStatus,
        )
        from media_preview_generator.web.jobs import JobManager

        with jobs_mod._job_lock:
            jobs_mod._job_manager = JobManager(config_dir=str(tmp_path / "config"))

        attempts_seen = []
        third_attempt = threading.Event()

        def fake_process(**kwargs):
            attempts_seen.append(kwargs.get("retry_attempt"))
            if len(attempts_seen) >= 3:
                third_attempt.set()
            return MultiServerResult(
                canonical_path=kwargs["canonical_path"],
                status=MultiServerStatus.PUBLISHED,
                publishers=[
                    PublisherResult(
                        server_id="jelly-1",
                        server_name="jelly-1",
                        adapter_name="jellyfin_trickplay",
                        status=PublisherStatus.PUBLISHED_PENDING_REGISTRATION,
                        message="awaiting registration",
                    )
                ],
                frame_count=0,
                message="pending",
            )

        with (
            patch(
                "media_preview_generator.processing.retry_queue._BACKOFF",
                (0.05,) + tuple([0.05] * (len(_BACKOFF) - 1)),
            ),
            patch(
                "media_preview_generator.processing.multi_server.process_canonical_path",
                side_effect=fake_process,
            ),
        ):
            schedule_retry_for_unindexed(
                "/data/Foo.mkv",
                registry=MagicMock(),
                config=MagicMock(),
                item_id_by_server=None,
                attempt=1,
            )
            assert third_attempt.wait(timeout=3), (
                f"Chain didn't get to attempt 3 — attempts_seen={attempts_seen}. "
                "PUBLISHED_PENDING_REGISTRATION must drive continuation."
            )
            time.sleep(0.05)

        # Exactly ONE retry-chain Job row regardless of how many attempts fired.
        chain_jobs = [j for j in jobs_mod._job_manager.get_all_jobs() if j.id.startswith("retry-")]
        assert len(chain_jobs) == 1, (
            f"Multiple retry attempts MUST update the same row, not spawn new ones. "
            f"Got {len(chain_jobs)} rows for {len(attempts_seen)} attempts."
        )
        chain = chain_jobs[0]
        assert chain.config["retry_attempt"] >= 3


class TestPerAttemptJobSpawn:
    """Each retry firing must spawn a real per-attempt Job so the user
    has a properly-levelled, colour-coded log to drill into — instead
    of only the chain row's synthesized status text. Pre-fix
    ``process_canonical_path`` ran in the timer thread with no Job
    context, so its ``logger.info`` calls landed only in the global
    container log; the chain row's ``View Logs`` modal showed plain
    grey text because ``colorizeLogLine`` had no INFO/WARNING tokens
    to match.
    """

    def test_retry_firing_creates_a_child_attempt_job_per_attempt(self, tmp_path):
        import media_preview_generator.web.jobs as jobs_mod
        from media_preview_generator.processing.multi_server import (
            MultiServerResult,
            MultiServerStatus,
            PublisherResult,
            PublisherStatus,
        )
        from media_preview_generator.web.jobs import JobManager

        with jobs_mod._job_lock:
            jobs_mod._job_manager = JobManager(config_dir=str(tmp_path / "config"))

        chain_done = threading.Event()

        def fake_process(**kwargs):
            chain_done.set()
            return MultiServerResult(
                canonical_path=kwargs["canonical_path"],
                status=MultiServerStatus.PUBLISHED,
                publishers=[
                    PublisherResult(
                        server_id="jelly-1",
                        server_name="jelly-1",
                        adapter_name="jellyfin_trickplay",
                        status=PublisherStatus.PUBLISHED,
                        message="ok",
                    )
                ],
                frame_count=1,
                message="ok",
            )

        with (
            patch(
                "media_preview_generator.processing.retry_queue._BACKOFF",
                (0.05,) + tuple([0.05] * (len(_BACKOFF) - 1)),
            ),
            patch(
                "media_preview_generator.processing.multi_server.process_canonical_path",
                side_effect=fake_process,
            ),
        ):
            schedule_retry_for_unindexed(
                "/data/Foo.mkv",
                registry=MagicMock(),
                config=MagicMock(),
                item_id_by_server=None,
                attempt=1,
            )
            assert chain_done.wait(timeout=2)
            time.sleep(0.1)

        all_jobs = jobs_mod._job_manager.get_all_jobs()
        attempt_jobs = [j for j in all_jobs if j.config.get("is_retry_attempt")]
        assert len(attempt_jobs) == 1, (
            f"One firing must spawn exactly one per-attempt Job; got {len(attempt_jobs)} "
            "(check _create_retry_attempt_job is called inside the timer thread)."
        )
        attempt = attempt_jobs[0]
        # Contract assertions the dispatcher → multi_server boundary depends on:
        #   * ``parent_chain_id`` lets the UI render a "← Part of <chain>" link.
        #   * ``retry_attempt`` MUST match the firing number (not always 1) —
        #     pre-final-pass this was the kwargs-not-just-call-count gap that
        #     hid bug D34 in production for months (see .claude/rules/testing.md
        #     "Asserting boundary calls").
        assert attempt.config["parent_chain_id"].startswith("retry-")
        assert attempt.config["retry_attempt"] == 1
        assert attempt.config["retry_chain_for"] == "/data/Foo.mkv"

    def test_chain_row_lists_child_job_ids_after_firings(self, tmp_path):
        """After each firing, the chain row's ``child_job_ids`` config
        list grows by one — the synthesized chain log uses that list to
        hand the user UUIDs they can paste into the Jobs panel filter
        to open the real coloured per-attempt logs.
        """
        import media_preview_generator.web.jobs as jobs_mod
        from media_preview_generator.processing.multi_server import (
            MultiServerResult,
            MultiServerStatus,
            PublisherResult,
            PublisherStatus,
        )
        from media_preview_generator.web.jobs import JobManager

        with jobs_mod._job_lock:
            jobs_mod._job_manager = JobManager(config_dir=str(tmp_path / "config"))

        attempts = []
        third = threading.Event()

        def fake_process(**kwargs):
            attempts.append(kwargs.get("retry_attempt"))
            if len(attempts) >= 3:
                third.set()
            return MultiServerResult(
                canonical_path=kwargs["canonical_path"],
                status=MultiServerStatus.PUBLISHED,
                publishers=[
                    PublisherResult(
                        server_id="jelly-1",
                        server_name="jelly-1",
                        adapter_name="jellyfin_trickplay",
                        status=PublisherStatus.PUBLISHED_PENDING_REGISTRATION,
                        message="awaiting",
                    )
                ],
                frame_count=0,
                message="pending",
            )

        with (
            patch(
                "media_preview_generator.processing.retry_queue._BACKOFF",
                (0.05,) + tuple([0.05] * (len(_BACKOFF) - 1)),
            ),
            patch(
                "media_preview_generator.processing.multi_server.process_canonical_path",
                side_effect=fake_process,
            ),
        ):
            schedule_retry_for_unindexed(
                "/data/Foo.mkv",
                registry=MagicMock(),
                config=MagicMock(),
                item_id_by_server=None,
                attempt=1,
            )
            assert third.wait(timeout=3)
            time.sleep(0.1)

        chain_jobs = [j for j in jobs_mod._job_manager.get_all_jobs() if j.id.startswith("retry-")]
        assert len(chain_jobs) == 1
        child_ids = chain_jobs[0].config.get("child_job_ids") or []
        assert len(child_ids) >= 3, (
            f"Chain row must accumulate one child Job ID per firing; got {len(child_ids)} for {len(attempts)} attempts."
        )

    def test_attempt_job_log_captures_dispatch_logs(self, tmp_path):
        """The whole point of spawning a per-attempt Job: ``process_canonical_path``'s
        ``logger.info`` calls inside the timer thread land in THIS Job's
        log file (with ``INFO -`` level prefix) so the dashboard's
        ``colorizeLogLine`` paints them teal — same as every other Job
        log. Without the loguru sink + thread filter in
        ``_capture_attempt_logs``, the dispatch lines would only hit
        the global container log.
        """
        from loguru import logger as _logger

        import media_preview_generator.web.jobs as jobs_mod
        from media_preview_generator.processing.multi_server import (
            MultiServerResult,
            MultiServerStatus,
            PublisherResult,
            PublisherStatus,
        )
        from media_preview_generator.web.jobs import JobManager

        with jobs_mod._job_lock:
            jobs_mod._job_manager = JobManager(config_dir=str(tmp_path / "config"))

        done = threading.Event()

        def fake_process(**kwargs):
            # Stand in for process_canonical_path's own INFO emissions —
            # the test asserts THIS message lands in the per-attempt log.
            _logger.info("Test dispatch payload landed for {}", kwargs["canonical_path"])
            done.set()
            return MultiServerResult(
                canonical_path=kwargs["canonical_path"],
                status=MultiServerStatus.PUBLISHED,
                publishers=[
                    PublisherResult(
                        server_id="jelly-1",
                        server_name="jelly-1",
                        adapter_name="jellyfin_trickplay",
                        status=PublisherStatus.PUBLISHED,
                        message="ok",
                    )
                ],
                frame_count=1,
                message="ok",
            )

        with (
            patch(
                "media_preview_generator.processing.retry_queue._BACKOFF",
                (0.05,) + tuple([0.05] * (len(_BACKOFF) - 1)),
            ),
            patch(
                "media_preview_generator.processing.multi_server.process_canonical_path",
                side_effect=fake_process,
            ),
        ):
            schedule_retry_for_unindexed(
                "/data/Foo.mkv",
                registry=MagicMock(),
                config=MagicMock(),
                item_id_by_server=None,
                attempt=1,
            )
            assert done.wait(timeout=2)
            # Give the loguru `enqueue=True` sink time to drain.
            time.sleep(0.3)

        attempt_jobs = [j for j in jobs_mod._job_manager.get_all_jobs() if j.config.get("is_retry_attempt")]
        assert len(attempt_jobs) == 1
        logs = jobs_mod._job_manager.get_logs(attempt_jobs[0].id)
        joined = "\n".join(logs)
        assert "Test dispatch payload landed" in joined, (
            f"Dispatch logs MUST be captured into the per-attempt Job's log file — "
            f"otherwise the user opens the attempt Job and sees nothing. Got: {joined!r}"
        )
        # Level prefix is what triggers colour coding in the dashboard —
        # bug-blind without it.
        assert "INFO - " in joined, (
            "Per-attempt Job logs must include level prefixes so colorizeLogLine "
            "applies the same teal tint other Job logs get."
        )

    def test_attempt_job_marked_warning_when_chain_continues(self, tmp_path):
        """If the chain re-arms after this firing, the per-attempt Job
        finishes with a warning (amber pill) — the firing didn't fail
        but didn't end the chain either. Greens-only would mislead the
        operator into thinking the work is done.
        """
        import media_preview_generator.web.jobs as jobs_mod
        from media_preview_generator.processing.multi_server import (
            MultiServerResult,
            MultiServerStatus,
            PublisherResult,
            PublisherStatus,
        )
        from media_preview_generator.web.jobs import JobManager, JobStatus

        with jobs_mod._job_lock:
            jobs_mod._job_manager = JobManager(config_dir=str(tmp_path / "config"))

        attempts = []
        ready = threading.Event()

        def fake_process(**kwargs):
            attempts.append(kwargs.get("retry_attempt"))
            if len(attempts) >= 1:
                ready.set()
            # Always pending so the chain keeps re-arming.
            return MultiServerResult(
                canonical_path=kwargs["canonical_path"],
                status=MultiServerStatus.PUBLISHED,
                publishers=[
                    PublisherResult(
                        server_id="jelly-1",
                        server_name="jelly-1",
                        adapter_name="jellyfin_trickplay",
                        status=PublisherStatus.PUBLISHED_PENDING_REGISTRATION,
                        message="awaiting",
                    )
                ],
                frame_count=0,
                message="pending",
            )

        with (
            patch(
                "media_preview_generator.processing.retry_queue._BACKOFF",
                # Long enough for our first firing to land before the second.
                (0.05, 60, 60, 60, 60),
            ),
            patch(
                "media_preview_generator.processing.multi_server.process_canonical_path",
                side_effect=fake_process,
            ),
        ):
            schedule_retry_for_unindexed(
                "/data/Foo.mkv",
                registry=MagicMock(),
                config=MagicMock(),
                item_id_by_server=None,
                attempt=1,
            )
            assert ready.wait(timeout=2)
            time.sleep(0.2)

        attempt_jobs = [j for j in jobs_mod._job_manager.get_all_jobs() if j.config.get("is_retry_attempt")]
        # At least the first firing's job exists and is closed out.
        first = next((j for j in attempt_jobs if j.config["retry_attempt"] == 1), None)
        assert first is not None, "First attempt Job must exist after firing"
        assert first.status == JobStatus.COMPLETED
        assert first.error and "still pending" in first.error.lower(), (
            f"Re-armed chain → per-attempt Job MUST land as completed-with-warning, not silent green. "
            f"status={first.status} error={first.error!r}"
        )

    def test_attempt_job_marked_failed_when_dispatch_raises(self, tmp_path):
        """Matrix cell #4 (dispatch-crash): when ``process_canonical_path``
        itself raises (codec unsupported, frame_cache corrupted, …),
        the per-attempt Job must end as FAILED with the exception text
        in ``error`` — not green-completed, not silently warning-amber.
        Operators tracing a retry chain need the failure visible on the
        attempt row, not just buried in the global container log.
        """
        import media_preview_generator.web.jobs as jobs_mod
        from media_preview_generator.web.jobs import JobManager, JobStatus

        with jobs_mod._job_lock:
            jobs_mod._job_manager = JobManager(config_dir=str(tmp_path / "config"))

        crashed = threading.Event()

        def fake_process(**kwargs):
            crashed.set()
            raise RuntimeError("synthetic FFmpeg crash")

        with (
            patch(
                "media_preview_generator.processing.retry_queue._BACKOFF",
                # First firing uses 0.05s; we patch the recursive
                # schedule below to keep the crash-induced rescheduled
                # attempt from also firing inside the test window.
                (0.05, 60, 60, 60, 60),
            ),
            patch(
                "media_preview_generator.processing.multi_server.process_canonical_path",
                side_effect=fake_process,
            ),
        ):
            schedule_retry_for_unindexed(
                "/data/Foo.mkv",
                registry=MagicMock(),
                config=MagicMock(),
                item_id_by_server=None,
                attempt=1,
            )
            assert crashed.wait(timeout=2), "Dispatch must be invoked at least once"
            time.sleep(0.2)

        attempt_jobs = [j for j in jobs_mod._job_manager.get_all_jobs() if j.config.get("is_retry_attempt")]
        crashed_attempt = next((j for j in attempt_jobs if j.config["retry_attempt"] == 1), None)
        assert crashed_attempt is not None
        assert crashed_attempt.status == JobStatus.FAILED, (
            f"Dispatch crash → attempt Job status MUST be FAILED, got {crashed_attempt.status}"
        )
        assert crashed_attempt.error and "RuntimeError" in crashed_attempt.error, (
            f"Per-attempt Job's error must surface the exception class so the user can see "
            f"WHY the firing crashed; got {crashed_attempt.error!r}"
        )

    def test_attempt_job_marked_failed_on_chain_exhaustion(self, tmp_path):
        """Matrix cell #3 (exhaustion): when the chain's last firing
        still returns PENDING and the next attempt is past
        ``BACKOFF_SCHEDULE``, ``schedule_retry_for_unindexed`` returns
        False — the chain row goes to "exhausted" AND the per-attempt
        Job for that final firing must end as FAILED with the chain's
        exhaustion reason. Pre-fix this would have been silent
        green-completed because the only error path written was the
        dispatch-crash branch.
        """
        import media_preview_generator.web.jobs as jobs_mod
        from media_preview_generator.processing.multi_server import (
            MultiServerResult,
            MultiServerStatus,
            PublisherResult,
            PublisherStatus,
        )
        from media_preview_generator.web.jobs import JobManager, JobStatus

        with jobs_mod._job_lock:
            jobs_mod._job_manager = JobManager(config_dir=str(tmp_path / "config"))

        last_done = threading.Event()

        def fake_process(**kwargs):
            last_done.set()
            return MultiServerResult(
                canonical_path=kwargs["canonical_path"],
                status=MultiServerStatus.PUBLISHED,
                publishers=[
                    PublisherResult(
                        server_id="jelly-1",
                        server_name="jelly-1",
                        adapter_name="jellyfin_trickplay",
                        status=PublisherStatus.PUBLISHED_PENDING_REGISTRATION,
                        message="awaiting",
                    )
                ],
                frame_count=0,
                message="pending",
            )

        # Schedule directly at the LAST attempt index so the post-firing
        # reschedule attempt is past BACKOFF_SCHEDULE — that's the
        # ``rescheduled is False`` branch the test pins.
        last_idx = len(_BACKOFF)
        with (
            patch(
                "media_preview_generator.processing.retry_queue._BACKOFF",
                (0.05,) * len(_BACKOFF),
            ),
            patch(
                "media_preview_generator.processing.multi_server.process_canonical_path",
                side_effect=fake_process,
            ),
        ):
            schedule_retry_for_unindexed(
                "/data/Foo.mkv",
                registry=MagicMock(),
                config=MagicMock(),
                item_id_by_server=None,
                attempt=last_idx,
            )
            assert last_done.wait(timeout=2)
            time.sleep(0.2)

        attempt_jobs = [j for j in jobs_mod._job_manager.get_all_jobs() if j.config.get("is_retry_attempt")]
        last_attempt = next((j for j in attempt_jobs if j.config["retry_attempt"] == last_idx), None)
        assert last_attempt is not None
        assert last_attempt.status == JobStatus.FAILED, (
            f"Final-firing on an exhausted chain must end FAILED, got {last_attempt.status}"
        )
        assert last_attempt.error and "indexed" in last_attempt.error.lower(), (
            f"Exhaustion reason must propagate to the per-attempt Job's error; got {last_attempt.error!r}"
        )
        # The chain row also flips to FAILED (exhausted).
        chain_jobs = [j for j in jobs_mod._job_manager.get_all_jobs() if j.id.startswith("retry-")]
        assert chain_jobs and chain_jobs[0].status == JobStatus.FAILED

    def test_per_attempt_jobs_are_not_persisted_to_disk(self, tmp_path):
        """Per-attempt Jobs are EPHEMERAL the same way chain rows are.
        Pre-review they were going through ``create_job`` →
        ``_persist_job`` (and again on every ``complete_job`` /
        ``update_job_config``), which on a busy install (~88
        firings/hour × 5 BACKOFF stages) would accumulate thousands of
        orphaned rows in ``jobs.db`` — the same row-explosion pathology
        the chain-row skip already documents (2026-05-09 incident,
        5,387 rows).
        """
        import media_preview_generator.web.jobs as jobs_mod
        from media_preview_generator.processing.multi_server import (
            MultiServerResult,
            MultiServerStatus,
            PublisherResult,
            PublisherStatus,
        )
        from media_preview_generator.web.jobs import JobManager

        config_dir = tmp_path / "config"
        with jobs_mod._job_lock:
            jobs_mod._job_manager = JobManager(config_dir=str(config_dir))

        done = threading.Event()

        def fake_process(**kwargs):
            done.set()
            return MultiServerResult(
                canonical_path=kwargs["canonical_path"],
                status=MultiServerStatus.PUBLISHED,
                publishers=[
                    PublisherResult(
                        server_id="jelly-1",
                        server_name="jelly-1",
                        adapter_name="jellyfin_trickplay",
                        status=PublisherStatus.PUBLISHED,
                        message="ok",
                    )
                ],
                frame_count=1,
                message="ok",
            )

        with (
            patch(
                "media_preview_generator.processing.retry_queue._BACKOFF",
                (0.05,) + tuple([0.05] * (len(_BACKOFF) - 1)),
            ),
            patch(
                "media_preview_generator.processing.multi_server.process_canonical_path",
                side_effect=fake_process,
            ),
        ):
            schedule_retry_for_unindexed(
                "/data/Foo.mkv",
                registry=MagicMock(),
                config=MagicMock(),
                item_id_by_server=None,
                attempt=1,
            )
            assert done.wait(timeout=2)
            time.sleep(0.2)

        # Per-attempt Job IS visible in this JobManager instance...
        live_attempts = [j for j in jobs_mod._job_manager.get_all_jobs() if j.config.get("is_retry_attempt")]
        assert len(live_attempts) == 1

        # ...but a brand-new JobManager loading the same config dir
        # (simulating a container restart) MUST NOT see it. The retry
        # chain's threading.Timer is gone after restart, so the
        # per-attempt row would be orphaned with nothing to drive it.
        with jobs_mod._job_lock:
            jobs_mod._job_manager = None
        jm2 = JobManager(config_dir=str(config_dir))
        survivors = [j for j in jm2.get_all_jobs() if j.config.get("is_retry_attempt")]
        assert survivors == [], (
            f"Per-attempt retry Jobs MUST NOT survive a JobManager restart — they "
            f"are ephemeral by design (the chain timer driving them is gone). "
            f"Found {len(survivors)} orphaned attempt row(s); accumulating these "
            f"is the row-explosion pathology the chain-row skip documents."
        )
