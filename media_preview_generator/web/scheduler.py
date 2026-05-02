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


def _parse_hhmm(value: str | None) -> tuple[int, int] | None:
    """Parse an "HH:MM" string into ``(hour, minute)``.

    Returns ``None`` when ``value`` is empty / None (caller treats this
    as "no stop time configured"). Raises ``ValueError`` on a non-empty
    but malformed input so the API layer can surface a 400 rather than
    silently dropping a misconfiguration.
    """
    if not value:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    parts = raw.split(":")
    if len(parts) != 2:
        raise ValueError(f"stop_time must be HH:MM, got {value!r}")
    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"stop_time must be HH:MM, got {value!r}") from exc
    if not (0 <= hour < 24 and 0 <= minute < 60):
        raise ValueError(f"stop_time out of range, got {value!r}")
    return hour, minute


# D21 / D26 — Quiet-hours cron job-id prefixes. With multi-window
# support each window registers TWO crons (`__qh_pause_{idx}` /
# `__qh_resume_{idx}`); the legacy single-window IDs are kept here so
# apply_quiet_hours can clean them up on first multi-window save for
# installs that ran the D21 single-window code.
_QUIET_HOURS_PAUSE_JOB_ID = "__quiet_hours_pause"  # legacy D21 single-window id
_QUIET_HOURS_RESUME_JOB_ID = "__quiet_hours_resume"  # legacy D21 single-window id
_QUIET_HOURS_PAUSE_PREFIX = "__qh_pause_"
_QUIET_HOURS_RESUME_PREFIX = "__qh_resume_"

# APScheduler day_of_week names (Mon-first). Order matters for cron
# strings — keep these literal so a typo in the JS payload can't slip
# through to a silent no-op.
_QUIET_HOURS_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def is_in_quiet_window(
    now_hm: tuple[int, int],
    pause_hm: tuple[int, int],
    resume_hm: tuple[int, int],
) -> bool:
    """Return ``True`` when ``now_hm`` falls inside the paused window.

    Equal pause/resume times disable the window (returns ``False``).
    Cross-midnight windows (pause > resume) are handled — e.g. pause=08:00,
    resume=01:00 means processing is paused all day until 1 AM. Day-of-
    week filtering is the caller's responsibility (see
    :func:`is_now_in_any_quiet_window`).
    """
    n = now_hm[0] * 60 + now_hm[1]
    p = pause_hm[0] * 60 + pause_hm[1]
    r = resume_hm[0] * 60 + resume_hm[1]
    if p == r:
        return False
    if p < r:
        return p <= n < r
    return n >= p or n < r


def normalise_quiet_hours(raw: dict | None) -> dict:
    """Normalise the persisted quiet_hours shape (D26).

    Accepts the legacy D21 single-window form
    ``{"enabled": bool, "start": "HH:MM", "end": "HH:MM"}`` and migrates
    it to the multi-window form
    ``{"enabled": bool, "windows": [{"start", "end", "days"}]}``.
    Legacy entries map to one window covering all 7 days.

    Per-window ``days`` defaults to all 7 when missing. Invalid /
    out-of-range day names are silently dropped — never raises so a
    bad on-disk shape can't crash the boot path.
    """
    raw = raw if isinstance(raw, dict) else {}
    enabled = bool(raw.get("enabled"))
    raw_windows = raw.get("windows")
    if isinstance(raw_windows, list) and raw_windows:
        windows = []
        for w in raw_windows:
            if not isinstance(w, dict):
                continue
            start = str(w.get("start") or "")
            end = str(w.get("end") or "")
            if not start or not end:
                continue
            days = w.get("days")
            if not isinstance(days, list) or not days:
                days = list(_QUIET_HOURS_DAYS)
            else:
                days = [d for d in (str(x).strip().lower() for x in days) if d in _QUIET_HOURS_DAYS]
                if not days:
                    days = list(_QUIET_HOURS_DAYS)
            windows.append({"start": start, "end": end, "days": days})
        return {"enabled": enabled, "windows": windows}
    # Legacy single-window migration.
    start = str(raw.get("start") or "")
    end = str(raw.get("end") or "")
    if start and end:
        return {
            "enabled": enabled,
            "windows": [{"start": start, "end": end, "days": list(_QUIET_HOURS_DAYS)}],
        }
    return {"enabled": enabled, "windows": []}


