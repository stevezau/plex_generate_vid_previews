"""
Job management for the web interface.

Provides JobManager class for tracking job state, emitting SocketIO events,
and persisting job data to disk.
"""

import json
import os
import threading
import uuid
from collections import deque
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, List, Optional, Callable, Any

from loguru import logger


class JobStatus(str, Enum):
    """Job status enumeration."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class WorkerStatus:
    """Status information for a single worker."""

    worker_id: int = 0
    worker_type: str = "CPU"  # "GPU" or "CPU"
    worker_name: str = "CPU Worker"
    status: str = "idle"  # "idle", "processing"
    current_file: str = ""
    current_title: str = ""
    progress_percent: float = 0.0
    speed: str = "0.0x"
    eta: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class JobProgress:
    """Progress information for a job."""

    percent: float = 0.0
    current_item: str = ""
    total_items: int = 0
    processed_items: int = 0
    speed: str = "0.0x"
    eta: str = ""
    current_file: str = ""
    workers: List[WorkerStatus] = field(default_factory=list)

    def to_dict(self) -> dict:
        result = asdict(self)
        result["workers"] = [
            w.to_dict() if isinstance(w, WorkerStatus) else w for w in self.workers
        ]
        return result


@dataclass
class Job:
    """Represents a processing job."""

    id: str
    status: JobStatus = JobStatus.PENDING
    created_at: str = ""
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    library_id: Optional[str] = None
    library_name: str = ""
    progress: JobProgress = field(default_factory=JobProgress)
    error: Optional[str] = None
    config: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.created_at:
            self.created_at = datetime.now(timezone.utc).isoformat()
        if isinstance(self.progress, dict):
            self.progress = JobProgress(**self.progress)
        if isinstance(self.status, str):
            self.status = JobStatus(self.status)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "status": self.status.value,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "library_id": self.library_id,
            "library_name": self.library_name,
            "progress": self.progress.to_dict(),
            "error": self.error,
            "config": self.config,
        }


class JobManager:
    """
    Manages job queue and state for the web interface.

    Provides methods for creating, updating, and querying jobs,
    as well as emitting SocketIO events for real-time updates.
    """

    def __init__(self, config_dir: str = "/config", socketio=None):
        self.config_dir = config_dir
        self.jobs_file = os.path.join(config_dir, "jobs.json")
        self.socketio = socketio
        self._jobs: Dict[str, Job] = {}
        self._lock = threading.RLock()
        self._current_job_id: Optional[str] = None
        self._on_progress_callbacks: List[Callable] = []

        # Job logs storage (in-memory, last 500 lines per job)
        self._job_logs: Dict[str, deque] = {}
        self._max_log_lines = 500

        # Worker status tracking
        self._worker_statuses: Dict[str, WorkerStatus] = {}

        # Cancellation flags
        self._cancellation_flags: Dict[str, bool] = {}

        # Load existing jobs from disk
        self._load_jobs()

    def set_socketio(self, socketio) -> None:
        """Set the SocketIO instance for real-time updates."""
        self.socketio = socketio

    def _load_jobs(self) -> None:
        """Load jobs from persistent storage."""
        if os.path.exists(self.jobs_file):
            try:
                with open(self.jobs_file, "r") as f:
                    data = json.load(f)
                    needs_save = False
                    for job_data in data.get("jobs", []):
                        try:
                            job = Job(**job_data)
                            # Mark any "running" jobs as failed on startup
                            # (they were interrupted by restart/crash)
                            if job.status == JobStatus.RUNNING:
                                logger.warning(
                                    f"Job {job.id} was running when server stopped - marking as failed"
                                )
                                job.status = JobStatus.FAILED
                                job.error = "Job was interrupted by server restart"
                                job.completed_at = datetime.utcnow().isoformat()
                                needs_save = True
                            self._jobs[job.id] = job
                        except (TypeError, KeyError, ValueError) as job_error:
                            job_id = job_data.get("id", "unknown")
                            logger.warning(f"Failed to load job {job_id}: {job_error}")
                            continue
                logger.info(f"Loaded {len(self._jobs)} jobs from {self.jobs_file}")
                if needs_save:
                    self._save_jobs()
            except (json.JSONDecodeError, IOError) as e:
                logger.warning(f"Failed to load jobs file: {e}")
            except Exception as e:
                logger.error(f"Unexpected error loading jobs: {e}")

    def _save_jobs(self) -> None:
        """Save jobs to persistent storage."""
        os.makedirs(self.config_dir, exist_ok=True)
        try:
            with open(self.jobs_file, "w") as f:
                jobs_data = {"jobs": [job.to_dict() for job in self._jobs.values()]}
                json.dump(jobs_data, f, indent=2)
        except IOError as e:
            logger.error(f"Failed to save jobs: {e}")

    def _emit_event(self, event: str, data: dict) -> None:
        """Emit a SocketIO event if available."""
        if self.socketio:
            self.socketio.emit(event, data, namespace="/jobs")

    # Maximum number of terminal-state jobs to keep on disk
    _MAX_TERMINAL_JOBS = 50

    def _prune_terminal_jobs(self) -> None:
        """Remove oldest completed/failed/cancelled jobs when limit exceeded."""
        terminal = sorted(
            (
                j
                for j in self._jobs.values()
                if j.status
                in (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED)
            ),
            key=lambda j: j.completed_at or j.created_at,
        )
        excess = len(terminal) - self._MAX_TERMINAL_JOBS
        if excess > 0:
            for job in terminal[:excess]:
                del self._jobs[job.id]
            logger.debug(f"Pruned {excess} old terminal jobs")

    def create_job(
        self,
        library_id: Optional[str] = None,
        library_name: str = "",
        config: Optional[Dict[str, Any]] = None,
    ) -> Job:
        """Create a new job."""
        with self._lock:
            job = Job(
                id=str(uuid.uuid4()),
                library_id=library_id,
                library_name=library_name,
                config=config or {},
            )
            self._jobs[job.id] = job
            self._prune_terminal_jobs()
            self._save_jobs()
            self._emit_event("job_created", job.to_dict())
            logger.info(f"Created job {job.id} for library {library_name}")
            return job

    def get_job(self, job_id: str) -> Optional[Job]:
        """Get a job by ID."""
        return self._jobs.get(job_id)

    def get_all_jobs(self) -> List[Job]:
        """Get all jobs."""
        return list(self._jobs.values())

    def get_pending_jobs(self) -> List[Job]:
        """Get all pending jobs."""
        return [j for j in self._jobs.values() if j.status == JobStatus.PENDING]

    def get_running_job(self) -> Optional[Job]:
        """Get the currently running job."""
        if self._current_job_id:
            return self._jobs.get(self._current_job_id)
        return None

    def start_job(self, job_id: str) -> Optional[Job]:
        """Mark a job as started."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job:
                job.status = JobStatus.RUNNING
                job.started_at = datetime.now(timezone.utc).isoformat()
                self._current_job_id = job_id
                self._save_jobs()
                self._emit_event("job_started", job.to_dict())
                logger.info(f"Started job {job_id}")
            return job

    def update_progress(
        self,
        job_id: str,
        percent: float = None,
        current_item: str = None,
        total_items: int = None,
        processed_items: int = None,
        speed: str = None,
        eta: str = None,
        current_file: str = None,
    ) -> Optional[Job]:
        """Update job progress."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job:
                if percent is not None:
                    job.progress.percent = percent
                if current_item is not None:
                    job.progress.current_item = current_item
                if total_items is not None:
                    job.progress.total_items = total_items
                if processed_items is not None:
                    job.progress.processed_items = processed_items
                if speed is not None:
                    job.progress.speed = speed
                if eta is not None:
                    job.progress.eta = eta
                if current_file is not None:
                    job.progress.current_file = current_file

                # Emit progress event (don't save to disk on every update)
                self._emit_event(
                    "job_progress",
                    {"job_id": job_id, "progress": job.progress.to_dict()},
                )
            return job

    def complete_job(self, job_id: str, error: Optional[str] = None) -> Optional[Job]:
        """Mark a job as completed or failed."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job:
                job.completed_at = datetime.now(timezone.utc).isoformat()
                if error:
                    job.status = JobStatus.FAILED
                    job.error = error
                    self._emit_event("job_failed", job.to_dict())
                    logger.error(f"Job {job_id} failed: {error}")
                else:
                    job.status = JobStatus.COMPLETED
                    job.progress.percent = 100.0
                    self._emit_event("job_completed", job.to_dict())
                    logger.info(f"Job {job_id} completed successfully")

                if self._current_job_id == job_id:
                    self._current_job_id = None
                self._save_jobs()
            return job

    def cancel_job(self, job_id: str) -> Optional[Job]:
        """Cancel a job."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job and job.status in (JobStatus.PENDING, JobStatus.RUNNING):
                job.status = JobStatus.CANCELLED
                job.completed_at = datetime.now(timezone.utc).isoformat()
                if self._current_job_id == job_id:
                    self._current_job_id = None
                self._save_jobs()
                self._emit_event("job_cancelled", job.to_dict())
                logger.info(f"Cancelled job {job_id}")
            return job

    def delete_job(self, job_id: str) -> bool:
        """Delete a job."""
        with self._lock:
            if job_id in self._jobs:
                if self._current_job_id == job_id:
                    return False  # Can't delete running job
                del self._jobs[job_id]
                self._save_jobs()
                self._emit_event("job_deleted", {"job_id": job_id})
                logger.info(f"Deleted job {job_id}")
                return True
            return False

    def clear_completed_jobs(self, statuses: Optional[List[str]] = None) -> int:
        """Clear jobs by status.

        Args:
            statuses: List of status strings to clear (e.g. ["completed", "failed"]).
                Defaults to all terminal statuses: completed, failed, cancelled.
        """
        valid_terminal = {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}
        if statuses:
            target = {
                JobStatus(s) for s in statuses if s in {e.value for e in valid_terminal}
            }
        else:
            target = valid_terminal

        with self._lock:
            to_delete = [
                job_id for job_id, job in self._jobs.items() if job.status in target
            ]
            for job_id in to_delete:
                del self._jobs[job_id]
            if to_delete:
                self._save_jobs()
                self._emit_event("jobs_cleared", {"count": len(to_delete)})
            return len(to_delete)

    def get_stats(self) -> dict:
        """Get job statistics."""
        with self._lock:
            stats = {
                "total": len(self._jobs),
                "pending": 0,
                "running": 0,
                "completed": 0,
                "failed": 0,
                "cancelled": 0,
            }
            for job in self._jobs.values():
                stats[job.status.value] += 1
            return stats

    # ========================================================================
    # Job Logs Management
    # ========================================================================

    def add_log(self, job_id: str, message: str) -> None:
        """Add a log message for a job."""
        with self._lock:
            if job_id not in self._job_logs:
                self._job_logs[job_id] = deque(maxlen=self._max_log_lines)
            timestamp = datetime.utcnow().strftime("%H:%M:%S")
            self._job_logs[job_id].append(f"[{timestamp}] {message}")

    def get_logs(self, job_id: str, last_n: int = None) -> List[str]:
        """Get logs for a job."""
        with self._lock:
            if job_id not in self._job_logs:
                return []
            logs = list(self._job_logs[job_id])
            if last_n:
                return logs[-last_n:]
            return logs

    def clear_logs(self, job_id: str) -> None:
        """Clear logs for a job."""
        with self._lock:
            if job_id in self._job_logs:
                del self._job_logs[job_id]

    # ========================================================================
    # Worker Status Management
    # ========================================================================

    def update_worker_status(self, worker_key: str, status: WorkerStatus) -> None:
        """Update status for a worker."""
        with self._lock:
            self._worker_statuses[worker_key] = status

    def get_worker_statuses(self) -> List[WorkerStatus]:
        """Get all worker statuses."""
        with self._lock:
            return list(self._worker_statuses.values())

    def clear_worker_statuses(self) -> None:
        """Clear all worker statuses."""
        with self._lock:
            self._worker_statuses.clear()

    # ========================================================================
    # Cancellation Management
    # ========================================================================

    def request_cancellation(self, job_id: str) -> bool:
        """Request cancellation of a job."""
        with self._lock:
            if job_id in self._jobs:
                self._cancellation_flags[job_id] = True
                return True
            return False

    def is_cancellation_requested(self, job_id: str) -> bool:
        """Check if cancellation has been requested for a job."""
        with self._lock:
            return self._cancellation_flags.get(job_id, False)

    def clear_cancellation_flag(self, job_id: str) -> None:
        """Clear the cancellation flag for a job."""
        with self._lock:
            if job_id in self._cancellation_flags:
                del self._cancellation_flags[job_id]


# Global job manager instance
_job_manager: Optional[JobManager] = None
_job_lock = threading.Lock()

# Default config directory from environment
DEFAULT_CONFIG_DIR = os.environ.get("CONFIG_DIR", "/config")


def get_job_manager(config_dir: Optional[str] = None, socketio=None) -> JobManager:
    """Get or create the global JobManager instance (thread-safe)."""
    global _job_manager
    with _job_lock:
        if _job_manager is None:
            _job_manager = JobManager(
                config_dir=config_dir or DEFAULT_CONFIG_DIR, socketio=socketio
            )
        elif socketio and _job_manager.socketio is None:
            _job_manager.set_socketio(socketio)
        return _job_manager
