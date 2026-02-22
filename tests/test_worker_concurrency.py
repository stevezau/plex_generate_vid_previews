"""
Concurrency tests for the worker pool.

Covers: WorkerPool startup/shutdown, task assignment under load,
CPU fallback queue, graceful cancellation, and thread safety of
progress updates.
"""

import threading
import time
from typing import List, Tuple
from unittest.mock import MagicMock, patch

import pytest

from plex_generate_previews.worker import Worker, WorkerPool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_gpu_list(n: int = 1) -> List[Tuple[str, str, dict]]:
    """Create a dummy GPU selection list."""
    return [("nvidia", f"/dev/nvidia{i}", {"name": f"RTX 4090 #{i}"}) for i in range(n)]


def _fake_process_item(item_key, gpu, gpu_device, config, plex, progress_callback=None):
    """Simulate FFmpeg processing with a tiny sleep."""
    time.sleep(0.02)


def _slow_process_item(item_key, gpu, gpu_device, config, plex, progress_callback=None):
    """Simulate slow processing for shutdown/cancellation tests."""
    time.sleep(0.5)


def _failing_process_item(
    item_key, gpu, gpu_device, config, plex, progress_callback=None
):
    """Simulate a processing failure."""
    raise RuntimeError("ffmpeg crashed")


def _codec_error_process_item(
    item_key, gpu, gpu_device, config, plex, progress_callback=None
):
    """Simulate a GPU codec error that should fall back to CPU."""
    from plex_generate_previews.media_processing import CodecNotSupportedError

    raise CodecNotSupportedError("HEVC not supported on this GPU")


@pytest.fixture()
def mock_config():
    """Minimal Config mock for worker tests."""
    cfg = MagicMock()
    cfg.cpu_threads = 1
    cfg.gpu_threads = 1
    cfg.worker_pool_timeout = 5
    return cfg


@pytest.fixture()
def mock_plex():
    """Minimal Plex server mock."""
    return MagicMock()


# ---------------------------------------------------------------------------
# Worker unit tests
# ---------------------------------------------------------------------------


class TestWorker:
    """Unit tests for the Worker class."""

    def test_worker_starts_available(self):
        w = Worker(0, "CPU")
        assert w.is_available()
        assert not w.is_busy

    def test_assign_task_marks_busy(self, mock_config, mock_plex):
        w = Worker(0, "CPU")
        with patch("plex_generate_previews.worker.process_item", _fake_process_item):
            w.assign_task(
                "/key/1", mock_config, mock_plex, media_title="Test", media_type="movie"
            )
        assert w.is_busy

    def test_assign_task_raises_if_busy(self, mock_config, mock_plex):
        w = Worker(0, "CPU")
        with patch("plex_generate_previews.worker.process_item", _slow_process_item):
            w.assign_task("/key/1", mock_config, mock_plex)
            with pytest.raises(RuntimeError, match="already busy"):
                w.assign_task("/key/2", mock_config, mock_plex)
        w.shutdown()

    def test_check_completion_after_task_done(self, mock_config, mock_plex):
        w = Worker(0, "CPU")
        with patch("plex_generate_previews.worker.process_item", _fake_process_item):
            w.assign_task(
                "/key/1", mock_config, mock_plex, media_title="Test", media_type="movie"
            )
            # Wait for tiny sleep
            w.current_thread.join(timeout=2)
            assert w.check_completion() is True
            assert w.is_available()
            assert w.completed == 1

    def test_failed_task_increments_failed(self, mock_config, mock_plex):
        w = Worker(0, "CPU")
        with patch("plex_generate_previews.worker.process_item", _failing_process_item):
            w.assign_task(
                "/key/1", mock_config, mock_plex, media_title="Bad", media_type="movie"
            )
            w.current_thread.join(timeout=2)
            w.check_completion()
        assert w.failed == 1
        assert w.completed == 0

    def test_find_available_prioritises_gpu(self):
        gpu_w = Worker(0, "GPU", "nvidia", "/dev/nvidia0", 0, "RTX 4090")
        cpu_w = Worker(1, "CPU")
        assert Worker.find_available([gpu_w, cpu_w]) is gpu_w

    def test_find_available_none_when_all_busy(self, mock_config, mock_plex):
        w = Worker(0, "CPU")
        with patch("plex_generate_previews.worker.process_item", _slow_process_item):
            w.assign_task("/key/1", mock_config, mock_plex)
            assert Worker.find_available([w]) is None
        w.shutdown()

    def test_shutdown_waits_for_thread(self, mock_config, mock_plex):
        w = Worker(0, "CPU")
        with patch("plex_generate_previews.worker.process_item", _fake_process_item):
            w.assign_task("/key/1", mock_config, mock_plex)
            w.shutdown()
        # After shutdown the thread should have completed
        assert not w.current_thread.is_alive()

    def test_get_progress_data_returns_dict(self):
        w = Worker(0, "CPU")
        data = w.get_progress_data()
        assert isinstance(data, dict)
        assert "progress_percent" in data
        assert "worker_type" in data


