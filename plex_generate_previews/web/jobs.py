"""Job management for the web interface.

Provides JobManager class for tracking job state, emitting SocketIO events,
and persisting job data to disk.
"""

import json
import os
import threading
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Optional

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
    workers: list[WorkerStatus] = field(default_factory=list)
    outcome: dict[str, int] | None = None

    def to_dict(self) -> dict:
        """Serialize to dictionary."""
        result = asdict(self)
        result["workers"] = [w.to_dict() if isinstance(w, WorkerStatus) else w for w in self.workers]
        return result


@dataclass
class Job:
    """Represents a processing job."""

    id: str
    status: JobStatus = JobStatus.PENDING
    created_at: str = ""
    started_at: str | None = None
    completed_at: str | None = None
    library_id: str | None = None
    library_name: str = ""
    # Multi-server attribution: which configured server this job targets.
    # Optional / nullable for back-compat with jobs created before the
    # multi-server transition (those render as "All servers" in the UI).
    server_id: str | None = None
    server_name: str | None = None
    server_type: str | None = None  # plex / emby / jellyfin
    # Per-publisher outcomes from the multi-server dispatcher. Each entry:
    # {server_id, server_name, server_type, adapter_name, status, message,
    #  canonical_path}. Persists in jobs.json so historical jobs keep their
    # publisher breakdown across restarts. Empty list for legacy jobs.
    publishers: list[dict] = field(default_factory=list)
    progress: JobProgress = field(default_factory=JobProgress)
    error: str | None = None
    config: dict[str, Any] = field(default_factory=dict)
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
            "server_id": self.server_id,
            "server_name": self.server_name,
            "server_type": self.server_type,
            "publishers": list(self.publishers or []),
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
        self._job_file_results_dir = os.path.join(config_dir, "logs", "job_file_results")
        self.socketio = socketio
        self._jobs: dict[str, Job] = {}
        self._lock = threading.RLock()
        self._running_job_ids: set = set()
        self._on_progress_callbacks: list[Callable] = []

        # Job logs storage (in-memory for running jobs, plus file-backed under _job_logs_dir)
        self._job_logs: dict[str, deque] = {}
        self._max_log_lines = 500

        # Worker status tracking
        self._worker_statuses: dict[str, WorkerStatus] = {}

        # Cancellation flags
        self._cancellation_flags: dict[str, bool] = {}
        self._pause_flags: dict[str, bool] = {}
        self._pause_events: dict[str, threading.Event] = {}
        self._active_worker_pools: dict[str, Any] = {}

        # Background retention timer
        self._retention_timer: threading.Timer | None = None
        self._interrupted_jobs: list[Job] = []

        # Load existing jobs from disk
        self._load_jobs()
        try:
            os.makedirs(self._job_logs_dir, exist_ok=True)
            os.makedirs(self._job_file_results_dir, exist_ok=True)
            self._enforce_log_retention()
        except OSError as e:
            logger.warning(
                "Could not create the job-logs directory at {} ({}: {}). "
                "Jobs will still run, but per-job log lines won't be saved to disk — "
                "they'll only show in the live Job Detail page while the job is running. "
                "Check the config directory is writable (Docker: confirm volume mount permissions and PUID/PGID).",
                self._job_logs_dir,
                type(e).__name__,
                e,
            )
        self._start_retention_timer()

    def set_socketio(self, socketio) -> None:
        """Set the SocketIO instance for real-time updates."""
        self.socketio = socketio

    def _load_jobs(self) -> None:
        """Load jobs from persistent storage."""
        if os.path.exists(self.jobs_file):
            try:
                with open(self.jobs_file) as f:
                    data = json.load(f)
                    needs_save = False
                    # Allowed Job constructor kwargs — filter unknowns so a
                    # future field rename / removal in the dataclass doesn't
                    # mass-delete history. The "malformed" except clause below
                    # would otherwise silently drop every job in jobs.json.
                    _job_field_names = {f.name for f in fields(Job)}
                    for job_data in data.get("jobs", []):
                        try:
                            filtered_data = {k: v for k, v in (job_data or {}).items() if k in _job_field_names}
                            job = Job(**filtered_data)
                            # Mark any "running" jobs as failed on startup
                            # (they were interrupted by restart/crash)
                            if job.status == JobStatus.RUNNING:
                                logger.warning(
                                    "Job {} was still running when the app last shut down — marking it as failed. "
                                    "If auto-requeue-on-restart is enabled (Settings → Jobs), it will be revived shortly; "
                                    "otherwise re-run it from the Jobs page when you're ready.",
                                    job.id,
                                )
                                job.status = JobStatus.FAILED
                                job.error = "Job was interrupted by server restart"
                                job.completed_at = datetime.now(timezone.utc).isoformat()
                                needs_save = True
                                self._interrupted_jobs.append(job)
                            elif job.status == JobStatus.PENDING:
                                self._interrupted_jobs.append(job)
                            self._jobs[job.id] = job
                        except (TypeError, KeyError, ValueError) as job_error:
                            job_id = job_data.get("id", "unknown")
                            logger.warning(
                                "Skipping job {} from saved jobs file: its record is malformed ({}: {}). "
                                "Other jobs in the file will still load — most likely a leftover from an older "
                                "version. If you don't need to keep it, you can clear it from the Jobs page.",
                                job_id,
                                type(job_error).__name__,
                                job_error,
                            )
                            continue
                logger.info("Loaded {} jobs from {}", len(self._jobs), self.jobs_file)
                if self._interrupted_jobs:
                    logger.info("Found {} interrupted/pending job(s) from previous run", len(self._interrupted_jobs))
                if needs_save:
                    self._save_jobs()
            except (OSError, json.JSONDecodeError) as e:
                logger.warning(
                    "Could not read the saved jobs file at {} ({}: {}). "
                    "Starting with an empty jobs list — your job history won't appear, "
                    "but new jobs will still run and save normally.",
                    self.jobs_file,
                    type(e).__name__,
                    e,
                )
            except Exception as e:
                logger.error(
                    "Unexpected error loading jobs from {} ({}: {}). "
                    "Starting with an empty jobs list — please open an issue with the recent log lines "
                    "if you need the history recovered.",
                    self.jobs_file,
                    type(e).__name__,
                    e,
                )

    def _save_jobs(self) -> None:
        """Save jobs to persistent storage. Caller must hold _lock."""
        try:
            from ..utils import atomic_json_save

            jobs_data = {"jobs": [job.to_dict() for job in self._jobs.values()]}
            atomic_json_save(self.jobs_file, jobs_data)
        except OSError as e:
            logger.error(
                "Could not save the jobs file to {} ({}: {}). "
                "Job state will be lost when the app restarts — "
                "check that the config directory is writable (Docker: confirm volume mount permissions and PUID/PGID).",
                self.jobs_file,
                type(e).__name__,
                e,
            )

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
                logger.debug("SocketIO emit failed for {}", event, exc_info=True)

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
            logger.debug("Could not remove job log file {}: {}", path, e)

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
                self._delete_file_results(job_id)
                self._job_logs.pop(job_id, None)
                del self._jobs[job_id]
            self._save_jobs()
            logger.info("Retention: removed {} job(s) older than {} day(s)", len(expired_ids), days)

        # Clean orphaned job log files
        if os.path.isdir(self._job_logs_dir):
            for name in os.listdir(self._job_logs_dir):
                if not name.endswith(".log"):
                    continue
                job_id = name[:-4]
                if job_id not in self._jobs:
                    path = os.path.join(self._job_logs_dir, name)
                    try:
                        os.remove(path)
                        logger.debug("Removed orphaned job log: {}", path)
                    except OSError:
                        pass

        # Clean orphaned file-result JSONL files
        if os.path.isdir(self._job_file_results_dir):
            for name in os.listdir(self._job_file_results_dir):
                if not name.endswith(".jsonl"):
                    continue
                job_id = name[:-6]
                if job_id not in self._jobs:
                    path = os.path.join(self._job_file_results_dir, name)
                    try:
                        os.remove(path)
                        logger.debug("Removed orphaned file results: {}", path)
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
            logger.debug("Retention tick error: {}", e)
        self._start_retention_timer()

    def create_job(
        self,
        library_id: str | None = None,
        library_name: str = "",
        config: dict[str, Any] | None = None,
        priority: int = PRIORITY_NORMAL,
        server_id: str | None = None,
        server_name: str | None = None,
        server_type: str | None = None,
    ) -> Job:
        """Create a new job.

        Args:
            library_id: Library section ID (vendor-specific).
            library_name: Human-readable library name.
            config: Job configuration overrides.
            priority: Dispatch priority (1=high, 2=normal, 3=low).
            server_id: Configured media-server id this job targets (optional).
            server_name: Display name of the targeted server (optional).
            server_type: Vendor type — "plex" / "emby" / "jellyfin".
        """
        with self._lock:
            job = Job(
                id=str(uuid.uuid4()),
                library_id=library_id,
                library_name=library_name,
                server_id=server_id,
                server_name=server_name,
                server_type=server_type,
                config=config or {},
                priority=parse_priority(priority),
            )
            self._jobs[job.id] = job
            self._save_jobs()
            self._emit_event("job_created", job.to_dict())
        logger.info(
            "Created job {} for library {} (server={})",
            job.id,
            library_name or "(all)",
            server_name or server_id or "(all)",
        )
        return job

    def requeue_interrupted_jobs(self, max_age_minutes: int = 720) -> list[Job]:
        """Revive jobs that were interrupted by the last restart.

        Each interrupted job is restored to ``PENDING`` in place (same
        job ID, same ``created_at``) so it can be restarted.  No new
        jobs are created.  The library will be re-scanned on start and
        items that already have previews are skipped automatically.

        Args:
            max_age_minutes: Only revive jobs whose last activity
                (``started_at``, falling back to ``created_at``) is
                within this many minutes of the current time.  Older
                jobs are considered stale and left as-is.
                Range: 5 – 1440 (1 day).

        Returns:
            List of revived ``Job`` objects ready to be started.

        """
        if not self._interrupted_jobs:
            return []

        max_age_minutes = max(5, min(1440, max_age_minutes))
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=max_age_minutes)
        revived: list[Job] = []

        for job in self._interrupted_jobs:
            # Check age — skip stale jobs.  Use started_at (when
            # available) so long-running jobs aren't wrongly considered
            # "too old" based on their creation timestamp.
            try:
                ref_str = job.started_at or job.created_at
                ref_time = datetime.fromisoformat(ref_str.replace("Z", "+00:00"))
                if ref_time.tzinfo is None:
                    ref_time = ref_time.replace(tzinfo=timezone.utc)
                if ref_time < cutoff:
                    logger.debug("Skipping revive of job {} — too old (ref={})", job.id[:8], ref_str)
                    continue
            except (ValueError, AttributeError):
                # Can't parse date — skip to be safe
                continue

            # Revive the job in place — same ID, same created_at
            with self._lock:
                job.status = JobStatus.PENDING
                job.error = None
                job.completed_at = None
                job.paused = False
                job.progress = JobProgress()

            self.add_log(
                job.id,
                "INFO - Job revived after server restart; processing will resume.",
            )
            revived.append(job)
            logger.info("Revived interrupted job {} ({})", job.id[:8], job.library_name)

        if revived:
            with self._lock:
                self._save_jobs()

        # Clear the list so it's not processed again
        self._interrupted_jobs = []
        return revived

    def get_job(self, job_id: str) -> Job | None:
        """Get a job by ID."""
        return self._jobs.get(job_id)

    def get_all_jobs(self) -> list[Job]:
        """Get all jobs."""
        return list(self._jobs.values())

    def get_pending_jobs(self) -> list[Job]:
        """Get all pending jobs."""
        return [j for j in self._jobs.values() if j.status == JobStatus.PENDING]

    def get_running_job(self) -> Job | None:
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

    def get_running_jobs(self) -> list[Job]:
        """Get all currently running jobs."""
        with self._lock:
            return [
                self._jobs[jid]
                for jid in self._running_job_ids
                if jid in self._jobs and self._jobs[jid].status == JobStatus.RUNNING
            ]

    def update_job_config(self, job_id: str, config: dict[str, Any]) -> None:
        """Update stored config for a job (e.g. when start is deferred due to pause)."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job:
                job.config = dict(config)
                self._save_jobs()

    def update_job_priority(self, job_id: str, priority: int) -> Job | None:
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

    def start_job(self, job_id: str) -> Job | None:
        """Mark a job as started.

        Idempotent: if the job is already running, this is a no-op.  The
        existing ``started_at`` timestamp is preserved so wall-clock
        duration reflects when the worker thread began, not when items
        were first dispatched.
        """
        started = False
        with self._lock:
            job = self._jobs.get(job_id)
            if job and job.status != JobStatus.RUNNING:
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
            logger.info("Started job {}", job_id)
        return job

    def append_publishers(self, job_id: str, publisher_rows: list[dict]) -> None:
        """Append per-publisher outcome rows to a job (Phase H5).

        Called by the multi-server dispatcher after each ``process_canonical_path``
        run so the Jobs UI can show "this file published to Plex (ok), Emby
        (failed)" without users having to grep the log stream.
        """
        if not publisher_rows:
            return
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            existing = list(job.publishers or [])
            existing.extend(publisher_rows)
            job.publishers = existing
            self._save_jobs()
            self._emit_event("job_updated", job.to_dict())

    def update_progress(
        self,
        job_id: str,
        percent: float = None,
        current_item: str = None,
        total_items: int = None,
        processed_items: int = None,
        speed: str = None,
        current_file: str = None,
    ) -> Job | None:
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

    def set_job_outcome(self, job_id: str, outcome: dict[str, int]) -> Optional["Job"]:
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
        error: str | None = None,
        warning: str | None = None,
    ) -> Job | None:
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
                    log_msg = f"Job {job_id} already cancelled; skipping completion update"
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

    def cancel_job(self, job_id: str) -> Job | None:
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
            logger.info("Cancelled job {}", job_id)
        return job

    def delete_job(self, job_id: str) -> bool:
        """Delete a job."""
        deleted = False
        with self._lock:
            if job_id in self._jobs:
                if job_id in self._running_job_ids:
                    return False  # Can't delete running job
                self._delete_job_log_file(job_id)
                self._delete_file_results(job_id)
                if job_id in self._job_logs:
                    del self._job_logs[job_id]
                del self._jobs[job_id]
                self._save_jobs()
                self._emit_event("job_deleted", {"job_id": job_id})
                deleted = True
        if deleted:
            logger.info("Deleted job {}", job_id)
        return deleted

    def clear_completed_jobs(self, statuses: list[str] | None = None) -> int:
        """Clear jobs by status.

        Args:
            statuses: List of status strings to clear (e.g. ["completed", "failed"]).
                Defaults to all terminal statuses: completed, failed, cancelled.

        """
        valid_terminal = {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}
        if statuses:
            target = {JobStatus(s) for s in statuses if s in {e.value for e in valid_terminal}}
        else:
            target = valid_terminal

        with self._lock:
            to_delete = [job_id for job_id, job in self._jobs.items() if job.status in target]
            for job_id in to_delete:
                self._delete_job_log_file(job_id)
                self._delete_file_results(job_id)
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
                logger.debug("Could not append to job log {}: {}", log_path, e)

    def get_logs(self, job_id: str, last_n: int = None) -> list[str]:
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
                    with open(log_path) as f:
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

    def get_logs_paginated(self, job_id: str, offset: int = 0, limit: int | None = None) -> dict[str, Any]:
        """Get a slice of log lines with total count for pagination.

        Args:
            job_id: The job identifier.
            offset: 0-based line index to start from.
            limit: Maximum number of lines to return (None = all from offset).

        Returns:
            Dict with keys: lines, total_lines, offset.
        """
        with self._lock:
            if job_id in self._job_logs:
                logs = list(self._job_logs[job_id])
                total = len(logs)
                sliced = logs[offset : offset + limit] if limit is not None else logs[offset:]
                return {"lines": sliced, "total_lines": total, "offset": offset}

            log_path = os.path.join(self._job_logs_dir, f"{job_id}.log")
            if os.path.isfile(log_path):
                try:
                    with open(log_path) as f:
                        all_lines = [line.rstrip("\n") for line in f if line]
                    total = len(all_lines)
                    end = offset + limit if limit is not None else total
                    sliced = all_lines[offset:end]
                    return {"lines": sliced, "total_lines": total, "offset": offset}
                except OSError:
                    pass

            job = self._jobs.get(job_id)
            if job and job.status in (
                JobStatus.COMPLETED,
                JobStatus.FAILED,
                JobStatus.CANCELLED,
            ):
                return {
                    "lines": [LOG_RETENTION_CLEARED_MESSAGE],
                    "total_lines": 1,
                    "offset": 0,
                }
            return {"lines": [], "total_lines": 0, "offset": 0}

    def clear_logs(self, job_id: str) -> None:
        """Clear logs for a job (memory and file)."""
        with self._lock:
            self._delete_job_log_file(job_id)
            if job_id in self._job_logs:
                del self._job_logs[job_id]

    # ========================================================================
    # Per-File Result Tracking
    # ========================================================================

    def _file_results_path(self, job_id: str) -> str:
        """Return the JSONL file path for per-file results of a job."""
        return os.path.join(self._job_file_results_dir, f"{job_id}.jsonl")

    def record_file_result(
        self,
        job_id: str,
        file_path: str,
        outcome: str,
        reason: str = "",
        worker: str = "",
    ) -> None:
        """Append a per-file processing result to the job's JSONL file.

        Args:
            job_id: Job identifier.
            file_path: Absolute path of the media file processed.
            outcome: ProcessingResult value string (e.g. "generated", "failed").
            reason: Human-readable detail (skip/failure reason).
            worker: Worker display name (e.g. "GPU Worker 1 (NVIDIA TITAN RTX)").
        """
        record = {
            "file": file_path,
            "outcome": outcome,
            "reason": reason,
            "worker": worker,
            "ts": datetime.now(timezone.utc).strftime("%H:%M:%S"),
        }
        path = self._file_results_path(job_id)
        try:
            with open(path, "a") as f:
                f.write(json.dumps(record, separators=(",", ":")) + "\n")
        except OSError as e:
            logger.debug("Could not append file result to {}: {}", path, e)

    def get_file_results(
        self,
        job_id: str,
        outcome_filter: str = "",
        search: str = "",
    ) -> list[dict]:
        """Read per-file results for a job from its JSONL file.

        Args:
            job_id: Job identifier.
            outcome_filter: If set, only return records matching this outcome.
            search: If set, only return records whose file path contains this substring (case-insensitive).

        Returns:
            List of result dicts, each with keys: file, outcome, reason, worker, ts.
        """
        path = self._file_results_path(job_id)
        if not os.path.isfile(path):
            return []
        results: list[dict] = []
        search_lower = search.lower() if search else ""
        try:
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if outcome_filter and record.get("outcome") != outcome_filter:
                        continue
                    if search_lower and search_lower not in record.get("file", "").lower():
                        continue
                    results.append(record)
        except OSError as e:
            logger.debug("Could not read file results from {}: {}", path, e)
        return results

    def _delete_file_results(self, job_id: str) -> None:
        """Remove the per-file results JSONL file for a job. Caller must hold _lock."""
        path = self._file_results_path(job_id)
        try:
            if os.path.isfile(path):
                os.remove(path)
        except OSError as e:
            logger.debug("Could not remove file results {}: {}", path, e)

    # ========================================================================
    # Worker Status Management
    # ========================================================================

    def update_worker_status(self, worker_key: str, status: WorkerStatus) -> None:
        """Update status for a worker."""
        with self._lock:
            self._worker_statuses[worker_key] = status

    def get_worker_statuses(self) -> list[WorkerStatus]:
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
            logger.info("Pause audit: job_id={}, status={}, paused=True", job_id, status_val)
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
            logger.info("Resume audit: job_id={}, status={}, paused=False", job_id, status_val)
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

    def get_active_worker_pool(self, job_id: str = "") -> Any | None:
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
_job_manager: JobManager | None = None
_job_lock = threading.Lock()

# Default config directory from environment
DEFAULT_CONFIG_DIR = os.environ.get("CONFIG_DIR", "/config")


def get_job_manager(config_dir: str | None = None, socketio=None) -> JobManager:
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
            _job_manager = JobManager(config_dir=config_dir or DEFAULT_CONFIG_DIR, socketio=socketio)
        else:
            if config_dir and _job_manager.config_dir != config_dir:
                _job_manager = JobManager(config_dir=config_dir, socketio=socketio)
            elif socketio and _job_manager.socketio is None:
                _job_manager.set_socketio(socketio)
        return _job_manager
