"""
Tests for the JobDispatcher multi-job concurrent dispatch system.

Verifies that multiple jobs can share workers, idle workers pick up items
from the next job, and per-job pause/cancel work independently.
"""

import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from plex_generate_previews.job_dispatcher import (
    JobDispatcher,
    JobTracker,
    reset_dispatcher,
)
from plex_generate_previews.media_processing import (
    CodecNotSupportedError,
    ProcessingResult,
)
from plex_generate_previews.worker import WorkerPool

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(cpu_threads=1, gpu_threads=0):
    config = MagicMock()
    config.cpu_threads = cpu_threads
    config.gpu_threads = gpu_threads
    config.fallback_cpu_threads = 0
    config.worker_pool_timeout = 5
    return config


def _make_gpu_list(n=1):
    return [("nvidia", f"/dev/nvidia{i}", {"name": f"RTX 4090 #{i}"}) for i in range(n)]


def _fake_process_item(*args, **kwargs):
    time.sleep(0.02)
    return ProcessingResult.GENERATED


# ---------------------------------------------------------------------------
# JobTracker unit tests
# ---------------------------------------------------------------------------


class TestJobTracker:
    def test_record_completion_success(self):
        tracker = JobTracker(
            job_id="job-1",
            items=[("k1", "t1", "movie"), ("k2", "t2", "movie")],
            config=_make_config(),
            plex=MagicMock(),
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
            items=[("k1", "t1", "movie")],
            config=_make_config(),
            plex=MagicMock(),
        )
        tracker.record_completion(False, "Worker 1", "t1")
        assert tracker.failed == 1
        assert tracker.done_event.is_set()

    def test_cancel_drains_queue(self):
        items = [("k1", "t1", "movie"), ("k2", "t2", "movie"), ("k3", "t3", "movie")]
        tracker = JobTracker(
            job_id="job-1",
            items=items,
            config=_make_config(),
            plex=MagicMock(),
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
            items=[("k1", "t1", "movie")],
            config=_make_config(),
            plex=MagicMock(),
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
            items=[("k1", "t1", "movie")],
            config=_make_config(),
            plex=MagicMock(),
            callbacks={
                "progress_callback": lambda c, t, m: progress_calls.append((c, t)),
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

    @patch("plex_generate_previews.worker.process_item")
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
            items=items,
            config=_make_config(),
            plex=MagicMock(),
        )
        completed = tracker.wait(timeout=10)
        assert completed, "Tracker should complete within timeout"
        result = tracker.get_result()
        assert result["completed"] == 3
        assert result["failed"] == 0
        dispatcher.shutdown()

    @patch("plex_generate_previews.worker.process_item")
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
            items=items_a,
            config=_make_config(),
            plex=MagicMock(),
        )
        tracker_b = dispatcher.submit_items(
            job_id="job-b",
            items=items_b,
            config=_make_config(),
            plex=MagicMock(),
        )

        assert tracker_a.wait(timeout=10)
        assert tracker_b.wait(timeout=10)

        assert tracker_a.get_result()["completed"] == 2
        assert tracker_b.get_result()["completed"] == 3
        dispatcher.shutdown()

    @patch("plex_generate_previews.worker.process_item")
    def test_idle_workers_pick_up_next_job(self, mock_process):
        """If job A has 1 item and 3 workers, free workers spill to job B."""
        call_log = []

        def tracking_process(item_key, *args, **kwargs):
            call_log.append(item_key)
            time.sleep(0.05)
            return ProcessingResult.GENERATED

        mock_process.side_effect = tracking_process

        pool = WorkerPool(gpu_workers=0, cpu_workers=3, selected_gpus=[])
        dispatcher = JobDispatcher(pool)

        tracker_a = dispatcher.submit_items(
            job_id="job-a",
            items=[("/a/1", "A1", "movie")],
            config=_make_config(),
            plex=MagicMock(),
        )
        tracker_b = dispatcher.submit_items(
            job_id="job-b",
            items=[
                ("/b/1", "B1", "movie"),
                ("/b/2", "B2", "movie"),
                ("/b/3", "B3", "movie"),
            ],
            config=_make_config(),
            plex=MagicMock(),
        )

        assert tracker_a.wait(timeout=10)
        assert tracker_b.wait(timeout=10)

        assert tracker_a.get_result()["completed"] == 1
        assert tracker_b.get_result()["completed"] == 3

        # FIFO: /a/1 should be dispatched first (oldest job drained first)
        assert call_log[0] == "/a/1"
        dispatcher.shutdown()

    @patch("plex_generate_previews.worker.process_item")
    def test_cancel_one_job_others_continue(self, mock_process):
        """Cancelling one job does not affect other jobs."""

        def slow_process(*args, **kwargs):
            time.sleep(0.15)
            return ProcessingResult.GENERATED

        mock_process.side_effect = slow_process

        pool = WorkerPool(gpu_workers=0, cpu_workers=2, selected_gpus=[])
        dispatcher = JobDispatcher(pool)

        cancelled = threading.Event()

        tracker_a = dispatcher.submit_items(
            job_id="job-a",
            items=[
                ("/a/1", "A1", "movie"),
                ("/a/2", "A2", "movie"),
                ("/a/3", "A3", "movie"),
            ],
            config=_make_config(),
            plex=MagicMock(),
            callbacks={"cancel_check": lambda: cancelled.is_set()},
        )
        tracker_b = dispatcher.submit_items(
            job_id="job-b",
            items=[("/b/1", "B1", "movie")],
            config=_make_config(),
            plex=MagicMock(),
        )

        # Let processing start, then cancel job A
        time.sleep(0.05)
        cancelled.set()

        assert tracker_a.wait(timeout=10)
        assert tracker_b.wait(timeout=10)

        result_a = tracker_a.get_result()
        result_b = tracker_b.get_result()
        assert result_a["cancelled"]
        assert result_b["completed"] == 1
        assert not result_b["cancelled"]
        dispatcher.shutdown()

    @patch("plex_generate_previews.worker.process_item")
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
            items=[("/a/1", "A1", "movie")],
            config=_make_config(),
            plex=MagicMock(),
            callbacks={"pause_check": lambda: paused.is_set()},
        )
        tracker_b = dispatcher.submit_items(
            job_id="job-b",
            items=[("/b/1", "B1", "movie"), ("/b/2", "B2", "movie")],
            config=_make_config(),
            plex=MagicMock(),
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

    @patch("plex_generate_previews.worker.process_item")
    def test_gpu_fallback_routes_to_correct_job(self, mock_process):
        """GPU codec fallback items route back to the correct job tracker."""
        call_count = {"gpu": 0, "cpu": 0}

        def mixed_process(item_key, gpu, gpu_device, *args, **kwargs):
            if gpu:
                call_count["gpu"] += 1
                raise CodecNotSupportedError("unsupported on GPU")
            call_count["cpu"] += 1
            time.sleep(0.02)
            return ProcessingResult.GENERATED

        mock_process.side_effect = mixed_process

        gpus = _make_gpu_list(1)
        pool = WorkerPool(gpu_workers=1, cpu_workers=1, selected_gpus=gpus)
        dispatcher = JobDispatcher(pool)

        config = _make_config(cpu_threads=1, gpu_threads=1)
        tracker = dispatcher.submit_items(
            job_id="job-fb",
            items=[("/fb/1", "Fallback Movie", "movie")],
            config=config,
            plex=MagicMock(),
        )

        assert tracker.wait(timeout=10)
        result = tracker.get_result()
        # The item should complete once (via CPU fallback)
        assert result["completed"] == 1
        assert result["failed"] == 0
        dispatcher.shutdown()

    @patch("plex_generate_previews.worker.process_item")
    def test_mixed_success_and_failure(self, mock_process):
        """Items that succeed and fail are tracked correctly per job."""
        call_idx = {"n": 0}

        def alternating_process(*args, **kwargs):
            call_idx["n"] += 1
            time.sleep(0.02)
            if call_idx["n"] % 2 == 0:
                raise RuntimeError("boom")
            return ProcessingResult.GENERATED

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
            items=items,
            config=_make_config(),
            plex=MagicMock(),
        )
        assert tracker.wait(timeout=10)
        result = tracker.get_result()
        assert result["completed"] + result["failed"] == 4
        dispatcher.shutdown()

    @patch("plex_generate_previews.worker.process_item")
    def test_submit_after_previous_completes(self, mock_process):
        """A second job can be submitted after the first finishes."""
        mock_process.side_effect = _fake_process_item

        pool = WorkerPool(gpu_workers=0, cpu_workers=1, selected_gpus=[])
        dispatcher = JobDispatcher(pool)

        t1 = dispatcher.submit_items(
            job_id="job-1",
            items=[("/k/1", "M1", "movie")],
            config=_make_config(),
            plex=MagicMock(),
        )
        assert t1.wait(timeout=10)
        assert t1.get_result()["completed"] == 1

        t2 = dispatcher.submit_items(
            job_id="job-2",
            items=[("/k/2", "M2", "movie"), ("/k/3", "M3", "movie")],
            config=_make_config(),
            plex=MagicMock(),
        )
        assert t2.wait(timeout=10)
        assert t2.get_result()["completed"] == 2
        dispatcher.shutdown()

    @patch("plex_generate_previews.worker.process_item")
    def test_fifo_priority_drains_first_job_before_second(self, mock_process):
        """FIFO scheduling: all items from job 1 are dispatched before job 2."""
        dispatch_order = []

        def tracking_process(item_key, *args, **kwargs):
            dispatch_order.append(item_key)
            time.sleep(0.02)
            return ProcessingResult.GENERATED

        mock_process.side_effect = tracking_process

        pool = WorkerPool(gpu_workers=0, cpu_workers=1, selected_gpus=[])
        dispatcher = JobDispatcher(pool)

        tracker_a = dispatcher.submit_items(
            job_id="job-a",
            items=[
                ("/a/1", "A1", "movie"),
                ("/a/2", "A2", "movie"),
                ("/a/3", "A3", "movie"),
            ],
            config=_make_config(),
            plex=MagicMock(),
        )
        tracker_b = dispatcher.submit_items(
            job_id="job-b",
            items=[("/b/1", "B1", "movie"), ("/b/2", "B2", "movie")],
            config=_make_config(),
            plex=MagicMock(),
        )

        assert tracker_a.wait(timeout=10)
        assert tracker_b.wait(timeout=10)

        # With 1 worker, strict FIFO means all of A before any of B
        assert dispatch_order == ["/a/1", "/a/2", "/a/3", "/b/1", "/b/2"]
        dispatcher.shutdown()

    @patch("plex_generate_previews.worker.process_item")
    def test_outcome_counts_merged_to_tracker(self, mock_process):
        """Per-task outcome deltas are correctly merged into the tracker."""
        mock_process.side_effect = _fake_process_item

        pool = WorkerPool(gpu_workers=0, cpu_workers=1, selected_gpus=[])
        dispatcher = JobDispatcher(pool)

        tracker = dispatcher.submit_items(
            job_id="job-oc",
            items=[("/k/1", "M1", "movie"), ("/k/2", "M2", "movie")],
            config=_make_config(),
            plex=MagicMock(),
        )
        assert tracker.wait(timeout=10)
        result = tracker.get_result()
        assert result["completed"] == 2
        assert result["outcome"].get("generated", 0) == 2
        dispatcher.shutdown()

    @patch("plex_generate_previews.worker.process_item")
    def test_drain_orphaned_fallback_routes_to_tracker(self, mock_process):
        """Draining orphan fallback items attributes failures to the correct tracker."""

        def gpu_codec_fail(*args, **kwargs):
            raise CodecNotSupportedError("unsupported codec")

        mock_process.side_effect = gpu_codec_fail

        gpus = _make_gpu_list(1)
        config = _make_config(cpu_threads=0, gpu_threads=1)
        config.fallback_cpu_threads = 0
        pool = WorkerPool(gpu_workers=1, cpu_workers=0, selected_gpus=gpus)
        dispatcher = JobDispatcher(pool)

        tracker = dispatcher.submit_items(
            job_id="job-drain",
            items=[("/d/1", "Drain1", "movie")],
            config=config,
            plex=MagicMock(),
        )
        assert tracker.wait(timeout=10)
        result = tracker.get_result()
        # With no CPU workers, the GPU codec error leads to a failed item:
        # either the GPU marks it failed directly (when no fallback queue) or
        # the dispatcher drains the orphaned fallback item as failed.
        assert result["completed"] + result["failed"] == 1
        assert result["failed"] >= 1
        dispatcher.shutdown()
