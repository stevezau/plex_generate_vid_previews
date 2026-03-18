"""Job management for the web interface.

Provides JobManager class for tracking job state, emitting SocketIO events,
and persisting job data to disk.
"""

import json
import os
import threading
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

from loguru import logger

# Message shown in UI when a job's log file was removed by retention policy.
LOG_RETENTION_CLEARED_MESSAGE = "Log file was cleared due to log retention policy."

PRIORITY_HIGH = 1
PRIORITY_NORMAL = 2
PRIORITY_LOW = 3

PRIORITY_LABELS = {
    PRIORITY_HIGH: "high",
    PRIORITY_NORMAL: "normal",
    PRIORITY_LOW: "low",
}
PRIORITY_FROM_LABEL = {v: k for k, v in PRIORITY_LABELS.items()}


def parse_priority(value) -> int:
    """Parse a priority value from int or string label.

    Args:
        value: Integer (1-3) or string ("high", "normal", "low").

    Returns:
        Priority integer, defaulting to PRIORITY_NORMAL for invalid input.
    """
    if isinstance(value, int) and value in PRIORITY_LABELS:
        return value
    if isinstance(value, str):
        return PRIORITY_FROM_LABEL.get(value.lower(), PRIORITY_NORMAL)
    return PRIORITY_NORMAL


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
    library_name: str = ""
    progress_percent: float = 0.0
    speed: str = "0.0x"
    eta: str = ""

    def to_dict(self) -> dict:
        """Serialize to dictionary."""
        return asdict(self)


@dataclass
class JobProgress:
    """Progress information for a job."""

    percent: float = 0.0
    current_item: str = ""
    total_items: int = 0
    processed_items: int = 0
    speed: str = "0.0x"
    current_file: str = ""
    workers: List[WorkerStatus] = field(default_factory=list)
    outcome: Optional[Dict[str, int]] = None

    def to_dict(self) -> dict:
        """Serialize to dictionary."""
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
    paused: bool = False
    priority: int = PRIORITY_NORMAL

    def __post_init__(self):
        if not self.created_at:
            self.created_at = datetime.now(timezone.utc).isoformat()
        if isinstance(self.progress, dict):
            # Strip legacy 'eta' so persisted jobs.json loads without error
            data = dict(self.progress)
            data.pop("eta", None)
            self.progress = JobProgress(**data)
        if isinstance(self.status, str):
            self.status = JobStatus(self.status)
        self.priority = parse_priority(self.priority)

    def to_dict(self) -> dict:
        """Serialize to dictionary."""
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
            "paused": self.paused,
            "priority": self.priority,
        }


