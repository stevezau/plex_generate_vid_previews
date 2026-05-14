"""
Concurrency tests for the worker pool.

Covers: WorkerPool startup/shutdown, task assignment under load,
in-place GPU→CPU fallback on codec errors, graceful cancellation,
and thread safety of progress updates.
"""

import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from media_preview_generator.jobs.worker import Worker, WorkerPool
from tests.conftest import _ms, _pi, _pi_list_or_passthrough  # noqa: F401

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_gpu_list(n: int = 1) -> list[tuple[str, str, dict]]:
    """Create a dummy GPU selection list."""
    return [("nvidia", f"/dev/nvidia{i}", {"name": f"RTX 4090 #{i}"}) for i in range(n)]


def _fake_process_item(*args, **kwargs):
    """Simulate FFmpeg processing with a tiny sleep."""
    time.sleep(0.02)
    return _ms("generated")


_SLOW_PROCESS_RELEASE = threading.Event()


def _slow_process_item(*args, **kwargs):
    """Block until ``_SLOW_PROCESS_RELEASE`` is set or a short timeout elapses."""
    _SLOW_PROCESS_RELEASE.wait(timeout=0.1)
    return _ms("generated")


def _very_slow_process_item(*args, progress_callback=None, **kwargs):
    """Simulate long processing so headless polling emits worker updates.

    The polling loop in ``process_items_headless`` only emits a worker
    callback every 1.0 s. Sleep just past that threshold so the callback
    fires at least once without burning the whole budget on a single test.
    """
    if progress_callback:
        progress_callback(
            progress_percent=50.0,
            speed="1.2x",
            current_duration=30.0,
            total_duration=60.0,
            remaining_time=30.0,
        )
    time.sleep(1.05)
    return _ms("generated")


def _failing_process_item(*args, **kwargs):
    """Simulate a processing failure."""
    raise RuntimeError("ffmpeg crashed")


