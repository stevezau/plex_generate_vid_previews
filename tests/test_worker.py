"""
Tests for worker.py module.

Tests Worker class, WorkerPool, threading, task assignment,
progress tracking, and error handling.
"""

import time
from unittest.mock import MagicMock, patch

import pytest

from plex_generate_previews.jobs.worker import Worker, WorkerPool
from plex_generate_previews.processing import (
    CancellationError,
    CodecNotSupportedError,
    ProcessingResult,
)


class TestWorker:
    """Test Worker class functionality."""

    def test_worker_initialization(self):
        """Test worker is initialized correctly."""
        worker = Worker(0, "GPU", "NVIDIA", "cuda", 0, "NVIDIA GeForce RTX 3080")

        assert worker.worker_id == 0
        assert worker.worker_type == "GPU"
        assert worker.gpu == "NVIDIA"
        assert worker.gpu_device == "cuda"
        assert worker.gpu_index == 0
        assert worker.gpu_name == "NVIDIA GeForce RTX 3080"
        assert worker.is_busy is False
        assert worker.completed == 0
        assert worker.failed == 0

    def test_worker_is_available(self):
        """Test availability check."""
        worker = Worker(0, "CPU")

        assert worker.is_available() is True

        worker.is_busy = True
        assert worker.is_available() is False

    @patch("plex_generate_previews.processing.orchestrator.process_item")
    def test_worker_assign_task(self, mock_process):
        """Test task assignment."""
        worker = Worker(0, "CPU")
        config = MagicMock()
        plex = MagicMock()

        # Mock process_item to return quickly
        mock_process.return_value = ProcessingResult.GENERATED

        worker.assign_task(
            "test_key",
            config,
            plex,
            media_title="Test Movie",
            media_type="movie",
            title_max_width=30,
        )

        assert worker.is_busy is True
        assert worker.current_task == "test_key"
        assert worker.media_title == "Test Movie"
        assert worker.media_type == "movie"

        # Wait for thread to complete
        if worker.current_thread:
            worker.current_thread.join(timeout=2)

    def test_worker_assign_task_when_busy(self):
        """Test that assigning task to busy worker raises error."""
        worker = Worker(0, "CPU")
        worker.is_busy = True

        config = MagicMock()
        plex = MagicMock()

        with pytest.raises(RuntimeError):
            worker.assign_task("test_key", config, plex)

    @patch("plex_generate_previews.processing.orchestrator.process_item")
    def test_worker_check_completion(self, mock_process):
        """Test completion detection."""
        worker = Worker(0, "CPU")
        config = MagicMock()
        plex = MagicMock()

        mock_process.return_value = ProcessingResult.GENERATED

        worker.assign_task("test_key", config, plex, media_title="Test", media_type="movie")

        # Should be busy initially
        assert worker.is_busy is True

        # Wait for task to complete
        if worker.current_thread:
            worker.current_thread.join(timeout=2)

        # Check completion
        completed = worker.check_completion()
        assert completed is True
        assert worker.is_busy is False

    def test_worker_progress_data(self):
        """Test getting progress data."""
        worker = Worker(0, "GPU", "NVIDIA", "cuda", 0, "RTX 3080")
        worker.progress_percent = 50
        worker.speed = "2.5x"
        worker.frame = 1000
        worker.fps = 30.0

        data = worker.get_progress_data()

        assert data["progress_percent"] == 50
        assert data["speed"] == "2.5x"
        assert data["frame"] == 1000
        assert abs(data["fps"] - 30.0) < 0.1
        assert data["worker_id"] == 0
        assert data["worker_type"] == "GPU"

    def test_worker_find_available(self):
        """Test finding first available worker."""
        workers = [
            Worker(0, "GPU"),
            Worker(1, "GPU"),
            Worker(2, "CPU"),
        ]

        # All available
        available = Worker.find_available(workers)
        assert available == workers[0]

        # First two busy
        workers[0].is_busy = True
        workers[1].is_busy = True
        available = Worker.find_available(workers)
        assert available == workers[2]

        # All busy
        workers[2].is_busy = True
        available = Worker.find_available(workers)
        assert available is None

    def test_worker_shutdown_waits_for_longer_timeout(self):
        """Worker shutdown should wait up to 60 seconds for current task."""
        worker = Worker(0, "CPU")
        thread = MagicMock()
        thread.is_alive.side_effect = [True, False]
        worker.current_thread = thread

        worker.shutdown()

        thread.join.assert_called_once_with(timeout=60)

    def test_worker_format_gpu_name(self):
        """Test GPU name formatting for display."""
        # Test NVIDIA GPU
        worker = Worker(0, "GPU", "NVIDIA", "cuda", 0, "NVIDIA GeForce RTX 3080")
        name = worker._format_gpu_name_for_display()
        assert len(name) == 10
        assert "RTX" in name or "NVIDIA" in name

        # Test AMD GPU
        worker = Worker(1, "GPU", "AMD", "/dev/dri/renderD128", 0, "AMD Radeon RX 6800 XT")
        name = worker._format_gpu_name_for_display()
        assert len(name) == 10

        # Test Intel GPU
        worker = Worker(2, "GPU", "INTEL", "/dev/dri/renderD128", 0, "Intel UHD Graphics 770")
        name = worker._format_gpu_name_for_display()
        assert len(name) == 10

    @patch("plex_generate_previews.jobs.worker.process_item")
    def test_worker_thread_execution(self, mock_process):
        """Test that worker executes in background thread."""
        worker = Worker(0, "CPU")
        config = MagicMock()
        plex = MagicMock()

        # Track if process_item was called
        call_count = [0]

        def mock_process_fn(*args, **kwargs):
            call_count[0] += 1
            time.sleep(0.1)  # Longer sleep to ensure thread is alive when checked
            return ProcessingResult.GENERATED

        mock_process.side_effect = mock_process_fn

        worker.assign_task("test_key", config, plex, media_title="Test", media_type="movie")

        # Give thread a moment to start
        time.sleep(0.01)

        # Should be running in background
        assert worker.current_thread is not None
        assert worker.current_thread.is_alive()

        # Wait for completion
        worker.current_thread.join(timeout=1)

        assert call_count[0] == 1
        assert worker.completed == 1

    @patch("plex_generate_previews.jobs.worker.process_item")
    def test_last_task_outcome_delta(self, mock_process):
        """Test that last_task_outcome_delta returns correct per-task delta."""
        worker = Worker(0, "CPU")
        config = MagicMock()
        plex = MagicMock()

        mock_process.side_effect = lambda *a, **kw: ProcessingResult.GENERATED

        worker.assign_task("key1", config, plex, media_title="T1", media_type="movie")
        worker.current_thread.join(timeout=2)
        worker.check_completion()

        delta = worker.last_task_outcome_delta()
        assert delta["generated"] == 1
        assert all(v == 0 for k, v in delta.items() if k != "generated")

        # Second task — delta should reflect only the second task
        mock_process.side_effect = lambda *a, **kw: ProcessingResult.SKIPPED_BIF_EXISTS
        worker.assign_task("key2", config, plex, media_title="T2", media_type="movie")
        worker.current_thread.join(timeout=2)
        worker.check_completion()

        delta2 = worker.last_task_outcome_delta()
        assert delta2["skipped_bif_exists"] == 1
        assert delta2["generated"] == 0

    @patch("plex_generate_previews.jobs.worker.process_item")
    def test_worker_gpu_codec_error_retries_on_cpu_in_place(self, mock_process):
        """GPU worker catches CodecNotSupportedError and retries the same item on CPU itself.

        The second call uses gpu=None / gpu_device=None (software path).  The
        outcome of the CPU retry is what the worker records — no separate
        fallback queue is involved.
        """
        worker = Worker(0, "GPU", "NVIDIA", "cuda", 0, "RTX 2060 SUPER")
        config = MagicMock()
        config.cpu_threads = 2
        plex = MagicMock()

        calls = []

        def mock_process_fn(*args, **kwargs):
            calls.append((args, kwargs))
            if len(calls) == 1:
                raise CodecNotSupportedError("Codec not supported by GPU")
            return ProcessingResult.GENERATED

        mock_process.side_effect = mock_process_fn

        worker.assign_task(
            "test_key",
            config,
            plex,
            media_title="AV1 Video",
            media_type="episode",
        )

        if worker.current_thread:
            worker.current_thread.join(timeout=2)

        # Two calls: first GPU (gpu='NVIDIA'), second CPU (gpu=None).
        assert len(calls) == 2
        first_args = calls[0][0]
        second_args = calls[1][0]
        assert first_args[1] == "NVIDIA"  # gpu arg
        assert second_args[1] is None  # gpu=None on CPU retry
        assert second_args[2] is None  # gpu_device_path=None on CPU retry

        # Worker records the retry outcome + exposes fallback state.
        assert worker.completed == 1
        assert worker.failed == 0
        assert worker.fallback_active is True
        assert "Codec not supported by GPU" in (worker.fallback_reason or "")

    @patch("plex_generate_previews.jobs.worker.process_item")
    def test_worker_gpu_cpu_fallback_records_failure_when_cpu_retry_fails(self, mock_process):
        """If the in-place CPU retry also fails, the worker counts the task as failed."""
        worker = Worker(0, "GPU", "NVIDIA", "cuda", 0, "RTX 2060 SUPER")
        config = MagicMock()
        config.cpu_threads = 1
        plex = MagicMock()

        calls = []

        def mock_process_fn(*args, **kwargs):
            calls.append((args, kwargs))
            if len(calls) == 1:
                raise CodecNotSupportedError("Codec not supported by GPU")
            return ProcessingResult.FAILED

        mock_process.side_effect = mock_process_fn

        worker.assign_task(
            "test_key",
            config,
            plex,
            media_title="AV1 Video",
            media_type="episode",
        )
        if worker.current_thread:
            worker.current_thread.join(timeout=2)

        assert len(calls) == 2
        assert worker.completed == 0
        assert worker.failed == 1
        assert worker.fallback_active is True

    @patch("plex_generate_previews.jobs.worker.process_item")
    def test_worker_cpu_handles_codec_error_as_failure(self, mock_process):
        """Test that CPU worker treats CodecNotSupportedError as unexpected failure."""
        worker = Worker(0, "CPU")
        config = MagicMock()
        plex = MagicMock()

        # Mock process_item to raise CodecNotSupportedError
        def mock_process_fn(*args, **kwargs):
            raise CodecNotSupportedError("Codec not supported")

        mock_process.side_effect = mock_process_fn

        worker.assign_task("test_key", config, plex, media_title="Test", media_type="movie")

        # Wait for thread to complete
        if worker.current_thread:
            worker.current_thread.join(timeout=2)

        # CPU worker should treat this as failure (unexpected on CPU)
        assert worker.failed == 1
        assert worker.completed == 0