class JobManager:
    """Manages job queue and state for the web interface.

    Provides methods for creating, updating, and querying jobs,
    as well as emitting SocketIO events for real-time updates.
    """

    def __init__(self, config_dir: str = "/config", socketio=None):
        """Initialize job manager with config directory and optional SocketIO instance."""
        self.config_dir = config_dir
        self.jobs_file = os.path.join(config_dir, "jobs.json")
        self._job_logs_dir = os.path.join(config_dir, "logs", "jobs")
        self.socketio = socketio
        self._jobs: Dict[str, Job] = {}
        self._lock = threading.RLock()
        self._running_job_ids: set = set()
        self._on_progress_callbacks: List[Callable] = []

        # Job logs storage (in-memory for running jobs, plus file-backed under _job_logs_dir)
        self._job_logs: Dict[str, deque] = {}
        self._max_log_lines = 500

        # Worker status tracking
        self._worker_statuses: Dict[str, WorkerStatus] = {}

        # Cancellation flags
        self._cancellation_flags: Dict[str, bool] = {}
        self._pause_flags: Dict[str, bool] = {}
        self._pause_events: Dict[str, threading.Event] = {}
        self._active_worker_pools: Dict[str, Any] = {}

        # Background retention timer
        self._retention_timer: Optional[threading.Timer] = None
        self._interrupted_jobs: List[Job] = []

        # Load existing jobs from disk
        self._load_jobs()
        try:
            os.makedirs(self._job_logs_dir, exist_ok=True)
            self._enforce_log_retention()
        except OSError as e:
            logger.warning(
                f"Could not create job logs directory {self._job_logs_dir}: {e}"
            )
        self._start_retention_timer()

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
                                job.completed_at = datetime.now(
                                    timezone.utc
                                ).isoformat()
                                needs_save = True
                                self._interrupted_jobs.append(job)
                            elif job.status == JobStatus.PENDING:
                                self._interrupted_jobs.append(job)
                            self._jobs[job.id] = job
                        except (TypeError, KeyError, ValueError) as job_error:
                            job_id = job_data.get("id", "unknown")
                            logger.warning(f"Failed to load job {job_id}: {job_error}")
                            continue
                logger.info(f"Loaded {len(self._jobs)} jobs from {self.jobs_file}")
                if self._interrupted_jobs:
                    logger.info(
                        f"Found {len(self._interrupted_jobs)} interrupted/pending job(s) "
                        f"from previous run"
                    )
                if needs_save:
                    self._save_jobs()
            except (json.JSONDecodeError, IOError) as e:
                logger.warning(f"Failed to load jobs file: {e}")
            except Exception as e:
                logger.error(f"Unexpected error loading jobs: {e}")

    def _save_jobs(self) -> None:
        """Save jobs to persistent storage. Caller must hold _lock."""
        try:
            from ..utils import atomic_json_save

            jobs_data = {"jobs": [job.to_dict() for job in self._jobs.values()]}
            atomic_json_save(self.jobs_file, jobs_data)
        except IOError as e:
            logger.error(f"Failed to save jobs: {e}")

    def _emit_event(self, event: str, data: dict) -> None:
        """Emit a SocketIO event without blocking the caller.

        Runs the emit in a separate green thread so the processing
        loop never pauses while data is being sent to clients.
        """
        if not self.socketio:
            return

        def _do_emit():
            try:
                self.socketio.emit(event, data, namespace="/jobs")
            except Exception:
                logger.debug(f"SocketIO emit failed for {event}", exc_info=True)

        t = threading.Thread(target=_do_emit, daemon=True)
        t.start()

    def emit_processing_paused_changed(self, paused: bool) -> None:
        """Emit event when global processing pause state changes."""
        self._emit_event("processing_paused_changed", {"paused": paused})

    def _delete_job_log_file(self, job_id: str) -> None:
        """Remove the persisted log file for a job if it exists. Caller must hold _lock."""
        path = os.path.join(self._job_logs_dir, f"{job_id}.log")
        try:
            if os.path.isfile(path):
                os.remove(path)
        except OSError as e:
            logger.debug(f"Could not remove job log file {path}: {e}")

    def _get_job_history_days(self) -> int:
        """Read job_history_days from settings, defaulting to 30."""
        try:
            from .settings_manager import get_settings_manager

            sm = get_settings_manager()
            return int(sm.get("job_history_days", 30))
        except Exception:
            logger.debug("Could not read job_history_days from settings", exc_info=True)
            return 30

    def _enforce_log_retention(self) -> None:
        """Delete terminal jobs and their log files older than job_history_days.

        Also removes orphaned log files (no matching job entry).
        Caller must hold _lock.
        """
        days = self._get_job_history_days()
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        cutoff_iso = cutoff.isoformat()

        expired_ids = []
        for job_id, job in self._jobs.items():
            if job.status not in (
                JobStatus.COMPLETED,
                JobStatus.FAILED,
                JobStatus.CANCELLED,
            ):
                continue
            ref_time = job.completed_at or job.created_at
            if ref_time and ref_time < cutoff_iso:
                expired_ids.append(job_id)

        if expired_ids:
            for job_id in expired_ids:
                self._delete_job_log_file(job_id)
                self._job_logs.pop(job_id, None)
                del self._jobs[job_id]
            self._save_jobs()
            logger.info(
                f"Retention: removed {len(expired_ids)} job(s) older than {days} day(s)"
            )

        if os.path.isdir(self._job_logs_dir):
            for name in os.listdir(self._job_logs_dir):
                if not name.endswith(".log"):
                    continue
                job_id = name[:-4]
                if job_id not in self._jobs:
                    path = os.path.join(self._job_logs_dir, name)
                    try:
                        os.remove(path)
                        logger.debug(f"Removed orphaned job log: {path}")
                    except OSError:
                        pass

    _RETENTION_INTERVAL_SEC = 3600  # 1 hour

    def _start_retention_timer(self) -> None:
        """Start (or restart) the background hourly retention timer."""
        self._stop_retention_timer()
        timer = threading.Timer(self._RETENTION_INTERVAL_SEC, self._retention_tick)
        timer.daemon = True
        timer.start()
        self._retention_timer = timer

    def _stop_retention_timer(self) -> None:
        """Cancel the background retention timer if running."""
        if self._retention_timer is not None:
            self._retention_timer.cancel()
            self._retention_timer = None

    def _retention_tick(self) -> None:
        """Periodic callback: run retention then schedule the next tick."""
        try:
            with self._lock:
                self._enforce_log_retention()
        except Exception as e:
            logger.debug(f"Retention tick error: {e}")
        self._start_retention_timer()

    def create_job(
        self,
        library_id: Optional[str] = None,
        library_name: str = "",
        config: Optional[Dict[str, Any]] = None,
        priority: int = PRIORITY_NORMAL,
    ) -> Job:
        """Create a new job.

        Args:
            library_id: Plex library section ID.
            library_name: Human-readable library name.
            config: Job configuration overrides.
            priority: Dispatch priority (1=high, 2=normal, 3=low).
        """
        with self._lock:
            job = Job(
                id=str(uuid.uuid4()),
                library_id=library_id,
                library_name=library_name,
                config=config or {},
                priority=parse_priority(priority),
            )
            self._jobs[job.id] = job
            self._save_jobs()
            self._emit_event("job_created", job.to_dict())
        logger.info(f"Created job {job.id} for library {library_name}")
        return job

    def requeue_interrupted_jobs(self, max_age_minutes: int = 60) -> List[Job]:
        """Create new jobs for any that were interrupted by the last restart.

        For each interrupted job (was running or pending when the server
        stopped), a **new** job is created with the same configuration.
        The original job is left in history for auditability.

        Pending jobs that are superseded are cancelled so they don't run
        alongside the new clone.

        Args:
            max_age_minutes: Only requeue jobs created within this many
                minutes of the current time.  Older jobs are considered
                stale and skipped.  Range: 5 – 1440 (1 day).

        Returns:
            List of newly created ``Job`` objects ready to be started.

        """
        if not self._interrupted_jobs:
            return []

        max_age_minutes = max(5, min(1440, max_age_minutes))
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=max_age_minutes)
        requeued: List[Job] = []

        for orig in self._interrupted_jobs:
            if (orig.config or {}).get("requeued_from"):
                logger.debug(
                    f"Skipping requeue of job {orig.id[:8]} - already requeued"
                )
                continue

            # Check age — skip stale jobs
            try:
                created = datetime.fromisoformat(orig.created_at.replace("Z", "+00:00"))
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                if created < cutoff:
                    logger.debug(
                        f"Skipping requeue of job {orig.id[:8]} — "
                        f"too old ({orig.created_at})"
                    )
                    continue
            except (ValueError, AttributeError):
                # Can't parse date — skip to be safe
                continue

            # Clone config, stripping retry metadata so it starts fresh
            new_config = dict(orig.config or {})
            for key in (
                "is_retry",
                "retry_delay",
                "retry_attempt",
                "max_retries",
                "parent_job_id",
            ):
                new_config.pop(key, None)
            new_config["requeued_from"] = orig.id

            # Cancel stale pending jobs so they don't also run
            if orig.status == JobStatus.PENDING:
                with self._lock:
                    orig.status = JobStatus.CANCELLED
                    orig.error = "Superseded by auto-requeue after restart"
                    orig.completed_at = datetime.now(timezone.utc).isoformat()

            # Create the new job, preserving original priority
            new_job = self.create_job(
                library_id=orig.library_id,
                library_name=orig.library_name,
                config=new_config,
                priority=orig.priority,
            )
            self.add_log(
                new_job.id,
                f"INFO - Auto-requeued: original job {orig.id[:8]} was "
                f"interrupted by server restart",
            )
            requeued.append(new_job)
            logger.info(
                f"Requeued job {orig.id[:8]} as {new_job.id[:8]} ({orig.library_name})"
            )

        # Persist the cancelled pending jobs
        if requeued:
            with self._lock:
                self._save_jobs()

        # Clear the list so it's not processed again
        self._interrupted_jobs = []
        return requeued

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
        """Get a currently running job (first found).

        For backward compatibility. Prefer ``get_running_jobs`` when
        multiple jobs may run concurrently.
        """
        with self._lock:
            for jid in list(self._running_job_ids):
                job = self._jobs.get(jid)
                if job and job.status == JobStatus.RUNNING:
                    return job
        return None

    def get_running_jobs(self) -> List[Job]:
        """Get all currently running jobs."""
        with self._lock:
            return [
                self._jobs[jid]
                for jid in self._running_job_ids
                if jid in self._jobs and self._jobs[jid].status == JobStatus.RUNNING
            ]

    def update_job_config(self, job_id: str, config: Dict[str, Any]) -> None:
        """Update stored config for a job (e.g. when start is deferred due to pause)."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job:
                job.config = dict(config)
                self._save_jobs()

    def update_job_priority(self, job_id: str, priority: int) -> Optional[Job]:
        """Update the dispatch priority of a job.

        Args:
            job_id: Job identifier.
            priority: New priority value (1=high, 2=normal, 3=low).

        Returns:
            The updated Job, or None if not found.
        """
        priority = parse_priority(priority)
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return None
            job.priority = priority
            self._save_jobs()
            self._emit_event("job_updated", job.to_dict())
        return job

    def start_job(self, job_id: str) -> Optional[Job]:
        """Mark a job as started."""
        started = False
        with self._lock:
            job = self._jobs.get(job_id)
            if job:
                job.status = JobStatus.RUNNING
                job.paused = False
                job.started_at = datetime.now(timezone.utc).isoformat()
                self._running_job_ids.add(job_id)
                self._pause_flags[job_id] = False
                self._pause_events[job_id] = threading.Event()
                self._pause_events[job_id].set()
                self._save_jobs()
                self._emit_event("job_started", job.to_dict())
                started = True
        if started:
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
                if current_file is not None:
                    job.progress.current_file = current_file

                # Emit progress event (don't save to disk on every update)
                self._emit_event(
                    "job_progress",
                    {"job_id": job_id, "progress": job.progress.to_dict()},
                )
            return job

    def set_job_outcome(self, job_id: str, outcome: Dict[str, int]) -> Optional["Job"]:
        """Store the processing outcome breakdown on a job.

        Args:
            job_id: Job identifier.
            outcome: Dict mapping ProcessingResult values to counts.

        """
        with self._lock:
            job = self._jobs.get(job_id)
            if job:
                job.progress.outcome = outcome
            return job

    def complete_job(
        self,
        job_id: str,
        error: Optional[str] = None,
        warning: Optional[str] = None,
    ) -> Optional[Job]:
        """Mark a job as completed, completed-with-warning, or failed.

        Args:
            job_id: Job identifier.
            error: If set, marks job as FAILED (red badge).
            warning: If set (and error is not), marks job as COMPLETED with a
                     warning message (amber badge in UI). The warning text is
                     stored in `job.error` so the UI can display it.

        """
        log_msg = None
        log_level = "info"
        with self._lock:
            job = self._jobs.get(job_id)
            if job:
                if job.status == JobStatus.CANCELLED:
                    self.clear_pause_flag(job_id)
                    self.clear_cancellation_flag(job_id)
                    self.clear_active_worker_pool(job_id)
                    self._save_jobs()
                    log_msg = (
                        f"Job {job_id} already cancelled; skipping completion update"
                    )
                    # Early return after logging outside the lock
                else:
                    job.completed_at = datetime.now(timezone.utc).isoformat()
                    if error:
                        job.status = JobStatus.FAILED
                        job.error = error
                        job.paused = False
                        self._emit_event("job_failed", job.to_dict())
                        log_msg = f"Job {job_id} failed: {error}"
                        log_level = "error"
                    elif warning:
                        job.status = JobStatus.COMPLETED
                        job.error = warning
                        job.paused = False
                        job.progress.percent = 100.0
                        self._emit_event("job_completed", job.to_dict())
                        log_msg = f"Job {job_id} completed with warnings: {warning}"
                    else:
                        job.status = JobStatus.COMPLETED
                        job.paused = False
                        job.progress.percent = 100.0
                        self._emit_event("job_completed", job.to_dict())
                        log_msg = f"Job {job_id} completed successfully"

                    self._running_job_ids.discard(job_id)
                    self.clear_pause_flag(job_id)
                    self.clear_cancellation_flag(job_id)
                    self.clear_active_worker_pool(job_id)
                    self._save_jobs()

        if log_msg:
            getattr(logger, log_level)(log_msg)
        return job

    def cancel_job(self, job_id: str) -> Optional[Job]:
        """Cancel a job."""
        cancelled = False
        with self._lock:
            job = self._jobs.get(job_id)
            if job and job.status in (JobStatus.PENDING, JobStatus.RUNNING):
                was_running = job.status == JobStatus.RUNNING
                job.status = JobStatus.CANCELLED
                job.paused = False
                job.completed_at = datetime.now(timezone.utc).isoformat()
                self._running_job_ids.discard(job_id)
                self.clear_pause_flag(job_id)
                if not was_running:
                    self.clear_cancellation_flag(job_id)
                self.clear_active_worker_pool(job_id)
                self._save_jobs()
                self._emit_event("job_cancelled", job.to_dict())
                cancelled = True
        if cancelled:
            logger.info(f"Cancelled job {job_id}")
        return job

    def delete_job(self, job_id: str) -> bool:
        """Delete a job."""
        deleted = False
        with self._lock:
            if job_id in self._jobs:
                if job_id in self._running_job_ids:
                    return False  # Can't delete running job
                self._delete_job_log_file(job_id)
                if job_id in self._job_logs:
                    del self._job_logs[job_id]
                del self._jobs[job_id]
                self._save_jobs()
                self._emit_event("job_deleted", {"job_id": job_id})
                deleted = True
        if deleted:
            logger.info(f"Deleted job {job_id}")
        return deleted

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
                self._delete_job_log_file(job_id)
                self._job_logs.pop(job_id, None)
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
        """Add a log message for a job (in-memory and append to file)."""
        with self._lock:
            if job_id not in self._job_logs:
                self._job_logs[job_id] = deque(maxlen=self._max_log_lines)
            timestamp = datetime.utcnow().strftime("%H:%M:%S")
            line = f"[{timestamp}] {message}"
            self._job_logs[job_id].append(line)
            log_path = os.path.join(self._job_logs_dir, f"{job_id}.log")
            try:
                with open(log_path, "a") as f:
                    f.write(line + "\n")
            except OSError as e:
                logger.debug(f"Could not append to job log {log_path}: {e}")

    def get_logs(self, job_id: str, last_n: int = None) -> List[str]:
        """Get logs for a job (from memory if present, else from file; retention message if cleared)."""
        with self._lock:
            if job_id in self._job_logs:
                logs = list(self._job_logs[job_id])
                if last_n:
                    return logs[-last_n:]
                return logs
            log_path = os.path.join(self._job_logs_dir, f"{job_id}.log")
            if os.path.isfile(log_path):
                try:
                    with open(log_path, "r") as f:
                        logs = [line.rstrip("\n") for line in f if line]
                    if last_n:
                        return logs[-last_n:]
                    return logs
                except OSError:
                    pass
            job = self._jobs.get(job_id)
            if job and job.status in (
                JobStatus.COMPLETED,
                JobStatus.FAILED,
                JobStatus.CANCELLED,
            ):
                return [LOG_RETENTION_CLEARED_MESSAGE]
            return []

    def clear_logs(self, job_id: str) -> None:
        """Clear logs for a job (memory and file)."""
        with self._lock:
            self._delete_job_log_file(job_id)
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

    def prune_worker_statuses(self, valid_keys: set[str]) -> None:
        """Remove stale worker statuses not present in the latest snapshot."""
        with self._lock:
            for key in list(self._worker_statuses.keys()):
                if key not in valid_keys:
                    del self._worker_statuses[key]

    def emit_worker_statuses(self) -> None:
        """Emit current worker statuses to connected clients via SocketIO."""
        workers = self.get_worker_statuses()
        self._emit_event(
            "worker_update",
            {"workers": [w.to_dict() for w in workers]},
        )

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

    # ========================================================================
    # Pause / Resume Management
    # ========================================================================

    def request_pause(self, job_id: str) -> bool:
        """Request pause for a running job."""
        paused = False
        status_val = ""
        with self._lock:
            job = self._jobs.get(job_id)
            if not job or job.status != JobStatus.RUNNING:
                return False
            self._pause_flags[job_id] = True
            event = self._pause_events.get(job_id)
            if event is None:
                event = threading.Event()
                self._pause_events[job_id] = event
            event.clear()
            job.paused = True
            status_val = job.status.value
            self._save_jobs()
            self._emit_event("job_paused", {"job_id": job_id, "paused": True})
            paused = True
        if paused:
            logger.info(
                f"Pause audit: job_id={job_id}, status={status_val}, paused=True"
            )
            self.add_log(
                job_id,
                "INFO - Pause requested; no new tasks will be dispatched until resume.",
            )
        return paused

    def request_resume(self, job_id: str) -> bool:
        """Request resume for a paused job."""
        resumed = False
        status_val = ""
        with self._lock:
            job = self._jobs.get(job_id)
            if not job or job.status != JobStatus.RUNNING:
                return False
            self._pause_flags[job_id] = False
            event = self._pause_events.get(job_id)
            if event is None:
                event = threading.Event()
                self._pause_events[job_id] = event
            event.set()
            job.paused = False
            status_val = job.status.value
            self._save_jobs()
            self._emit_event("job_resumed", {"job_id": job_id, "paused": False})
            resumed = True
        if resumed:
            logger.info(
                f"Resume audit: job_id={job_id}, status={status_val}, paused=False"
            )
            self.add_log(job_id, "INFO - Resume requested; dispatch will continue.")
        return resumed

    def is_pause_requested(self, job_id: str) -> bool:
        """Check if pause has been requested for a job."""
        with self._lock:
            return self._pause_flags.get(job_id, False)

    def clear_pause_flag(self, job_id: str) -> None:
        """Clear pause state for a job."""
        with self._lock:
            self._pause_flags.pop(job_id, None)
            self._pause_events.pop(job_id, None)
            job = self._jobs.get(job_id)
            if job:
                job.paused = False

    # ========================================================================
    # Active Worker Pool Management
    # ========================================================================

    def set_active_worker_pool(self, job_id: str, worker_pool: Any) -> None:
        """Store the active worker pool for a running job."""
        with self._lock:
            self._active_worker_pools[job_id] = worker_pool

    def get_active_worker_pool(self, job_id: str = "") -> Optional[Any]:
        """Get active worker pool for a job, or any running pool.

        Args:
            job_id: Specific job ID, or empty string to return any active pool.

        """
        with self._lock:
            if job_id:
                return self._active_worker_pools.get(job_id)
            # Return the first available pool (shared pool model)
            for pool in self._active_worker_pools.values():
                if pool is not None:
                    return pool
            return None

    def clear_active_worker_pool(self, job_id: str) -> None:
        """Clear active worker pool reference for a job."""
        with self._lock:
            self._active_worker_pools.pop(job_id, None)


# Global job manager instance
_job_manager: Optional[JobManager] = None
_job_lock = threading.Lock()

# Default config directory from environment
DEFAULT_CONFIG_DIR = os.environ.get("CONFIG_DIR", "/config")


def get_job_manager(config_dir: Optional[str] = None, socketio=None) -> JobManager:
    """Get or create the global JobManager instance (thread-safe).

    When ``config_dir`` is explicitly provided and differs from the
    current singleton's directory, the singleton is recreated so that
    all derived paths (jobs file, log directory) stay consistent.
    In production this never happens; it guards against race conditions
    in tests where background threads can recreate the singleton with
    the module-level default between fixture resets.

    Args:
        config_dir: Configuration directory path, or ``None`` to use
            the existing singleton / module default.
        socketio: Optional SocketIO instance for real-time events.

    Returns:
        The global ``JobManager`` singleton.

    """
    global _job_manager
    with _job_lock:
        if _job_manager is None:
            _job_manager = JobManager(
                config_dir=config_dir or DEFAULT_CONFIG_DIR, socketio=socketio
            )
        else:
            if config_dir and _job_manager.config_dir != config_dir:
                _job_manager = JobManager(config_dir=config_dir, socketio=socketio)
            elif socketio and _job_manager.socketio is None:
                _job_manager.set_socketio(socketio)
        return _job_manager
