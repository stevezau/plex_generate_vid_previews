"""
Worker classes for processing media items using threading.

Provides Worker and WorkerPool classes that use threading instead of
multiprocessing for better simplicity and performance with FFmpeg tasks.
"""

import threading
import time
import shutil
from functools import partial
from typing import List, Optional, Any, Tuple
from loguru import logger

from .config import Config
from .media_processing import process_item


def calculate_title_width():
    """
    Calculate optimal title width based on terminal size.
    
    This ensures progress bar titles fit well within the available
    terminal space while maintaining readability.
    
    Returns:
        int: Maximum characters for title display (20-50 range)
    """
    terminal_width = shutil.get_terminal_size().columns
    
    worker_prefix = 7  # "GPU 0: " or "CPU 0: "
    percentage = 6     # " 100% "
    time_elapsed = 10  # " 00:00:05 "
    count_display = 12 # " (1234/5678) "
    speed_display = 8  # " ━ 1.2x"
    progress_bar = 20  # Approximate progress bar width
    
    reserved_space = worker_prefix + percentage + time_elapsed + count_display + speed_display + progress_bar
    available_width = terminal_width - reserved_space
    
    # Set reasonable limits: minimum 20 chars, maximum 50 chars
    return max(min(available_width, 50), 20)


def format_display_title(title: str, media_type: str, title_max_width: int) -> str:
    """
    Format and truncate display title based on media type.
    
    Args:
        title: The media title to format
        media_type: 'episode' or 'movie'
        title_max_width: Maximum width for the title
        
    Returns:
        str: Formatted and padded title
    """
    if media_type == 'episode':
        # For episodes, ensure S01E01 format is always visible
        if len(title) > title_max_width:
            # Simple truncation: keep last 6 chars (S01E01) + show title
            season_episode = title[-6:]  # Last 6 characters (S01E01)
            available_space = title_max_width - 6 - 3  # 6 for S01E01, 3 for "..."
            if available_space > 0:
                show_title = title[:-6].strip()  # Everything except last 6 chars
                if len(show_title) > available_space:
                    show_title = show_title[:available_space] + "..."
                    display_title = show_title + " " + season_episode
                else:
                    # If very constrained, just show season/episode
                    display_title = "..." + season_episode
            else:
                display_title = title
        else:
            display_title = title
    else:
        # For movies, use title as-is
        display_title = title
        
        # Regular truncation for movies
        if len(display_title) > title_max_width:
            display_title = display_title[:title_max_width-3] + "..."  # Leave room for "..."
    
    # Add padding to prevent progress bar jumping (only if not already truncated)
    if len(display_title) <= title_max_width:
        padding_needed = title_max_width - len(display_title)
        display_title = display_title + " " * padding_needed
    
    return display_title


