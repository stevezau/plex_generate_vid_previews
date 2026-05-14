"""
Tests for the JobDispatcher multi-job concurrent dispatch system.

Verifies that multiple jobs can share workers, idle workers pick up items
from the next job, and per-job pause/cancel work independently.
"""

import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from media_preview_generator.jobs.dispatcher import (
    JobDispatcher,
    JobTracker,
    reset_dispatcher,
)
from media_preview_generator.jobs.worker import WorkerPool
from media_preview_generator.processing import (
    CodecNotSupportedError,
)
from tests.conftest import _ms, _pi, _pi_list_or_passthrough  # noqa: F401

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(cpu_threads=1, gpu_threads=0):
    config = MagicMock()
    config.cpu_threads = cpu_threads
    config.gpu_threads = gpu_threads
    config.worker_pool_timeout = 5
    return config


def _make_gpu_list(n=1):
    return [("nvidia", f"/dev/nvidia{i}", {"name": f"RTX 4090 #{i}"}) for i in range(n)]


def _fake_process_item(*args, **kwargs):
    time.sleep(0.02)
    return _ms("generated")


# ---------------------------------------------------------------------------
# JobTracker unit tests
# ---------------------------------------------------------------------------


class TestJobTracker:
    def test_record_completion_success(self):
        tracker = JobTracker(
            job_id="job-1",
            items=_pi_list_or_passthrough([("k1", "t1", "movie"), ("k2", "t2", "movie")]),
            config=_make_config(),
            registry=MagicMock(),
        )
        assert tracker.total_items == 2
        assert tracker.completed == 0

        tracker.record_completion(True, "Worker 1", "t1")
        assert tracker.successful == 1
        assert tracker.failed == 0
        assert not tracker.done_event.is_set()

        tracker.record_completion(True, "Worker 2", "t2")
        assert tracker.successful == 2
        assert tracker.done_event.is_set()

    def test_record_completion_failure(self):
        tracker = JobTracker(
            job_id="job-1",
            items=_pi_list_or_passthrough([("k1", "t1", "movie")]),
            config=_make_config(),
            registry=MagicMock(),
        )
        tracker.record_completion(False, "Worker 1", "t1")
        assert tracker.failed == 1
        assert tracker.done_event.is_set()

    def test_cancel_drains_queue(self):
        items = [("k1", "t1", "movie"), ("k2", "t2", "movie"), ("k3", "t3", "movie")]
        tracker = JobTracker(
            job_id="job-1",
            items=_pi_list_or_passthrough(items),
            config=_make_config(),
            registry=MagicMock(),
        )
        assert len(tracker.item_queue) == 3
        tracker.cancel()
        assert len(tracker.item_queue) == 0
        assert tracker.cancelled
        assert tracker.done_event.is_set()
        assert tracker.failed == 3

    def test_get_result(self):
        tracker = JobTracker(
            job_id="job-1",
            items=_pi_list_or_passthrough([("k1", "t1", "movie")]),
            config=_make_config(),
            registry=MagicMock(),
        )
        tracker.record_completion(True, "W", "t1")
        result = tracker.get_result()
        assert result["completed"] == 1
        assert result["failed"] == 0
        assert result["total"] == 1
        assert result["cancelled"] is False

    def test_callbacks_fire(self):
        progress_calls = []
        item_calls = []

        tracker = JobTracker(
            job_id="job-1",
            items=_pi_list_or_passthrough([("k1", "t1", "movie")]),
            config=_make_config(),
            registry=MagicMock(),
            callbacks={
                "progress_callback": lambda c, t, m, percent_override=None: progress_calls.append((c, t)),
                "on_item_complete": lambda dn, t, s: item_calls.append((dn, t, s)),
            },
        )
        tracker.record_completion(True, "CPU 1", "t1")
        assert len(progress_calls) == 1
        assert progress_calls[0] == (1, 1)
        assert item_calls == [("CPU 1", "t1", True)]


# ---------------------------------------------------------------------------
# JobDispatcher integration tests
# ---------------------------------------------------------------------------


