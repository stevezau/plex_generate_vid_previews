"""
Worker classes for processing media items using threading.

Provides Worker and WorkerPool classes that use threading instead of
multiprocessing for better simplicity and performance with FFmpeg tasks.
"""

import queue
import re
import threading
import time
from collections import defaultdict, deque
from functools import partial
from typing import Any, List, Optional, Tuple

from loguru import logger

from .config import Config
from .media_processing import CodecNotSupportedError, ProcessingResult, process_item
from .utils import format_display_title

_job_thread_ids: set = set()
_job_thread_ids_lock = threading.Lock()


def register_job_thread():
    """Register the current thread as belonging to the active job."""
    with _job_thread_ids_lock:
        _job_thread_ids.add(threading.current_thread().ident)


def clear_job_threads():
    """Clear all registered job thread IDs (call when job finishes)."""
    with _job_thread_ids_lock:
        _job_thread_ids.clear()


def is_job_thread(thread_id: int) -> bool:
    """Check if a thread ID belongs to the active job."""
    with _job_thread_ids_lock:
        return thread_id in _job_thread_ids


class Worker:
    """Represents a worker thread for processing media items."""

    def __init__(
        self,
        worker_id: int,
        worker_type: str,
        gpu: Optional[str] = None,
        gpu_device: Optional[str] = None,
        gpu_index: Optional[int] = None,
        gpu_name: Optional[str] = None,
    ):
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
        self.display_name = f"{worker_type} {worker_id}"

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

        # Job tracking for multi-job dispatch
        self.current_job_id: Optional[str] = None

        # Pre-task baselines for per-task success/failure detection
        self._pre_task_completed = 0
        self._pre_task_failed = 0
        self._pre_task_outcome_counts: dict = {}

        # Statistics
        self.completed = 0
        self.failed = 0
        self.requeued_to_cpu = False  # Set when GPU re-queues item to CPU fallback
        self.outcome_counts = {r.value: 0 for r in ProcessingResult}

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
            (r".*TITAN.*RTX.*", lambda m: "TITAN RTX"),  # TITAN RTX -> "TITAN RTX"
            (
                r".*RTX\s*(\d+).*",
                lambda m: f"RTX{m.group(1)}"[:8],
            ),  # Extract RTX number
            (
                r".*GTX\s*(\d+).*",
                lambda m: f"GTX{m.group(1)}"[:8],
            ),  # Extract GTX number
            (
                r".*GeForce\s+([A-Z0-9\s]+).*",
                lambda m: m.group(1).strip()[:8],
            ),  # Extract GeForce model
            (r".*TITAN.*", lambda m: "TITAN"),  # TITAN (without RTX)
            (r".*Intel.*", lambda m: "Intel"),  # Intel GPUs
            (r".*AMD.*", lambda m: "AMD"),  # AMD GPUs
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
        if self.worker_type == "GPU":
            gpu_display = self._format_gpu_name_for_display()
            return f"[{gpu_display}]: Idle - Waiting for task..."
        if self.worker_type == "CPU_FALLBACK":
            return "[CPU FB   ]: Idle - Waiting for fallback task..."
        return "[CPU      ]: Idle - Waiting for task..."

    def assign_task(
        self,
        item_key: str,
        config: Config,
        plex,
        progress_callback=None,
        media_title: str = "",
        media_type: str = "",
        title_max_width: int = 20,
        cpu_fallback_queue=None,
        job_id: Optional[str] = None,
    ) -> None:
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
            job_id: Optional job identifier for multi-job dispatch routing
        """
        if self.is_busy:
            raise RuntimeError(f"{self.display_name} is already busy")

        # Snapshot pre-task baselines for per-task success/failure detection
        self._pre_task_completed = self.completed
        self._pre_task_failed = self.failed
        self._pre_task_outcome_counts = dict(self.outcome_counts)

        # Reset all progress tracking to ensure clean state
        self.is_busy = True
        self.current_task = item_key
        self.current_job_id = job_id
        self.media_title = media_title
        self.media_type = media_type
        self.media_file = ""  # Will be populated by progress callback
        self.title_max_width = title_max_width
        self.display_title = format_display_title(
            media_title, media_type, title_max_width
        )
        # Show GPU name in display for GPU workers, show CPU identifier for CPU workers
        if self.worker_type == "GPU":
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
        self.requeued_to_cpu = False  # Reset per-task GPU→CPU handoff flag

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
            daemon=True,
        )
        self.current_thread.start()

    def _process_item(
        self,
        item_key: str,
        config: Config,
        plex,
        progress_callback=None,
        cpu_fallback_queue=None,
    ) -> None:
        """
        Process a media item in the background thread.

        Args:
            item_key: Plex media item key
            config: Configuration object
            plex: Plex server instance
            progress_callback: Callback function for progress updates
            cpu_fallback_queue: Optional queue to add task to if codec error occurs (GPU workers only)
        """
        register_job_thread()

        # Use file path if available, otherwise fall back to title or item_key
        display_name = (
            self.media_file
            if self.media_file
            else (self.media_title if self.media_title else item_key)
        )

        # Bind structured context so every log line in this thread carries
        # worker metadata (useful for JSON/ELK aggregation pipelines)
        ctx_logger = logger.bind(
            worker_id=self.worker_id,
            worker_type=self.worker_type,
            gpu_index=self.gpu_index,
            media_title=self.media_title,
            item_key=item_key,
        )
        ctx_logger.info(f"{self.display_name} started: {display_name}")

        try:
            result = process_item(
                item_key, self.gpu, self.gpu_device, config, plex, progress_callback
            )
            self.outcome_counts[result.value] += 1
            if result == ProcessingResult.FAILED:
                self.failed += 1
            else:
                self.completed += 1
        except CodecNotSupportedError as e:
            # Codec not supported by GPU - re-queue for CPU worker
            if self.worker_type == "GPU":
                ctx_logger.warning(
                    f"{self.display_name} detected unsupported codec for {display_name}; handing off to CPU worker"
                )
                # Add to fallback queue for CPU worker processing (multiple CPU workers can compete for items)
                fallback_threads = getattr(config, "fallback_cpu_threads", 0)
                if isinstance(fallback_threads, bool):
                    fallback_threads = int(fallback_threads)
                elif isinstance(fallback_threads, str):
                    try:
                        fallback_threads = int(fallback_threads)
                    except ValueError:
                        fallback_threads = 0
                elif not isinstance(fallback_threads, int):
                    fallback_threads = 0
                can_use_cpu_fallback = config.cpu_threads > 0 or fallback_threads > 0
                if cpu_fallback_queue is not None and can_use_cpu_fallback:
                    # Preserve media info and job_id (set during assign_task)
                    try:
                        cpu_fallback_queue.put(
                            (self.current_job_id, item_key, self.media_title, self.media_type)
                        )
                        self.requeued_to_cpu = True
                        ctx_logger.info(
                            f"Added {display_name} to CPU fallback queue (unsupported codec on GPU)"
                        )
                    except Exception as queue_error:
                        ctx_logger.error(
                            f"Failed to add {item_key} to fallback queue: {queue_error}"
                        )
                        self.outcome_counts["failed"] += 1
                        self.failed += 1
                else:
                    if config.cpu_threads == 0 and fallback_threads == 0:
                        ctx_logger.warning(
                            "Codec not supported by GPU, but CPU fallback is disabled "
                            "(CPU_THREADS=0 and FALLBACK_CPU_THREADS=0); "
                            f"skipping {display_name}"
                        )
                    self.outcome_counts["failed"] += 1
                    self.failed += 1
                # Do NOT mark as completed here - CPU worker will mark it when actually processed
            else:
                # CPU worker received codec error - this is unexpected, treat as failure
                ctx_logger.error(
                    f"{self.display_name} encountered codec error for {display_name}: {e}"
                )
                ctx_logger.error(
                    "Codec errors should not occur on CPU workers - file may be corrupted"
                )
                self.outcome_counts["failed"] += 1
                self.failed += 1
        except Exception as e:
            ctx_logger.error(
                f"{self.display_name} failed to process {display_name}: {e}"
            )
            self.outcome_counts["failed"] += 1
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

    def last_task_succeeded(self) -> bool:
        """Check whether the most recently completed task succeeded.

        Compares current completed/failed counters against the pre-task
        baselines captured when ``assign_task`` was called.
        """
        completed_delta = self.completed - self._pre_task_completed
        failed_delta = self.failed - self._pre_task_failed
        return completed_delta == 1 and failed_delta == 0

    def last_task_outcome_delta(self) -> dict:
        """Return the outcome count changes for the most recent task.

        Compares current outcome_counts against the pre-task snapshot
        captured when ``assign_task`` was called.

        Returns:
            Dict mapping outcome keys to their delta (usually 0 or 1).
        """
        return {
            key: self.outcome_counts.get(key, 0) - self._pre_task_outcome_counts.get(key, 0)
            for key in self.outcome_counts
        }

    def get_progress_data(self) -> dict:
        """Get current progress data for main thread."""
        return {
            "progress_percent": self.progress_percent,
            "speed": self.speed,
            "task_title": self.task_title,
            "is_busy": self.is_busy,
            "current_duration": self.current_duration,
            "total_duration": self.total_duration,
            "remaining_time": self.remaining_time,
            "worker_id": self.worker_id,  # Add worker ID for debugging
            "worker_type": self.worker_type,  # Add worker type for debugging
            # FFmpeg data for display
            "frame": self.frame,
            "fps": self.fps,
            "q": self.q,
            "size": self.size,
            "time_str": self.time_str,
            "bitrate": self.bitrate,
        }

    def shutdown(self) -> None:
        """Shutdown the worker gracefully."""
        if self.current_thread and self.current_thread.is_alive():
            # Wait for current task to complete (with timeout)
            self.current_thread.join(timeout=60)
            if self.current_thread.is_alive():
                logger.warning(
                    f"{self.display_name} did not finish within shutdown timeout"
                )

    @staticmethod
    def find_available(workers: List["Worker"]) -> Optional["Worker"]:
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

    def __init__(
        self,
        gpu_workers: int,
        cpu_workers: int,
        selected_gpus: List[Tuple[str, str, dict]],
        fallback_cpu_workers: int = 0,
    ):
        """
        Initialize worker pool.

        Args:
            gpu_workers: Number of GPU workers to create
            cpu_workers: Number of CPU workers to create
            selected_gpus: List of (gpu_type, gpu_device, gpu_info) tuples for GPU workers
            fallback_cpu_workers: Number of fallback-only CPU workers to create
        """
        self.workers = []
        self._workers_lock = threading.RLock()
        self._progress_lock = threading.Lock()  # Thread-safe progress updates
        self.selected_gpus = selected_gpus
        self._next_gpu_assignment_index = 0
        self._next_worker_id = 0
        self._next_type_index: dict[str, int] = defaultdict(lambda: 1)
        self.cpu_fallback_queue = (
            queue.Queue()
        )  # Thread-safe queue for CPU-only tasks (codec fallback)
        # Deferred scale-down requests by worker type; busy workers are retired
        # when they finish their current task.
        self._pending_removals = defaultdict(int)
        self.add_workers("GPU", gpu_workers)
        self.add_workers("CPU", cpu_workers)
        self.add_workers("CPU_FALLBACK", fallback_cpu_workers)

        logger.info(
            "Initialized "
            f"{len(self.workers)} workers: {gpu_workers} GPU + {cpu_workers} CPU"
            + (
                f" + {fallback_cpu_workers} CPU_FALLBACK"
                if fallback_cpu_workers > 0
                else ""
            )
        )

    def has_busy_workers(self) -> bool:
        """Check if any workers are currently busy."""
        with self._workers_lock:
            return any(worker.is_busy for worker in self.workers)

    def has_available_workers(self) -> bool:
        """Check if any workers are available for new tasks."""
        with self._workers_lock:
            return any(worker.is_available() for worker in self.workers)

    def _snapshot_workers(self) -> List["Worker"]:
        """Return a stable snapshot of workers for safe iteration."""
        with self._workers_lock:
            return list(self.workers)

    def _create_worker(self, worker_type: str) -> "Worker":
        """Create a worker instance with a unique ID and display name."""
        worker_id = self._next_worker_id
        self._next_worker_id += 1
        normalized_type = worker_type.upper()
        type_idx = self._next_type_index[normalized_type]
        self._next_type_index[normalized_type] = type_idx + 1

        if normalized_type == "GPU":
            if not self.selected_gpus:
                raise ValueError("Cannot create GPU worker: no GPUs available")
            gpu_index = self._next_gpu_assignment_index % len(self.selected_gpus)
            self._next_gpu_assignment_index += 1
            gpu_type, gpu_device, gpu_info = self.selected_gpus[gpu_index]
            gpu_name = gpu_info.get("name", f"{gpu_type} GPU")
            worker = Worker(
                worker_id,
                "GPU",
                gpu_type,
                gpu_device,
                gpu_index,
                gpu_name,
            )
            worker.display_name = f"GPU Worker {type_idx} ({gpu_name})"
            return worker

        if normalized_type == "CPU":
            worker = Worker(worker_id, "CPU")
            worker.display_name = f"CPU Worker {type_idx}"
            return worker
        if normalized_type == "CPU_FALLBACK":
            worker = Worker(worker_id, "CPU_FALLBACK")
            worker.display_name = f"CPU Fallback Worker {type_idx}"
            return worker
        raise ValueError(f"Unsupported worker type: {worker_type}")

    def add_workers(self, worker_type: str, count: int) -> int:
        """Add workers of a specific type and return added count."""
        if count <= 0:
            return 0

        added = 0
        with self._workers_lock:
            for _ in range(count):
                self.workers.append(self._create_worker(worker_type))
                added += 1
        if added > 0:
            logger.info(f"Added {added} {worker_type.upper()} worker(s)")
        return added

    def remove_workers(self, worker_type: str, count: int) -> dict:
        """Remove workers of a type.

        Idle workers are removed immediately. Busy workers are scheduled for
        deferred removal and retired when they become idle.

        Returns:
            {"removed": int, "scheduled": int, "unavailable": int}
        """
        if count <= 0:
            return {"removed": 0, "scheduled": 0, "unavailable": 0}

        normalized_type = worker_type.upper()
        if normalized_type not in {"GPU", "CPU", "CPU_FALLBACK"}:
            raise ValueError(f"Unsupported worker type: {worker_type}")

        removed = 0
        scheduled = 0
        unavailable = 0
        with self._workers_lock:
            matches = [w for w in self.workers if w.worker_type == normalized_type]
            unavailable = max(0, count - len(matches))
            idle_matches = [w for w in matches if not w.is_busy]
            busy_matches = [w for w in matches if w.is_busy]

            for worker in idle_matches:
                if removed >= count:
                    break
                self.workers.remove(worker)
                removed += 1

            remaining = count - removed
            if remaining > 0 and busy_matches:
                scheduled = min(remaining, len(busy_matches))
                self._pending_removals[normalized_type] += scheduled

        if removed > 0:
            logger.info(f"Removed {removed} idle {normalized_type} worker(s)")
        if scheduled > 0:
            logger.info(
                f"Scheduled {scheduled} busy {normalized_type} worker(s) for removal when idle"
            )
        return {"removed": removed, "scheduled": scheduled, "unavailable": unavailable}

    def _retire_idle_worker_if_scheduled(self, worker: "Worker") -> bool:
        """Retire an idle worker if deferred removal was requested for its type."""
        if worker.is_busy:
            return False

        with self._workers_lock:
            pending = int(self._pending_removals.get(worker.worker_type, 0))
            if pending <= 0:
                return False
            if worker not in self.workers:
                return False
            self.workers.remove(worker)
            self._pending_removals[worker.worker_type] = pending - 1

        logger.info(f"Retired {worker.display_name} after deferred removal request")
        return True

    def _apply_deferred_removals(self) -> int:
        """Remove currently idle workers that are pending deferred retirement."""
        retired = 0
        # Work on a snapshot to avoid mutating the list while iterating.
        for worker in self._snapshot_workers():
            if self._retire_idle_worker_if_scheduled(worker):
                retired += 1
        return retired

    def _find_available_worker(self, cpu_only: bool = False) -> Optional["Worker"]:
        """
        Find an available worker.

        Args:
            cpu_only: If True, only look for CPU workers

        Returns:
            First available worker matching criteria, or None
        """
        with self._workers_lock:
            if cpu_only:
                for worker in self.workers:
                    if (
                        worker.worker_type
                        in (
                            "CPU",
                            "CPU_FALLBACK",
                        )
                        and worker.is_available()
                    ):
                        return worker
                return None
            # Fallback-only workers should never consume main-queue tasks.
            for worker in self.workers:
                if worker.worker_type != "CPU_FALLBACK" and worker.is_available():
                    return worker
            return None

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
                video_element = data.find("Video") or data.find("Directory")
                if video_element is not None:
                    return (
                        video_element.get("title", "Unknown (fallback)"),
                        video_element.tag.lower(),
                    )
        except Exception as e:
            logger.debug(f"Could not re-query Plex for {item_key}: {e}")
        return ("Unknown (fallback)", "unknown")

    def _assign_fallback_task(
        self, worker: "Worker", config: Config, plex, title_max_width: int
    ) -> bool:
        """
        Assign a task from fallback queue to a CPU worker.

        The fallback queue contains 4-tuples: (job_id, item_key, title, type).

        Returns:
            True if task was assigned, False if queue was empty
        """
        try:
            fallback_item = self.cpu_fallback_queue.get_nowait()
            job_id, item_key, media_title, media_type = fallback_item

            # Re-query Plex for media info if not available
            if media_title is None or media_type is None:
                media_title, media_type = self._get_plex_media_info(plex, item_key)

            progress_callback = partial(self._update_worker_progress, worker)
            worker.assign_task(
                item_key,
                config,
                plex,
                progress_callback=progress_callback,
                media_title=media_title,
                media_type=media_type,
                title_max_width=title_max_width,
                cpu_fallback_queue=None,
                job_id=job_id,
            )
            logger.info(f"Dispatch: assigned fallback item to {worker.display_name}")
            return True
        except queue.Empty:
            return False

    def _assign_main_queue_task(
        self,
        worker: "Worker",
        media_queue: List[tuple],
        config: Config,
        plex,
        title_max_width: int,
    ) -> bool:
        """
        Assign a task from main queue to a worker.

        Returns:
            True if task was assigned, False if queue was empty
        """
        if not media_queue:
            return False

        item_key, media_title, media_type = media_queue.popleft()
        progress_callback = partial(self._update_worker_progress, worker)
        cpu_fallback_queue = (
            self.cpu_fallback_queue if worker.worker_type == "GPU" else None
        )

        worker.assign_task(
            item_key,
            config,
            plex,
            progress_callback=progress_callback,
            media_title=media_title,
            media_type=media_type,
            title_max_width=title_max_width,
            cpu_fallback_queue=cpu_fallback_queue,
        )
        logger.info(
            f"Dispatch: assigned main queue item to {worker.display_name} (title={media_title!r})"
        )
        return True

    def _check_fallback_queue_empty(self) -> bool:
        """
        Check if fallback queue is empty without consuming items.

        Uses qsize() for advisory check — acceptable since this is only
        used for exit-condition heuristics, not synchronization.

        Returns:
            True if queue is empty, False if it has items
        """
        return self.cpu_fallback_queue.qsize() == 0

    def _has_cpu_capable_workers(self) -> bool:
        """Check if any CPU or CPU_FALLBACK workers exist in the pool."""
        with self._workers_lock:
            return any(w.worker_type in ("CPU", "CPU_FALLBACK") for w in self.workers)

    def _drain_fallback_queue_as_failed(self) -> int:
        """Drain unreachable fallback items, counting them as failures.

        Called when the fallback queue has items but no CPU-capable workers
        remain to process them, preventing the dispatch loop from hanging.

        Fallback items are 4-tuples: (job_id, item_key, title, type).

        Returns:
            Number of items drained.
        """
        drained = 0
        while not self.cpu_fallback_queue.empty():
            try:
                item = self.cpu_fallback_queue.get_nowait()
                # 4-tuple: (job_id, item_key, title, type) — item_key is at index 1
                item_key = item[1] if isinstance(item, (list, tuple)) and len(item) >= 2 else item
                logger.warning(
                    f"Draining unreachable fallback item {item_key} as failed "
                    "(no CPU workers available)"
                )
                drained += 1
            except queue.Empty:
                break
        if drained:
            logger.warning(
                f"Drained {drained} fallback item(s) as failed — "
                "add CPU or CPU_FALLBACK workers to process codec-fallback items"
            )
        return drained

    def process_items(
        self,
        media_items: List[tuple],
        config: Config,
        plex,
        worker_progress,
        main_progress,
        main_task_id=None,
        title_max_width: int = 20,
        library_name: str = "",
    ) -> dict:
        """
        Process all media items using available workers with Rich progress display.

        Uses dynamic task assignment - workers pull tasks as they become available.

        Args:
            media_items: List of tuples (key, title, media_type) to process
            config: Configuration object
            plex: Plex server instance
            worker_progress: Rich Progress object for displaying worker progress
            main_progress: Rich Progress object for main progress bar
            main_task_id: ID of the main progress task to update
            title_max_width: Maximum width for title display
            library_name: Name of the library section being processed
        """
        # Create progress tasks for each worker in the worker progress instance
        for worker in self._snapshot_workers():
            worker.progress_task_id = worker_progress.add_task(
                worker._format_idle_description(),
                total=100,
                completed=0,
                speed="0.0x",
                style="cyan",
            )

        def on_task_complete(completed_tasks: int, total_items: int) -> None:
            """Update main progress bar on task completion."""
            if main_task_id is not None:
                main_progress.update(main_task_id, completed=completed_tasks)

        def on_poll(completed_tasks: int, total_items: int) -> None:
            """Update Rich worker progress display each poll cycle."""
            for worker in self._snapshot_workers():
                current_time = time.time()
                with self._progress_lock:
                    progress_data = worker.get_progress_data()
                    is_busy = worker.is_busy
                    ffmpeg_started = worker.ffmpeg_started

                if is_busy:
                    should_update = (
                        progress_data["progress_percent"]
                        != worker.last_progress_percent
                        or progress_data["speed"] != worker.last_speed
                        or not ffmpeg_started
                    ) and (current_time - worker.last_update_time > 0.05)
                    if should_update:
                        worker_progress.update(
                            worker.progress_task_id,
                            description=worker.task_title,
                            completed=progress_data["progress_percent"],
                            speed=progress_data["speed"],
                            remaining_time=progress_data["remaining_time"],
                            frame=progress_data["frame"],
                            fps=progress_data["fps"],
                            q=progress_data["q"],
                            size=progress_data["size"],
                            time_str=progress_data["time_str"],
                            bitrate=progress_data["bitrate"],
                        )
                        worker.last_progress_percent = progress_data["progress_percent"]
                        worker.last_speed = progress_data["speed"]
                        worker.last_update_time = current_time
                else:
                    if worker.last_progress_percent != -1:
                        worker_progress.update(
                            worker.progress_task_id,
                            description=worker._format_idle_description(),
                            completed=0,
                            speed="0.0x",
                        )
                        worker.last_progress_percent = -1
                        worker.last_speed = ""

        def on_finish(
            total_completed: int, total_failed: int, total_items: int
        ) -> None:
            """Clean up Rich progress tasks."""
            for worker in self._snapshot_workers():
                if (
                    hasattr(worker, "progress_task_id")
                    and worker.progress_task_id is not None
                ):
                    worker_progress.remove_task(worker.progress_task_id)
                    worker.progress_task_id = None

        return self._process_items_loop(
            media_items=media_items,
            config=config,
            plex=plex,
            title_max_width=title_max_width,
            library_name=library_name,
            on_task_complete=on_task_complete,
            on_poll=on_poll,
            on_finish=on_finish,
            on_item_complete=None,
        )

    def process_items_headless(
        self,
        media_items: List[tuple],
        config: Config,
        plex,
        title_max_width: int = 20,
        library_name: str = "",
        progress_callback=None,
        worker_callback=None,
        on_item_complete=None,
        cancel_check=None,
        pause_check=None,
    ) -> dict:
        """
        Process all media items using available workers in headless mode (no Rich display).

        Uses dynamic task assignment - workers pull tasks as they become available.
        This is used for web/background execution where Rich console is not available.

        Args:
            media_items: List of tuples (key, title, media_type) to process
            config: Configuration object
            plex: Plex server instance
            title_max_width: Maximum width for title display
            library_name: Name of the library section being processed
            progress_callback: Optional callback function(current, total, message) for progress updates
            worker_callback: Optional callback function(workers_list) for worker status updates
            on_item_complete: Optional callback(display_name, title, success) when a worker finishes an item
            cancel_check: Optional callable returning True when processing should stop
            pause_check: Optional callable returning True when dispatch should pause
        """
        last_worker_update = time.time()
        last_progress_update = time.time()
        library_prefix = f"[{library_name}] " if library_name else ""

        def on_task_complete(completed_tasks: int, total_items: int) -> None:
            """Call progress callback on task completion (throttled to avoid SocketIO flood).

            Always fires for the final item so callers see 100% completion.
            """
            nonlocal last_progress_update
            if progress_callback:
                now = time.time()
                is_final = completed_tasks >= total_items
                if is_final or now - last_progress_update >= 0.5:
                    progress_callback(
                        completed_tasks,
                        total_items,
                        f"{library_prefix}{completed_tasks}/{total_items} completed",
                    )
                    last_progress_update = now

        def on_poll(completed_tasks: int, total_items: int) -> None:
            """Emit worker status and progress updates periodically."""
            nonlocal last_worker_update, last_progress_update
            current_time = time.time()

            # Emit progress/ETA updates every 3 seconds so the ETA stays
            # fresh even during long FFmpeg runs between task completions.
            if progress_callback and current_time - last_progress_update >= 3.0:
                progress_callback(
                    completed_tasks,
                    total_items,
                    f"{library_prefix}{completed_tasks}/{total_items} completed",
                )
                last_progress_update = current_time

            if worker_callback and current_time - last_worker_update >= 1.0:
                worker_statuses = []
                all_workers = self._snapshot_workers()

                # Build per-type 1-based indices for display names
                type_counters: dict[str, int] = {}
                worker_type_index: dict[int, int] = {}
                for w in all_workers:
                    type_counters[w.worker_type] = (
                        type_counters.get(w.worker_type, 0) + 1
                    )
                    worker_type_index[w.worker_id] = type_counters[w.worker_type]

                for worker in all_workers:
                    with self._progress_lock:
                        progress_data = worker.get_progress_data()
                        is_busy = worker.is_busy

                    idx = worker_type_index[worker.worker_id]
                    gpu_base_name = (
                        worker.gpu_name or ""
                    ).strip() or f"GPU {worker.gpu_index}"

                    if worker.worker_type == "GPU":
                        display_name = (
                            f"{gpu_base_name} #{idx}"
                            if type_counters["GPU"] > 1
                            else gpu_base_name
                        )
                    elif worker.worker_type == "CPU_FALLBACK":
                        display_name = f"CPU Fallback - Worker {idx}"
                    else:
                        display_name = f"CPU - Worker {idx}"

                    worker_statuses.append(
                        {
                            "worker_id": worker.worker_id,
                            "worker_type": worker.worker_type,
                            "worker_name": display_name,
                            "status": "processing" if is_busy else "idle",
                            "current_title": worker.media_title if is_busy else "",
                            "progress_percent": progress_data["progress_percent"]
                            if is_busy
                            else 0,
                            "speed": progress_data["speed"] if is_busy else "0.0x",
                            "remaining_time": progress_data["remaining_time"]
                            if is_busy
                            else 0.0,
                        }
                    )
                worker_callback(worker_statuses)
                last_worker_update = current_time

        def on_finish(
            total_completed: int, total_failed: int, total_items: int
        ) -> None:
            """Final progress callback."""
            if progress_callback:
                progress_callback(
                    total_completed,
                    total_items,
                    f"{library_prefix}Complete: {total_completed} successful, {total_failed} failed",
                )

        return self._process_items_loop(
            media_items=media_items,
            config=config,
            plex=plex,
            title_max_width=title_max_width,
            library_name=library_name,
            on_task_complete=on_task_complete,
            on_poll=on_poll,
            on_finish=on_finish,
            on_item_complete=on_item_complete,
            cancel_check=cancel_check,
            pause_check=pause_check,
        )

    def _process_items_loop(
        self,
        media_items: List[tuple],
        config: Config,
        plex,
        title_max_width: int,
        library_name: str,
        on_task_complete: Optional[Any] = None,
        on_poll: Optional[Any] = None,
        on_finish: Optional[Any] = None,
        on_item_complete: Optional[Any] = None,
        cancel_check: Optional[Any] = None,
        pause_check: Optional[Any] = None,
    ) -> dict:
        """
        Core processing loop shared by process_items and process_items_headless.

        Handles queue management, task assignment, exit-condition checking, and
        adaptive sleeping. Progress reporting is delegated to the caller via callbacks.

        Args:
            media_items: List of tuples (key, title, media_type) to process
            config: Configuration object
            plex: Plex server instance
            title_max_width: Maximum width for title display
            library_name: Name of the library section being processed
            on_task_complete: Called as on_task_complete(completed_tasks, total_items) after each task finishes
            on_poll: Called as on_poll(completed_tasks, total_items) every poll cycle for UI updates
            on_finish: Called as on_finish(total_completed, total_failed, total_items) at the end
            on_item_complete: Optional. Called as on_item_complete(display_name, title, success)
                when a worker finishes an item (not called for GPU→CPU handoffs; CPU completion is reported when CPU finishes).
            cancel_check: Optional callable returning True when processing should stop
        """
        media_queue = deque(media_items)  # O(1) popleft
        completed_tasks = 0
        total_items = len(media_items)
        last_overall_progress_log = time.time()
        run_successful = 0
        run_failed = 0
        cancellation_requested = False
        per_worker_totals = {}

        library_prefix = f"[{library_name}] " if library_name else ""

        logger.info(
            f"Processing {total_items} items with {len(self._snapshot_workers())} workers"
        )

        def _record_worker_delta(worker: "Worker") -> None:
            """Track per-worker success/failure deltas for this run."""
            nonlocal run_successful, run_failed
            prev_completed, prev_failed = per_worker_totals.get(
                worker.worker_id, (0, 0)
            )
            completed_delta = max(0, worker.completed - prev_completed)
            failed_delta = max(0, worker.failed - prev_failed)
            if completed_delta or failed_delta:
                run_successful += completed_delta
                run_failed += failed_delta
                per_worker_totals[worker.worker_id] = (worker.completed, worker.failed)

        def _handle_completions(workers: List["Worker"]) -> None:
            """Check completions, update counters, and retire deferred workers."""
            nonlocal completed_tasks
            for worker in workers:
                if not worker.check_completion():
                    continue
                title = worker.media_title or "(unknown)"
                prev_completed, prev_failed = per_worker_totals.get(
                    worker.worker_id, (0, 0)
                )
                completed_delta = max(0, worker.completed - prev_completed)
                failed_delta = max(0, worker.failed - prev_failed)
                _record_worker_delta(worker)
                # Don't count GPU→CPU re-queued items — the CPU worker
                # will increment completed_tasks when it actually finishes.
                if not worker.requeued_to_cpu:
                    completed_tasks += 1
                    if on_task_complete:
                        on_task_complete(completed_tasks, total_items)
                    if on_item_complete:
                        success = completed_delta == 1 and failed_delta == 0
                        on_item_complete(
                            worker.display_name,
                            title,
                            success,
                        )
                self._retire_idle_worker_if_scheduled(worker)

        # Initialize per-worker accounting baseline.
        for worker in self._snapshot_workers():
            per_worker_totals[worker.worker_id] = (worker.completed, worker.failed)

        paused_gate_logged = False  # Log pause entry/exit once per pause period
        # Main processing loop
        while True:
            # Check cancellation before doing more work
            if cancel_check and cancel_check():
                logger.info(f"{library_prefix}Cancellation requested — stopping")
                cancellation_requested = True
                break

            # Check for completed tasks and apply deferred retirements.
            _handle_completions(self._snapshot_workers())

            # Delegate UI/progress updates to caller
            if on_poll:
                on_poll(completed_tasks, total_items)

            # Pause between dispatch cycles without interrupting active tasks.
            while pause_check and pause_check():
                if not paused_gate_logged:
                    workers_snap = self._snapshot_workers()
                    busy = sum(1 for w in workers_snap if w.is_busy)
                    logger.info(
                        f"{library_prefix}Pause gate entered; queue_length={len(media_queue)}, busy_workers={busy}, idle_workers={len(workers_snap) - busy}"
                    )
                    paused_gate_logged = True
                if cancel_check and cancel_check():
                    logger.info(f"{library_prefix}Cancellation requested while paused")
                    cancellation_requested = True
                    break
                _handle_completions(self._snapshot_workers())
                if on_poll:
                    on_poll(completed_tasks, total_items)
                time.sleep(0.2)
            if paused_gate_logged:
                workers_snap = self._snapshot_workers()
                busy = sum(1 for w in workers_snap if w.is_busy)
                logger.info(
                    f"{library_prefix}Pause gate exited; queue_length={len(media_queue)}, busy_workers={busy}, idle_workers={len(workers_snap) - busy}"
                )
                paused_gate_logged = False
            if cancellation_requested:
                break

            # Log overall progress every 5 seconds
            current_time = time.time()
            if current_time - last_overall_progress_log >= 5.0:
                progress_percent = (
                    int((completed_tasks / total_items) * 100) if total_items > 0 else 0
                )
                logger.info(
                    f"Processing progress {library_prefix}{completed_tasks}/{total_items} ({progress_percent}%) completed"
                )
                last_overall_progress_log = current_time

            # Assign new tasks to available workers
            while True:
                self._apply_deferred_removals()
                cpu_only = not media_queue
                available_worker = self._find_available_worker(cpu_only=cpu_only)
                if not available_worker:
                    logger.debug(
                        f"{library_prefix}Dispatch: no worker available (cpu_only={cpu_only})"
                    )
                    break

                if available_worker.worker_type in ("CPU", "CPU_FALLBACK"):
                    if self._assign_fallback_task(
                        available_worker, config, plex, title_max_width
                    ):
                        continue
                    if (
                        available_worker.worker_type == "CPU_FALLBACK"
                        or not media_queue
                    ):
                        break

                if not self._assign_main_queue_task(
                    available_worker, media_queue, config, plex, title_max_width
                ):
                    break

            # Check exit condition
            if not media_queue:
                _handle_completions(self._snapshot_workers())
                self._apply_deferred_removals()

                actual_completed = run_successful
                actual_failed = run_failed
                actual_processed = actual_completed + actual_failed

                if actual_processed >= total_items:
                    busy_retries = 0
                    max_busy_retries = 20
                    while self.has_busy_workers() and busy_retries < max_busy_retries:
                        time.sleep(0.001)
                        _handle_completions(self._snapshot_workers())
                        self._apply_deferred_removals()
                        busy_retries += 1

                    actual_completed = run_successful
                    actual_failed = run_failed
                    actual_processed = actual_completed + actual_failed

                    if (
                        not self.has_busy_workers()
                        and self._check_fallback_queue_empty()
                        and actual_processed >= total_items
                    ):
                        logger.debug(
                            f"All items processed ({actual_processed}/{total_items}), exiting"
                        )
                        break

                    # Fallback queue has items but no CPU-capable workers
                    # can drain it — fail them to avoid spinning forever.
                    if (
                        not self.has_busy_workers()
                        and not self._check_fallback_queue_empty()
                        and not self._has_cpu_capable_workers()
                    ):
                        drained = self._drain_fallback_queue_as_failed()
                        run_failed += drained
                        break

            # Adaptive sleep
            if self.has_busy_workers():
                time.sleep(0.005)
            elif not media_queue:
                time.sleep(0.001)

        # Final statistics from run-local accounting (robust to dynamic worker removal).
        total_completed = run_successful
        total_failed = run_failed

        # Aggregate fine-grained outcome counts across all workers.
        outcome = {r.value: 0 for r in ProcessingResult}
        for worker in self._snapshot_workers():
            for key, count in worker.outcome_counts.items():
                outcome[key] += count

        if on_finish:
            on_finish(total_completed, total_failed, total_items)

        logger.info(
            f"Processing complete: {total_completed} successful, {total_failed} failed"
        )

        return {
            "completed": total_completed,
            "failed": total_failed,
            "total": total_items,
            "cancelled": cancellation_requested,
            "outcome": outcome,
        }

    def _update_worker_progress(
        self,
        worker,
        progress_percent,
        current_duration,
        total_duration,
        speed=None,
        remaining_time=None,
        frame=0,
        fps=0,
        q=0,
        size=0,
        time_str="00:00:00.00",
        bitrate=0,
        media_file=None,
    ):
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
                display_path = (
                    worker.media_file if worker.media_file else worker.media_title
                )
                if worker.worker_type == "GPU":
                    logger.info(
                        f"[GPU {worker.gpu_index}]: Started processing {display_path}"
                    )
                else:
                    logger.info(f"[CPU]: Started processing {display_path}")

            # Mark that FFmpeg has started outputting progress
            worker.ffmpeg_started = True

            # Emit periodic progress logs every 5 seconds
            current_time = time.time()
            if current_time - worker.last_verbose_log_time >= 5.0:
                worker.last_verbose_log_time = current_time
                speed_display = speed if speed else "0.0x"
                if worker.worker_type == "GPU":
                    logger.info(
                        f"[GPU {worker.gpu_index}]: {worker.media_title} - {progress_percent}% (speed={speed_display})"
                    )
                else:
                    logger.info(
                        f"[CPU]: {worker.media_title} - {progress_percent}% (speed={speed_display})"
                    )

    def shutdown(self) -> None:
        """Shutdown all workers gracefully."""
        logger.info("Shutting down worker pool...")
        for worker in self._snapshot_workers():
            worker.shutdown()
        logger.info("Worker pool shutdown complete")
