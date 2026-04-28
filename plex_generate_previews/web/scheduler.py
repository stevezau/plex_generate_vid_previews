"""Scheduling system for the web interface.

Uses APScheduler with SQLite storage for persistent scheduled jobs.
"""

import json
import os
import threading
import uuid
from collections.abc import Callable
from datetime import datetime, timezone

from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED, EVENT_JOB_MISSED
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from loguru import logger


# Module-level function for APScheduler to call
# Must be at module level to be picklable
def execute_scheduled_job(
    schedule_id: str,
    library_id: str | None = None,
    library_name: str = "",
    config: dict | None = None,
    priority: int | None = None,
) -> None:
    """Execute a scheduled job — module-level function for APScheduler pickling.

    This function must be at module level (not a class method) because
    APScheduler's SQLAlchemy jobstore needs to pickle it.

    Dispatches on ``config["job_type"]``:

    * ``"recently_added"`` — runs the Recently Added scanner against the
      schedule's library (or all libraries when ``library_id`` is None).
      Uses ``config["lookback_hours"]`` (default 1).
    * anything else (including missing) — legacy **full library** scan via
      ``manager.run_job_callback``, which creates a job processing every
      item in the targeted library.

    Args:
        schedule_id: The ID of the schedule triggering this job
        library_id: Plex library section ID (``None`` = all libraries)
        library_name: Human-readable library name
        config: Job configuration dict — may include ``job_type`` and
            ``lookback_hours``
        priority: Dispatch priority (1=high, 2=normal, 3=low)

    """
    cfg = config or {}
    job_type = str(cfg.get("job_type", "full_library"))
    manager = get_schedule_manager()

    if job_type == "recently_added":
        try:
            lookback = float(cfg.get("lookback_hours", 1) or 1)
        except (TypeError, ValueError):
            lookback = 1.0
        lookback = max(0.25, min(720.0, lookback))
        logger.info(
            "Executing scheduled recently-added scan: {} (library={}, lookback={:.2g}h)",
            schedule_id,
            library_name or "all libraries",
            lookback,
        )
        try:
            from .recent_added_scanner import scan_recently_added

            library_ids = [str(library_id)] if library_id else None
            scan_recently_added(lookback, library_ids=library_ids)
            manager._update_last_run(schedule_id)
        except Exception as e:
            logger.error(
                "Scheduled 'recently added' scan {} could not run ({}: {}). "
                "It will retry on its next scheduled tick — verify Plex is reachable and the token in Settings is valid.",
                schedule_id,
                type(e).__name__,
                e,
            )
        return

    logger.info(f"Executing scheduled job: {schedule_id} for library: {library_name}")

    if manager.run_job_callback:
        try:
            kwargs = {
                "library_id": library_id,
                "library_name": library_name,
                "config": config or {},
            }
            if priority is not None:
                kwargs["priority"] = priority
            manager.run_job_callback(**kwargs)
            manager._update_last_run(schedule_id)
        except Exception as e:
            logger.error(
                "Scheduled job {} for library {!r} could not start ({}: {}). "
                "It will retry on its next scheduled tick — check the Jobs page for any prior error details.",
                schedule_id,
                library_name or "all libraries",
                type(e).__name__,
                e,
            )
    else:
        logger.warning(
            "Scheduled job {} fired but no job runner is wired up — this is an internal startup issue. "
            "Restart the app; if it persists, please open an issue with the latest log lines.",
            schedule_id,
        )


