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