class TestJobDispatcher:
    @pytest.fixture(autouse=True)
    def _reset(self):
        reset_dispatcher()
        yield
        reset_dispatcher()

    @patch("media_preview_generator.processing.multi_server.process_canonical_path")
    def test_single_job_completes(self, mock_process):
        """A single job with multiple items completes via the dispatcher."""
        mock_process.side_effect = _fake_process_item

        pool = WorkerPool(gpu_workers=0, cpu_workers=2, selected_gpus=[])
        dispatcher = JobDispatcher(pool)

        items = [
            ("/key/1", "Movie 1", "movie"),
            ("/key/2", "Movie 2", "movie"),
            ("/key/3", "Movie 3", "movie"),
        ]
        tracker = dispatcher.submit_items(
            job_id="job-1",
            items=_pi_list_or_passthrough(items),
            config=_make_config(),
            registry=MagicMock(),
        )
        completed = tracker.wait(timeout=10)
        assert completed, "Tracker should complete within timeout"
        result = tracker.get_result()
        assert result["completed"] == 3
        assert result["failed"] == 0
        dispatcher.shutdown()

    @patch("media_preview_generator.processing.multi_server.process_canonical_path")
    def test_two_jobs_share_workers(self, mock_process):
        """Two jobs submitted concurrently share workers."""
        mock_process.side_effect = _fake_process_item

        pool = WorkerPool(gpu_workers=0, cpu_workers=3, selected_gpus=[])
        dispatcher = JobDispatcher(pool)

        items_a = [("/a/1", "A1", "movie"), ("/a/2", "A2", "movie")]
        items_b = [
            ("/b/1", "B1", "movie"),
            ("/b/2", "B2", "movie"),
            ("/b/3", "B3", "movie"),
        ]

        tracker_a = dispatcher.submit_items(
            job_id="job-a",
            items=_pi_list_or_passthrough(items_a),
            config=_make_config(),
            registry=MagicMock(),
        )
        tracker_b = dispatcher.submit_items(
            job_id="job-b",
            items=_pi_list_or_passthrough(items_b),
            config=_make_config(),
            registry=MagicMock(),
        )

        assert tracker_a.wait(timeout=10)
        assert tracker_b.wait(timeout=10)

        assert tracker_a.get_result()["completed"] == 2
        assert tracker_b.get_result()["completed"] == 3
        dispatcher.shutdown()

    @patch("media_preview_generator.processing.multi_server.process_canonical_path")
    def test_idle_workers_pick_up_next_job(self, mock_process):
        """If job A has 1 item and 3 workers, free workers spill to job B."""
        call_log = []

        def tracking_process(*args, canonical_path=None, **kwargs):
            call_log.append(canonical_path)
            time.sleep(0.05)
            return _ms("generated", canonical_path=canonical_path or "/data/test.mkv")

        mock_process.side_effect = tracking_process

        pool = WorkerPool(gpu_workers=0, cpu_workers=3, selected_gpus=[])
        dispatcher = JobDispatcher(pool)

        tracker_a = dispatcher.submit_items(
            job_id="job-a",
            items=_pi_list_or_passthrough([("/a/1", "A1", "movie")]),
            config=_make_config(),
            registry=MagicMock(),
        )
        tracker_b = dispatcher.submit_items(
            job_id="job-b",
            items=_pi_list_or_passthrough(
                [
                    ("/b/1", "B1", "movie"),
                    ("/b/2", "B2", "movie"),
                    ("/b/3", "B3", "movie"),
                ]
            ),
            config=_make_config(),
            registry=MagicMock(),
        )

        assert tracker_a.wait(timeout=10)
        assert tracker_b.wait(timeout=10)

        assert tracker_a.get_result()["completed"] == 1
        assert tracker_b.get_result()["completed"] == 3

        # All 4 items should be processed; the /a/1 canonical_path must appear.
        # _pi() builds canonical_paths from the first tuple element via
        # f"/data/{key.strip('/').replace('/', '_')}.mkv".
        assert "/data/a_1.mkv" in call_log
        assert set(call_log) == {"/data/a_1.mkv", "/data/b_1.mkv", "/data/b_2.mkv", "/data/b_3.mkv"}
        dispatcher.shutdown()

    @patch("media_preview_generator.processing.multi_server.process_canonical_path")
    def test_cancel_one_job_others_continue(self, mock_process):
        """Cancelling one job does not affect other jobs."""
        processing_started = threading.Event()

        def slow_process(*args, **kwargs):
            processing_started.set()
            time.sleep(0.15)
            return _ms("generated")

        mock_process.side_effect = slow_process

        pool = WorkerPool(gpu_workers=0, cpu_workers=2, selected_gpus=[])
        dispatcher = JobDispatcher(pool)

        cancelled = threading.Event()

        tracker_a = dispatcher.submit_items(
            job_id="job-a",
            items=_pi_list_or_passthrough(
                [
                    ("/a/1", "A1", "movie"),
                    ("/a/2", "A2", "movie"),
                    ("/a/3", "A3", "movie"),
                ]
            ),
            config=_make_config(),
            registry=MagicMock(),
            callbacks={"cancel_check": lambda: cancelled.is_set()},
        )
        tracker_b = dispatcher.submit_items(
            job_id="job-b",
            items=_pi_list_or_passthrough([("/b/1", "B1", "movie")]),
            config=_make_config(),
            registry=MagicMock(),
        )

        # Wait for processing to actually start before cancelling — previously
        # a flaky 50 ms sleep could beat the dispatcher on slow CI agents.
        assert processing_started.wait(timeout=5), "expected processing to start"
        cancelled.set()

        assert tracker_a.wait(timeout=10)
        assert tracker_b.wait(timeout=10)

        result_a = tracker_a.get_result()
        result_b = tracker_b.get_result()
        assert result_a["cancelled"]
        assert result_b["completed"] == 1
        assert not result_b["cancelled"]
        dispatcher.shutdown()

    @patch("media_preview_generator.processing.multi_server.process_canonical_path")
    def test_pause_one_job_others_continue(self, mock_process):
        """Pausing one job lets other jobs continue."""
        mock_process.side_effect = _fake_process_item

        pool = WorkerPool(gpu_workers=0, cpu_workers=2, selected_gpus=[])
        dispatcher = JobDispatcher(pool)

        paused = threading.Event()
        paused.set()  # Start paused

        # Job A is paused; Job B should still run
        tracker_a = dispatcher.submit_items(
            job_id="job-a",
            items=_pi_list_or_passthrough([("/a/1", "A1", "movie")]),
            config=_make_config(),
            registry=MagicMock(),
            callbacks={"pause_check": lambda: paused.is_set()},
        )
        tracker_b = dispatcher.submit_items(
            job_id="job-b",
            items=_pi_list_or_passthrough([("/b/1", "B1", "movie"), ("/b/2", "B2", "movie")]),
            config=_make_config(),
            registry=MagicMock(),
        )

        # Job B should finish while A is paused
        assert tracker_b.wait(timeout=10)
        assert tracker_b.get_result()["completed"] == 2

        # Job A should not be done yet
        assert not tracker_a.done_event.is_set()
        assert tracker_a.completed == 0

        # Unpause job A
        paused.clear()
        assert tracker_a.wait(timeout=10)
        assert tracker_a.get_result()["completed"] == 1
        dispatcher.shutdown()

    @patch("media_preview_generator.processing.multi_server.process_canonical_path")
    def test_gpu_fallback_routes_to_correct_job(self, mock_process):
        """GPU codec fallback items route back to the correct job tracker."""
        call_count = {"gpu": 0, "cpu": 0}

        def mixed_process(*args, gpu=None, **kwargs):
            if gpu:
                call_count["gpu"] += 1
                raise CodecNotSupportedError("unsupported on GPU")
            call_count["cpu"] += 1
            time.sleep(0.02)
            return _ms("generated")

        mock_process.side_effect = mixed_process

        gpus = _make_gpu_list(1)
        pool = WorkerPool(gpu_workers=1, cpu_workers=1, selected_gpus=gpus)
        dispatcher = JobDispatcher(pool)

        config = _make_config(cpu_threads=1, gpu_threads=1)
        tracker = dispatcher.submit_items(
            job_id="job-fb",
            items=_pi_list_or_passthrough([("/fb/1", "Fallback Movie", "movie")]),
            config=config,
            registry=MagicMock(),
        )

        assert tracker.wait(timeout=10)
        result = tracker.get_result()
        # The item should complete once (via CPU fallback)
        assert result["completed"] == 1
        assert result["failed"] == 0
        dispatcher.shutdown()

    @patch("media_preview_generator.processing.multi_server.process_canonical_path")
    def test_mixed_success_and_failure(self, mock_process):
        """Items that succeed and fail are tracked correctly per job."""
        call_idx = {"n": 0}

        def alternating_process(*args, **kwargs):
            call_idx["n"] += 1
            time.sleep(0.02)
            if call_idx["n"] % 2 == 0:
                raise RuntimeError("boom")
            return _ms("generated")

        mock_process.side_effect = alternating_process

        pool = WorkerPool(gpu_workers=0, cpu_workers=2, selected_gpus=[])
        dispatcher = JobDispatcher(pool)

        items = [
            ("/k/1", "M1", "movie"),
            ("/k/2", "M2", "movie"),
            ("/k/3", "M3", "movie"),
            ("/k/4", "M4", "movie"),
        ]
        tracker = dispatcher.submit_items(
            job_id="job-mix",
            items=_pi_list_or_passthrough(items),
            config=_make_config(),
            registry=MagicMock(),
        )
        assert tracker.wait(timeout=10)
        result = tracker.get_result()
        assert result["completed"] + result["failed"] == 4
        dispatcher.shutdown()

    @patch("media_preview_generator.processing.multi_server.process_canonical_path")
    def test_submit_after_previous_completes(self, mock_process):
        """A second job can be submitted after the first finishes."""
        mock_process.side_effect = _fake_process_item

        pool = WorkerPool(gpu_workers=0, cpu_workers=1, selected_gpus=[])
        dispatcher = JobDispatcher(pool)

        t1 = dispatcher.submit_items(
            job_id="job-1",
            items=_pi_list_or_passthrough([("/k/1", "M1", "movie")]),
            config=_make_config(),
            registry=MagicMock(),
        )
        assert t1.wait(timeout=10)
        assert t1.get_result()["completed"] == 1

        t2 = dispatcher.submit_items(
            job_id="job-2",
            items=_pi_list_or_passthrough([("/k/2", "M2", "movie"), ("/k/3", "M3", "movie")]),
            config=_make_config(),
            registry=MagicMock(),
        )
        assert t2.wait(timeout=10)
        assert t2.get_result()["completed"] == 2
        dispatcher.shutdown()

    @patch("media_preview_generator.processing.multi_server.process_canonical_path")
    def test_fifo_priority_drains_first_job_before_second(self, mock_process):
        """FIFO scheduling: all items from job 1 are dispatched before job 2."""
        dispatch_order = []

        def tracking_process(*args, canonical_path=None, **kwargs):
            dispatch_order.append(canonical_path)
            time.sleep(0.02)
            return _ms("generated", canonical_path=canonical_path or "/data/test.mkv")

        mock_process.side_effect = tracking_process

        pool = WorkerPool(gpu_workers=0, cpu_workers=1, selected_gpus=[])
        dispatcher = JobDispatcher(pool)

        tracker_a = dispatcher.submit_items(
            job_id="job-a",
            items=_pi_list_or_passthrough(
                [
                    ("/a/1", "A1", "movie"),
                    ("/a/2", "A2", "movie"),
                    ("/a/3", "A3", "movie"),
                ]
            ),
            config=_make_config(),
            registry=MagicMock(),
        )
        tracker_b = dispatcher.submit_items(
            job_id="job-b",
            items=_pi_list_or_passthrough([("/b/1", "B1", "movie"), ("/b/2", "B2", "movie")]),
            config=_make_config(),
            registry=MagicMock(),
        )

        assert tracker_a.wait(timeout=10)
        assert tracker_b.wait(timeout=10)

        # With 1 worker, strict FIFO means all of A before any of B.
        # _pi() builds canonical_paths from f"/data/{key.strip('/').replace('/', '_')}.mkv".
        assert dispatch_order == ["/data/a_1.mkv", "/data/a_2.mkv", "/data/a_3.mkv", "/data/b_1.mkv", "/data/b_2.mkv"]
        dispatcher.shutdown()

    @patch("media_preview_generator.processing.multi_server.process_canonical_path")
    def test_outcome_counts_merged_to_tracker(self, mock_process):
        """Per-task outcome deltas are correctly merged into the tracker."""
        mock_process.side_effect = _fake_process_item

        pool = WorkerPool(gpu_workers=0, cpu_workers=1, selected_gpus=[])
        dispatcher = JobDispatcher(pool)

        tracker = dispatcher.submit_items(
            job_id="job-oc",
            items=_pi_list_or_passthrough([("/k/1", "M1", "movie"), ("/k/2", "M2", "movie")]),
            config=_make_config(),
            registry=MagicMock(),
        )
        assert tracker.wait(timeout=10)
        result = tracker.get_result()
        assert result["completed"] == 2
        assert result["outcome"].get("generated", 0) == 2
        dispatcher.shutdown()

    def test_merge_worker_outcome_aggregates_publishers_per_server(self):
        """D12 — _merge_worker_outcome folds per-task publisher rows into
        a per-server aggregate (server_id → status counts) on the
        tracker. The earlier per-task append path made job.publishers
        grow O(files × servers); the aggregate keeps it bounded by the
        number of registered media servers, regardless of file count."""
        pool = WorkerPool(gpu_workers=0, cpu_workers=1, selected_gpus=[])
        dispatcher = JobDispatcher(pool)
        try:
            tracker = JobTracker(
                job_id="job-agg",
                items=[],
                config=_make_config(),
                registry=MagicMock(),
            )
            with patch.object(
                __import__("media_preview_generator.web.jobs", fromlist=["get_job_manager"]),
                "get_job_manager",
                return_value=MagicMock(),
            ):
                # Two files × two servers — what bloated the row before.
                worker_a = MagicMock()
                worker_a.last_task_outcome_delta.return_value = {"generated": 1}
                worker_a.last_publishers = [
                    {"server_id": "p1", "server_name": "Plex", "server_type": "plex", "status": "published"},
                    {"server_id": "e1", "server_name": "Emby", "server_type": "emby", "status": "published"},
                ]
                dispatcher._merge_worker_outcome(worker_a, tracker)

                worker_b = MagicMock()
                worker_b.last_task_outcome_delta.return_value = {"generated": 1}
                worker_b.last_publishers = [
                    {"server_id": "p1", "server_name": "Plex", "server_type": "plex", "status": "published"},
                    {"server_id": "e1", "server_name": "Emby", "server_type": "emby", "status": "failed"},
                ]
                dispatcher._merge_worker_outcome(worker_b, tracker)

            agg = tracker.publishers_aggregate
            assert set(agg.keys()) == {"p1", "e1"}
            assert agg["p1"]["counts"] == {"published": 2}
            assert agg["e1"]["counts"] == {"published": 1, "failed": 1}
            assert agg["p1"]["server_type"] == "plex"
            assert agg["e1"]["server_name"] == "Emby"
        finally:
            dispatcher.shutdown()

    @patch("media_preview_generator.processing.multi_server.process_canonical_path")
    def test_drain_orphaned_fallback_routes_to_tracker(self, mock_process):
        """Draining orphan fallback items attributes failures to the correct tracker."""

        def gpu_codec_fail(*args, **kwargs):
            raise CodecNotSupportedError("unsupported codec")

        mock_process.side_effect = gpu_codec_fail

        gpus = _make_gpu_list(1)
        config = _make_config(cpu_threads=0, gpu_threads=1)
        pool = WorkerPool(gpu_workers=1, cpu_workers=0, selected_gpus=gpus)
        dispatcher = JobDispatcher(pool)

        tracker = dispatcher.submit_items(
            job_id="job-drain",
            items=_pi_list_or_passthrough([("/d/1", "Drain1", "movie")]),
            config=config,
            registry=MagicMock(),
        )
        assert tracker.wait(timeout=10)
        result = tracker.get_result()
        # Audit fix — original asserted ``result["completed"] + result["failed"] == 1``
        # AND ``result["failed"] >= 1`` which technically allows BOTH paths
        # ("GPU marks failed" OR "dispatcher drains as failed"). That OR
        # absorbs whichever implementation is current — a regression that
        # silently switched paths would still pass.
        # Tighten: with no CPU workers + GPU codec error, the contract is
        # exactly 1 failed, 0 completed. If the implementation legitimately
        # changes (e.g. orphan retry path completes the item), update this
        # assertion deliberately rather than letting the OR absorb it.
        assert result["completed"] == 0, f"with no CPU fallback, GPU codec error must NOT mark complete; got {result!r}"
        assert result["failed"] == 1, f"GPU codec error with no fallback should mark exactly 1 failed; got {result!r}"
        dispatcher.shutdown()

    @patch("media_preview_generator.processing.multi_server.process_canonical_path")
    def test_cancel_passes_cancel_check_to_worker(self, mock_process):
        """Cancelled job's cancel_check is passed through to the worker thread."""
        cancel_checks_received = []

        def capturing_process(*args, **kwargs):
            cancel_checks_received.append(kwargs.get("cancel_check"))
            return _ms("generated")

        mock_process.side_effect = capturing_process

        pool = WorkerPool(gpu_workers=0, cpu_workers=1, selected_gpus=[])
        dispatcher = JobDispatcher(pool)

        def cancel_fn():
            return False

        tracker = dispatcher.submit_items(
            job_id="job-cc",
            items=_pi_list_or_passthrough([("/cc/1", "CC1", "movie")]),
            config=_make_config(),
            registry=MagicMock(),
            callbacks={"cancel_check": cancel_fn},
        )
        assert tracker.wait(timeout=10)

        assert len(cancel_checks_received) == 1
        assert cancel_checks_received[0] is cancel_fn
        dispatcher.shutdown()

    @patch("media_preview_generator.processing.multi_server.process_canonical_path")
    def test_cancelled_fallback_items_are_not_dispatched(self, mock_process):
        """Fallback items from a cancelled job are skipped, not assigned to CPU."""
        call_count = [0]
        cancelled = threading.Event()

        def process_fn(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                cancelled.set()
                raise CodecNotSupportedError("test codec error")
            time.sleep(0.1)
            return _ms("generated")

        mock_process.side_effect = process_fn

        pool = WorkerPool(
            gpu_workers=1,
            cpu_workers=1,
            selected_gpus=[("NVIDIA", None, {"name": "Test GPU"})],
        )
        dispatcher = JobDispatcher(pool)

        tracker = dispatcher.submit_items(
            job_id="job-fb",
            items=_pi_list_or_passthrough([("/fb/1", "FB1", "movie")]),
            config=_make_config(cpu_threads=1),
            registry=MagicMock(),
            callbacks={"cancel_check": lambda: cancelled.is_set()},
        )
        assert tracker.wait(timeout=10)

        result = tracker.get_result()
        assert result["completed"] == 0
        # process_item should only have been called once (GPU), not a second
        # time for CPU fallback -- this is the core assertion.
        assert call_count[0] == 1
        dispatcher.shutdown()


# ---------------------------------------------------------------------------
# First-file progress visibility
# ---------------------------------------------------------------------------


class TestDispatchLoopOrdering:
    """Verify that task assignment happens before status emissions."""

    @patch("media_preview_generator.processing.multi_server.process_canonical_path", _fake_process_item)
    def test_first_worker_update_shows_busy_worker(self):
        """The first worker_callback emission should reflect a busy worker,
        not stale idle data from before task assignment."""
        pool = WorkerPool(cpu_workers=1, gpu_workers=0, selected_gpus=[])
        dispatcher = JobDispatcher(pool)

        worker_snapshots = []

        def capture_workers(workers_list):
            worker_snapshots.append([(w["status"], w.get("current_title", "")) for w in workers_list])

        tracker = dispatcher.submit_items(
            job_id="order-test",
            items=_pi_list_or_passthrough([("k1", "File1", "movie")]),
            config=_make_config(),
            registry=MagicMock(),
            callbacks={"worker_callback": capture_workers},
        )
        assert tracker.wait(timeout=10)

        # The very first snapshot should show the worker as processing.
        assert len(worker_snapshots) > 0
        first_statuses = [s for s, _ in worker_snapshots[0]]
        assert "processing" in first_statuses, (
            f"Expected 'processing' in first worker update, got {worker_snapshots[0]}"
        )
        dispatcher.shutdown()


class TestInProgressFraction:
    """Verify that _get_in_progress_fraction reads live worker progress."""

    def test_fraction_from_busy_worker(self):
        """A worker at 50% on job X contributes 0.5 to that job's fraction."""
        pool = WorkerPool(cpu_workers=1, gpu_workers=0, selected_gpus=[])
        dispatcher = JobDispatcher(pool)

        # Manually set up a worker to look busy at 50%
        worker = pool._snapshot_workers()[0]
        worker.is_busy = True
        worker.current_job_id = "frac-job"
        worker.progress_percent = 50.0

        assert dispatcher._get_in_progress_fraction("frac-job") == pytest.approx(0.5)
        assert dispatcher._get_in_progress_fraction("other-job") == pytest.approx(0.0)
        dispatcher.shutdown()

    def test_fraction_zero_when_idle(self):
        """Idle workers contribute nothing."""
        pool = WorkerPool(cpu_workers=1, gpu_workers=0, selected_gpus=[])
        dispatcher = JobDispatcher(pool)
        assert dispatcher._get_in_progress_fraction("any") == pytest.approx(0.0)
        dispatcher.shutdown()


class TestProgressBarMonotonicity:
    """Regression: the bar must not bounce between 12% → 30% → 12% (D37).

    Two emit paths can fire for the same job:

    * ``JobTracker.record_completion`` fires on every completion
      (0.5Hz throttle).
    * ``JobDispatcher._emit_progress_updates`` fires periodically
      (3s throttle) and includes ``in_progress_fraction``.

    Before D37, the completion path emitted ``percent = completed/total``
    while the periodic path emitted ``percent = (completed +
    in_progress_fraction)/total``. With even one busy worker, the second
    value was higher — so the UI bar visibly jumped UP every 3s and
    fell back DOWN on the next completion. This test pins both paths
    to the same formula by wiring an in-flight fraction getter onto
    the tracker at submit time.
    """

    def test_record_completion_includes_in_flight_fraction(self):
        """Both emit paths must agree on the percent for a given (completed, fraction) pair."""
        pool = WorkerPool(cpu_workers=1, gpu_workers=0, selected_gpus=[])
        dispatcher = JobDispatcher(pool)

        # Force a synthetic in-flight worker for "monotone-job".
        worker = pool._snapshot_workers()[0]
        worker.is_busy = True
        worker.current_job_id = "monotone-job"
        worker.progress_percent = 80.0

        progress_calls: list[dict] = []

        def capture(current, total, message, percent_override=None):
            progress_calls.append({"current": current, "percent_override": percent_override})

        from media_preview_generator.jobs.dispatcher import JobTracker

        tracker = JobTracker(
            job_id="monotone-job",
            items=_pi_list_or_passthrough([("k1", "F1", "movie"), ("k2", "F2", "movie")]),
            config=_make_config(),
            registry=MagicMock(),
            callbacks={"progress_callback": capture},
        )
        tracker.in_progress_fraction_getter = lambda: dispatcher._get_in_progress_fraction("monotone-job")
        tracker.total_items = 100  # easier arithmetic

        # Path A: record_completion fires the completion-path emit.
        tracker.completed_when_recording = tracker.completed  # baseline
        tracker.successful = 12
        tracker._last_progress_update = 0.0  # bypass throttle
        tracker.record_completion(True, "CPU 1", "F1")  # successful → 13

        # Path B: dispatcher._emit_progress_updates would compute the same
        # formula. Mirror it inline so the assertion ties the two paths
        # together explicitly.
        in_flight = dispatcher._get_in_progress_fraction("monotone-job")
        path_b_percent = (tracker.completed + in_flight) / tracker.total_items * 100

        # Path A's emit should match Path B's formula. Without the D37
        # wiring, Path A would have emitted 13.0 while Path B emitted
        # 13.8 (12 completed + 0.8 fraction)/100 — visible jump.
        path_a_percent = progress_calls[-1]["percent_override"]
        assert path_a_percent == pytest.approx(path_b_percent), (
            f"Both emit paths must agree. record_completion={path_a_percent}, periodic={path_b_percent}"
        )
        dispatcher.shutdown()


class TestProgressCallbackPercentOverride:
    """Verify that progress_callback accepts percent_override."""

    @patch("media_preview_generator.processing.multi_server.process_canonical_path", _fake_process_item)
    def test_progress_includes_in_flight_work(self):
        """Progress percent should be non-zero while a file is processing,
        not stuck at 0% until the first completion."""
        pool = WorkerPool(cpu_workers=1, gpu_workers=0, selected_gpus=[])
        dispatcher = JobDispatcher(pool)

        progress_calls = []

        def capture_progress(current, total, message, percent_override=None):
            progress_calls.append(
                {
                    "current": current,
                    "total": total,
                    "message": message,
                    "percent_override": percent_override,
                }
            )

        tracker = dispatcher.submit_items(
            job_id="pct-test",
            items=_pi_list_or_passthrough([("k1", "F1", "movie"), ("k2", "F2", "movie")]),
            config=_make_config(),
            registry=MagicMock(),
            callbacks={"progress_callback": capture_progress},
        )
        assert tracker.wait(timeout=10)

        # Every completion callback now carries percent_override so the
        # bar can't bounce between the completion path and the periodic
        # path emitting different percent values for the same instant.
        completions = [c for c in progress_calls if c["current"] > 0]
        assert len(completions) >= 1, "Expected at least one completion callback"
        for c in completions:
            assert c["percent_override"] is not None, (
                "record_completion should pass percent_override so it stays "
                "in lock-step with _emit_progress_updates — see D37."
            )
        dispatcher.shutdown()


class TestReapRetrySkipThroughput:
    """Verify that fast-completing tasks (BIF-exists skips) are reaped
    within _assign_tasks so the worker can immediately receive the next
    item instead of waiting for the next dispatch cycle."""

    @pytest.fixture(autouse=True)
    def _reset(self):
        reset_dispatcher()
        yield
        reset_dispatcher()

    @patch("media_preview_generator.processing.multi_server.process_canonical_path")
    def test_fast_items_complete_quickly(self, mock_process):
        """Items that complete nearly instantly (< 1ms) should not each
        cost a full 5ms dispatch cycle.  With 10 instant items and 1
        worker, total wall time should be well under 100ms."""

        def instant_process(*args, **kwargs):
            return _ms("skipped_bif_exists")

        mock_process.side_effect = instant_process

        pool = WorkerPool(gpu_workers=0, cpu_workers=1, selected_gpus=[])
        dispatcher = JobDispatcher(pool)

        items = [(f"/key/{i}", f"Skip {i}", "movie") for i in range(10)]
        t0 = time.monotonic()
        tracker = dispatcher.submit_items(
            job_id="skip-fast",
            items=_pi_list_or_passthrough(items),
            config=_make_config(),
            registry=MagicMock(),
        )
        assert tracker.wait(timeout=5), "Fast-skip items should complete quickly"
        elapsed = time.monotonic() - t0
        assert tracker.get_result()["completed"] == 10
        # Without reap-retry: 10 items × ~10ms = ~100ms minimum.
        # With reap-retry: significantly faster due to in-loop reaping.
        assert elapsed < 0.5, f"Expected < 500ms, took {elapsed:.3f}s"
        dispatcher.shutdown()

    @patch("media_preview_generator.processing.multi_server.process_canonical_path")
    def test_slow_and_fast_items_mixed(self, mock_process):
        """When one worker is busy with a slow item, another worker
        should still cycle through fast items efficiently."""
        call_order = []

        def mixed_process(*args, canonical_path=None, **kwargs):
            cp = canonical_path or ""
            call_order.append(cp)
            if "slow" in cp:
                time.sleep(0.1)
                return _ms("generated", canonical_path=cp)
            return _ms("skipped_bif_exists", canonical_path=cp)

        mock_process.side_effect = mixed_process

        pool = WorkerPool(gpu_workers=0, cpu_workers=2, selected_gpus=[])
        dispatcher = JobDispatcher(pool)

        items = [
            ("/slow/1", "Slow Item", "movie"),
            ("/fast/1", "Fast 1", "movie"),
            ("/fast/2", "Fast 2", "movie"),
            ("/fast/3", "Fast 3", "movie"),
            ("/fast/4", "Fast 4", "movie"),
        ]
        tracker = dispatcher.submit_items(
            job_id="mixed",
            items=_pi_list_or_passthrough(items),
            config=_make_config(),
            registry=MagicMock(),
        )
        assert tracker.wait(timeout=5)
        result = tracker.get_result()
        assert result["completed"] == 5
        dispatcher.shutdown()


class TestPoolReconciliationOnDispatch:
    """Verify that _dispatch_items reconciles the pool with current settings
    even when the pool was originally created with 0 workers."""

    @pytest.fixture(autouse=True)
    def _reset(self):
        reset_dispatcher()
        yield
        reset_dispatcher()

    @patch("media_preview_generator.processing.multi_server.process_canonical_path")
    def test_pool_gains_workers_via_callback(self, mock_process):
        """A pool created with 0 GPU workers should gain workers when the
        worker_pool_callback reconciles with fresh settings."""
        mock_process.return_value = _ms("skipped_bif_exists")

        pool = WorkerPool(gpu_workers=0, cpu_workers=0, selected_gpus=[])
        assert len(pool.workers) == 0

        gpu_info = {
            "name": "Test GPU",
            "workers": 2,
            "ffmpeg_threads": 2,
            "type": "nvidia",
            "device": "/dev/dri/renderD128",
        }
        fresh_gpus = [("nvidia", "/dev/dri/renderD128", gpu_info)]

        pool.reconcile_gpu_workers(fresh_gpus)
        assert len(pool.workers) == 2
        assert all(w.worker_type == "GPU" for w in pool.workers)

        dispatcher = JobDispatcher(pool)
        items = [(f"/key/{i}", f"Item {i}", "movie") for i in range(4)]
        tracker = dispatcher.submit_items(
            job_id="reconciled",
            items=_pi_list_or_passthrough(items),
            config=_make_config(gpu_threads=2, cpu_threads=0),
            registry=MagicMock(),
        )
        assert tracker.wait(timeout=5), "Items should complete after reconciliation"
        assert tracker.get_result()["completed"] == 4
        dispatcher.shutdown()


class TestInflightJobGuard:
    """Verify _start_job_async skips duplicate calls for the same job ID."""

    def test_duplicate_call_is_skipped(self):
        from media_preview_generator.web.routes.job_runner import (
            _inflight_jobs,
            _inflight_lock,
            _start_job_async,
        )

        fake_id = "test-guard-12345"
        calls = []

        with _inflight_lock:
            _inflight_jobs.discard(fake_id)

        original_Thread = threading.Thread

        class SpyThread(original_Thread):
            def __init__(self, *a, **kw):
                calls.append("spawned")
                super().__init__(*a, **kw)

        with patch("media_preview_generator.web.routes.job_runner.threading.Thread", SpyThread):
            with _inflight_lock:
                _inflight_jobs.add(fake_id)

            _start_job_async(fake_id)
            assert len(calls) == 0, "Second call should not spawn a thread"

        with _inflight_lock:
            _inflight_jobs.discard(fake_id)


# ---------------------------------------------------------------------------
# D34 — sub-second worker visibility (state-change throttle bypass)
# ---------------------------------------------------------------------------


class TestEmitWorkerUpdatesStateChangeBypass:
    """D34 hindsight test: ``_emit_worker_updates`` must bypass the 1Hz
    throttle whenever any worker's busy state has flipped since the
    previous emit pass.

    Without the bypass, sub-second tasks (skip-cached BIF exists, frame-
    cache hits) flickered processing→idle inside a single 1-second
    throttle window and the user saw NO worker activity at all (the
    user-flagged "I see progress sometimes but not for this job"
    symptom from incident D34 / commit a64030c).

    Pin point: ``dispatcher.py:574``
    ``state_changed = current_busy != self._last_worker_busy_snapshot``
    plus the ``or state_changed`` clause on the throttle check.
    """

    @pytest.fixture(autouse=True)
    def _reset(self):
        reset_dispatcher()
        yield
        reset_dispatcher()

    def _make_dispatcher_with_two_workers(self):
        pool = WorkerPool(cpu_workers=2, gpu_workers=0, selected_gpus=[])
        dispatcher = JobDispatcher(pool)
        return dispatcher, pool

    def _make_tracker(self, dispatcher, callback):
        from media_preview_generator.jobs.dispatcher import JobTracker

        tracker = JobTracker(
            job_id="d34-job",
            items=_pi_list_or_passthrough([("k1", "Item 1", "movie")]),
            config=_make_config(),
            registry=MagicMock(),
            callbacks={"worker_callback": callback},
        )
        # Register on dispatcher so _emit_worker_updates picks it up.
        with dispatcher._trackers_lock:
            dispatcher._trackers["d34-job"] = tracker
        return tracker

    def test_state_change_bypasses_subsecond_throttle(self):
        """Two back-to-back emits with worker.is_busy flipped between them
        must both fire the worker_callback even though the second emit is
        ~0ms after the first (well inside the 1Hz throttle window).

        Then a third emit with NO state change must NOT fire the callback
        because the throttle is still active.

        Real wall-clock timing — no freezing. The two flipped emits run
        back-to-back so the gap is naturally << 1 second.
        """
        dispatcher, pool = self._make_dispatcher_with_two_workers()
        try:
            callback_calls: list[list[dict]] = []
            tracker = self._make_tracker(dispatcher, callback=callback_calls.append)

            workers = pool._snapshot_workers()
            assert len(workers) == 2, "Test needs exactly 2 workers to flip one"
            w0, w1 = workers
            # Establish baseline state on the dispatcher snapshot so the
            # FIRST emit below already counts as a state change (idle
            # snapshot {} → {w0:False, w1:False}). This call also primes
            # tracker._last_worker_update so the SECOND emit's throttle
            # check is genuinely contested.
            w0.is_busy = False
            w1.is_busy = False

            # --- Emit #1: idle baseline. State CHANGED (snapshot was {}
            # before init) so the callback should fire even though
            # _last_worker_update == 0.0 happens to be > 1 second old.
            dispatcher._emit_worker_updates()
            assert len(callback_calls) == 1, "Emit #1 should fire (state change from empty snapshot)"
            time_after_first = tracker._last_worker_update
            assert time_after_first > 0.0, "Emit #1 should have stamped _last_worker_update"

            # --- Emit #2: flip w0 busy → state changed → BYPASS throttle.
            # This is the load-bearing case. Without the bypass the
            # throttle (now - last < 1.0) would suppress this callback.
            w0.is_busy = True
            dispatcher._emit_worker_updates()
            assert len(callback_calls) == 2, "Emit #2 must fire despite sub-second gap because w0.is_busy flipped"
            elapsed_emit2 = tracker._last_worker_update - time_after_first
            assert elapsed_emit2 < 1.0, (
                f"This test only proves the bypass if emit #2 ran within 1s of "
                f"emit #1; got elapsed={elapsed_emit2:.4f}s. Slow CI? Increase parallelism."
            )

            # --- Emit #3: NO state change (snapshot already {w0:True,
            # w1:False}). Throttle still active (< 1s since emit #2). The
            # callback must NOT fire — that's the other half of the
            # contract: throttle is real when state is stable.
            dispatcher._emit_worker_updates()
            assert len(callback_calls) == 2, (
                "Emit #3 must NOT fire — no state change AND throttle still active. "
                "If this fired, the bypass condition is too loose (always-fire)."
            )
        finally:
            dispatcher.shutdown()

    def test_throttle_still_active_when_no_state_change(self):
        """Inverse cell of the matrix: rapid-fire emits with stable busy
        state must respect the 1Hz throttle.

        Guards against an over-eager fix that drops the throttle entirely
        ("just always emit on every loop iteration"). The user-observed
        symptom was missing emits, not surplus emits — surplus emits
        flood SocketIO and rebuild the workers panel ~100x/sec, the
        problem the throttle existed to solve in the first place.
        """
        dispatcher, pool = self._make_dispatcher_with_two_workers()
        try:
            callback_calls: list[list[dict]] = []
            self._make_tracker(dispatcher, callback=callback_calls.append)

            workers = pool._snapshot_workers()
            workers[0].is_busy = True
            workers[1].is_busy = False

            # First emit primes the snapshot AND fires (state changed
            # from {} to current).
            dispatcher._emit_worker_updates()
            assert len(callback_calls) == 1

            # Now emit 5 more times with the same state. The throttle
            # must suppress every one of them.
            for _ in range(5):
                dispatcher._emit_worker_updates()

            assert len(callback_calls) == 1, (
                f"Expected throttle to suppress 5 stable-state emits; got {len(callback_calls)} total callbacks"
            )
        finally:
            dispatcher.shutdown()

    def test_state_change_snapshot_includes_all_workers(self):
        """A flip on ANY worker (not just the first) must trip the
        bypass. Guards against a fix that only watches one worker or
        compares only worker[0].is_busy.
        """
        dispatcher, pool = self._make_dispatcher_with_two_workers()
        try:
            callback_calls: list[list[dict]] = []
            self._make_tracker(dispatcher, callback=callback_calls.append)

            workers = pool._snapshot_workers()
            # Prime: both idle.
            workers[0].is_busy = False
            workers[1].is_busy = False
            dispatcher._emit_worker_updates()
            assert len(callback_calls) == 1, "baseline emit"

            # Flip the SECOND worker only, leave w0 idle.
            workers[1].is_busy = True
            dispatcher._emit_worker_updates()
            assert len(callback_calls) == 2, (
                "Flipping w1 (not w0) must still trigger the bypass — "
                "the snapshot dict compares all worker IDs, not just one."
            )
        finally:
            dispatcher.shutdown()