class TestWorkerPool:
    """Test WorkerPool functionality."""

    def test_worker_pool_initialization(self):
        """Test worker pool creates workers correctly."""
        selected_gpus = [
            ("NVIDIA", "cuda", {"name": "RTX 3080"}),
            ("AMD", "/dev/dri/renderD128", {"name": "RX 6800 XT"}),
        ]

        pool = WorkerPool(gpu_workers=4, cpu_workers=2, selected_gpus=selected_gpus)

        assert len(pool.workers) == 6
        # First 4 should be GPU workers
        assert pool.workers[0].worker_type == "GPU"
        assert pool.workers[1].worker_type == "GPU"
        assert pool.workers[2].worker_type == "GPU"
        assert pool.workers[3].worker_type == "GPU"
        # Last 2 should be CPU workers
        assert pool.workers[4].worker_type == "CPU"
        assert pool.workers[5].worker_type == "CPU"

    def test_worker_pool_gpu_assignment(self):
        """Test round-robin GPU assignment."""
        selected_gpus = [
            ("NVIDIA", "cuda", {"name": "RTX 3080"}),
            ("AMD", "/dev/dri/renderD128", {"name": "RX 6800 XT"}),
        ]

        pool = WorkerPool(gpu_workers=4, cpu_workers=0, selected_gpus=selected_gpus)

        # Worker 0 and 2 should use GPU 0
        assert pool.workers[0].gpu_index == 0
        assert pool.workers[2].gpu_index == 0

        # Worker 1 and 3 should use GPU 1
        assert pool.workers[1].gpu_index == 1
        assert pool.workers[3].gpu_index == 1

    @patch("plex_generate_previews.jobs.worker.process_item")
    def test_worker_pool_process_items(self, mock_process):
        """Test processing items with worker pool."""
        # Track if mock was called
        mock_process.return_value = ProcessingResult.GENERATED

        selected_gpus = []
        pool = WorkerPool(gpu_workers=0, cpu_workers=2, selected_gpus=selected_gpus)

        config = MagicMock()
        plex = MagicMock()

        items = [
            ("key1", "Movie 1", "movie"),
            ("key2", "Movie 2", "movie"),
        ]

        main_progress = MagicMock()
        worker_progress = MagicMock()
        worker_progress.add_task = MagicMock(side_effect=[0, 1])

        pool.process_items(items, config, plex, worker_progress, main_progress)

        # All items should be processed
        total_completed = sum(w.completed for w in pool.workers)
        assert total_completed == 2

    def test_worker_pool_has_busy_workers(self):
        """Test detection of busy workers."""
        pool = WorkerPool(gpu_workers=0, cpu_workers=2, selected_gpus=[])

        assert pool.has_busy_workers() is False

        pool.workers[0].is_busy = True
        assert pool.has_busy_workers() is True

    def test_worker_pool_has_available_workers(self):
        """Test detection of available workers."""
        pool = WorkerPool(gpu_workers=0, cpu_workers=2, selected_gpus=[])

        assert pool.has_available_workers() is True

        pool.workers[0].is_busy = True
        pool.workers[1].is_busy = True
        assert pool.has_available_workers() is False

    def test_worker_pool_shutdown(self):
        """Test graceful shutdown."""
        pool = WorkerPool(gpu_workers=0, cpu_workers=2, selected_gpus=[])

        # Should not crash
        pool.shutdown()

    def test_worker_pool_add_and_remove_workers(self):
        """Test dynamic worker add/remove behavior."""
        selected_gpus = [("NVIDIA", "cuda", {"name": "RTX 3080"})]
        pool = WorkerPool(gpu_workers=1, cpu_workers=1, selected_gpus=selected_gpus)

        added_cpu = pool.add_workers("CPU", 2)
        assert added_cpu == 2
        assert len(pool.workers) == 4

        cpu_workers = [w for w in pool.workers if w.worker_type == "CPU"]
        cpu_workers[0].is_busy = True
        result = pool.remove_workers("CPU", 3)
        assert result["removed"] == 2
        assert result["scheduled"] == 1
        assert result["unavailable"] == 0

    def test_remove_workers_schedules_busy_and_retires_when_idle(self):
        """Busy workers should be scheduled and retired after task completion."""
        pool = WorkerPool(gpu_workers=0, cpu_workers=2, selected_gpus=[])
        cpu_workers = [w for w in pool.workers if w.worker_type == "CPU"]
        cpu_workers[0].is_busy = True

        result = pool.remove_workers("CPU", 2)
        assert result == {"removed": 1, "scheduled": 1, "unavailable": 0}
        assert len([w for w in pool.workers if w.worker_type == "CPU"]) == 1

        cpu_workers[0].is_busy = False
        retired = pool._apply_deferred_removals()
        assert retired == 1
        assert len([w for w in pool.workers if w.worker_type == "CPU"]) == 0

    @patch("plex_generate_previews.jobs.worker.process_item")
    def test_dynamic_remove_does_not_stall_completion(self, mock_process):
        """Dynamic worker removal should not trap processing at 100%."""
        mock_process.side_effect = lambda *args, **kwargs: (
            time.sleep(0.01),
            ProcessingResult.GENERATED,
        )[-1]
        pool = WorkerPool(gpu_workers=0, cpu_workers=2, selected_gpus=[])
        config = MagicMock()
        plex = MagicMock()
        items = [(f"key{i}", f"Movie {i}", "movie") for i in range(8)]

        original_assign = pool._assign_main_queue_task
        assigned_count = {"value": 0, "removed": False}

        def assign_and_remove(*args, **kwargs):
            assigned = original_assign(*args, **kwargs)
            if assigned:
                assigned_count["value"] += 1
                if assigned_count["value"] >= 3 and not assigned_count["removed"]:
                    pool.remove_workers("CPU", 1)
                    assigned_count["removed"] = True
            return assigned

        with patch.object(pool, "_assign_main_queue_task", side_effect=assign_and_remove):
            start = time.time()
            result = pool.process_items_headless(items, config, plex)
            elapsed = time.time() - start

        assert elapsed < 2.0
        assert result["completed"] + result["failed"] == len(items)

    @patch("plex_generate_previews.jobs.worker.process_item")
    def test_dynamic_gpu_removal_does_not_stall_completion(self, mock_process):
        """Dynamic GPU worker removal during active processing must not stall at 100%."""
        mock_process.side_effect = lambda *args, **kwargs: (
            time.sleep(0.01),
            ProcessingResult.GENERATED,
        )[-1]
        selected_gpus = [
            ("NVIDIA", "cuda", {"name": "GPU0"}),
            ("NVIDIA", "cuda", {"name": "GPU1"}),
        ]
        pool = WorkerPool(gpu_workers=2, cpu_workers=0, selected_gpus=selected_gpus)
        config = MagicMock()
        plex = MagicMock()
        items = [(f"key{i}", f"Movie {i}", "movie") for i in range(8)]

        original_assign = pool._assign_main_queue_task
        assign_count = {"value": 0, "removed": False}

        def assign_and_remove_gpu(*args, **kwargs):
            assigned = original_assign(*args, **kwargs)
            if assigned:
                assign_count["value"] += 1
                if assign_count["value"] >= 3 and not assign_count["removed"]:
                    pool.remove_workers("GPU", 1)
                    assign_count["removed"] = True
            return assigned

        with patch.object(pool, "_assign_main_queue_task", side_effect=assign_and_remove_gpu):
            start = time.time()
            result = pool.process_items_headless(items, config, plex)
            elapsed = time.time() - start

        assert elapsed < 2.0, "Run must not stall after GPU removal"
        assert result["completed"] + result["failed"] == len(items), (
            f"All items must be accounted for; got completed={result['completed']}, "
            f"failed={result['failed']}, total={len(items)}"
        )

    @patch("plex_generate_previews.jobs.worker.process_item")
    def test_worker_pool_pause_check_blocks_dispatch(self, mock_process):
        """Pause check should delay task dispatch until resumed."""
        mock_process.return_value = ProcessingResult.GENERATED
        pool = WorkerPool(gpu_workers=0, cpu_workers=1, selected_gpus=[])
        config = MagicMock()
        plex = MagicMock()
        items = [("key1", "Movie 1", "movie"), ("key2", "Movie 2", "movie")]

        pause_state = {"paused": True}

        def unpause_later():
            time.sleep(0.25)
            pause_state["paused"] = False

        import threading

        threading.Thread(target=unpause_later, daemon=True).start()
        start = time.time()
        result = pool.process_items_headless(
            items,
            config,
            plex,
            pause_check=lambda: pause_state["paused"],
        )
        elapsed = time.time() - start

        assert result["completed"] == 2
        assert elapsed >= 0.2

    @patch("plex_generate_previews.jobs.worker.process_item")
    def test_no_dispatch_while_paused(self, mock_process):
        """No new task is assigned while pause_check returns True; first assignment after unpause."""
        mock_process.return_value = ProcessingResult.GENERATED
        pool = WorkerPool(gpu_workers=0, cpu_workers=1, selected_gpus=[])
        config = MagicMock()
        plex = MagicMock()
        items = [("key1", "Movie 1", "movie"), ("key2", "Movie 2", "movie")]

        pause_duration = 0.3
        start_time = time.time()
        pause_state = {"paused": True}
        first_assign_time = {}

        def unpause_later():
            time.sleep(pause_duration)
            pause_state["paused"] = False

        original_assign = pool._assign_main_queue_task

        def record_assign(*args, **kwargs):
            if "first_assign_time" not in first_assign_time:
                first_assign_time["first_assign_time"] = time.time() - start_time
            return original_assign(*args, **kwargs)

        with patch.object(pool, "_assign_main_queue_task", side_effect=record_assign):
            import threading

            threading.Thread(target=unpause_later, daemon=True).start()
            result = pool.process_items_headless(
                items,
                config,
                plex,
                pause_check=lambda: pause_state["paused"],
            )

        assert result["completed"] == 2
        assert "first_assign_time" in first_assign_time
        assert first_assign_time["first_assign_time"] >= pause_duration * 0.9, (
            "First assignment must occur after pause window; got "
            f"first_assign_time={first_assign_time['first_assign_time']:.3f}s, "
            f"pause_duration={pause_duration}s"
        )

    @patch("plex_generate_previews.jobs.worker.process_item")
    def test_worker_pool_stats_are_per_library(self, mock_process):
        """Returned processing stats should be scoped to one library call."""
        mock_process.return_value = ProcessingResult.GENERATED

        pool = WorkerPool(gpu_workers=0, cpu_workers=1, selected_gpus=[])
        config = MagicMock()
        plex = MagicMock()
        worker_progress = MagicMock()
        worker_progress.add_task = MagicMock(return_value=0)
        main_progress = MagicMock()

        first_items = [
            ("key1", "Movie 1", "movie"),
            ("key2", "Movie 2", "movie"),
        ]
        second_items = [("key3", "Movie 3", "movie")]

        first_result = pool.process_items(
            first_items,
            config,
            plex,
            worker_progress,
            main_progress,
        )
        second_result = pool.process_items(
            second_items,
            config,
            plex,
            worker_progress,
            main_progress,
        )

        assert first_result["completed"] == 2
        assert first_result["failed"] == 0
        assert second_result["completed"] == 1
        assert second_result["failed"] == 0

    @patch("plex_generate_previews.processing.orchestrator.process_item")
    def test_worker_pool_task_completion(self, mock_process):
        """Test that all tasks complete."""

        # Simulate slow processing
        def slow_process(*args, **kwargs):
            time.sleep(0.05)
            return ProcessingResult.GENERATED

        mock_process.side_effect = slow_process

        pool = WorkerPool(gpu_workers=0, cpu_workers=2, selected_gpus=[])

        config = MagicMock()
        plex = MagicMock()

        items = [
            ("key1", "Movie 1", "movie"),
            ("key2", "Movie 2", "movie"),
            ("key3", "Movie 3", "movie"),
            ("key4", "Movie 4", "movie"),
        ]

        main_progress = MagicMock()
        worker_progress = MagicMock()
        worker_progress.add_task = MagicMock(side_effect=list(range(10)))

        pool.process_items(items, config, plex, worker_progress, main_progress)

        # All 4 items should be completed
        total_completed = sum(w.completed for w in pool.workers)
        assert total_completed == 4

    @patch("plex_generate_previews.jobs.worker.process_item")
    def test_worker_pool_error_handling(self, mock_process):
        """Test that failed tasks are tracked."""
        # Simulate failures
        call_count = [0]

        def failing_process(*args, **kwargs):
            time.sleep(0.01)  # Small delay to simulate work
            call_count[0] += 1
            if call_count[0] % 2 == 0:
                raise Exception("Processing failed")
            return ProcessingResult.GENERATED

        mock_process.side_effect = failing_process

        pool = WorkerPool(gpu_workers=0, cpu_workers=2, selected_gpus=[])

        config = MagicMock()
        plex = MagicMock()

        items = [
            ("key1", "Movie 1", "movie"),
            ("key2", "Movie 2", "movie"),
            ("key3", "Movie 3", "movie"),
            ("key4", "Movie 4", "movie"),
        ]

        main_progress = MagicMock()
        worker_progress = MagicMock()
        worker_progress.add_task = MagicMock(side_effect=list(range(10)))

        pool.process_items(items, config, plex, worker_progress, main_progress)

        # Some should fail
        total_failed = sum(w.failed for w in pool.workers)
        assert total_failed > 0

    @patch("plex_generate_previews.processing.orchestrator.process_item")
    def test_worker_pool_progress_updates(self, mock_process):
        """Test that progress callbacks work correctly."""
        mock_process.return_value = ProcessingResult.GENERATED

        pool = WorkerPool(gpu_workers=0, cpu_workers=1, selected_gpus=[])

        config = MagicMock()
        plex = MagicMock()

        items = [("key1", "Movie 1", "movie")]

        main_progress = MagicMock()
        worker_progress = MagicMock()
        task_id = 0
        worker_progress.add_task = MagicMock(return_value=task_id)

        pool.process_items(items, config, plex, worker_progress, main_progress)

        # Progress update should have been called
        assert worker_progress.update.called or worker_progress.remove_task.called

    def test_worker_statistics(self):
        """Test worker completed/failed statistics."""
        pool = WorkerPool(gpu_workers=0, cpu_workers=2, selected_gpus=[])

        pool.workers[0].completed = 5
        pool.workers[0].failed = 1
        pool.workers[1].completed = 3
        pool.workers[1].failed = 2

        total_completed = sum(w.completed for w in pool.workers)
        total_failed = sum(w.failed for w in pool.workers)

        assert total_completed == 8
        assert total_failed == 3

    @patch("plex_generate_previews.jobs.worker.process_item")
    def test_worker_pool_cpu_fallback_on_codec_error(self, mock_process):
        """GPU worker codec errors now trigger an in-place CPU retry on the same worker."""
        call_order = []

        def mock_process_fn(
            item_key,
            gpu,
            gpu_device,
            config,
            plex,
            progress_callback=None,
            ffmpeg_threads_override=None,
            cancel_check=None,
            worker_name=None,
            fingerprint_store=None,
        ):
            call_order.append((item_key, gpu))
            time.sleep(0.01)
            if gpu is not None:
                raise CodecNotSupportedError("Codec not supported by GPU")
            return ProcessingResult.GENERATED

        mock_process.side_effect = mock_process_fn

        selected_gpus = [("NVIDIA", "cuda", {"name": "RTX 2060 SUPER"})]
        pool = WorkerPool(gpu_workers=1, cpu_workers=0, selected_gpus=selected_gpus)

        config = MagicMock()
        config.cpu_threads = 0
        plex = MagicMock()

        items = [
            ("key1", "AV1 Video", "episode"),
        ]

        main_progress = MagicMock()
        worker_progress = MagicMock()
        worker_progress.add_task = MagicMock(side_effect=[0, 1])

        pool.process_items(items, config, plex, worker_progress, main_progress)
        time.sleep(0.2)

        # Two calls on the same GPU worker: GPU first, then in-place CPU retry.
        assert call_order == [("key1", "NVIDIA"), ("key1", None)]
        assert pool.workers[0].completed == 1
        assert pool.workers[0].failed == 0
        assert pool.workers[0].fallback_active is True

    @patch("plex_generate_previews.jobs.worker.process_item")
    def test_mixed_workload_with_gpu_cpu_fallback(self, mock_process):
        """GPU+CPU pool handles a mix of items including in-place fallbacks."""
        call_order = []

        def mock_process_fn(
            item_key,
            gpu,
            gpu_device,
            config,
            plex,
            progress_callback=None,
            ffmpeg_threads_override=None,
            cancel_check=None,
            worker_name=None,
            fingerprint_store=None,
        ):
            call_order.append((item_key, gpu))
            time.sleep(0.01)
            if gpu is not None and item_key == "key2":
                raise CodecNotSupportedError("Codec not supported")
            return ProcessingResult.GENERATED

        mock_process.side_effect = mock_process_fn

        selected_gpus = [("NVIDIA", "cuda", {"name": "RTX 2060"})]
        pool = WorkerPool(gpu_workers=1, cpu_workers=2, selected_gpus=selected_gpus)

        config = MagicMock()
        config.cpu_threads = 2
        plex = MagicMock()

        items = [
            ("key1", "Normal Video", "movie"),
            ("key2", "AV1 Video", "episode"),
            ("key3", "Normal Video 2", "movie"),
        ]

        main_progress = MagicMock()
        worker_progress = MagicMock()
        worker_progress.add_task = MagicMock(side_effect=list(range(10)))

        pool.process_items(items, config, plex, worker_progress, main_progress)
        time.sleep(0.2)

        total_completed = sum(w.completed for w in pool.workers)
        assert total_completed == 3

    @patch("plex_generate_previews.jobs.worker.process_item")
    def test_codec_error_fails_when_cpu_retry_also_fails(self, mock_process):
        """If the in-place CPU retry itself fails, the GPU worker records a failure."""

        def mock_process_fn(
            item_key,
            gpu,
            gpu_device,
            config,
            plex,
            progress_callback=None,
            ffmpeg_threads_override=None,
            cancel_check=None,
            worker_name=None,
            fingerprint_store=None,
        ):
            time.sleep(0.01)
            if gpu is not None:
                raise CodecNotSupportedError("Codec not supported")
            return ProcessingResult.FAILED

        mock_process.side_effect = mock_process_fn

        selected_gpus = [("NVIDIA", "cuda", {"name": "RTX 2060"})]
        pool = WorkerPool(gpu_workers=1, cpu_workers=0, selected_gpus=selected_gpus)

        config = MagicMock()
        config.cpu_threads = 0
        plex = MagicMock()

        items = [
            ("key1", "AV1 Video", "episode"),
        ]

        main_progress = MagicMock()
        worker_progress = MagicMock()
        worker_progress.add_task = MagicMock(return_value=0)

        pool.process_items(items, config, plex, worker_progress, main_progress)

        assert pool.workers[0].failed == 1
        assert pool.workers[0].completed == 0
        assert pool.workers[0].fallback_active is True


