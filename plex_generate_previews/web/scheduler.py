"""
Scheduling system for the web interface.

Uses APScheduler with SQLite storage for persistent scheduled jobs.
"""

import json
import os
import threading
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional, Callable

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.events import EVENT_JOB_EXECUTED, EVENT_JOB_ERROR, EVENT_JOB_MISSED
from loguru import logger


# Module-level function for APScheduler to call
# Must be at module level to be picklable
def execute_scheduled_job(
    schedule_id: str,
    library_id: Optional[str] = None,
    library_name: str = "",
    config: Optional[dict] = None,
) -> None:
    """
    Execute a scheduled job - module-level function for APScheduler pickling.

    This function must be at module level (not a class method) because
    APScheduler's SQLAlchemy jobstore needs to pickle it.

    Args:
        schedule_id: The ID of the schedule triggering this job
        library_id: Plex library section ID
        library_name: Human-readable library name
        config: Job configuration dict
    """
    logger.info(f"Executing scheduled job: {schedule_id} for library: {library_name}")

    # Get the schedule manager singleton to access the callback
    manager = get_schedule_manager()

    if manager.run_job_callback:
        try:
            manager.run_job_callback(
                library_id=library_id, library_name=library_name, config=config or {}
            )
            # Update last run time
            manager._update_last_run(schedule_id)
        except Exception as e:
            logger.error(f"Failed to execute scheduled job {schedule_id}: {e}")
    else:
        logger.warning("No run_job_callback set, cannot execute scheduled job")


