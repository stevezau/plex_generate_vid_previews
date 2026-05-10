"""Tests for chain-row cancellation cascade — see ``cancel_job`` in
``media_preview_generator/web/jobs.py``.

Pre-fix the chain row's Cancel button marked the chain CANCELLED in
the UI but did NOT call ``RetryScheduler.cancel(canonical_path)`` or
touch the in-flight per-attempt child Jobs. The pending Timer kept
counting down and the next firing executed regardless. These tests
pin both halves of the cascade so the bug stays fixed.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


@pytest.fixture(autouse=True)
def _reset_singletons():
    import media_preview_generator.web.jobs as jobs_mod
    from media_preview_generator.processing.retry_queue import reset_retry_scheduler

    with jobs_mod._job_lock:
        jobs_mod._job_manager = None
    reset_retry_scheduler()
    yield
    with jobs_mod._job_lock:
        jobs_mod._job_manager = None
    reset_retry_scheduler()


def _make_jm(tmp_path):
    import media_preview_generator.web.jobs as jobs_mod
    from media_preview_generator.web.jobs import JobManager

    config_dir = tmp_path / "config"
    jm = JobManager(config_dir=str(config_dir))
    with jobs_mod._job_lock:
        jobs_mod._job_manager = jm
    return jm


class TestChainCancelCancelsTimer:
    def test_cancel_chain_calls_retry_scheduler_cancel(self, tmp_path):
        """``cancel_job`` on a chain row MUST also call
        ``RetryScheduler.cancel(canonical_path)`` so the in-flight
        Timer stops counting down — otherwise the next firing still
        executes after the user clicked Cancel.
        """
        from media_preview_generator.processing.retry_queue import (
            get_retry_scheduler,
            schedule_retry_for_unindexed,
        )

        jm = _make_jm(tmp_path)

        # Use a long backoff so the Timer is still pending when we cancel.
        path = "/data/Foo.mkv"
        from unittest.mock import patch

        with patch(
            "media_preview_generator.processing.retry_queue._BACKOFF",
            (3600, 3600, 3600, 3600, 3600),
        ):
            scheduled = schedule_retry_for_unindexed(
                path,
                registry=MagicMock(),
                config=MagicMock(),
                item_id_by_server=None,
                attempt=1,
            )
        assert scheduled is True
        scheduler = get_retry_scheduler()
        assert scheduler.pending_count() == 1, "Timer should be pending before cancel"

        chain = next(j for j in jm.get_all_jobs() if j.id.startswith("retry-"))
        jm.cancel_job(chain.id)

        assert scheduler.pending_count() == 0, (
            "RetryScheduler.cancel was not called — the Timer is still pending. "
            "Chain-row cancel must cascade to the scheduler, otherwise the next "
            "firing executes after the user clicked Cancel."
        )

    def test_cancel_non_chain_job_does_not_touch_scheduler(self, tmp_path):
        """Cancelling a regular (non-chain) Job MUST NOT touch the
        RetryScheduler — the cascade is chain-row-only.
        """
        from media_preview_generator.processing.retry_queue import (
            get_retry_scheduler,
            schedule_retry_for_unindexed,
        )

        jm = _make_jm(tmp_path)

        # Set up a pending Timer.
        from unittest.mock import patch

        with patch(
            "media_preview_generator.processing.retry_queue._BACKOFF",
            (3600, 3600, 3600, 3600, 3600),
        ):
            schedule_retry_for_unindexed(
                "/data/Foo.mkv",
                registry=MagicMock(),
                config=MagicMock(),
                item_id_by_server=None,
                attempt=1,
            )
        assert get_retry_scheduler().pending_count() == 1

        # Create + cancel an unrelated regular Job.
        regular = jm.create_job(library_name="Regular", config={})
        jm.cancel_job(regular.id)

        assert get_retry_scheduler().pending_count() == 1, (
            "Cancelling a regular job should NOT cancel unrelated retry Timers."
        )


class TestChainCancelCancelsChildren:
    def test_cancel_chain_marks_inflight_child_attempts_cancelled(self, tmp_path):
        """When the chain row is cancelled, any in-flight per-attempt
        child Jobs (status PENDING or RUNNING with matching
        ``parent_chain_id``) must be marked CANCELLED with a clear
        ``error`` message — otherwise they sit in the modal Attempts
        dropdown forever showing "running" against a dead Timer.
        """
        from media_preview_generator.web.jobs import JobStatus

        jm = _make_jm(tmp_path)

        chain = jm.upsert_retry_chain_job(
            canonical_path="/data/Foo.mkv",
            basename="Foo",
            attempt=1,
            max_attempts=5,
            next_run_at=None,
            wait_seconds=30,
            outcome="scheduled",
        )

        # Hand-create two in-flight children + one already-terminal.
        running_child = jm.create_job(
            library_name="Foo",
            config={
                "is_retry_attempt": True,
                "parent_chain_id": chain.id,
                "retry_attempt": 1,
                "retry_max_attempts": 5,
            },
        )
        running_child.status = JobStatus.RUNNING
        jm._persist_job(running_child)
        pending_child = jm.create_job(
            library_name="Foo",
            config={
                "is_retry_attempt": True,
                "parent_chain_id": chain.id,
                "retry_attempt": 2,
                "retry_max_attempts": 5,
            },
        )
        # PENDING by default.
        done_child = jm.create_job(
            library_name="Foo",
            config={
                "is_retry_attempt": True,
                "parent_chain_id": chain.id,
                "retry_attempt": 0,  # earlier attempt that already finished
                "retry_max_attempts": 5,
            },
        )
        jm.complete_job(done_child.id)
        assert jm.get_job(done_child.id).status == JobStatus.COMPLETED

        jm.cancel_job(chain.id)

        # Both in-flight children flip to CANCELLED with the cascade reason.
        for child_id in (running_child.id, pending_child.id):
            child = jm.get_job(child_id)
            assert child.status == JobStatus.CANCELLED, (
                f"In-flight child {child_id} must be CANCELLED after chain cancel; got {child.status}"
            )
            assert child.error and "Parent retry chain was cancelled" in child.error, (
                f"Child cancel reason must mention parent chain; got {child.error!r}"
            )

        # The already-completed child is left alone.
        assert jm.get_job(done_child.id).status == JobStatus.COMPLETED, (
            "Already-terminal children must NOT be overwritten by the cascade — "
            "their state is the user's record of what happened."
        )

    def test_cancel_chain_skips_children_of_other_chains(self, tmp_path):
        """The cascade matches on ``parent_chain_id`` — a child of a
        DIFFERENT chain must be untouched when this chain is
        cancelled. Bug shape: a global "cancel all in-flight retry
        attempts" sweep would lose isolation between chains.
        """
        from media_preview_generator.web.jobs import JobStatus

        jm = _make_jm(tmp_path)

        chain_a = jm.upsert_retry_chain_job(
            canonical_path="/data/A.mkv",
            basename="A",
            attempt=1,
            max_attempts=5,
            next_run_at=None,
            wait_seconds=30,
            outcome="scheduled",
        )
        chain_b = jm.upsert_retry_chain_job(
            canonical_path="/data/B.mkv",
            basename="B",
            attempt=1,
            max_attempts=5,
            next_run_at=None,
            wait_seconds=30,
            outcome="scheduled",
        )
        child_b = jm.create_job(
            library_name="B",
            config={
                "is_retry_attempt": True,
                "parent_chain_id": chain_b.id,
                "retry_attempt": 1,
                "retry_max_attempts": 5,
            },
        )

        jm.cancel_job(chain_a.id)

        assert jm.get_job(child_b.id).status == JobStatus.PENDING, (
            "Cancelling chain A must NOT cancel chain B's children — the cascade must filter by parent_chain_id."
        )


class TestCancelDuringFiringRace:
    """TOCTOU race between user clicking Cancel and the Timer's
    ``_fire`` callback executing.

    Pre-PLAN-collapse the chain row's Cancel was unwired entirely
    (commit before f0f08a6) so this race didn't matter — the next
    firing would still execute. With the cascade wired in 5dea715,
    the architecture review flagged a remaining hole: if the user
    cancels at the EXACT instant a Timer's ``_fire`` pops the timer
    from the dict but hasn't yet run the callback,
    ``RetryScheduler.cancel`` becomes a no-op (the Timer is no longer
    in the dict to cancel). The callback then proceeds to spawn a
    "ghost" attempt under a CANCELLED chain.

    These tests force that exact ordering — Timer mid-fire, then
    cancel — and pin the cascade's protective behavior: even if a
    ghost attempt slips through, it must be marked CANCELLED so the
    modal Attempts dropdown doesn't show stale "running" rows under
    a cancelled chain.
    """

    def test_attempt_created_during_cancel_window_gets_cancelled_by_cascade(self, tmp_path):
        """Force the race: pause the Timer's callback so it's mid-
        execution when cancel runs. The cascade re-snapshots child
        Jobs INSIDE the JobManager lock after releasing it once for
        the scheduler call, so any attempt spawned between snapshot
        and cancel must still get caught.

        Pattern: monkey-patch ``process_canonical_path`` to wait on
        an event before returning. While it's waiting, we issue the
        cancel from the test thread. After we release the event, the
        callback proceeds to spawn its attempt + record file_result.
        We then assert the spawned attempt was cancelled — either by
        the cascade catching it on a SECOND pass, or by an explicit
        race-protective re-snap.
        """
        import threading
        import time
        from unittest.mock import patch

        from media_preview_generator.processing.multi_server import (
            MultiServerResult,
            MultiServerStatus,
            PublisherResult,
            PublisherStatus,
        )
        from media_preview_generator.processing.retry_queue import (
            _BACKOFF,
            schedule_retry_for_unindexed,
        )
        from media_preview_generator.web.jobs import JobStatus

        jm = _make_jm(tmp_path)

        # Used to block process_canonical_path mid-execution so we can
        # issue the cancel before the callback finishes spawning the
        # attempt Job + recording its file_result.
        callback_running = threading.Event()
        release_callback = threading.Event()

        def slow_process(**kwargs):
            callback_running.set()
            # Block until the test releases us (cancel happens here).
            release_callback.wait(timeout=3)
            return MultiServerResult(
                canonical_path=kwargs["canonical_path"],
                status=MultiServerStatus.PUBLISHED,
                publishers=[
                    PublisherResult(
                        server_id="jelly-1",
                        server_name="JellyTest",
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
                side_effect=slow_process,
            ),
        ):
            schedule_retry_for_unindexed(
                "/data/Race.mkv",
                registry=MagicMock(),
                config=MagicMock(),
                item_id_by_server=None,
                attempt=1,
            )
            # Wait until the Timer has fired and process_canonical_path
            # is in flight. At this point the attempt Job has been
            # created (it happens before process_canonical_path is
            # called inside _callback) and the chain row was upserted
            # to "running" — but the callback is BLOCKED.
            assert callback_running.wait(timeout=2), "Timer should have fired"

            # Find the chain id from the live state.
            chain = next(j for j in jm.get_all_jobs() if j.id.startswith("retry-"))
            inflight_attempt = next(
                (j for j in jm.get_all_jobs() if j.config.get("is_retry_attempt")),
                None,
            )
            assert inflight_attempt is not None, "Attempt Job should have been created before process_canonical_path"

            # Issue cancel WHILE the callback is mid-execution.
            jm.cancel_job(chain.id)

            # Chain is now CANCELLED. Release the callback to finish.
            release_callback.set()
            # Give the callback time to record file_result + try to
            # mark itself completed (it will hit the
            # already-cancelled short-circuit in complete_job).
            time.sleep(0.3)

        # Post-race state:
        chain_final = jm.get_job(chain.id)
        assert chain_final.status == JobStatus.CANCELLED, (
            f"Chain row must stay CANCELLED after race; got {chain_final.status}"
        )
        attempt_final = jm.get_job(inflight_attempt.id)
        assert attempt_final.status == JobStatus.CANCELLED, (
            f"In-flight attempt caught by the cascade must be CANCELLED, not "
            f"COMPLETED by the post-callback complete_job. Got {attempt_final.status} "
            f"error={attempt_final.error!r}. If this test fails the race is real: "
            f"the callback's complete_job ran AFTER cancel and resurrected the "
            f"attempt as COMPLETED — modal Attempts dropdown then shows a "
            f"completed attempt under a cancelled chain."
        )

    def test_no_new_firings_after_cancel(self, tmp_path):
        """After a chain is cancelled, no further attempt Jobs may
        be spawned regardless of where the in-flight callback was.
        Even if a Timer slipped through the cancel window and its
        callback re-schedules (the existing _callback re-arms the
        chain via schedule_retry_for_unindexed for PENDING outcomes),
        that re-armed Timer's CALLBACK must short-circuit when it
        sees the chain is CANCELLED — otherwise we get an infinite
        cascade of cancel-then-resurrect.

        Currently the retry_queue does NOT short-circuit on
        already-cancelled chains. This test pins the desired
        behavior: count attempt Jobs before vs 500ms after cancel.
        Pre-fix this can FAIL because nothing in retry_queue checks
        whether the chain row is still alive before spawning the
        next attempt.
        """
        import threading
        import time
        from unittest.mock import patch

        from media_preview_generator.processing.multi_server import (
            MultiServerResult,
            MultiServerStatus,
            PublisherResult,
            PublisherStatus,
        )
        from media_preview_generator.processing.retry_queue import (
            schedule_retry_for_unindexed,
        )

        jm = _make_jm(tmp_path)

        first_fired = threading.Event()
        fires = []

        def fake_process(**kwargs):
            fires.append(kwargs.get("retry_attempt"))
            first_fired.set()
            return MultiServerResult(
                canonical_path=kwargs["canonical_path"],
                status=MultiServerStatus.PUBLISHED,
                publishers=[
                    PublisherResult(
                        server_id="jelly-1",
                        server_name="JellyTest",
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
                (0.05, 0.5, 0.5, 0.5, 0.5),
            ),
            patch(
                "media_preview_generator.processing.multi_server.process_canonical_path",
                side_effect=fake_process,
            ),
        ):
            schedule_retry_for_unindexed(
                "/data/Race.mkv",
                registry=MagicMock(),
                config=MagicMock(),
                item_id_by_server=None,
                attempt=1,
            )
            # Wait for the first firing.
            assert first_fired.wait(timeout=2)
            time.sleep(0.1)  # let it record file_result + complete

            # Cancel BEFORE the next Timer fires (next backoff is 0.5s).
            chain = next(j for j in jm.get_all_jobs() if j.id.startswith("retry-"))
            attempts_at_cancel = len([j for j in jm.get_all_jobs() if j.config.get("is_retry_attempt")])
            jm.cancel_job(chain.id)

            # Wait past the next would-be firing window.
            time.sleep(0.8)

            attempts_after_cancel = len([j for j in jm.get_all_jobs() if j.config.get("is_retry_attempt")])

        assert attempts_after_cancel == attempts_at_cancel, (
            f"No new attempts may spawn after cancel. Before cancel: "
            f"{attempts_at_cancel} attempts; after 0.8s wait past next-fire "
            f"window: {attempts_after_cancel}. Fires recorded: {fires}. "
            f"If this fails, a Timer scheduled BEFORE cancel kept firing "
            f"and the cascade only protected the in-flight callback, not "
            f"the re-armed one."
        )