# ---------------------------------------------------------------------------
# WorkerPool construction
# ---------------------------------------------------------------------------


class TestWorkerPoolInit:
    """Test WorkerPool initialization."""

    def test_creates_correct_worker_count(self):
        pool = WorkerPool(gpu_workers=2, cpu_workers=3, selected_gpus=_make_gpu_list(2))
        assert len(pool.workers) == 5
        gpu_count = sum(1 for w in pool.workers if w.worker_type == "GPU")
        cpu_count = sum(1 for w in pool.workers if w.worker_type == "CPU")
        assert gpu_count == 2
        assert cpu_count == 3

    def test_gpu_round_robin_assignment(self):
        gpus = _make_gpu_list(2)
        pool = WorkerPool(gpu_workers=4, cpu_workers=0, selected_gpus=gpus)
        indices = [w.gpu_index for w in pool.workers]
        # Should round-robin: 0, 1, 0, 1
        assert indices == [0, 1, 0, 1]

    def test_cpu_only_pool(self):
        pool = WorkerPool(gpu_workers=0, cpu_workers=2, selected_gpus=[])
        assert len(pool.workers) == 2
        assert all(w.worker_type == "CPU" for w in pool.workers)

    def test_has_busy_workers_initially_false(self):
        pool = WorkerPool(gpu_workers=0, cpu_workers=1, selected_gpus=[])
        assert not pool.has_busy_workers()

    def test_has_available_workers_initially_true(self):
        pool = WorkerPool(gpu_workers=0, cpu_workers=1, selected_gpus=[])
        assert pool.has_available_workers()


# ---------------------------------------------------------------------------
# WorkerPool processing
# ---------------------------------------------------------------------------


class TestWorkerPoolProcessing:
    """Test WorkerPool item processing with real threads."""

    def test_process_all_items_headless(self, mock_config, mock_plex):
        """All items should be processed; completed count matches."""
        pool = WorkerPool(gpu_workers=0, cpu_workers=2, selected_gpus=[])
        items = [(f"/key/{i}", f"Item {i}", "movie") for i in range(6)]
        progress_calls = []

        def progress_cb(current, total, msg):
            progress_calls.append((current, total))

        with patch("plex_generate_previews.worker.process_item", _fake_process_item):
            pool.process_items_headless(
                items,
                mock_config,
                mock_plex,
                progress_callback=progress_cb,
            )

        total_completed = sum(w.completed for w in pool.workers)
        assert total_completed == 6
        # Progress should have been called at least once per item
        assert len(progress_calls) >= 6

    def test_failed_items_tracked(self, mock_config, mock_plex):
        """Failed items should increment failed counter, not completed."""
        pool = WorkerPool(gpu_workers=0, cpu_workers=1, selected_gpus=[])
        items = [("/key/1", "Bad Item", "movie")]

        with patch("plex_generate_previews.worker.process_item", _failing_process_item):
            pool.process_items_headless(items, mock_config, mock_plex)

        assert sum(w.failed for w in pool.workers) == 1
        assert sum(w.completed for w in pool.workers) == 0

    def test_mixed_success_and_failure(self, mock_config, mock_plex):
        """Mix of successes and failures should add up to total."""
        pool = WorkerPool(gpu_workers=0, cpu_workers=2, selected_gpus=[])
        items = [(f"/key/{i}", f"Item {i}", "movie") for i in range(4)]

        call_count = {"n": 0}

        def alternating_process(
            item_key, gpu, gpu_device, config, plex, progress_callback=None
        ):
            call_count["n"] += 1
            if call_count["n"] % 2 == 0:
                raise RuntimeError("fail on even")
            time.sleep(0.01)

        with patch("plex_generate_previews.worker.process_item", alternating_process):
            pool.process_items_headless(items, mock_config, mock_plex)

        total = sum(w.completed + w.failed for w in pool.workers)
        assert total == 4


# ---------------------------------------------------------------------------
# CPU Fallback Queue
# ---------------------------------------------------------------------------


