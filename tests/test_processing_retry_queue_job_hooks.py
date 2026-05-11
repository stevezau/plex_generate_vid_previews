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


def _seed_originating(jm, library_name="Origin"):
    """Create an originating dispatch Job for the chain to mutate.

    Post-rewrite the chain IS this Job — same UUID. ``schedule_retry_for_unindexed``
    requires ``originating_job_id`` to find which Job to mutate, so
    tests that exercise the retry queue must seed an originating Job
    first and pass its id.
    """
    j = jm.create_job(library_name=library_name, config={})
    jm.complete_job(j.id)
    return j.id


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
        origin_id = _seed_originating(jobs_mod._job_manager)

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
                originating_job_id=origin_id,
            )
        assert scheduled is True

        # Chain Job IS the originating dispatch (same UUID after rewrite).
        chain = jobs_mod._job_manager.get_job(origin_id)
        assert chain is not None
        assert chain.config["is_retry_chain"] is True
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
        origin_id = _seed_originating(jobs_mod._job_manager)

        registry = MagicMock()
        config = MagicMock()
        scheduled = schedule_retry_for_unindexed(
            "/data/Foo.mkv",
            registry=registry,
            config=config,
            item_id_by_server=None,
            attempt=len(_BACKOFF) + 1,  # past the end of the schedule
            originating_job_id=origin_id,
        )
        assert scheduled is False
        # Originating Job must NOT have been mutated into chain mode
        # when the backoff was already exhausted.
        chain = jobs_mod._job_manager.get_job(origin_id)
        assert chain is not None
        assert not chain.config.get("is_retry_chain")


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

        origin_id = _seed_originating(jobs_mod._job_manager)
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
                originating_job_id=origin_id,
            )
            assert chain_complete.wait(timeout=2)
            time.sleep(0.05)

        chain = jobs_mod._job_manager.get_job(origin_id)
        assert chain is not None
        assert chain.config["is_retry_chain"] is True
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
        origin_id = _seed_originating(jobs_mod._job_manager)

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
                originating_job_id=origin_id,
            )
            assert third_attempt.wait(timeout=3), (
                f"Chain didn't get to attempt 3 — attempts_seen={attempts_seen}. "
                "PUBLISHED_PENDING_REGISTRATION must drive continuation."
            )
            time.sleep(0.05)

        # The originating Job IS the chain — mutated in place, not
        # a separate row. Same UUID across all firings.
        chain = jobs_mod._job_manager.get_job(origin_id)
        assert chain is not None
        assert chain.config["is_retry_chain"] is True
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
        origin_id = _seed_originating(jobs_mod._job_manager)

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
                originating_job_id=origin_id,
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
        # ``parent_chain_id`` IS the originating dispatch's UUID after
        # the rewrite (chain = originating Job, same identity).
        assert attempt.config["parent_chain_id"] == origin_id, (
            f"parent_chain_id must point at the originating Job; got {attempt.config['parent_chain_id']!r}"
        )
        assert attempt.config["retry_attempt"] == 1
        assert attempt.config["retry_chain_for"] == "/data/Foo.mkv"

    def test_attempt_config_carries_chip_aliases_so_app_js_renders_chip(self, tmp_path):
        """app.js's retry-chip renderer (line ~1682) gates on
        ``config.is_retry && config.max_retries > 0``. Per-attempt
        Jobs MUST carry both aliases so the chip renders cleanly
        when the user opts into ``?include_retry_attempts=1`` —
        otherwise the prefix lands awkwardly in ``library_name``
        text (the exact UX bug the user reported pre-collapse).
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
        origin_id = _seed_originating(jobs_mod._job_manager)

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
                originating_job_id=origin_id,
            )
            assert done.wait(timeout=2)
            time.sleep(0.1)

        attempt = next(j for j in jobs_mod._job_manager.get_all_jobs() if j.config.get("is_retry_attempt"))
        assert attempt.config.get("is_retry") is True, "is_retry alias missing — app.js retry-chip renderer won't fire"
        assert attempt.config.get("max_retries") == len(_BACKOFF), (
            f"max_retries alias missing or wrong — got {attempt.config.get('max_retries')!r}, expected {len(_BACKOFF)}"
        )

    def test_attempt_library_name_is_clean_no_retry_prefix(self, tmp_path):
        """library_name must be the CLEAN title — no
        ``"Retry attempt N/M: …"`` prefix and no raw ``.mkv`` extension
        leak. The chip carries the retry-count info now; the title slot
        should read just like the parent dispatch row's title.
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

        # Pre-seed the originating dispatch Job with the clean Sonarr
        # title (matching the real webhook flow where the parent
        # dispatch's library_name is the cleaned title). The chain
        # will mutate THIS Job — and the per-attempt _create_retry_attempt_job
        # inherits library_name from the chain.
        origin = jobs_mod._job_manager.create_job(library_name="Standout The Ben Kjar Story (2026)", config={})
        jobs_mod._job_manager.complete_job(origin.id)
        origin_id = origin.id
        jobs_mod._job_manager.upsert_retry_chain_job(
            canonical_path="/data/Movies/Standout (2026).mkv",
            basename="Standout The Ben Kjar Story (2026)",
            attempt=1,
            max_attempts=5,
            next_run_at=None,
            wait_seconds=30,
            outcome="scheduled",
            originating_job_id=origin_id,
        )

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
            # Pass display_name explicitly — without it the retry-queue
            # callback's internal ``_upsert_retry_chain_job`` would
            # re-upsert the chain row with ``basename=os.path.basename(...)``
            # = "Standout (2026).mkv", and the "prefer shorter title"
            # heuristic at ``upsert_retry_chain_job:1138`` would clobber
            # our seeded clean title with the .mkv-bearing basename.
            # Real webhook flows propagate display_name from the worker.
            schedule_retry_for_unindexed(
                "/data/Movies/Standout (2026).mkv",
                registry=MagicMock(),
                config=MagicMock(),
                item_id_by_server=None,
                attempt=1,
                display_name="Standout The Ben Kjar Story (2026)",
                originating_job_id=origin_id,
            )
            assert done.wait(timeout=2)
            time.sleep(0.1)

        attempt = next(j for j in jobs_mod._job_manager.get_all_jobs() if j.config.get("is_retry_attempt"))
        assert "Retry attempt" not in attempt.library_name, (
            f"library_name must NOT include the 'Retry attempt N/M:' prefix — the "
            f"chip carries that. Got {attempt.library_name!r}."
        )
        assert ".mkv" not in attempt.library_name, (
            f"library_name must not leak the .mkv extension; got {attempt.library_name!r}"
        )
        assert attempt.library_name == "Standout The Ben Kjar Story (2026)", (
            f"library_name should inherit the chain row's cleaned title; got {attempt.library_name!r}"
        )

    def test_attempt_title_falls_back_to_splitext_basename_when_chain_missing(self, tmp_path):
        """When neither a chain row nor a display_name is available
        (CLI smoke test, headless retry from a path-only context),
        the fallback is ``os.path.splitext(os.path.basename(...))``
        — strips the ``.mkv`` so the title still reads better than
        the raw filename.
        """
        # Use schedule_retry_for_unindexed's lower-level helper to
        # exercise the no-chain-row path directly without dealing
        # with the timer thread.
        import media_preview_generator.web.jobs as jobs_mod
        from media_preview_generator.processing.retry_queue import _create_retry_attempt_job
        from media_preview_generator.web.jobs import JobManager

        with jobs_mod._job_lock:
            jobs_mod._job_manager = JobManager(config_dir=str(tmp_path / "config"))

        job_id = _create_retry_attempt_job(
            canonical_path="/data/Movies/Show [1080p][x264]-RELEASE.mkv",
            chain_id="retry-doesnotexist",
            attempt=1,
            max_attempts=5,
            server_id=None,
            server_name=None,
            server_type=None,
            display_name=None,
            source=None,
        )
        assert job_id is not None
        job = jobs_mod._job_manager.get_job(job_id)
        assert job is not None
        assert job.library_name == "Show [1080p][x264]-RELEASE", (
            f"Fallback must strip .mkv extension from basename; got {job.library_name!r}"
        )

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
        origin_id = _seed_originating(jobs_mod._job_manager)

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
                originating_job_id=origin_id,
            )
            assert third.wait(timeout=3)
            time.sleep(0.1)

        # Chain Job IS the originating dispatch (same UUID).
        chain = jobs_mod._job_manager.get_job(origin_id)
        assert chain is not None
        assert chain.config["is_retry_chain"] is True
        child_ids = chain.config.get("child_job_ids") or []
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
        origin_id = _seed_originating(jobs_mod._job_manager)

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
                originating_job_id=origin_id,
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
        # The chain Job IS the originating dispatch — also flips to FAILED.
        chain = jobs_mod._job_manager.get_job(origin_id)
        assert chain is not None
        assert chain.config["is_retry_chain"] is True
        assert chain.status == JobStatus.FAILED

    def test_completed_per_attempt_jobs_persist_across_restart(self, tmp_path):
        """Per-attempt Jobs PERSIST so the Job Details modal's Attempts
        dropdown can show history. A TERMINAL (completed/failed)
        attempt loads as-is — its log file on disk is the authoritative
        record of what happened in that firing. Pre-PLAN-collapse this
        contract was inverted (ephemeral); the row-count bound now
        comes from the `is_retry_attempt` filter in /api/jobs that
        hides children from the main list.
        """
        import media_preview_generator.web.jobs as jobs_mod
        from media_preview_generator.processing.multi_server import (
            MultiServerResult,
            MultiServerStatus,
            PublisherResult,
            PublisherStatus,
        )
        from media_preview_generator.web.jobs import JobManager, JobStatus

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

        # Per-attempt Job is visible AND terminal in this JobManager instance.
        live_attempts = [j for j in jobs_mod._job_manager.get_all_jobs() if j.config.get("is_retry_attempt")]
        assert len(live_attempts) == 1
        assert live_attempts[0].status == JobStatus.COMPLETED

        # Restart simulation: a brand-new JobManager loading the same
        # config dir DOES see the terminal attempt unchanged. Modal's
        # Attempts dropdown reads this list to populate its options.
        with jobs_mod._job_lock:
            jobs_mod._job_manager = None
        jm2 = JobManager(config_dir=str(config_dir))
        survivors = [j for j in jm2.get_all_jobs() if j.config.get("is_retry_attempt")]
        assert len(survivors) == 1, (
            f"Per-attempt retry Jobs MUST survive restart so the modal can show history. "
            f"Got {len(survivors)} survivor(s)."
        )
        assert survivors[0].status == JobStatus.COMPLETED, (
            f"Terminal attempt Jobs must load as-is; got {survivors[0].status}"
        )

    def test_attempt_files_tab_gets_file_result_per_firing(self, tmp_path):
        """The Job Details modal's Files tab reads from
        ``record_file_result`` JSONL keyed by Job UUID. Pre-fix the
        retry queue's ``_callback`` ran ``process_canonical_path``
        directly in the timer thread with no wiring to
        ``record_file_result`` (that hook lives in ``job_runner.py``'s
        completion handler, which retries bypass). The result: Files
        tab for every per-attempt Job rendered an empty
        "No file results available" message.

        Fix: ``_record_attempt_file_result`` after each firing writes
        a per-canonical-path row with the aggregate ``MultiServerStatus``
        and a publisher breakdown.
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

        done = threading.Event()

        def fake_process(**kwargs):
            done.set()
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
                    ),
                    PublisherResult(
                        server_id="plex-default",
                        server_name="Plex",
                        adapter_name="plex_bundle",
                        status=PublisherStatus.PUBLISHED_PENDING_REGISTRATION,
                        message="awaiting",
                    ),
                ],
                frame_count=1,
                message="Published with 1 pending registration",
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

        # Pick attempt #1 specifically — subsequent firings may fall
        # through to the REAL process_canonical_path once the
        # context-manager-bounded patch exits (BACKOFF=0.05s means
        # retries fire so fast they outrun the with-block), and those
        # would record different file_result rows.
        attempts = [j for j in jobs_mod._job_manager.get_all_jobs() if j.config.get("is_retry_attempt")]
        first = next((j for j in attempts if j.config["retry_attempt"] == 1), None)
        assert first is not None, "First-firing attempt Job must exist"
        results = jobs_mod._job_manager.get_file_results(first.id)
        assert len(results) == 1, (
            f"Per-attempt Job MUST have exactly one file_result for the canonical path; "
            f"got {len(results)}. Files tab would render empty without this."
        )
        r = results[0]
        assert r["file"] == "/data/Foo.mkv", f"file_result keyed on canonical path; got {r['file']!r}"
        assert r["outcome"] == "published", f"outcome must reflect MultiServerStatus.PUBLISHED; got {r['outcome']!r}"
        assert "Published" in (r.get("reason") or ""), (
            f"reason must surface MultiServerResult.message; got {r.get('reason')!r}"
        )
        servers = r.get("servers") or []
        assert len(servers) == 2, f"Per-server pills should map 1:1 with PublisherResult entries; got {len(servers)}"
        # Vendor-type derivation runs on adapter_name substring.
        types = {s.get("type") for s in servers}
        assert types == {"jellyfin", "plex"}, f"Vendor types must derive from adapter names; got {types}"

    def test_inflight_per_attempt_jobs_marked_failed_on_restart(self, tmp_path):
        """Counterpoint: a PENDING/RUNNING attempt Job recovered from
        disk at restart MUST be marked FAILED with the interruption
        reason — its parent chain's ``threading.Timer`` is gone, so
        leaving it PENDING would show a row counting down to a Timer
        that will never fire.
        """
        import media_preview_generator.web.jobs as jobs_mod
        from media_preview_generator.web.jobs import Job, JobManager, JobStatus, JobStorage

        with jobs_mod._job_lock:
            jobs_mod._job_manager = None

        config_dir = tmp_path / "config_inflight"
        config_dir.mkdir()
        storage = JobStorage(str(config_dir / "jobs.db"))
        inflight_attempt = Job(
            id="abc-uuid-pending",
            library_name="In flight at crash",
            status=JobStatus.RUNNING,
            config={
                "is_retry_attempt": True,
                "parent_chain_id": "retry-aaa",
                "retry_attempt": 2,
                "retry_max_attempts": 5,
            },
        )
        storage.upsert(inflight_attempt)
        storage.close()

        jm = JobManager(config_dir=str(config_dir))
        recovered = next((j for j in jm.get_all_jobs() if j.id == "abc-uuid-pending"), None)
        assert recovered is not None
        assert recovered.status == JobStatus.FAILED
        assert recovered.error and "interrupted" in recovered.error.lower()