class Worker:
    """Represents a worker thread for processing media items."""
    
    def __init__(self, worker_id: int, worker_type: str, gpu: Optional[str] = None, 
                 gpu_device: Optional[str] = None, gpu_index: Optional[int] = None):
        """
        Initialize a worker.
        
        Args:
            worker_id: Unique identifier for this worker
            worker_type: 'GPU' or 'CPU'
            gpu: GPU type for acceleration
            gpu_device: GPU device path
            gpu_index: Index of the assigned GPU hardware
        """
        self.worker_id = worker_id
        self.worker_type = worker_type
        self.gpu = gpu
        self.gpu_device = gpu_device
        self.gpu_index = gpu_index
        
        # Task state
        self.is_busy = False
        self.current_thread = None
        self.current_task = None
        
        # Progress tracking
        self.progress_percent = 0
        self.speed = "0.0x"
        self.current_duration = 0.0
        self.total_duration = 0.0
        self.remaining_time = 0.0  # Remaining time calculated from FFmpeg data
        self.task_title = ""
        self.display_title = ""
        self.media_title = ""
        self.media_type = ""
        self.title_max_width = 20
        self.progress_task_id = None
        self.ffmpeg_started = False  # Track if FFmpeg has started outputting progress
        self.task_start_time = 0  # Track when task started
        
        # FFmpeg data fields for display
        self.frame = 0
        self.fps = 0
        self.q = 0
        self.size = 0
        self.time_str = "00:00:00.00"
        self.bitrate = 0
        
        # Track last update to avoid unnecessary updates
        self.last_progress_percent = -1
        self.last_speed = ""
        self.last_update_time = 0
        
        # Statistics
        self.completed = 0
        self.failed = 0
    
    def is_available(self) -> bool:
        """Check if this worker is available for a new task."""
        return not self.is_busy
    
    def assign_task(self, item_key: str, config: Config, plex, progress_callback=None, 
                   media_title: str = "", media_type: str = "", title_max_width: int = 20) -> None:
        """
        Assign a new task to this worker.
        
        Args:
            item_key: Plex media item key to process
            config: Configuration object
            plex: Plex server instance
            progress_callback: Callback function for progress updates
            media_title: Media title for display
            media_type: Media type ('episode' or 'movie')
            title_max_width: Maximum width for title display
        """
        if self.is_busy:
            raise RuntimeError(f"Worker {self.worker_id} is already busy")
        
        # Reset all progress tracking to ensure clean state
        self.is_busy = True
        self.current_task = item_key
        self.media_title = media_title
        self.media_type = media_type
        self.title_max_width = title_max_width
        self.display_title = format_display_title(media_title, media_type, title_max_width)
        # Show GPU index in display for GPU workers
        if self.worker_type == 'GPU':
            if self.gpu_index is not None:
                self.task_title = f"{self.worker_type} {self.worker_id} [HW:{self.gpu_index}]: {self.display_title}"
            else:
                self.task_title = f"{self.worker_type} {self.worker_id} [CPU]: {self.display_title}"
        else:
            self.task_title = f"{self.worker_type} {self.worker_id}: {self.display_title}"
        self.progress_percent = 0
        self.speed = "0.0x"
        self.current_duration = 0.0
        self.total_duration = 0.0
        self.remaining_time = 0.0
        self.ffmpeg_started = False
        self.task_start_time = time.time()
        
        # Reset FFmpeg data fields
        self.frame = 0
        self.fps = 0
        self.q = 0
        self.size = 0
        self.time_str = "00:00:00.00"
        self.bitrate = 0
        
        # Reset tracking variables for clean state
        self.last_progress_percent = -1
        self.last_speed = ""
        self.last_update_time = 0
        
        # Start processing in background thread
        self.current_thread = threading.Thread(
            target=self._process_item, 
            args=(item_key, config, plex, progress_callback),
            daemon=True
        )
        self.current_thread.start()
    
    def _process_item(self, item_key: str, config: Config, plex, progress_callback=None) -> None:
        """
        Process a media item in the background thread.
        
        Args:
            item_key: Plex media item key
            config: Configuration object
            plex: Plex server instance
            progress_callback: Callback function for progress updates
        """
        try:
            process_item(item_key, self.gpu, self.gpu_device, config, plex, progress_callback)
            self.completed += 1
        except Exception as e:
            logger.error(f"Worker {self.worker_id} failed to process {item_key}: {e}")
            self.failed += 1
    
    def check_completion(self) -> bool:
        """
        Check if this worker has completed its current task.
        
        Returns:
            bool: True if task completed, False if still running
        """
        if not self.is_busy:
            return False  # Worker is available, not completing
        
        if self.current_thread and not self.current_thread.is_alive():
            # Thread finished, mark as completed
            self.is_busy = False
            self.current_task = None
            return True
        
        return False
    
    def get_progress_data(self) -> dict:
        """Get current progress data for main thread."""
        return {
            'progress_percent': self.progress_percent,
            'speed': self.speed,
            'task_title': self.task_title,
            'is_busy': self.is_busy,
            'current_duration': self.current_duration,
            'total_duration': self.total_duration,
            'remaining_time': self.remaining_time,
            'worker_id': self.worker_id,  # Add worker ID for debugging
            'worker_type': self.worker_type,  # Add worker type for debugging
            # FFmpeg data for display
            'frame': self.frame,
            'fps': self.fps,
            'q': self.q,
            'size': self.size,
            'time_str': self.time_str,
            'bitrate': self.bitrate
        }
    
    def shutdown(self) -> None:
        """Shutdown the worker gracefully."""
        if self.current_thread and self.current_thread.is_alive():
            # Wait for current task to complete (with timeout)
            self.current_thread.join(timeout=5)
    
    @staticmethod
    def find_available(workers: List['Worker']) -> Optional['Worker']:
        """
        Find the first available worker.
        
        GPU workers are prioritized (they come first in the array).
        
        Args:
            workers: List of Worker instances
            
        Returns:
            Worker: First available worker, or None if all are busy
        """
        for worker in workers:
            if worker.is_available():
                return worker
        return None