class TestReconcileGpuWorkers:
    """Test that reconcile_gpu_workers defers busy worker removal."""

    def test_reconcile_removes_idle_workers_immediately(self):
        """Idle excess workers are removed from the pool at once."""
        selected_gpus = [("NVIDIA", "/dev/dri/renderD128", {"name": "GPU0", "workers": 3})]
        pool = WorkerPool(gpu_workers=3, cpu_workers=0, selected_gpus=selected_gpus)
        assert len(pool.workers) == 3

        new_gpus = [("NVIDIA", "/dev/dri/renderD128", {"name": "GPU0", "workers": 1})]
        result = pool.reconcile_gpu_workers(new_gpus)

        assert result["removed"] == 2
        assert result["deferred"] == 0
        assert len(pool.workers) == 1

    def test_reconcile_defers_busy_workers(self):
        """Busy workers stay in the pool with _pending_removal set."""
        selected_gpus = [("NVIDIA", "/dev/dri/renderD128", {"name": "GPU0", "workers": 3})]
        pool = WorkerPool(gpu_workers=3, cpu_workers=0, selected_gpus=selected_gpus)
        for w in pool.workers:
            w.is_busy = True

        new_gpus = [("NVIDIA", "/dev/dri/renderD128", {"name": "GPU0", "workers": 1})]
        result = pool.reconcile_gpu_workers(new_gpus)

        assert result["removed"] == 0
        assert result["deferred"] == 2
        assert len(pool.workers) == 3

        deferred = [w for w in pool.workers if w._pending_removal]
        assert len(deferred) == 2

    def test_reconcile_mixed_idle_and_busy(self):
        """Idle workers removed first; only remaining busy workers deferred."""
        selected_gpus = [("NVIDIA", "/dev/dri/renderD128", {"name": "GPU0", "workers": 4})]
        pool = WorkerPool(gpu_workers=4, cpu_workers=0, selected_gpus=selected_gpus)
        pool.workers[0].is_busy = True
        pool.workers[1].is_busy = True

        new_gpus = [("NVIDIA", "/dev/dri/renderD128", {"name": "GPU0", "workers": 1})]
        result = pool.reconcile_gpu_workers(new_gpus)

        assert result["removed"] == 2
        assert result["deferred"] == 1
        assert len(pool.workers) == 2
        kept = [w for w in pool.workers if not w._pending_removal]
        assert len(kept) == 1

    def test_pending_removal_prevents_task_assignment(self):
        """Workers flagged for removal are not considered available."""
        selected_gpus = [("NVIDIA", "/dev/dri/renderD128", {"name": "GPU0", "workers": 3})]
        pool = WorkerPool(gpu_workers=3, cpu_workers=0, selected_gpus=selected_gpus)
        for w in pool.workers:
            w.is_busy = True

        new_gpus = [("NVIDIA", "/dev/dri/renderD128", {"name": "GPU0", "workers": 1})]
        pool.reconcile_gpu_workers(new_gpus)

        deferred = [w for w in pool.workers if w._pending_removal]
        assert len(deferred) == 2
        for w in deferred:
            assert not w.is_available()

        # After finishing, still not available (pending removal flag blocks it)
        for w in deferred:
            w.is_busy = False
            assert not w.is_available()

    def test_deferred_worker_retired_after_completion(self):
        """Deferred workers are retired by _retire_idle_worker_if_scheduled."""
        selected_gpus = [("NVIDIA", "/dev/dri/renderD128", {"name": "GPU0", "workers": 2})]
        pool = WorkerPool(gpu_workers=2, cpu_workers=0, selected_gpus=selected_gpus)
        for w in pool.workers:
            w.is_busy = True

        new_gpus = [("NVIDIA", "/dev/dri/renderD128", {"name": "GPU0", "workers": 1})]
        pool.reconcile_gpu_workers(new_gpus)
        assert len(pool.workers) == 2

        deferred_worker = [w for w in pool.workers if w._pending_removal][0]
        deferred_worker.is_busy = False

        retired = pool._retire_idle_worker_if_scheduled(deferred_worker)
        assert retired is True
        assert len(pool.workers) == 1
        assert deferred_worker not in pool.workers

    def test_deferred_workers_cleaned_by_apply_deferred_removals(self):
        """_apply_deferred_removals sweeps all idle deferred workers."""
        selected_gpus = [("NVIDIA", "/dev/dri/renderD128", {"name": "GPU0", "workers": 3})]
        pool = WorkerPool(gpu_workers=3, cpu_workers=0, selected_gpus=selected_gpus)
        for w in pool.workers:
            w.is_busy = True

        new_gpus = [("NVIDIA", "/dev/dri/renderD128", {"name": "GPU0", "workers": 1})]
        pool.reconcile_gpu_workers(new_gpus)

        for w in pool.workers:
            if w._pending_removal:
                w.is_busy = False

        retired = pool._apply_deferred_removals()
        assert retired == 2
        assert len(pool.workers) == 1

    def test_reconcile_disabled_device_defers_busy(self):
        """Disabling an entire device defers busy workers instead of dropping them."""
        selected_gpus = [("NVIDIA", "/dev/dri/renderD128", {"name": "GPU0", "workers": 2})]
        pool = WorkerPool(gpu_workers=2, cpu_workers=0, selected_gpus=selected_gpus)
        pool.workers[0].is_busy = True

        result = pool.reconcile_gpu_workers([])

        assert result["removed"] == 1
        assert result["deferred"] == 1
        assert len(pool.workers) == 1
        assert pool.workers[0]._pending_removal is True


