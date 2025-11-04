"""
Worker classes for processing media items using threading.

Provides Worker and WorkerPool classes that use threading instead of
multiprocessing for better simplicity and performance with FFmpeg tasks.
"""

import re
import threading
import time
import queue
from functools import partial
from typing import List, Optional, Any, Tuple
from loguru import logger

from .config import Config
from .media_processing import process_item, CodecNotSupportedError
from .utils import format_display_title


class Worker:
    """Represents a worker thread for processing media items."""
    
    def __init__(self, worker_id: int, worker_type: str, gpu: Optional[str] = None, 
                 gpu_device: Optional[str] = None, gpu_index: Optional[int] = None, 
                 gpu_name: Optional[str] = None):
        """
        Initialize a worker.
        
        Args:
            worker_id: Unique identifier for this worker
            worker_type: 'GPU' or 'CPU'
            gpu: GPU type for acceleration
            gpu_device: GPU device path
            gpu_index: Index of the assigned GPU hardware
            gpu_name: Human-readable GPU name for display
        """
        self.worker_id = worker_id
        self.worker_type = worker_type
        self.gpu = gpu
        self.gpu_device = gpu_device
        self.gpu_index = gpu_index
        self.gpu_name = gpu_name
        
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
        self.media_file = ""  # Actual file path being processed
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
        
        # Track verbose logging
        self.last_verbose_log_time = 0
        
        # Statistics
        self.completed = 0
        self.failed = 0
    
    def is_available(self) -> bool:
        """Check if this worker is available for a new task."""
        return not self.is_busy
    
    def _format_gpu_name_for_display(self) -> str:
        """Format GPU name for consistent display width."""
        if not self.gpu_name:
            return f"GPU {self.gpu_index}"
        
        # If already short enough, pad to 10 characters
        if len(self.gpu_name) <= 10:
            return self.gpu_name.ljust(10)[:10]
        
        # Dictionary of GPU name patterns and their shortened forms
        # Pattern matching rules: (pattern, replacement or extraction function)
        patterns = [
            (r'.*TITAN.*RTX.*', lambda m: "TITAN RTX"),  # TITAN RTX -> "TITAN RTX"
            (r'.*RTX\s*(\d+).*', lambda m: f"RTX{m.group(1)}"[:8]),  # Extract RTX number
            (r'.*GTX\s*(\d+).*', lambda m: f"GTX{m.group(1)}"[:8]),  # Extract GTX number
            (r'.*GeForce\s+([A-Z0-9\s]+).*', lambda m: m.group(1).strip()[:8]),  # Extract GeForce model
            (r'.*TITAN.*', lambda m: "TITAN"),  # TITAN (without RTX)
            (r'.*Intel.*', lambda m: "Intel"),  # Intel GPUs
            (r'.*AMD.*', lambda m: "AMD"),  # AMD GPUs
        ]
        
        # Try each pattern in order
        for pattern, replacement in patterns:
            match = re.search(pattern, self.gpu_name)
            if match:
                result = replacement(match) if callable(replacement) else replacement
                return result.ljust(10)[:10]
        
        # Fallback: truncate to 8 characters
        return self.gpu_name[:8].ljust(10)[:10]
    
    def _format_idle_description(self) -> str:
        """Format idle description for display."""
        if self.worker_type == 'GPU':
            gpu_display = self._format_gpu_name_for_display()
            return f"[{gpu_display}]: Idle - Waiting for task..."
        return f"[CPU      ]: Idle - Waiting for task..."
    
    def assign_task(self, item_key: str, config: Config, plex, progress_callback=None, 
                   media_title: str = "", media_type: str = "", title_max_width: int = 20, 
                   cpu_fallback_queue=None) -> None:
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
            cpu_fallback_queue: Optional queue to add task to if codec error occurs (GPU workers only)
        """
        if self.is_busy:
            raise RuntimeError(f"Worker {self.worker_id} is already busy")
        
        # Reset all progress tracking to ensure clean state
        self.is_busy = True
        self.current_task = item_key
        self.media_title = media_title
        self.media_type = media_type
        self.media_file = ""  # Will be populated by progress callback
        self.title_max_width = title_max_width
        self.display_title = format_display_title(media_title, media_type, title_max_width)
        # Show GPU name in display for GPU workers, show CPU identifier for CPU workers
        if self.worker_type == 'GPU':
            gpu_display = self._format_gpu_name_for_display()
            self.task_title = f"[{gpu_display}]: {self.display_title}"
        else:
            self.task_title = f"[CPU      ]: {self.display_title}"
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
        self.last_verbose_log_time = 0
        
        # Start processing in background thread
        self.current_thread = threading.Thread(
            target=self._process_item, 
            args=(item_key, config, plex, progress_callback, cpu_fallback_queue),
            daemon=True
        )
        self.current_thread.start()
    
    def _process_item(self, item_key: str, config: Config, plex, progress_callback=None, cpu_fallback_queue=None) -> None:
        """
        Process a media item in the background thread.
        
        Args:
            item_key: Plex media item key
            config: Configuration object
            plex: Plex server instance
            progress_callback: Callback function for progress updates
            cpu_fallback_queue: Optional queue to add task to if codec error occurs (GPU workers only)
        """
        # Use file path if available, otherwise fall back to title or item_key
        display_name = self.media_file if self.media_file else (self.media_title if self.media_title else item_key)
        
        try:
            process_item(item_key, self.gpu, self.gpu_device, config, plex, progress_callback)
            # Mark as completed immediately (thread will finish after this)
            self.completed += 1
        except CodecNotSupportedError as e:
            # Codec not supported by GPU - re-queue for CPU worker
            if self.worker_type == 'GPU':
                logger.warning(f"GPU Worker {self.worker_id} detected unsupported codec for {display_name}; handing off to CPU worker")
                # Add to fallback queue for CPU worker processing (multiple CPU workers can compete for items)
                if cpu_fallback_queue is not None and config.cpu_threads > 0:
                    # Preserve media info (set during assign_task)
                    try:
                        cpu_fallback_queue.put((item_key, self.media_title, self.media_type))
                        logger.debug(f"Added {display_name} to CPU fallback queue")
                    except Exception as queue_error:
                        logger.error(f"Failed to add {item_key} to fallback queue: {queue_error}")
                        self.failed += 1
                else:
                    if config.cpu_threads == 0:
                        logger.warning(f"Codec not supported by GPU, but CPU threads are disabled (CPU_THREADS=0); skipping {display_name}")
                    self.failed += 1
                # Mark as completed from GPU worker perspective (task will be handled by CPU)
                self.completed += 1
            else:
                # CPU worker received codec error - this is unexpected, treat as failure
                logger.error(f"CPU Worker {self.worker_id} encountered codec error for {display_name}: {e}")
                logger.error("Codec errors should not occur on CPU workers - file may be corrupted")
                self.failed += 1
        except Exception as e:
            logger.error(f"Worker {self.worker_id} failed to process {display_name}: {e}")
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
        self.cpu_fallback_queue = queue.Queue()  # Thread-safe queue for CPU-only tasks (codec fallback)
        
        # Add GPU workers first (prioritized) with round-robin GPU assignment
        for i in range(gpu_workers):
            # selected_gpus is guaranteed to be non-empty if gpu_workers > 0
            # because detect_and_select_gpus() exits with error if no GPUs detected
            gpu_index = i % len(selected_gpus)
            gpu_type, gpu_device, gpu_info = selected_gpus[gpu_index]
            gpu_name = gpu_info.get('name', f'{gpu_type} GPU')
            
            worker = Worker(i, 'GPU', gpu_type, gpu_device, gpu_index, gpu_name)
            self.workers.append(worker)
            
            logger.info(f'GPU Worker {i} assigned to GPU {gpu_index} ({gpu_name})')
        
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
    
    def _find_available_worker(self, cpu_only: bool = False) -> Optional['Worker']:
        """
        Find an available worker.
        
        Args:
            cpu_only: If True, only look for CPU workers
            
        Returns:
            First available worker matching criteria, or None
        """
        if cpu_only:
            for worker in self.workers:
                if worker.worker_type == 'CPU' and worker.is_available():
                    return worker
            return None
        return Worker.find_available(self.workers)
    
    def _get_plex_media_info(self, plex, item_key: str) -> Tuple[str, str]:
        """
        Re-query Plex for media information if not available.
        
        Returns:
            Tuple of (media_title, media_type)
        """
        try:
            from .plex_client import retry_plex_call
            data = retry_plex_call(plex.query, item_key)
            if data is not None:
                video_element = data.find('Video') or data.find('Directory')
                if video_element is not None:
                    return (video_element.get('title', 'Unknown (fallback)'), 
                           video_element.tag.lower())
        except Exception as e:
            logger.debug(f"Could not re-query Plex for {item_key}: {e}")
        return ('Unknown (fallback)', 'unknown')
    
    def _assign_fallback_task(self, worker: 'Worker', config: Config, plex, 
                              title_max_width: int) -> bool:
        """
        Assign a task from fallback queue to a CPU worker.
        
        Returns:
            True if task was assigned, False if queue was empty
        """
        try:
            fallback_item = self.cpu_fallback_queue.get_nowait()
            item_key, media_title, media_type = fallback_item
            
            # Re-query Plex for media info if not available
            if media_title is None or media_type is None:
                media_title, media_type = self._get_plex_media_info(plex, item_key)
            
            progress_callback = partial(self._update_worker_progress, worker)
            worker.assign_task(
                item_key, config, plex, 
                progress_callback=progress_callback,
                media_title=media_title,
                media_type=media_type,
                title_max_width=title_max_width,
                cpu_fallback_queue=None
            )
            return True
        except queue.Empty:
            return False
    
    def _assign_main_queue_task(self, worker: 'Worker', media_queue: List[tuple], 
                                config: Config, plex, title_max_width: int) -> bool:
        """
        Assign a task from main queue to a worker.
        
        Returns:
            True if task was assigned, False if queue was empty
        """
        if not media_queue:
            return False
        
        item_key, media_title, media_type = media_queue.pop(0)
        progress_callback = partial(self._update_worker_progress, worker)
        cpu_fallback_queue = self.cpu_fallback_queue if worker.worker_type == 'GPU' else None
        
        worker.assign_task(
            item_key, config, plex,
            progress_callback=progress_callback,
            media_title=media_title,
            media_type=media_type,
            title_max_width=title_max_width,
            cpu_fallback_queue=cpu_fallback_queue
        )
        return True
    
    def _check_fallback_queue_empty(self) -> bool:
        """
        Check if fallback queue is empty without consuming items.
        
        Returns:
            True if queue is empty, False if it has items
        """
        try:
            test_item = self.cpu_fallback_queue.get_nowait()
            self.cpu_fallback_queue.put(test_item)
            return False
        except queue.Empty:
            return True
    
    def process_items(self, media_items: List[tuple], config: Config, plex, worker_progress, main_progress, main_task_id=None, title_max_width: int = 20, library_name: str = "") -> None:
        """
        Process all media items using available workers.
        
        Uses dynamic task assignment - workers pull tasks as they become available.
        
        Args:
            media_items: List of tuples (key, title, media_type) to process
            config: Configuration object
            plex: Plex server instance
            progress: Rich Progress object for displaying worker progress
            main_task_id: ID of the main progress task to update
            title_max_width: Maximum width for title display
            library_name: Name of the library section being processed
        """
        media_queue = list(media_items)  # Copy the list
        completed_tasks = 0
        total_items = len(media_items)
        last_overall_progress_log = time.time()
        
        # Use provided title width for display formatting
        library_prefix = f"[{library_name}] " if library_name else ""
        
        logger.info(f'Processing {total_items} items with {len(self.workers)} workers')
        
        # Create progress tasks for each worker in the worker progress instance
        for worker in self.workers:
            worker.progress_task_id = worker_progress.add_task(
                worker._format_idle_description(),
                total=100,
                completed=0,
                speed="0.0x",
                style="cyan"
            )
        
        # Process all items
        # Continue while we have items in main queue, fallback queue, or busy workers
        # Exit conditions: main queue empty, all items processed, no busy workers, fallback queue empty
        while True: 
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
                        worker_progress.update(
                            worker.progress_task_id,
                            description=worker._format_idle_description(),
                            completed=0,
                            speed="0.0x"
                        )
                        worker.last_progress_percent = -1
                        worker.last_speed = ""
            
            # Log overall progress every 5 seconds
            current_time = time.time()
            if current_time - last_overall_progress_log >= 5.0:
                progress_percent = int((completed_tasks / total_items) * 100) if total_items > 0 else 0
                logger.info(f"Processing progress {library_prefix}{completed_tasks}/{total_items} ({progress_percent}%) completed")
                last_overall_progress_log = current_time
            
            # Assign new tasks to available workers
            # Prioritize fallback queue for CPU workers, then assign from main queue
            while True:
                # If main queue is empty, only look for CPU workers (to process fallback queue)
                cpu_only = not media_queue
                available_worker = self._find_available_worker(cpu_only=cpu_only)
                if not available_worker:
                    break
                
                # For CPU workers, try fallback queue first (codec error fallback)
                if available_worker.worker_type == 'CPU':
                    if self._assign_fallback_task(available_worker, config, plex, title_max_width):
                        continue
                    # No fallback items - if main queue is also empty, break
                    if not media_queue:
                        break
                
                # Assign from main queue (for GPU workers or when main queue has items)
                if not self._assign_main_queue_task(available_worker, media_queue, config, plex, title_max_width):
                    break
            
            # Check exit condition after trying to assign all tasks
            # Exit only if: main queue empty, all items processed, and fallback queue empty
            if not media_queue:
                # Re-check completion one more time (workers might have just finished)
                for worker in self.workers:
                    if worker.check_completion():
                        completed_tasks += 1
                
                # Calculate actual completed count from worker stats (most reliable)
                # This uses worker.completed which is set directly in _process_item
                # This is set when the task actually completes, before the thread finishes
                # Also count failed items - they've been processed even if they failed
                actual_completed = sum(worker.completed for worker in self.workers)
                actual_failed = sum(worker.failed for worker in self.workers)
                actual_processed = actual_completed + actual_failed
                
                # Exit if all items processed (completed or failed)
                # Note: actual_completed can be >= total_items if GPU hands off to CPU
                # (GPU marks as completed + CPU marks as completed = 2 for 1 item)
                # So we check that we've processed at least total_items
                if actual_processed >= total_items:
                    # Give threads time to finish and update is_busy flags
                    # Retry multiple times to catch threads that finish between checks
                    busy_retries = 0
                    max_busy_retries = 20  # Wait up to 20ms for threads to finish
                    while self.has_busy_workers() and busy_retries < max_busy_retries:
                        time.sleep(0.001)  # 1ms delay
                        for worker in self.workers:
                            worker.check_completion()
                        busy_retries += 1
                    
                    # After retries, check if we should exit
                    # Exit if: no busy workers OR we've waited long enough and all items are completed
                    should_exit = (not self.has_busy_workers() or 
                                  (busy_retries >= max_busy_retries and actual_processed >= total_items))
                    
                    if should_exit:
                        if busy_retries >= max_busy_retries and actual_processed >= total_items:
                            # Log that we're exiting after waiting
                            logger.debug(f"All items processed ({actual_processed}/{total_items}), exiting after {busy_retries} retries")
                        
                        # Check fallback queue - if empty, we're done
                        if self._check_fallback_queue_empty():
                            break
            
            # Adaptive sleep to balance responsiveness and CPU usage
            if self.has_busy_workers():
                time.sleep(0.005)  # 5ms sleep for better responsiveness with multiple workers
            elif not media_queue:
                # No busy workers and no main queue items - give a tiny delay to ensure workers finished
                time.sleep(0.001)  # 1ms sleep when idle to let threads finish
        
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
                               remaining_time=None, frame=0, fps=0, q=0, size=0, time_str="00:00:00.00", bitrate=0, media_file=None):
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
            
            # Store media file path if provided
            if media_file:
                worker.media_file = media_file
            
            # Store FFmpeg data for display
            worker.frame = frame
            worker.fps = fps
            worker.q = q
            worker.size = size
            worker.time_str = time_str
            worker.bitrate = bitrate
            
            # Log when FFmpeg actually starts processing (only once)
            if not worker.ffmpeg_started:
                display_path = worker.media_file if worker.media_file else worker.media_title
                if worker.worker_type == 'GPU':
                    logger.info(f"[GPU {worker.gpu_index}]: Started processing {display_path}")
                else:
                    logger.info(f"[CPU]: Started processing {display_path}")
            
            # Mark that FFmpeg has started outputting progress
            worker.ffmpeg_started = True
            
            # Emit periodic progress logs every 5 seconds
            current_time = time.time()
            if current_time - worker.last_verbose_log_time >= 5.0:
                worker.last_verbose_log_time = current_time
                speed_display = speed if speed else "0.0x"
                if worker.worker_type == 'GPU':
                    logger.info(f"[GPU {worker.gpu_index}]: {worker.media_title} - {progress_percent}% (speed={speed_display})")
                else:
                    logger.info(f"[CPU]: {worker.media_title} - {progress_percent}% (speed={speed_display})")
    
    def shutdown(self) -> None:
        """Shutdown all workers gracefully."""
        logger.debug("Shutting down worker pool...")
        for worker in self.workers:
            worker.shutdown()
        logger.debug("Worker pool shutdown complete")
