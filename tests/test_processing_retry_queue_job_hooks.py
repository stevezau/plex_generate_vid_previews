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