def _codec_error_process_item(*args, **kwargs):
    """Simulate a GPU codec error that should fall back to CPU."""
    from media_preview_generator.processing import CodecNotSupportedError

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
def mock_registry():
    """Minimal ServerRegistry mock."""
    return MagicMock()


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

    def test_assign_task_marks_busy(self, mock_config, mock_registry):
        w = Worker(0, "CPU")
        with patch("media_preview_generator.processing.multi_server.process_canonical_path", _fake_process_item):
            w.assign_task(_pi("/key/1", title="Test", media_type="movie"), mock_config, mock_registry)
        assert w.is_busy

    def test_assign_task_raises_if_busy(self, mock_config, mock_registry):
        w = Worker(0, "CPU")
        with patch("media_preview_generator.processing.multi_server.process_canonical_path", _slow_process_item):
            w.assign_task(_pi("/key/1", title="Test", media_type="movie"), mock_config, mock_registry)
            with pytest.raises(RuntimeError, match="already busy"):
                w.assign_task(_pi("/key/2", title="Test", media_type="movie"), mock_config, mock_registry)
        w.shutdown()

    def test_check_completion_after_task_done(self, mock_config, mock_registry):
        w = Worker(0, "CPU")
        with patch("media_preview_generator.processing.multi_server.process_canonical_path", _fake_process_item):
            w.assign_task(_pi("/key/1", title="Test", media_type="movie"), mock_config, mock_registry)
            # Wait for tiny sleep
            w.current_thread.join(timeout=2)
            assert w.check_completion() is True
            assert w.is_available()
            assert w.completed == 1

    def test_failed_task_increments_failed(self, mock_config, mock_registry):
        w = Worker(0, "CPU")
        with patch("media_preview_generator.processing.multi_server.process_canonical_path", _failing_process_item):
            w.assign_task(_pi("/key/1", title="Bad", media_type="movie"), mock_config, mock_registry)
            w.current_thread.join(timeout=2)
            w.check_completion()
        assert w.failed == 1
        assert w.completed == 0

    def test_find_available_prioritises_gpu(self):
        gpu_w = Worker(0, "GPU", "nvidia", "/dev/nvidia0", 0, "RTX 4090")
        cpu_w = Worker(1, "CPU")
        assert Worker.find_available([gpu_w, cpu_w]) is gpu_w

    def test_find_available_none_when_all_busy(self, mock_config, mock_registry):
        w = Worker(0, "CPU")
        with patch("media_preview_generator.processing.multi_server.process_canonical_path", _slow_process_item):
            w.assign_task(_pi("/key/1", title="Test", media_type="movie"), mock_config, mock_registry)
            assert Worker.find_available([w]) is None
        w.shutdown()

    def test_shutdown_waits_for_thread(self, mock_config, mock_registry):
        w = Worker(0, "CPU")
        with patch("media_preview_generator.processing.multi_server.process_canonical_path", _fake_process_item):
            w.assign_task(_pi("/key/1", title="Test", media_type="movie"), mock_config, mock_registry)
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

    def test_process_all_items_headless(self, mock_config, mock_registry):
        """All items should be processed; completed count matches."""
        pool = WorkerPool(gpu_workers=0, cpu_workers=2, selected_gpus=[])
        items = [(f"/key/{i}", f"Item {i}", "movie") for i in range(6)]
        progress_calls = []

        def progress_cb(current, total, msg):
            progress_calls.append((current, total))

        with patch("media_preview_generator.processing.multi_server.process_canonical_path", _fake_process_item):
            pool.process_items_headless(
                _pi_list_or_passthrough(items),
                mock_config,
                mock_registry,
                progress_callback=progress_cb,
            )

        total_completed = sum(w.completed for w in pool.workers)
        assert total_completed == 6
        # Progress is throttled (0.5s) to avoid SocketIO flood, but the
        # final completion is always reported.
        assert len(progress_calls) >= 1
        assert progress_calls[-1] == (6, 6)

    def test_failed_items_tracked(self, mock_config, mock_registry):
        """Failed items should increment failed counter, not completed."""
        pool = WorkerPool(gpu_workers=0, cpu_workers=1, selected_gpus=[])
        items = [("/key/1", "Bad Item", "movie")]

        with patch("media_preview_generator.processing.multi_server.process_canonical_path", _failing_process_item):
            pool.process_items_headless(_pi_list_or_passthrough(items), mock_config, mock_registry)

        assert sum(w.failed for w in pool.workers) == 1
        assert sum(w.completed for w in pool.workers) == 0

    def test_mixed_success_and_failure(self, mock_config, mock_registry):
        """Mix of successes and failures should add up to total."""
        pool = WorkerPool(gpu_workers=0, cpu_workers=2, selected_gpus=[])
        items = [(f"/key/{i}", f"Item {i}", "movie") for i in range(4)]

        call_count = {"n": 0}

        def alternating_process(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] % 2 == 0:
                raise RuntimeError("fail on even")
            time.sleep(0.01)
            return _ms("generated")

        with patch("media_preview_generator.processing.multi_server.process_canonical_path", alternating_process):
            pool.process_items_headless(_pi_list_or_passthrough(items), mock_config, mock_registry)

        total = sum(w.completed + w.failed for w in pool.workers)
        assert total == 4


# ---------------------------------------------------------------------------
# In-place CPU fallback
# ---------------------------------------------------------------------------


class TestInPlaceCpuFallback:
    """Test GPU→CPU fallback via the codec-error path.

    The GPU worker retries the same item on CPU in-place — there is no
    longer a separate fallback queue or dedicated fallback pool.
    """

    def test_codec_error_retries_on_cpu_in_place(self, mock_config, mock_registry):
        """GPU worker catches CodecNotSupportedError, retries with gpu=None, succeeds."""
        mock_config.cpu_threads = 0  # No dedicated CPU workers — retry stays in GPU worker.

        pool = WorkerPool(gpu_workers=1, cpu_workers=0, selected_gpus=_make_gpu_list(1))
        gpu_worker = [w for w in pool.workers if w.worker_type == "GPU"][0]

        call_log = []

        def gpu_then_cpu(*args, gpu=None, **kwargs):
            call_log.append(gpu)
            if gpu is not None:
                from media_preview_generator.processing import (
                    CodecNotSupportedError,
                )

                raise CodecNotSupportedError("HEVC not supported")
            time.sleep(0.01)
            return _ms("generated")

        items = [("/key/1", "Codec Test", "movie")]

        with patch("media_preview_generator.processing.multi_server.process_canonical_path", gpu_then_cpu):
            pool.process_items_headless(_pi_list_or_passthrough(items), mock_config, mock_registry)

        # Two calls on the same GPU worker: first GPU, then CPU retry.
        assert call_log == ["nvidia", None]
        assert gpu_worker.completed == 1
        assert gpu_worker.failed == 0
        assert gpu_worker.fallback_active is True
        assert "HEVC not supported" in (gpu_worker.fallback_reason or "")


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------