def is_now_in_any_quiet_window(
    quiet_hours: dict | None,
    now: datetime | None = None,
) -> bool:
    """Return True if ``now`` falls inside ANY enabled quiet-hours window.

    Considers both the time-of-day (via :func:`is_in_quiet_window`) AND
    the per-window day-of-week filter. Used by the boot-time gate, the
    `currently_in_quiet_window` API field, and the pause/resume cron
    callbacks (which need to recompute state to handle overlapping
    windows correctly — resume of window A must NOT un-pause when
    window B is still active).
    """
    qh = normalise_quiet_hours(quiet_hours)
    if not qh.get("enabled"):
        return False
    n = now or datetime.now()
    today = _QUIET_HOURS_DAYS[n.weekday()]
    now_hm = (n.hour, n.minute)
    for w in qh.get("windows", []):
        if today not in (w.get("days") or _QUIET_HOURS_DAYS):
            continue
        try:
            shm = _parse_hhmm(w["start"])
            ehm = _parse_hhmm(w["end"])
        except (KeyError, ValueError):
            continue
        if shm is None or ehm is None:
            continue
        if is_in_quiet_window(now_hm, shm, ehm):
            return True
    return False


def _quiet_hours_recompute_and_apply() -> None:
    """Idempotent state flip — set processing_paused to whether ANY window is active.

    Called from BOTH the pause-boundary and resume-boundary crons so
    overlapping windows don't fight each other (e.g. window A's resume
    cron firing while window B is still active correctly leaves the
    queue paused). Also called from the boot-time gate.
    """
    try:
        from .jobs import get_job_manager
        from .settings_manager import get_settings_manager

        sm = get_settings_manager()
        target = is_now_in_any_quiet_window(sm.get("quiet_hours"))
        if sm.processing_paused == target:
            return
        sm.processing_paused = target
        try:
            get_job_manager().emit_processing_paused_changed(target)
        except Exception:
            logger.debug(
                "Could not emit processing_paused_changed on quiet-hours flip",
                exc_info=True,
            )
        if target:
            logger.info("Quiet hours: processing paused (queue will fill until resume time)")
        else:
            logger.info("Quiet hours: processing resumed (queue draining)")
    except Exception:
        logger.exception("Quiet-hours recompute hit an unexpected error")


# Module-level callbacks (must be picklable for APScheduler's SQLAlchemy
# jobstore). Both the pause-edge and resume-edge cron jobs call the
# same recompute helper — see _quiet_hours_recompute_and_apply.
def _quiet_hours_pause() -> None:
    _quiet_hours_recompute_and_apply()


def _quiet_hours_resume() -> None:
    _quiet_hours_recompute_and_apply()


def execute_schedule_stop(schedule_id: str) -> None:
    """Module-level stop handler — pauses every running job spawned by ``schedule_id``.

    Pickled by APScheduler's SQLAlchemy jobstore alongside the start
    cron, so it must live at module scope. Looks up the JobManager
    singleton, finds RUNNING jobs whose ``parent_schedule_id`` matches,
    and calls ``request_pause`` on each. Cooperative — in-flight
    FFmpeg processes finish their current task naturally.
    """
    from .jobs import JobStatus, get_job_manager

    manager = get_schedule_manager()
    schedule = manager.get_schedule(schedule_id) if manager else None
    name = (schedule or {}).get("name", schedule_id)
    stop_time = (schedule or {}).get("stop_time", "")

    job_manager = get_job_manager()
    paused_count = 0
    for job in job_manager.get_all_jobs():
        if job.parent_schedule_id != schedule_id:
            continue
        if job.status is not JobStatus.RUNNING or job.paused:
            continue
        if job_manager.request_pause(job.id):
            job_manager.add_log(
                job.id,
                f"INFO - Paused by schedule {name!r} stop time ({stop_time})",
            )
            paused_count += 1

    if paused_count:
        logger.info(
            "Schedule {!r} ({}): stop-time fired, paused {} running job(s)",
            name,
            schedule_id,
            paused_count,
        )
    else:
        logger.info(
            "Schedule {!r} ({}): stop-time fired, no running jobs from this schedule to pause",
            name,
            schedule_id,
        )