class WorkerPool:
    """Manages a pool of workers for processing media items."""
    
    def __init__(self, gpu_workers: int, cpu_workers: int, selected_gpus: List[Tuple[str, str, dict]]):
        """
        Initialize worker pool.
        
        Args:
            gpu_workers: Number of GPU workers to create
            cpu_workers: Number of CPU workers to create
            selected_gpus: List of (gpu_type, gpu_device, gpu_info) tuples for GPU workers
        """
        self.workers = []
        self._progress_lock = threading.Lock()  # Thread-safe progress updates
        
        # Add GPU workers first (prioritized) with round-robin GPU assignment
        for i in range(gpu_workers):
            if selected_gpus:
                # Round-robin assignment: worker 0 -> gpu 0, worker 1 -> gpu 1, worker 2 -> gpu 0, etc.
                gpu_index = i % len(selected_gpus)
                gpu_type, gpu_device, gpu_info = selected_gpus[gpu_index]
                gpu_name = gpu_info.get('name', f'{gpu_type} GPU')
                
                worker = Worker(i, 'GPU', gpu_type, gpu_device, gpu_index)
                self.workers.append(worker)
                
                logger.info(f'GPU Worker {i} assigned to GPU {gpu_index} ({gpu_name})')
            else:
                # Fallback to CPU if no GPUs available - but keep as GPU worker type for consistency
                self.workers.append(Worker(i, 'GPU', None, None, None))
        
        # Add CPU workers
        for i in range(cpu_workers):
            self.workers.append(Worker(i + gpu_workers, 'CPU'))
        
        logger.info(f'Initialized {len(self.workers)} workers: {gpu_workers} GPU + {cpu_workers} CPU')
    
    def has_busy_workers(self) -> bool:
        """Check if any workers are currently busy."""
        return any(worker.is_busy for worker in self.workers)
    
    def has_available_workers(self) -> bool:
        """Check if any workers are available for new tasks."""
        return any(worker.is_available() for worker in self.workers)
    
    def process_items(self, media_items: List[tuple], config: Config, plex, worker_progress, main_progress, main_task_id=None) -> None:
        """
        Process all media items using available workers.
        
        Uses dynamic task assignment - workers pull tasks as they become available.
        
        Args:
            media_items: List of tuples (key, title, media_type) to process
            config: Configuration object
            plex: Plex server instance
            progress: Rich Progress object for displaying worker progress
            main_task_id: ID of the main progress task to update
        """
        media_queue = list(media_items)  # Copy the list
        completed_tasks = 0
        
        # Calculate initial title width and track terminal resize
        title_max_width = calculate_title_width()
        last_terminal_width = shutil.get_terminal_size().columns
        
        logger.info(f'Processing {len(media_items)} items with {len(self.workers)} workers')
        
        # Create progress tasks for each worker in the worker progress instance
        for worker in self.workers:
            # Show GPU index in initial task description for GPU workers
            if worker.worker_type == 'GPU':
                if worker.gpu_index is not None:
                    initial_desc = f"{worker.worker_type} {worker.worker_id} [HW:{worker.gpu_index}]: Idle - Waiting for task..."
                else:
                    initial_desc = f"{worker.worker_type} {worker.worker_id} [CPU]: Idle - Waiting for task..."
            else:
                initial_desc = f"{worker.worker_type} {worker.worker_id}: Idle - Waiting for task..."
            
            worker.progress_task_id = worker_progress.add_task(
                initial_desc,
                total=100,
                completed=0,
                speed="0.0x",
                style="cyan"
            )
        
        # Process all items
        while media_queue or self.has_busy_workers():
            # Check for terminal resize and recalculate title width if needed
            current_terminal_width = shutil.get_terminal_size().columns
            if current_terminal_width != last_terminal_width:
                title_max_width = calculate_title_width()
                last_terminal_width = current_terminal_width
                logger.debug(f"Terminal resized: {current_terminal_width} cols, new title width: {title_max_width}")
            
            # Check for completed tasks and update progress
            for worker in self.workers:
                if worker.check_completion():
                    completed_tasks += 1
                    # Update main progress bar if main_task_id is provided
                    if main_task_id is not None:
                        main_progress.update(main_task_id, completed=completed_tasks)
                
                # Update worker progress display with thread-safe access
                current_time = time.time()
                
                # Use thread-safe access to worker progress data
                with self._progress_lock:
                    progress_data = worker.get_progress_data()
                    is_busy = worker.is_busy
                    ffmpeg_started = worker.ffmpeg_started
                
                if is_busy:
                    # Update busy worker only if progress or speed changed and enough time has passed
                    should_update = (
                        (progress_data['progress_percent'] != worker.last_progress_percent or 
                         progress_data['speed'] != worker.last_speed or
                         not ffmpeg_started) and
                        (current_time - worker.last_update_time > 0.05)  # Throttle to 20fps for stability
                    )
                    
                    if should_update:
                        # Use the formatted display title
                        worker_progress.update(
                            worker.progress_task_id,
                            description=worker.task_title,
                            completed=progress_data['progress_percent'],
                            speed=progress_data['speed'],
                            remaining_time=progress_data['remaining_time'],
                            # FFmpeg data for display
                            frame=progress_data['frame'],
                            fps=progress_data['fps'],
                            q=progress_data['q'],
                            size=progress_data['size'],
                            time_str=progress_data['time_str'],
                            bitrate=progress_data['bitrate']
                        )
                        worker.last_progress_percent = progress_data['progress_percent']
                        worker.last_speed = progress_data['speed']
                        worker.last_update_time = current_time
                else:
                    # Update idle worker only if it was previously busy
                    if worker.last_progress_percent != -1:
                        # Show GPU index in idle display for GPU workers
                        if worker.worker_type == 'GPU':
                            if worker.gpu_index is not None:
                                idle_desc = f"{worker.worker_type} {worker.worker_id} [HW:{worker.gpu_index}]: Idle - Waiting for task..."
                            else:
                                idle_desc = f"{worker.worker_type} {worker.worker_id} [CPU]: Idle - Waiting for task..."
                        else:
                            idle_desc = f"{worker.worker_type} {worker.worker_id}: Idle - Waiting for task..."
                        
                        worker_progress.update(
                            worker.progress_task_id,
                            description=idle_desc,
                            completed=0,
                            speed="0.0x"
                        )
                        worker.last_progress_percent = -1
                        worker.last_speed = ""
            
            # Assign new tasks to available workers
            while media_queue and self.has_available_workers():
                available_worker = Worker.find_available(self.workers)
                if available_worker:
                    item_key, media_title, media_type = media_queue.pop(0)
                    
                    # Create progress callback using functools.partial
                    progress_callback = partial(self._update_worker_progress, available_worker)
                    
                    available_worker.assign_task(
                        item_key, 
                        config, 
                        plex, 
                        progress_callback=progress_callback,
                        media_title=media_title,
                        media_type=media_type,
                        title_max_width=title_max_width
                    )
            
            # Adaptive sleep to balance responsiveness and CPU usage
            if self.has_busy_workers():
                time.sleep(0.01)  # 10ms sleep for better stability with multiple workers
        
        # Final statistics
        total_completed = sum(worker.completed for worker in self.workers)
        total_failed = sum(worker.failed for worker in self.workers)
        
        # Clean up worker progress tasks
        for worker in self.workers:
            if hasattr(worker, 'progress_task_id') and worker.progress_task_id is not None:
                worker_progress.remove_task(worker.progress_task_id)
                worker.progress_task_id = None
        
        logger.info(f'Processing complete: {total_completed} successful, {total_failed} failed')
    
    def _update_worker_progress(self, worker, progress_percent, current_duration, total_duration, speed=None, 
                               remaining_time=None, frame=0, fps=0, q=0, size=0, time_str="00:00:00.00", bitrate=0):
        """Update worker progress data from callback."""
        # Use thread-safe updates to prevent race conditions
        with self._progress_lock:
            worker.progress_percent = progress_percent
            worker.current_duration = current_duration
            worker.total_duration = total_duration
            if speed:
                worker.speed = speed
            if remaining_time is not None:
                worker.remaining_time = remaining_time
            
            # Store FFmpeg data for display
            worker.frame = frame
            worker.fps = fps
            worker.q = q
            worker.size = size
            worker.time_str = time_str
            worker.bitrate = bitrate
            
            # Mark that FFmpeg has started outputting progress
            worker.ffmpeg_started = True
    
    def shutdown(self) -> None:
        """Shutdown all workers gracefully."""
        logger.debug("Shutting down worker pool...")
        for worker in self.workers:
            worker.shutdown()
        logger.debug("Worker pool shutdown complete")