class TestWorkerPoolShutdown:
    """Test graceful shutdown."""

    def test_shutdown_completes_without_error(self, mock_config, mock_registry):
        pool = WorkerPool(gpu_workers=0, cpu_workers=2, selected_gpus=[])
        # Assign tasks then shutdown immediately
        with patch("media_preview_generator.processing.multi_server.process_canonical_path", _fake_process_item):
            for i, worker in enumerate(pool.workers):
                worker.assign_task(_pi(f"/key/{i}", title="", media_type="movie"), mock_config, mock_registry)
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

    def test_worker_callback_called(self, mock_config, mock_registry):
        """Worker callback MUST fire at least once during processing.

        Originally this test had a comment "Callback may or may not fire
        depending on timing, but shouldn't crash" — which is exactly the
        bug-blind pattern that lets "callback never wired" regressions
        slip through (the production symptom: UI never shows worker
        activity for short jobs). Drive a slow-enough item so the 1Hz
        emit cadence guarantees at least one fire.
        """
        pool = WorkerPool(gpu_workers=0, cpu_workers=1, selected_gpus=[])
        items = [("/key/1", "CB Test", "movie")]
        worker_updates = []

        def worker_cb(statuses):
            worker_updates.append(statuses)

        # ``_very_slow_process_item`` sleeps just past the 1.0s emit
        # threshold so the polling loop guarantees ≥1 callback fire.
        with patch("media_preview_generator.processing.multi_server.process_canonical_path", _very_slow_process_item):
            pool.process_items_headless(
                _pi_list_or_passthrough(items),
                mock_config,
                mock_registry,
                worker_callback=worker_cb,
            )

        # Hard contract: callback fired at least once with a non-empty
        # worker list, AND the worker became "busy" with our test item
        # at some point (proves the integration boundary is wired).
        assert worker_updates, "worker_callback never fired — UI worker panel would be silent for this job"
        saw_busy = False
        for update in worker_updates:
            assert isinstance(update, list)
            for ws in update:
                assert "worker_id" in ws
                assert "status" in ws
                if ws.get("status") in ("processing", "busy") and ws.get("current_title") == "CB Test":
                    saw_busy = True
        assert saw_busy, (
            f"worker_callback fired but no update showed worker busy with our test item — updates={worker_updates!r}"
        )

    def test_worker_callback_includes_remaining_time(self, mock_config, mock_registry):
        """Worker callback payload should include remaining_time while processing."""
        pool = WorkerPool(gpu_workers=0, cpu_workers=1, selected_gpus=[])
        items = [("/key/1", "CB ETA Test", "movie")]
        worker_updates = []

        def worker_cb(statuses):
            worker_updates.append(statuses)

        with patch("media_preview_generator.processing.multi_server.process_canonical_path", _very_slow_process_item):
            pool.process_items_headless(
                _pi_list_or_passthrough(items),
                mock_config,
                mock_registry,
                worker_callback=worker_cb,
            )

        assert worker_updates, "Expected at least one worker status callback"
        flat_updates = [ws for update in worker_updates for ws in update]
        processing_updates = [ws for ws in flat_updates if ws.get("status") == "processing"]
        assert processing_updates, "Expected at least one processing worker update"
        assert any("remaining_time" in ws for ws in processing_updates)
        assert any(
            isinstance(ws.get("remaining_time"), int | float) and ws.get("remaining_time", 0) > 0
            for ws in processing_updates
        )