# Module-level function for APScheduler to call
# Must be at module level to be picklable
def execute_scheduled_job(
    schedule_id: str,
    library_ids_or_id=None,
    library_name: str = "",
    config: dict | None = None,
    priority: int | None = None,
    server_id: str | None = None,
    *,
    library_id: str | None = None,
) -> None:
    """Execute a scheduled job — module-level function for APScheduler pickling.

    This function must be at module level (not a class method) because
    APScheduler's SQLAlchemy jobstore needs to pickle it.

    Dispatches on ``config["job_type"]``:

    * ``"recently_added"`` — runs the Recently Added scanner against the
      schedule's libraries (or all libraries when none specified).
      Uses ``config["lookback_hours"]`` (default 1). Plex-only.
    * anything else (including missing) — legacy **full library** scan via
      ``manager.run_job_callback``, which creates a job processing every
      item in the targeted libraries.

    Args:
        schedule_id: The ID of the schedule triggering this job
        library_ids_or_id: Library section IDs to process. Accepts either a
            list of strings (Phase H7 canonical shape) or a single string
            (back-compat with persisted schedules from earlier versions).
            Pass ``None`` or ``[]`` to process all libraries.
        library_name: Human-readable library name(s) for display
        config: Job configuration dict — may include ``job_type`` and
            ``lookback_hours``
        priority: Dispatch priority (1=high, 2=normal, 3=low)
        server_id: Configured-server id this schedule targets (optional).
            Pinned through to the created job so per-server attribution
            works in the Jobs UI and the dispatcher routes only to that
            server.

    """
    # Normalise to list[str]; tolerate legacy callers that still pass
    # ``library_id=`` (single string) instead of the new positional list.
    if library_ids_or_id is None and library_id is not None:
        library_ids_or_id = library_id
    if isinstance(library_ids_or_id, str):
        library_ids = [library_ids_or_id] if library_ids_or_id else []
    elif isinstance(library_ids_or_id, list):
        library_ids = [str(x) for x in library_ids_or_id if str(x).strip()]
    else:
        library_ids = []
    primary_library_id = library_ids[0] if len(library_ids) == 1 else None

    cfg = dict(config or {})
    if server_id and "server_id" not in cfg:
        cfg["server_id"] = server_id
    job_type = str(cfg.get("job_type", "full_library"))
    manager = get_schedule_manager()

    # D21 — global processing-paused gate. When the queue is paused
    # (manual Pause All button OR quiet hours window), we DO NOT spawn
    # a new Job from this scheduled tick — that would pile up redundant
    # work (e.g. a "every 15 min recently_added" schedule firing all
    # day during quiet hours would balloon the queue). The schedule
    # re-fires on its next normal tick; the first one to land outside
    # the paused window will spawn the Job. Manual jobs and webhook
    # triggers still queue up — those go through different code paths
    # and are what the user explicitly wanted "to pile up".
    try:
        from .settings_manager import get_settings_manager

        if get_settings_manager().processing_paused:
            logger.info(
                "Schedule {} skipped — processing is currently paused (quiet hours / manual pause). "
                "It will fire again on its next normal tick.",
                schedule_id,
            )
            return
    except Exception:
        logger.debug(
            "Could not check processing_paused gate for schedule {}; allowing dispatch",
            schedule_id,
            exc_info=True,
        )

    # D20 — auto-resume an existing paused job from this same schedule
    # instead of spawning a fresh one. Lets a multi-night library scan
    # span across stop_time pauses with the same Job ID and progress.
    # Only applies to full_library jobs; recently_added is a fast,
    # idempotent scan that can re-run cheaply.
    if job_type != "recently_added":
        try:
            from .jobs import JobStatus, get_job_manager

            job_manager = get_job_manager()
            for job in job_manager.get_all_jobs():
                if job.parent_schedule_id != schedule_id:
                    continue
                if not job.paused or job.status is not JobStatus.RUNNING:
                    continue
                if job_manager.request_resume(job.id):
                    job_manager.add_log(
                        job.id,
                        f"INFO - Resumed by schedule {schedule_id!r} start tick",
                    )
                    logger.info(
                        "Schedule {}: resumed paused job {} instead of spawning a new one",
                        schedule_id,
                        job.id[:8],
                    )
                    manager._update_last_run(schedule_id)
                    return
        except Exception:
            logger.exception(
                "Could not check for resumable paused jobs for schedule {} — falling back "
                "to spawning a new job (the previous paused one will need a manual resume).",
                schedule_id,
            )

    if job_type == "recently_added":
        try:
            lookback = float(cfg.get("lookback_hours", 1) or 1)
        except (TypeError, ValueError):
            lookback = 1.0
        lookback = max(0.25, min(720.0, lookback))
        logger.info(
            "Executing scheduled recently-added scan: {} (library={}, lookback={:.2g}h, server={})",
            schedule_id,
            library_name or "all libraries",
            lookback,
            server_id or "(all)",
        )
        try:
            # Per-vendor processor path (Phase E): works for Plex, Emby,
            # AND Jellyfin — every vendor's processor implements
            # scan_recently_added against its native API. No fall-back to
            # the old Plex-only scanner.
            from ..config import load_config
            from ..jobs.orchestrator import _run_recently_added_multi_server
            from .routes.job_runner import _build_selected_gpus
            from .settings_manager import get_settings_manager

            run_config = load_config()
            selected_gpus = _build_selected_gpus(get_settings_manager())
            _run_recently_added_multi_server(
                run_config,
                selected_gpus=selected_gpus,
                server_id_filter=server_id,
                library_ids=library_ids or None,
                lookback_hours=lookback,
            )
            manager._update_last_run(schedule_id)
        except Exception:
            logger.exception(
                "Scheduled 'recently added' scan {} could not run. "
                "It will retry on its next scheduled tick — verify the target server is reachable and credentials are valid.",
                schedule_id,
            )
        return

    logger.info("Executing scheduled job: {} for library: {}", schedule_id, library_name)

    if manager.run_job_callback:
        try:
            # For multi-library schedules, hand off the full list via
            # config.selected_library_ids so the orchestrator processes each
            # library in one job. The legacy single-library shortcut goes via
            # library_id when there's exactly one (keeps existing behaviour).
            if len(library_ids) > 1:
                cfg = dict(cfg)
                cfg["selected_library_ids"] = library_ids
            kwargs = {
                "library_id": primary_library_id,
                "library_name": library_name,
                "config": cfg,
                "parent_schedule_id": schedule_id,
            }
            if priority is not None:
                kwargs["priority"] = priority
            if server_id:
                kwargs["server_id"] = server_id
            manager.run_job_callback(**kwargs)
            manager._update_last_run(schedule_id)
        except Exception:
            logger.exception(
                "Scheduled job {} for library {!r} could not start. "
                "It will retry on its next scheduled tick — check the Jobs page for any prior error details.",
                schedule_id,
                library_name or "all libraries",
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
        # Single RLock guarding ``_schedules`` mutations + reads. Without
        # this, APScheduler firing _update_last_run on multiple schedules
        # concurrently with a CRUD call (create/update/delete) could trip
        # "dictionary changed size during iteration" inside _save_schedules
        # which serialises the dict for atomic_json_save_with_backup.
        self._lock = threading.RLock()

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
                raw_schedules = data.get("schedules", {})

                # J4: filter per-record so one bad schedule doesn't wipe the rest.
                # Mirrors web/jobs.py:_load_jobs (Fix-5 Phase H pattern). A
                # corrupt entry is logged + skipped; valid entries still load.
                self._schedules = {}
                bad_count = 0
                for sched_id, sched in raw_schedules.items():
                    if not isinstance(sched, dict):
                        bad_count += 1
                        logger.warning(
                            "Skipping schedule {!r}: not an object on disk (got {}).",
                            sched_id,
                            type(sched).__name__,
                        )
                        continue
                    self._schedules[sched_id] = sched
                if bad_count:
                    logger.warning(
                        "Skipped {} malformed schedule record(s); valid ones loaded normally.",
                        bad_count,
                    )

                # Phase H7 migration: legacy schedules stored a single
                # ``library_id``. Promote to ``library_ids`` (list) so the
                # rest of the codebase can treat them uniformly. We do NOT
                # delete ``library_id`` — kept as a derived back-compat
                # field for one release in case any external script still
                # reads it.
                migrated = 0
                for sched in self._schedules.values():
                    if "library_ids" not in sched or not isinstance(sched.get("library_ids"), list):
                        legacy = sched.get("library_id")
                        sched["library_ids"] = [str(legacy)] if legacy else []
                        migrated += 1
                logger.info("Loaded {} schedule configurations", len(self._schedules))
                if migrated:
                    logger.info(
                        "Migrated {} legacy schedule(s) from single library_id to library_ids list",
                        migrated,
                    )
                    self._save_schedules()
                # D30 — re-register every enabled schedule with APScheduler
                # so a fresh / wiped scheduler.db doesn't leave the schedules
                # dormant. Treat schedules.json as the source of truth and
                # the SQLAlchemy jobstore as a derived/cache; rebuilding on
                # every load makes restarts robust to a scheduler.db that
                # was wiped, corrupted, or never persisted (we saw this on
                # the canary: 3 schedules in JSON, 0 jobs in apscheduler_jobs,
                # crons silently never fired). replace_existing=True is the
                # safe-merge with whatever the jobstore did persist.
                self._reregister_loaded_schedules()
            except (OSError, json.JSONDecodeError) as e:
                bak = self.schedules_file + ".bak"
                bak_hint = (
                    f" A backup is at {bak} — `mv` it to {self.schedules_file} and restart to recover."
                    if os.path.exists(bak)
                    else ""
                )
                logger.warning(
                    "Could not read saved schedules from {} ({}: {}).{}"
                    " Starting with an empty schedule list — your existing schedules will reappear "
                    "if the file becomes readable; otherwise re-create them on the Schedules page.",
                    self.schedules_file,
                    type(e).__name__,
                    e,
                    bak_hint,
                )

    def _reregister_loaded_schedules(self) -> None:
        """Re-register every enabled in-memory schedule with APScheduler (D30).

        Called from :meth:`_load_schedules` after the JSON metadata has been
        loaded. Builds a trigger from each schedule's persisted
        ``trigger_type``/``trigger_value`` and adds the job back into the
        APScheduler instance with ``replace_existing=True`` so a fresh /
        empty / corrupted ``scheduler.db`` (the SQLAlchemy jobstore) can't
        leave the schedules dormant. The schedules.json file is treated as
        the source of truth; the jobstore is just a derived cache.

        Disabled schedules and schedules with malformed trigger expressions
        are skipped with a warning — never raises so a single bad entry can
        never block boot.
        """
        if not self._schedules:
            return
        registered = 0
        skipped_bad = 0
        skipped_disabled = 0
        for sched_id, sched in self._schedules.items():
            if not sched.get("enabled"):
                skipped_disabled += 1
                continue
            try:
                trigger_type = sched.get("trigger_type")
                trigger_value = sched.get("trigger_value")
                if trigger_type == "cron":
                    trigger = CronTrigger.from_crontab(str(trigger_value or ""))
                elif trigger_type == "interval":
                    trigger = IntervalTrigger(minutes=int(trigger_value))
                else:
                    logger.warning(
                        "Schedule {!r} has unknown trigger_type {!r}; skipping re-registration.",
                        sched.get("name") or sched_id,
                        trigger_type,
                    )
                    skipped_bad += 1
                    continue

                ids_canonical = list(sched.get("library_ids") or [])
                ids_canonical = [str(x) for x in ids_canonical if str(x).strip()]
                self.scheduler.add_job(
                    execute_scheduled_job,
                    trigger=trigger,
                    id=sched_id,
                    args=[
                        sched_id,
                        ids_canonical,
                        sched.get("library_name", ""),
                        sched.get("config") or {},
                        sched.get("priority"),
                        sched.get("server_id"),
                    ],
                    replace_existing=True,
                )
                # Refresh the persisted next_run snapshot so the UI shows
                # the future fire time immediately (not the stale value
                # from the previous boot — which the canary observed as
                # "Next: 3 days ago" until the cron actually fired).
                # Compute via the trigger directly because add_job() called
                # BEFORE scheduler.start() returns a pending Job that has
                # no next_run_time attribute yet (APScheduler queues these
                # and assigns next_run_time at start time).
                try:
                    next_fire = trigger.get_next_fire_time(None, datetime.now(timezone.utc))
                    if next_fire is not None:
                        sched["next_run"] = next_fire.isoformat()
                except Exception as exc:
                    logger.debug(
                        "next_run computation failed for schedule {!r}: {}",
                        sched.get("name") or sched_id,
                        exc,
                    )
                # Re-register the daily stop-cron (D20) if configured. _parse_hhmm
                # returns None for empty/blank, in which case we skip silently.
                stop_time = str(sched.get("stop_time") or "")
                try:
                    stop_hm = _parse_hhmm(stop_time)
                except ValueError:
                    stop_hm = None  # tolerate bad on-disk data
                if stop_hm is not None:
                    self._register_stop_job(sched_id, stop_hm)
                registered += 1
            except Exception as exc:
                logger.warning(
                    "Could not re-register schedule {!r} on startup ({}: {}); it will not fire "
                    "until edited via the UI. Other schedules are unaffected.",
                    sched.get("name") or sched_id,
                    type(exc).__name__,
                    exc,
                )
                skipped_bad += 1
        logger.info(
            "Re-registered {} schedule(s) with APScheduler ({} disabled, {} skipped due to errors)",
            registered,
            skipped_disabled,
            skipped_bad,
        )
        # Persist the refreshed next_run snapshots so the UI sees future
        # fire times immediately on next API call instead of the stale
        # value from the previous boot.
        if registered > 0:
            self._save_schedules()

    def _save_schedules(self) -> None:
        """Save schedule metadata to persistent storage.

        Caller is expected to hold ``self._lock`` (or this is the bootstrap
        load path before any concurrent firings can happen). Callers from
        the public CRUD methods all wrap save under the same lock.
        """
        try:
            from ..utils import atomic_json_save_with_backup

            # Snapshot under the lock so the dict can't mutate mid-serialisation.
            with self._lock:
                snapshot = dict(self._schedules)
            atomic_json_save_with_backup(self.schedules_file, {"schedules": snapshot})
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
        logger.info("Scheduled job {} executed successfully", event.job_id)

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

    def apply_quiet_hours(self, settings_dict: dict | None) -> None:
        """Register / refresh the per-window quiet-hours crons (D21 + D26).

        ``settings_dict`` is the value of ``settings["quiet_hours"]``.
        Accepts both the legacy single-window shape
        ``{"enabled": bool, "start": "HH:MM", "end": "HH:MM"}`` and the
        new multi-window shape
        ``{"enabled": bool, "windows": [{"start", "end", "days"}]}`` —
        normalise_quiet_hours migrates the former. Each window
        registers TWO crons (pause at start, resume at end) keyed by
        index. Both callbacks recompute "is any window currently
        active?" so overlapping windows don't fight each other (e.g. a
        resume cron firing while another window is still active won't
        accidentally un-pause the queue).
        """
        # Wipe ALL existing quiet-hours crons (legacy single-window IDs
        # AND any per-window IDs from a prior apply). One full rebuild
        # is simpler than reconciling adds/removes individually.
        for job in self.scheduler.get_jobs():
            if (
                job.id == _QUIET_HOURS_PAUSE_JOB_ID
                or job.id == _QUIET_HOURS_RESUME_JOB_ID
                or job.id.startswith(_QUIET_HOURS_PAUSE_PREFIX)
                or job.id.startswith(_QUIET_HOURS_RESUME_PREFIX)
            ):
                try:
                    self.scheduler.remove_job(job.id)
                except Exception:
                    pass

        qh = normalise_quiet_hours(settings_dict)
        if not qh.get("enabled") or not qh.get("windows"):
            return

        if not self.scheduler.running:
            self.start()

        registered = 0
        for idx, w in enumerate(qh["windows"]):
            try:
                start_hm = _parse_hhmm(str(w.get("start") or ""))
                end_hm = _parse_hhmm(str(w.get("end") or ""))
            except ValueError:
                logger.warning(
                    "Quiet-hours window #{} has malformed times {}; skipping.",
                    idx,
                    w,
                )
                continue
            if start_hm is None or end_hm is None or start_hm == end_hm:
                continue
            days = w.get("days") or list(_QUIET_HOURS_DAYS)
            day_filter = ",".join(d for d in days if d in _QUIET_HOURS_DAYS)
            if not day_filter:
                continue
            self.scheduler.add_job(
                _quiet_hours_pause,
                trigger=CronTrigger(day_of_week=day_filter, hour=start_hm[0], minute=start_hm[1]),
                id=f"{_QUIET_HOURS_PAUSE_PREFIX}{idx}",
                replace_existing=True,
            )
            self.scheduler.add_job(
                _quiet_hours_resume,
                trigger=CronTrigger(day_of_week=day_filter, hour=end_hm[0], minute=end_hm[1]),
                id=f"{_QUIET_HOURS_RESUME_PREFIX}{idx}",
                replace_existing=True,
            )
            registered += 1
            logger.info(
                "Quiet hours window #{}: pause {:02d}:{:02d} → resume {:02d}:{:02d} on {} (container TZ)",
                idx,
                start_hm[0],
                start_hm[1],
                end_hm[0],
                end_hm[1],
                day_filter,
            )

        if registered == 0:
            logger.info("Quiet hours enabled but no valid windows found — no crons registered.")

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
        with self._lock:
            if schedule_id in self._schedules:
                self._schedules[schedule_id]["last_run"] = datetime.now(timezone.utc).isoformat()
                self._save_schedules()

    def create_schedule(
        self,
        name: str,
        cron_expression: str | None = None,
        interval_minutes: int | None = None,
        library_id: str | None = None,
        library_name: str = "",
        config: dict | None = None,
        enabled: bool = True,
        priority: int | None = None,
        server_id: str | None = None,
        library_ids: list[str] | None = None,
        stop_time: str = "",
    ) -> dict:
        """Create a new schedule.

        Args:
            name: Human-readable name for the schedule
            cron_expression: Cron expression (e.g., "0 2 * * *" for 2 AM daily)
            interval_minutes: Interval in minutes (alternative to cron)
            library_id: Optional library ID to process
            library_name: Library name for display
            config: Optional configuration overrides
            enabled: Whether the schedule is enabled
            priority: Dispatch priority for jobs created by this schedule (1-3)
            server_id: Optional configured-server id this schedule targets.
                When set, jobs created by this schedule are pinned to that
                server only — important when multiple servers share a
                library name (e.g. both Plex and Emby have "Movies").
            stop_time: Optional "HH:MM" container-local time (D20). When
                set, registers a daily stop cron that pauses any RUNNING
                job spawned by this schedule. The next start tick
                resumes the paused job instead of spawning a new one,
                so a multi-night library scan can span pauses with the
                same Job ID. Empty / unset = no stop behaviour.

        Returns:
            Schedule metadata dict

        """
        schedule_id = str(uuid.uuid4())
        # Validate stop_time up front so callers see the ValueError
        # before any partial side-effects (jobstore add, json save).
        stop_hm = _parse_hhmm(stop_time)

        # Create trigger
        if cron_expression:
            trigger = CronTrigger.from_crontab(cron_expression)
            trigger_type = "cron"
            trigger_value = cron_expression
        elif interval_minutes:
            trigger = IntervalTrigger(minutes=interval_minutes)
            trigger_type = "interval"
            trigger_value = str(interval_minutes)
            # stop_time only makes sense for time-of-day triggers.
            stop_hm = None
            stop_time = ""
        else:
            raise ValueError("Either cron_expression or interval_minutes must be provided")

        # Multi-select libraries (Phase H7). Canonical store is library_ids
        # (a list); library_id is kept as a derived back-compat field for any
        # legacy reader that hasn't migrated yet.
        ids_canonical = list(library_ids) if library_ids else ([library_id] if library_id else [])
        ids_canonical = [str(x) for x in ids_canonical if str(x).strip()]
        single_id = ids_canonical[0] if len(ids_canonical) == 1 else None

        # Store metadata
        schedule_meta = {
            "id": schedule_id,
            "name": name,
            "trigger_type": trigger_type,
            "trigger_value": trigger_value,
            "library_id": single_id,
            "library_ids": ids_canonical,
            "library_name": library_name,
            "server_id": server_id,
            "config": config or {},
            "enabled": enabled,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "last_run": None,
            "next_run": None,
            "priority": priority,
            "stop_time": stop_time or "",
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
                args=[schedule_id, ids_canonical, library_name, config, priority, server_id],
                replace_existing=True,
            )
            schedule_meta["next_run"] = job.next_run_time.isoformat() if job.next_run_time else None
            if stop_hm is not None:
                self._register_stop_job(schedule_id, stop_hm)

        with self._lock:
            self._schedules[schedule_id] = schedule_meta
            self._save_schedules()

        logger.info("Created schedule '{}' (ID: {})", name, schedule_id)
        return schedule_meta

    def _stop_job_id(self, schedule_id: str) -> str:
        """APScheduler job id for the per-schedule stop-cron (D20)."""
        return f"{schedule_id}__stop"

    def _register_stop_job(self, schedule_id: str, stop_hm: tuple[int, int]) -> None:
        """Add the daily stop-cron for ``schedule_id`` (D20)."""
        hour, minute = stop_hm
        self.scheduler.add_job(
            execute_schedule_stop,
            trigger=CronTrigger(hour=hour, minute=minute),
            id=self._stop_job_id(schedule_id),
            args=[schedule_id],
            replace_existing=True,
        )

    def _remove_stop_job(self, schedule_id: str) -> None:
        """Best-effort removal of the per-schedule stop-cron (D20)."""
        try:
            self.scheduler.remove_job(self._stop_job_id(schedule_id))
        except Exception:
            logger.debug("No stop-cron to remove for schedule {}", schedule_id)

    def update_schedule(
        self,
        schedule_id: str,
        name: str | None = None,
        cron_expression: str | None = None,
        interval_minutes: int | None = None,
        library_id: str | None = None,
        library_name: str | None = None,
        config: dict | None = None,
        enabled: bool | None = None,
        priority: int | None = None,
        server_id: str | None = None,
        library_ids: list[str] | None = None,
        stop_time: str | None = None,
    ) -> dict | None:
        """Update an existing schedule.

        ``stop_time``: pass an "HH:MM" string to set, "" to clear, or
        ``None`` to leave unchanged. D20.
        """
        with self._lock:
            if schedule_id not in self._schedules:
                return None

            schedule = self._schedules[schedule_id]

        # Update fields
        if name is not None:
            schedule["name"] = name
        if library_ids is not None:
            # Canonical multi-select store. Also mirror to library_id for
            # any downstream that hasn't migrated.
            ids = [str(x) for x in (library_ids or []) if str(x).strip()]
            schedule["library_ids"] = ids
            schedule["library_id"] = ids[0] if len(ids) == 1 else None
        elif library_id is not None:
            # Single-library back-compat path.
            schedule["library_id"] = library_id
            schedule["library_ids"] = [str(library_id)] if library_id else []
        if library_name is not None:
            schedule["library_name"] = library_name
        if config is not None:
            schedule["config"] = config
        if enabled is not None:
            schedule["enabled"] = enabled
        if priority is not None:
            schedule["priority"] = priority
        if server_id is not None:
            # Empty string means "clear the pin", null means "leave alone".
            schedule["server_id"] = server_id or None
        if stop_time is not None:
            # Validate before persisting; ValueError surfaces as a 400 in API.
            _ = _parse_hhmm(stop_time)  # may raise
            schedule["stop_time"] = stop_time or ""

        # Update trigger if changed
        if cron_expression is not None:
            schedule["trigger_type"] = "cron"
            schedule["trigger_value"] = cron_expression
        elif interval_minutes is not None:
            schedule["trigger_type"] = "interval"
            schedule["trigger_value"] = str(interval_minutes)
            # stop_time is meaningless for interval triggers — clear it
            # automatically so a user changing trigger type doesn't end
            # up with an orphan stop cron firing daily.
            schedule["stop_time"] = ""

        # Remove existing job (may not exist if schedule was disabled)
        try:
            self.scheduler.remove_job(schedule_id)
        except Exception:
            logger.debug("No existing scheduler job to remove for {}", schedule_id)
        # Always remove the stop-cron too; we'll re-register it below if
        # the (possibly updated) stop_time still applies.
        self._remove_stop_job(schedule_id)

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
                    schedule.get("library_ids", []),
                    schedule["library_name"],
                    schedule["config"],
                    schedule.get("priority"),
                    schedule.get("server_id"),
                ],
                replace_existing=True,
            )
            schedule["next_run"] = job.next_run_time.isoformat() if job.next_run_time else None
            stop_hm = _parse_hhmm(schedule.get("stop_time") or "")
            if stop_hm is not None and schedule["trigger_type"] == "cron":
                self._register_stop_job(schedule_id, stop_hm)
        else:
            schedule["next_run"] = None

        self._save_schedules()
        logger.info("Updated schedule {}", schedule_id)
        return schedule

    def delete_schedule(self, schedule_id: str) -> bool:
        """Delete a schedule."""
        with self._lock:
            if schedule_id not in self._schedules:
                return False

            # Remove from scheduler (may not exist if schedule was disabled)
            try:
                self.scheduler.remove_job(schedule_id)
            except Exception:
                logger.debug("No existing scheduler job to remove for {}", schedule_id)
            self._remove_stop_job(schedule_id)

            del self._schedules[schedule_id]
            self._save_schedules()

            logger.info("Deleted schedule {}", schedule_id)
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
                logger.debug("Could not fetch next_run for schedule {}", schedule_id)
        return schedule

    def get_all_schedules(self) -> list[dict]:
        """Get all schedules."""
        with self._lock:
            entries = list(self._schedules.items())
        schedules = []
        for schedule_id, schedule in entries:
            try:
                job = self.scheduler.get_job(schedule_id)
                if job and job.next_run_time:
                    schedule["next_run"] = job.next_run_time.isoformat()
            except Exception:
                logger.debug("Could not fetch next_run for schedule {}", schedule_id)
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

        logger.info("Running schedule '{}' now", schedule["name"])
        ids = schedule.get("library_ids") or ([schedule["library_id"]] if schedule.get("library_id") else [])
        execute_scheduled_job(
            schedule_id,
            ids,
            schedule.get("library_name", ""),
            schedule.get("config"),
            schedule.get("priority"),
            schedule.get("server_id"),
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