class TestWorkerProgressCount:
    """Test that GPU→CPU fallback does not double-count completed_tasks (H2)."""

    @patch("plex_generate_previews.jobs.worker.process_item")
    def test_progress_not_double_counted_on_gpu_cpu_fallback(self, mock_process):
        """When a GPU worker re-queues to CPU, completed_tasks increments only once."""
        completed_counts = []

        def mock_process_fn(
            item_key,
            gpu,
            gpu_device,
            config,
            plex,
            progress_callback=None,
            ffmpeg_threads_override=None,
            cancel_check=None,
            worker_name=None,
            fingerprint_store=None,
        ):
            time.sleep(0.01)
            if gpu is not None:
                raise CodecNotSupportedError("Codec not supported by GPU")
            return ProcessingResult.GENERATED

        mock_process.side_effect = mock_process_fn

        selected_gpus = [("NVIDIA", "cuda", {"name": "RTX 3080"})]
        pool = WorkerPool(gpu_workers=1, cpu_workers=1, selected_gpus=selected_gpus)

        config = MagicMock()
        config.cpu_threads = 1
        plex = MagicMock()

        items = [("key1", "Test Video", "movie")]

        # We need to track the on_task_complete callback.
        # process_items_headless calls _process_items_loop with on_task_complete.
        # Instead, call _process_items_loop directly with a tracking callback.
        def on_task_complete(completed, total):
            completed_counts.append(completed)

        pool._process_items_loop(
            media_items=items,
            config=config,
            plex=plex,
            title_max_width=30,
            library_name="Test",
            on_task_complete=on_task_complete,
        )

        # completed_tasks should have incremented to exactly 1 — NOT 2
        assert completed_counts[-1] == 1, (
            f"Expected final completed_tasks=1, got {completed_counts[-1]}. All counts: {completed_counts}"
        )

    @patch("plex_generate_previews.jobs.worker.process_item")
    def test_fallback_state_resets_on_new_task(self, mock_process):
        """fallback_active / fallback_reason are cleared when a new task is assigned."""
        worker = Worker(0, "GPU", "NVIDIA", "cuda", 0, "RTX 3080")
        config = MagicMock()
        config.cpu_threads = 1
        plex = MagicMock()

        calls = []

        def first_fn(*args, **kwargs):
            calls.append(args)
            if len(calls) == 1:
                raise CodecNotSupportedError("Codec not supported")
            return ProcessingResult.GENERATED

        mock_process.side_effect = first_fn
        worker.assign_task(
            "key1",
            config,
            plex,
            media_title="Video 1",
            media_type="movie",
        )
        if worker.current_thread:
            worker.current_thread.join(timeout=2)
        worker.check_completion()
        assert worker.fallback_active is True
        assert worker.fallback_reason

        # Second task: clean start — fallback state must reset on assign.
        calls.clear()
        mock_process.side_effect = None
        mock_process.return_value = ProcessingResult.GENERATED
        worker.assign_task(
            "key2",
            config,
            plex,
            media_title="Video 2",
            media_type="movie",
        )
        assert worker.fallback_active is False
        assert worker.fallback_reason is None
        if worker.current_thread:
            worker.current_thread.join(timeout=2)
        worker.check_completion()


