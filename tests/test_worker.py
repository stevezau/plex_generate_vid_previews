"""
Tests for worker.py module.

Tests Worker class, WorkerPool, threading, task assignment,
progress tracking, and error handling.
"""

import time
import pytest
from unittest.mock import MagicMock, patch
import threading

from plex_generate_previews.worker import Worker, WorkerPool


class TestWorker:
    """Test Worker class functionality."""
    
    def test_worker_initialization(self):
        """Test worker is initialized correctly."""
        worker = Worker(0, 'GPU', 'NVIDIA', 'cuda', 0, 'NVIDIA GeForce RTX 3080')
        
        assert worker.worker_id == 0
        assert worker.worker_type == 'GPU'
        assert worker.gpu == 'NVIDIA'
        assert worker.gpu_device == 'cuda'
        assert worker.gpu_index == 0
        assert worker.gpu_name == 'NVIDIA GeForce RTX 3080'
        assert worker.is_busy == False
        assert worker.completed == 0
        assert worker.failed == 0
    
    def test_worker_is_available(self):
        """Test availability check."""
        worker = Worker(0, 'CPU')
        
        assert worker.is_available() == True
        
        worker.is_busy = True
        assert worker.is_available() == False
    
    @patch('plex_generate_previews.media_processing.process_item')
    def test_worker_assign_task(self, mock_process):
        """Test task assignment."""
        worker = Worker(0, 'CPU')
        config = MagicMock()
        plex = MagicMock()
        
        # Mock process_item to return quickly
        mock_process.return_value = None
        
        worker.assign_task(
            'test_key',
            config,
            plex,
            media_title='Test Movie',
            media_type='movie',
            title_max_width=30
        )
        
        assert worker.is_busy == True
        assert worker.current_task == 'test_key'
        assert worker.media_title == 'Test Movie'
        assert worker.media_type == 'movie'
        
        # Wait for thread to complete
        time.sleep(0.1)
    
    def test_worker_assign_task_when_busy(self):
        """Test that assigning task to busy worker raises error."""
        worker = Worker(0, 'CPU')
        worker.is_busy = True
        
        config = MagicMock()
        plex = MagicMock()
        
        with pytest.raises(RuntimeError):
            worker.assign_task('test_key', config, plex)
    
    @patch('plex_generate_previews.media_processing.process_item')
    def test_worker_check_completion(self, mock_process):
        """Test completion detection."""
        worker = Worker(0, 'CPU')
        config = MagicMock()
        plex = MagicMock()
        
        mock_process.return_value = None
        
        worker.assign_task('test_key', config, plex, media_title='Test', media_type='movie')
        
        # Should be busy initially
        assert worker.is_busy == True
        
        # Wait for task to complete
        time.sleep(0.2)
        
        # Check completion
        completed = worker.check_completion()
        assert completed == True
        assert worker.is_busy == False
    
    def test_worker_progress_data(self):
        """Test getting progress data."""
        worker = Worker(0, 'GPU', 'NVIDIA', 'cuda', 0, 'RTX 3080')
        worker.progress_percent = 50
        worker.speed = "2.5x"
        worker.frame = 1000
        worker.fps = 30.0
        
        data = worker.get_progress_data()
        
        assert data['progress_percent'] == 50
        assert data['speed'] == "2.5x"
        assert data['frame'] == 1000
        assert abs(data['fps'] - 30.0) < 0.1
        assert data['worker_id'] == 0
        assert data['worker_type'] == 'GPU'
    
    def test_worker_find_available(self):
        """Test finding first available worker."""
        workers = [
            Worker(0, 'GPU'),
            Worker(1, 'GPU'),
            Worker(2, 'CPU'),
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
    
    def test_worker_format_gpu_name(self):
        """Test GPU name formatting for display."""
        # Test NVIDIA GPU
        worker = Worker(0, 'GPU', 'NVIDIA', 'cuda', 0, 'NVIDIA GeForce RTX 3080')
        name = worker._format_gpu_name_for_display()
        assert len(name) == 10
        assert 'RTX' in name or 'NVIDIA' in name
        
        # Test AMD GPU
        worker = Worker(1, 'GPU', 'AMD', '/dev/dri/renderD128', 0, 'AMD Radeon RX 6800 XT')
        name = worker._format_gpu_name_for_display()
        assert len(name) == 10
        
        # Test Intel GPU
        worker = Worker(2, 'GPU', 'INTEL', 'qsv', 0, 'Intel UHD Graphics 770')
        name = worker._format_gpu_name_for_display()
        assert len(name) == 10
    
    @patch('plex_generate_previews.worker.process_item')
    def test_worker_thread_execution(self, mock_process):
        """Test that worker executes in background thread."""
        worker = Worker(0, 'CPU')
        config = MagicMock()
        plex = MagicMock()
        
        # Track if process_item was called
        call_count = [0]
        def mock_process_fn(*args, **kwargs):
            call_count[0] += 1
            time.sleep(0.1)  # Longer sleep to ensure thread is alive when checked
        
        mock_process.side_effect = mock_process_fn
        
        worker.assign_task('test_key', config, plex, media_title='Test', media_type='movie')
        
        # Give thread a moment to start
        time.sleep(0.01)
        
        # Should be running in background
        assert worker.current_thread is not None
        assert worker.current_thread.is_alive()
        
        # Wait for completion
        worker.current_thread.join(timeout=1)
        
        assert call_count[0] == 1
        assert worker.completed == 1


class TestWorkerPool:
    """Test WorkerPool functionality."""
    
    def test_worker_pool_initialization(self):
        """Test worker pool creates workers correctly."""
        selected_gpus = [
            ('NVIDIA', 'cuda', {'name': 'RTX 3080'}),
            ('AMD', '/dev/dri/renderD128', {'name': 'RX 6800 XT'}),
        ]
        
        pool = WorkerPool(gpu_workers=4, cpu_workers=2, selected_gpus=selected_gpus)
        
        assert len(pool.workers) == 6
        # First 4 should be GPU workers
        assert pool.workers[0].worker_type == 'GPU'
        assert pool.workers[1].worker_type == 'GPU'
        assert pool.workers[2].worker_type == 'GPU'
        assert pool.workers[3].worker_type == 'GPU'
        # Last 2 should be CPU workers
        assert pool.workers[4].worker_type == 'CPU'
        assert pool.workers[5].worker_type == 'CPU'
    
    def test_worker_pool_gpu_assignment(self):
        """Test round-robin GPU assignment."""
        selected_gpus = [
            ('NVIDIA', 'cuda', {'name': 'RTX 3080'}),
            ('AMD', '/dev/dri/renderD128', {'name': 'RX 6800 XT'}),
        ]
        
        pool = WorkerPool(gpu_workers=4, cpu_workers=0, selected_gpus=selected_gpus)
        
        # Worker 0 and 2 should use GPU 0
        assert pool.workers[0].gpu_index == 0
        assert pool.workers[2].gpu_index == 0
        
        # Worker 1 and 3 should use GPU 1
        assert pool.workers[1].gpu_index == 1
        assert pool.workers[3].gpu_index == 1
    
    @patch('plex_generate_previews.media_processing.process_item')
    def test_worker_pool_process_items(self, mock_process):
        """Test processing items with worker pool."""
        mock_process.return_value = None
        
        selected_gpus = []
        pool = WorkerPool(gpu_workers=0, cpu_workers=2, selected_gpus=selected_gpus)
        
        config = MagicMock()
        plex = MagicMock()
        
        items = [
            ('key1', 'Movie 1', 'movie'),
            ('key2', 'Movie 2', 'movie'),
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
        
        assert pool.has_busy_workers() == False
        
        pool.workers[0].is_busy = True
        assert pool.has_busy_workers() == True
    
    def test_worker_pool_has_available_workers(self):
        """Test detection of available workers."""
        pool = WorkerPool(gpu_workers=0, cpu_workers=2, selected_gpus=[])
        
        assert pool.has_available_workers() == True
        
        pool.workers[0].is_busy = True
        pool.workers[1].is_busy = True
        assert pool.has_available_workers() == False
    
    def test_worker_pool_shutdown(self):
        """Test graceful shutdown."""
        pool = WorkerPool(gpu_workers=0, cpu_workers=2, selected_gpus=[])
        
        # Should not crash
        pool.shutdown()
    
    @patch('plex_generate_previews.media_processing.process_item')
    def test_worker_pool_task_completion(self, mock_process):
        """Test that all tasks complete."""
        # Simulate slow processing
        def slow_process(*args, **kwargs):
            time.sleep(0.05)
        
        mock_process.side_effect = slow_process
        
        pool = WorkerPool(gpu_workers=0, cpu_workers=2, selected_gpus=[])
        
        config = MagicMock()
        plex = MagicMock()
        
        items = [
            ('key1', 'Movie 1', 'movie'),
            ('key2', 'Movie 2', 'movie'),
            ('key3', 'Movie 3', 'movie'),
            ('key4', 'Movie 4', 'movie'),
        ]
        
        main_progress = MagicMock()
        worker_progress = MagicMock()
        worker_progress.add_task = MagicMock(side_effect=list(range(10)))
        
        pool.process_items(items, config, plex, worker_progress, main_progress)
        
        # All 4 items should be completed
        total_completed = sum(w.completed for w in pool.workers)
        assert total_completed == 4
    
    @patch('plex_generate_previews.worker.process_item')
    def test_worker_pool_error_handling(self, mock_process):
        """Test that failed tasks are tracked."""
        # Simulate failures
        call_count = [0]
        def failing_process(*args, **kwargs):
            time.sleep(0.01)  # Small delay to simulate work
            call_count[0] += 1
            if call_count[0] % 2 == 0:
                raise Exception("Processing failed")
        
        mock_process.side_effect = failing_process
        
        pool = WorkerPool(gpu_workers=0, cpu_workers=2, selected_gpus=[])
        
        config = MagicMock()
        plex = MagicMock()
        
        items = [
            ('key1', 'Movie 1', 'movie'),
            ('key2', 'Movie 2', 'movie'),
            ('key3', 'Movie 3', 'movie'),
            ('key4', 'Movie 4', 'movie'),
        ]
        
        main_progress = MagicMock()
        worker_progress = MagicMock()
        worker_progress.add_task = MagicMock(side_effect=list(range(10)))
        
        pool.process_items(items, config, plex, worker_progress, main_progress)
        
        # Some should fail
        total_failed = sum(w.failed for w in pool.workers)
        assert total_failed > 0
    
    @patch('plex_generate_previews.media_processing.process_item')
    def test_worker_pool_progress_updates(self, mock_process):
        """Test that progress callbacks work correctly."""
        mock_process.return_value = None
        
        pool = WorkerPool(gpu_workers=0, cpu_workers=1, selected_gpus=[])
        
        config = MagicMock()
        plex = MagicMock()
        
        items = [('key1', 'Movie 1', 'movie')]
        
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