class ScheduleManager:
    """
    Manages scheduled jobs using APScheduler.

    Provides CRUD operations for schedules with cron expression support
    and persistent storage via SQLite.
    """

    def __init__(
        self, config_dir: str = "/config", run_job_callback: Optional[Callable] = None
    ):
        self.config_dir = config_dir
        self.db_path = os.path.join(config_dir, "scheduler.db")
        self.schedules_file = os.path.join(config_dir, "schedules.json")
        self.run_job_callback = run_job_callback
        self._schedules: Dict[str, dict] = {}

        # Ensure config directory exists
        os.makedirs(config_dir, exist_ok=True)

        # Initialize scheduler with SQLite job store
        jobstores = {"default": SQLAlchemyJobStore(url=f"sqlite:///{self.db_path}")}

        self.scheduler = BackgroundScheduler(
            jobstores=jobstores,
            job_defaults={
                "coalesce": True,
                "max_instances": 1,
                "misfire_grace_time": 3600,  # 1 hour grace period
            },
        )

        # Add event listeners
        self.scheduler.add_listener(self._on_job_executed, EVENT_JOB_EXECUTED)
        self.scheduler.add_listener(self._on_job_error, EVENT_JOB_ERROR)
        self.scheduler.add_listener(self._on_job_missed, EVENT_JOB_MISSED)

        # Load schedule metadata
        self._load_schedules()

    def _load_schedules(self) -> None:
        """Load schedule metadata from persistent storage."""
        if os.path.exists(self.schedules_file):
            try:
                with open(self.schedules_file, "r") as f:
                    data = json.load(f)
                    self._schedules = data.get("schedules", {})
                logger.info(f"Loaded {len(self._schedules)} schedule configurations")
            except (json.JSONDecodeError, IOError) as e:
                logger.warning(f"Failed to load schedules: {e}")

    def _save_schedules(self) -> None:
        """Save schedule metadata to persistent storage."""
        try:
            with open(self.schedules_file, "w") as f:
                json.dump({"schedules": self._schedules}, f, indent=2)
        except IOError as e:
            logger.error(f"Failed to save schedules: {e}")

    def _on_job_executed(self, event) -> None:
        """Handle successful job execution."""
        logger.info(f"Scheduled job {event.job_id} executed successfully")

    def _on_job_error(self, event) -> None:
        """Handle job execution error."""
        logger.error(f"Scheduled job {event.job_id} failed: {event.exception}")

    def _on_job_missed(self, event) -> None:
        """Handle missed job."""
        logger.warning(f"Scheduled job {event.job_id} was missed")

    def start(self) -> None:
        """Start the scheduler."""
        if not self.scheduler.running:
            self.scheduler.start()
            logger.info("Scheduler started")

    def stop(self) -> None:
        """Stop the scheduler."""
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
            logger.info("Scheduler stopped")

    def set_run_job_callback(self, callback: Callable) -> None:
        """Set the callback function for running jobs."""
        self.run_job_callback = callback

    def _update_last_run(self, schedule_id: str) -> None:
        """Update the last run time for a schedule."""
        if schedule_id in self._schedules:
            self._schedules[schedule_id]["last_run"] = datetime.now(
                timezone.utc
            ).isoformat()
            self._save_schedules()

    def create_schedule(
        self,
        name: str,
        cron_expression: str = None,
        interval_minutes: int = None,
        library_id: Optional[str] = None,
        library_name: str = "",
        config: Optional[dict] = None,
        enabled: bool = True,
    ) -> dict:
        """
        Create a new schedule.

        Args:
            name: Human-readable name for the schedule
            cron_expression: Cron expression (e.g., "0 2 * * *" for 2 AM daily)
            interval_minutes: Interval in minutes (alternative to cron)
            library_id: Optional Plex library ID to process
            library_name: Library name for display
            config: Optional configuration overrides
            enabled: Whether the schedule is enabled

        Returns:
            Schedule metadata dict
        """
        schedule_id = str(uuid.uuid4())

        # Create trigger
        if cron_expression:
            trigger = CronTrigger.from_crontab(cron_expression)
            trigger_type = "cron"
            trigger_value = cron_expression
        elif interval_minutes:
            trigger = IntervalTrigger(minutes=interval_minutes)
            trigger_type = "interval"
            trigger_value = str(interval_minutes)
        else:
            raise ValueError(
                "Either cron_expression or interval_minutes must be provided"
            )

        # Store metadata
        schedule_meta = {
            "id": schedule_id,
            "name": name,
            "trigger_type": trigger_type,
            "trigger_value": trigger_value,
            "library_id": library_id,
            "library_name": library_name,
            "config": config or {},
            "enabled": enabled,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "last_run": None,
            "next_run": None,
        }

        # Ensure scheduler is running
        if not self.scheduler.running:
            self.start()

        # Add job to scheduler if enabled
        if enabled:
            job = self.scheduler.add_job(
                execute_scheduled_job,
                trigger=trigger,
                id=schedule_id,
                args=[schedule_id, library_id, library_name, config],
                replace_existing=True,
            )
            schedule_meta["next_run"] = (
                job.next_run_time.isoformat() if job.next_run_time else None
            )

        self._schedules[schedule_id] = schedule_meta
        self._save_schedules()

        logger.info(f"Created schedule '{name}' (ID: {schedule_id})")
        return schedule_meta

    def update_schedule(
        self,
        schedule_id: str,
        name: str = None,
        cron_expression: str = None,
        interval_minutes: int = None,
        library_id: str = None,
        library_name: str = None,
        config: dict = None,
        enabled: bool = None,
    ) -> Optional[dict]:
        """Update an existing schedule."""
        if schedule_id not in self._schedules:
            return None

        schedule = self._schedules[schedule_id]

        # Update fields
        if name is not None:
            schedule["name"] = name
        if library_id is not None:
            schedule["library_id"] = library_id
        if library_name is not None:
            schedule["library_name"] = library_name
        if config is not None:
            schedule["config"] = config
        if enabled is not None:
            schedule["enabled"] = enabled

        # Update trigger if changed
        if cron_expression is not None:
            schedule["trigger_type"] = "cron"
            schedule["trigger_value"] = cron_expression
        elif interval_minutes is not None:
            schedule["trigger_type"] = "interval"
            schedule["trigger_value"] = str(interval_minutes)

        # Remove existing job
        try:
            self.scheduler.remove_job(schedule_id)
        except Exception:
            pass

        # Re-add job if enabled
        if schedule["enabled"]:
            if schedule["trigger_type"] == "cron":
                trigger = CronTrigger.from_crontab(schedule["trigger_value"])
            else:
                trigger = IntervalTrigger(minutes=int(schedule["trigger_value"]))

            job = self.scheduler.add_job(
                execute_scheduled_job,
                trigger=trigger,
                id=schedule_id,
                args=[
                    schedule_id,
                    schedule["library_id"],
                    schedule["library_name"],
                    schedule["config"],
                ],
                replace_existing=True,
            )
            schedule["next_run"] = (
                job.next_run_time.isoformat() if job.next_run_time else None
            )
        else:
            schedule["next_run"] = None

        self._save_schedules()
        logger.info(f"Updated schedule {schedule_id}")
        return schedule

    def delete_schedule(self, schedule_id: str) -> bool:
        """Delete a schedule."""
        if schedule_id not in self._schedules:
            return False

        # Remove from scheduler
        try:
            self.scheduler.remove_job(schedule_id)
        except Exception:
            pass

        del self._schedules[schedule_id]
        self._save_schedules()

        logger.info(f"Deleted schedule {schedule_id}")
        return True

    def get_schedule(self, schedule_id: str) -> Optional[dict]:
        """Get a schedule by ID."""
        schedule = self._schedules.get(schedule_id)
        if schedule:
            # Update next_run from scheduler
            try:
                job = self.scheduler.get_job(schedule_id)
                if job and job.next_run_time:
                    schedule["next_run"] = job.next_run_time.isoformat()
            except Exception:
                pass
        return schedule

    def get_all_schedules(self) -> List[dict]:
        """Get all schedules."""
        schedules = []
        for schedule_id, schedule in self._schedules.items():
            # Update next_run from scheduler
            try:
                job = self.scheduler.get_job(schedule_id)
                if job and job.next_run_time:
                    schedule["next_run"] = job.next_run_time.isoformat()
            except Exception:
                pass
            schedules.append(schedule)
        return schedules

    def enable_schedule(self, schedule_id: str) -> Optional[dict]:
        """Enable a schedule."""
        return self.update_schedule(schedule_id, enabled=True)

    def disable_schedule(self, schedule_id: str) -> Optional[dict]:
        """Disable a schedule."""
        return self.update_schedule(schedule_id, enabled=False)

    def run_now(self, schedule_id: str) -> bool:
        """Run a schedule immediately."""
        schedule = self._schedules.get(schedule_id)
        if not schedule:
            return False

        logger.info(f"Running schedule '{schedule['name']}' now")
        execute_scheduled_job(
            schedule_id,
            schedule.get("library_id"),
            schedule.get("library_name", ""),
            schedule.get("config"),
        )
        self._save_schedules()
        return True


# Global scheduler instance
_schedule_manager: Optional[ScheduleManager] = None
_schedule_lock = threading.Lock()

# Default config directory from environment
DEFAULT_CONFIG_DIR = os.environ.get("CONFIG_DIR", "/config")


def get_schedule_manager(
    config_dir: Optional[str] = None, run_job_callback: Optional[Callable] = None
) -> ScheduleManager:
    """Get or create the global ScheduleManager instance (thread-safe)."""
    global _schedule_manager
    with _schedule_lock:
        if _schedule_manager is None:
            _schedule_manager = ScheduleManager(
                config_dir=config_dir or DEFAULT_CONFIG_DIR,
                run_job_callback=run_job_callback,
            )
        elif run_job_callback and _schedule_manager.run_job_callback is None:
            _schedule_manager.set_run_job_callback(run_job_callback)
        return _schedule_manager