class TestWorkerCancellation:
    """Test that cancellation is properly handled by workers."""

    @patch("plex_generate_previews.jobs.worker.process_item")
    def test_worker_cancellation_does_not_fallback_to_cpu(self, mock_process):
        """Cancellation on GPU worker must not trigger an in-place CPU retry."""
        worker = Worker(0, "GPU", "NVIDIA", "cuda", 0, "RTX 3080")
        config = MagicMock()
        config.cpu_threads = 2
        plex = MagicMock()

        mock_process.side_effect = CancellationError("cancelled")

        worker.assign_task(
            "test_key",
            config,
            plex,
            media_title="Cancelled Movie",
            media_type="movie",
            cancel_check=lambda: True,
        )

        if worker.current_thread:
            worker.current_thread.join(timeout=2)

        assert worker.failed == 1
        assert worker.completed == 0
        assert worker.fallback_active is False
        # Only one call — cancellation short-circuits the CPU retry path.
        assert mock_process.call_count == 1

    @patch("plex_generate_previews.jobs.worker.process_item")
    def test_worker_passes_cancel_check_to_process_item(self, mock_process):
        """Test that cancel_check is forwarded from assign_task to process_item."""
        mock_process.return_value = ProcessingResult.GENERATED

        def cancel_fn():
            return False

        worker = Worker(0, "CPU", None, None, 0, None)
        config = MagicMock()
        plex = MagicMock()

        worker.assign_task(
            "test_key",
            config,
            plex,
            media_title="Test",
            media_type="movie",
            cancel_check=cancel_fn,
        )

        if worker.current_thread:
            worker.current_thread.join(timeout=2)

        assert mock_process.called
        call_kwargs = mock_process.call_args[1]
        assert call_kwargs.get("cancel_check") is cancel_fn