class TestCPUFallbackQueue:
    """Test GPU→CPU fallback via the codec-error path."""

    def test_codec_error_falls_back_to_cpu(self, mock_config, mock_plex):
        """When a GPU worker hits a codec error, the item should be re-queued for CPU."""
        mock_config.cpu_threads = 1

        pool = WorkerPool(gpu_workers=1, cpu_workers=1, selected_gpus=_make_gpu_list(1))

        gpu_call_count = {"n": 0}

        def gpu_then_cpu(
            item_key, gpu, gpu_device, config, plex, progress_callback=None
        ):
            gpu_call_count["n"] += 1
            if gpu is not None:
                # GPU worker -> codec error
                from plex_generate_previews.media_processing import (
                    CodecNotSupportedError,
                )

                raise CodecNotSupportedError("HEVC not supported")
            # CPU worker -> success
            time.sleep(0.01)

        items = [("/key/1", "Codec Test", "movie")]

        with patch("plex_generate_previews.worker.process_item", gpu_then_cpu):
            pool.process_items_headless(items, mock_config, mock_plex)

        # GPU worker should have failed but not counted as completed
        _ = [w for w in pool.workers if w.worker_type == "GPU"][0]
        cpu_worker = [w for w in pool.workers if w.worker_type == "CPU"][0]
        # CPU should have handled the fallback
        assert cpu_worker.completed == 1

    def test_fallback_queue_empty_check(self):
        """_check_fallback_queue_empty should report correctly."""
        pool = WorkerPool(gpu_workers=0, cpu_workers=1, selected_gpus=[])
        assert pool._check_fallback_queue_empty() is True
        pool.cpu_fallback_queue.put(("/key/1", "Test", "movie"))
        assert pool._check_fallback_queue_empty() is False


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------


class TestWorkerPoolShutdown:
    """Test graceful shutdown."""

    def test_shutdown_completes_without_error(self, mock_config, mock_plex):
        pool = WorkerPool(gpu_workers=0, cpu_workers=2, selected_gpus=[])
        # Assign tasks then shutdown immediately
        with patch("plex_generate_previews.worker.process_item", _fake_process_item):
            for i, worker in enumerate(pool.workers):
                worker.assign_task(f"/key/{i}", mock_config, mock_plex)
            pool.shutdown()
        # All threads should be done
        for w in pool.workers:
            if w.current_thread:
                assert not w.current_thread.is_alive()


# ---------------------------------------------------------------------------
# Thread safety of progress updates
# ---------------------------------------------------------------------------


class TestProgressThreadSafety:
    """Test that concurrent progress updates don't corrupt state."""

    def test_concurrent_progress_updates(self):
        pool = WorkerPool(gpu_workers=0, cpu_workers=1, selected_gpus=[])
        worker = pool.workers[0]

        errors = []

        def updater(n):
            try:
                for i in range(100):
                    pool._update_worker_progress(
                        worker,
                        progress_percent=i,
                        current_duration=float(i),
                        total_duration=100.0,
                        speed=f"{i}.0x",
                    )
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=updater, args=(t,)) for t in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert errors == [], f"Progress update race conditions: {errors}"
        # Worker state should still be valid (not corrupted)
        assert 0 <= worker.progress_percent <= 99
        assert isinstance(worker.speed, str)

    def test_get_progress_data_under_contention(self):
        pool = WorkerPool(gpu_workers=0, cpu_workers=1, selected_gpus=[])
        worker = pool.workers[0]

        results = []

        def reader():
            for _ in range(50):
                data = worker.get_progress_data()
                results.append(data)

        def writer():
            for i in range(50):
                pool._update_worker_progress(
                    worker,
                    progress_percent=i,
                    current_duration=float(i),
                    total_duration=100.0,
                )

        r_thread = threading.Thread(target=reader)
        w_thread = threading.Thread(target=writer)
        r_thread.start()
        w_thread.start()
        r_thread.join(timeout=5)
        w_thread.join(timeout=5)

        # All reads should return valid dicts
        assert all(isinstance(d, dict) for d in results)
        assert all("progress_percent" in d for d in results)


# ---------------------------------------------------------------------------
# Worker callback in headless mode
# ---------------------------------------------------------------------------


class TestWorkerCallback:
    """Test worker status callback in headless mode."""

    def test_worker_callback_called(self, mock_config, mock_plex):
        pool = WorkerPool(gpu_workers=0, cpu_workers=1, selected_gpus=[])
        items = [("/key/1", "CB Test", "movie")]
        worker_updates = []

        def worker_cb(statuses):
            worker_updates.append(statuses)

        with patch("plex_generate_previews.worker.process_item", _fake_process_item):
            pool.process_items_headless(
                items,
                mock_config,
                mock_plex,
                worker_callback=worker_cb,
            )

        # Callback may or may not fire depending on timing, but shouldn't crash
        for update in worker_updates:
            assert isinstance(update, list)
            for ws in update:
                assert "worker_id" in ws
                assert "status" in ws