class ScheduleManager:
    """Manages scheduled jobs using APScheduler.

    Provides CRUD operations for schedules with cron expression support
    and persistent storage via SQLite.
    """

    def __init__(self, config_dir: str = "/config", run_job_callback: Callable | None = None):
        """Initialize schedule manager with config directory and optional callback."""
        self.config_dir = config_dir
        self.db_path = os.path.join(config_dir, "scheduler.db")
        self.schedules_file = os.path.join(config_dir, "schedules.json")
        self.run_job_callback = run_job_callback
        self._schedules: dict[str, dict] = {}

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
                with open(self.schedules_file) as f:
                    data = json.load(f)
                    self._schedules = data.get("schedules", {})
                logger.info(f"Loaded {len(self._schedules)} schedule configurations")
            except (OSError, json.JSONDecodeError) as e:
                logger.warning(
                    "Could not read saved schedules from {} ({}: {}). "
                    "Starting with an empty schedule list — your existing schedules will reappear "
                    "if the file becomes readable; otherwise re-create them on the Schedules page.",
                    self.schedules_file,
                    type(e).__name__,
                    e,
                )

    def _save_schedules(self) -> None:
        """Save schedule metadata to persistent storage."""
        try:
            from ..utils import atomic_json_save

            atomic_json_save(self.schedules_file, {"schedules": self._schedules})
        except OSError as e:
            logger.error(
                "Could not save schedules to {} ({}: {}). "
                "Your changes are still active in memory but won't survive a restart — "
                "check that the config directory is writable (Docker: confirm the volume mount permissions and PUID/PGID).",
                self.schedules_file,
                type(e).__name__,
                e,
            )

    def _on_job_executed(self, event) -> None:
        """Handle successful job execution."""
        logger.info(f"Scheduled job {event.job_id} executed successfully")

    def _on_job_error(self, event) -> None:
        """Handle job execution error."""
        logger.error(
            "Scheduled job {} raised an error: {}. "
            "It will retry on its next scheduled tick — see earlier log lines for the underlying cause.",
            event.job_id,
            event.exception,
        )

    def _on_job_missed(self, event) -> None:
        """Handle missed job."""
        logger.warning(
            "Scheduled job {} did not run on time and was skipped. "
            "This usually means the app was offline when the schedule fired — "
            "it will run normally on the next scheduled tick.",
            event.job_id,
        )

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
            self._schedules[schedule_id]["last_run"] = datetime.now(timezone.utc).isoformat()
            self._save_schedules()

    def create_schedule(
        self,
        name: str,
        cron_expression: str = None,
        interval_minutes: int = None,
        library_id: str | None = None,
        library_name: str = "",
        config: dict | None = None,
        enabled: bool = True,
        priority: int | None = None,
    ) -> dict:
        """Create a new schedule.

        Args:
            name: Human-readable name for the schedule
            cron_expression: Cron expression (e.g., "0 2 * * *" for 2 AM daily)
            interval_minutes: Interval in minutes (alternative to cron)
            library_id: Optional Plex library ID to process
            library_name: Library name for display
            config: Optional configuration overrides
            enabled: Whether the schedule is enabled
            priority: Dispatch priority for jobs created by this schedule (1-3)

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
            raise ValueError("Either cron_expression or interval_minutes must be provided")

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
            "priority": priority,
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
                args=[schedule_id, library_id, library_name, config, priority],
                replace_existing=True,
            )
            schedule_meta["next_run"] = job.next_run_time.isoformat() if job.next_run_time else None

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
        priority: int = None,
    ) -> dict | None:
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
        if priority is not None:
            schedule["priority"] = priority

        # Update trigger if changed
        if cron_expression is not None:
            schedule["trigger_type"] = "cron"
            schedule["trigger_value"] = cron_expression
        elif interval_minutes is not None:
            schedule["trigger_type"] = "interval"
            schedule["trigger_value"] = str(interval_minutes)

        # Remove existing job (may not exist if schedule was disabled)
        try:
            self.scheduler.remove_job(schedule_id)
        except Exception:
            logger.debug(f"No existing scheduler job to remove for {schedule_id}")

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
                    schedule.get("priority"),
                ],
                replace_existing=True,
            )
            schedule["next_run"] = job.next_run_time.isoformat() if job.next_run_time else None
        else:
            schedule["next_run"] = None

        self._save_schedules()
        logger.info(f"Updated schedule {schedule_id}")
        return schedule

    def delete_schedule(self, schedule_id: str) -> bool:
        """Delete a schedule."""
        if schedule_id not in self._schedules:
            return False

        # Remove from scheduler (may not exist if schedule was disabled)
        try:
            self.scheduler.remove_job(schedule_id)
        except Exception:
            logger.debug(f"No existing scheduler job to remove for {schedule_id}")

        del self._schedules[schedule_id]
        self._save_schedules()

        logger.info(f"Deleted schedule {schedule_id}")
        return True

    def get_schedule(self, schedule_id: str) -> dict | None:
        """Get a schedule by ID."""
        schedule = self._schedules.get(schedule_id)
        if schedule:
            try:
                job = self.scheduler.get_job(schedule_id)
                if job and job.next_run_time:
                    schedule["next_run"] = job.next_run_time.isoformat()
            except Exception:
                logger.debug(f"Could not fetch next_run for schedule {schedule_id}")
        return schedule

    def get_all_schedules(self) -> list[dict]:
        """Get all schedules."""
        schedules = []
        for schedule_id, schedule in self._schedules.items():
            try:
                job = self.scheduler.get_job(schedule_id)
                if job and job.next_run_time:
                    schedule["next_run"] = job.next_run_time.isoformat()
            except Exception:
                logger.debug(f"Could not fetch next_run for schedule {schedule_id}")
            schedules.append(schedule)
        return schedules

    def enable_schedule(self, schedule_id: str) -> dict | None:
        """Enable a schedule."""
        return self.update_schedule(schedule_id, enabled=True)

    def disable_schedule(self, schedule_id: str) -> dict | None:
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
_schedule_manager: ScheduleManager | None = None
_schedule_lock = threading.Lock()

# Default config directory from environment
DEFAULT_CONFIG_DIR = os.environ.get("CONFIG_DIR", "/config")


def get_schedule_manager(config_dir: str | None = None, run_job_callback: Callable | None = None) -> ScheduleManager:
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