class TestBuildSelectedGpus:
    """Test _build_selected_gpus() — merges gpu_config with detected GPU cache."""

    @pytest.fixture(autouse=True)
    def _stub_gpu_cache(self, monkeypatch):
        """Control the GPU cache directly and bypass live detection."""
        from plex_generate_previews.web.routes import _helpers

        _helpers._gpu_cache["result"] = []
        monkeypatch.setattr(_helpers, "_ensure_gpu_cache", lambda: None)
        yield
        _helpers._gpu_cache["result"] = None

    def _set_cache(self, gpus):
        from plex_generate_previews.web.routes import _helpers

        _helpers._gpu_cache["result"] = gpus

    def _make_settings(self, gpu_config):
        sm = MagicMock()
        sm.gpu_config = gpu_config
        return sm

    def test_enabled_gpu_returned_with_config_values(self):
        from plex_generate_previews.web.routes.job_runner import _build_selected_gpus

        self._set_cache([{"type": "NVIDIA", "device": "cuda:0", "name": "RTX 3080"}])
        settings = self._make_settings([{"device": "cuda:0", "enabled": True, "workers": 3, "ffmpeg_threads": 4}])

        result = _build_selected_gpus(settings)

        assert len(result) == 1
        gpu_type, device, info = result[0]
        assert gpu_type == "NVIDIA"
        assert device == "cuda:0"
        assert info["workers"] == 3
        assert info["ffmpeg_threads"] == 4

    def test_disabled_gpu_is_skipped(self):
        from plex_generate_previews.web.routes.job_runner import _build_selected_gpus

        self._set_cache([{"type": "AMD", "device": "vaapi:/dev/dri/renderD128", "name": "RX 6800"}])
        settings = self._make_settings([{"device": "vaapi:/dev/dri/renderD128", "enabled": False, "workers": 2}])

        assert _build_selected_gpus(settings) == []

    def test_zero_workers_is_skipped(self):
        from plex_generate_previews.web.routes.job_runner import _build_selected_gpus

        self._set_cache([{"type": "NVIDIA", "device": "cuda:0", "name": "RTX"}])
        settings = self._make_settings([{"device": "cuda:0", "enabled": True, "workers": 0}])

        assert _build_selected_gpus(settings) == []

    def test_failed_gpu_is_skipped(self):
        from plex_generate_previews.web.routes.job_runner import _build_selected_gpus

        self._set_cache(
            [
                {"type": "NVIDIA", "device": "cuda:0", "name": "Working", "status": "ok"},
                {"type": "NVIDIA", "device": "cuda:1", "name": "Broken", "status": "failed"},
            ]
        )
        settings = self._make_settings(
            [
                {"device": "cuda:0", "enabled": True, "workers": 1},
                {"device": "cuda:1", "enabled": True, "workers": 1},
            ]
        )

        result = _build_selected_gpus(settings)
        devices = [r[1] for r in result]
        assert "cuda:0" in devices
        assert "cuda:1" not in devices

    def test_undetected_gpu_gets_default_config(self):
        """GPU in cache but not in gpu_config → defaults (workers=1, ffmpeg_threads=2)."""
        from plex_generate_previews.web.routes.job_runner import _build_selected_gpus

        self._set_cache([{"type": "INTEL", "device": "qsv", "name": "Arc"}])
        settings = self._make_settings([])

        result = _build_selected_gpus(settings)

        assert len(result) == 1
        _, device, info = result[0]
        assert device == "qsv"
        assert info["workers"] == 1
        assert info["ffmpeg_threads"] == 2

    def test_empty_cache_returns_empty_list(self):
        from plex_generate_previews.web.routes.job_runner import _build_selected_gpus

        self._set_cache([])
        settings = self._make_settings([])

        assert _build_selected_gpus(settings) == []

    def test_mixed_enabled_and_disabled(self):
        from plex_generate_previews.web.routes.job_runner import _build_selected_gpus

        self._set_cache(
            [
                {"type": "NVIDIA", "device": "cuda:0", "name": "A"},
                {"type": "NVIDIA", "device": "cuda:1", "name": "B"},
                {"type": "AMD", "device": "vaapi:/dev/dri/renderD128", "name": "C"},
            ]
        )
        settings = self._make_settings(
            [
                {"device": "cuda:0", "enabled": True, "workers": 2, "ffmpeg_threads": 3},
                {"device": "cuda:1", "enabled": False, "workers": 2},
                {"device": "vaapi:/dev/dri/renderD128", "enabled": True, "workers": 1},
            ]
        )

        result = _build_selected_gpus(settings)
        devices = [r[1] for r in result]
        assert set(devices) == {"cuda:0", "vaapi:/dev/dri/renderD128"}
