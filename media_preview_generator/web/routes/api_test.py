"""Test-only endpoint to reset Flask in-memory + on-disk state.

ONLY registered when the ``MPG_TEST_RESET`` environment variable is
set (the e2e test harness sets this in ``tests/e2e/conftest.py``).
Production builds never load this module's routes.

The endpoint exists to make the e2e test suite's wizard subprocess
fixture reusable across tests on the same xdist worker — without
this reset hook, the function-scoped subprocess fixture has to
respawn Flask per test (~2s × ~120 wizard tests = 4 minutes of pure
boot overhead) because Flask's in-memory caches (JobManager,
SettingsManager, ScheduleManager) don't clear when the config_dir
on disk is wiped.

With this endpoint:
  * Stop the APScheduler background thread cleanly
  * Cancel JobManager's retention timer
  * Reset all four global singletons to None (lazy re-init next call)
  * Delete config_dir state files (jobs.db, scheduler.db, settings.json,
    setup_state.json, auth.json)
  * Return 200 — next request re-initialises everything from scratch
"""

from __future__ import annotations

import os

from flask import jsonify
from loguru import logger

from . import api


@api.route("/__test/reset", methods=["POST"])
def __test_reset():
    """Nuke all in-memory + on-disk state. Test-only."""
    # Hard gate: refuse to do anything unless the env var that's
    # required to even REGISTER this endpoint is still set. Double
    # check so a misconfigured prod build that somehow loaded this
    # module still can't execute the reset.
    if not os.environ.get("MPG_TEST_RESET"):
        return jsonify({"error": "test reset is disabled"}), 403

    cleared: list[str] = []
    errors: list[str] = []

    # 1. Stop scheduler — APScheduler runs a background thread with
    #    its own SQLite jobstore. Shut it down so the next test's
    #    fresh ScheduleManager doesn't clash with the stale thread.
    try:
        from .. import scheduler as sched_mod

        with sched_mod._schedule_lock:  # noqa: SLF001
            if sched_mod._schedule_manager is not None:  # noqa: SLF001
                try:
                    sched_mod._schedule_manager.stop()  # noqa: SLF001
                except Exception as exc:
                    errors.append(f"scheduler.stop: {type(exc).__name__}: {exc}")
                sched_mod._schedule_manager = None  # noqa: SLF001
        cleared.append("ScheduleManager")
    except Exception as exc:
        errors.append(f"ScheduleManager: {type(exc).__name__}: {exc}")

    # 2. Stop JobManager retention timer + clear singleton.
    try:
        from .. import jobs as jobs_mod

        with jobs_mod._job_lock:  # noqa: SLF001
            jm = jobs_mod._job_manager  # noqa: SLF001
            if jm is not None:
                try:
                    jm._stop_retention_timer()  # noqa: SLF001
                except Exception as exc:
                    errors.append(f"jobmanager.timer: {type(exc).__name__}: {exc}")
            jobs_mod._job_manager = None  # noqa: SLF001
        cleared.append("JobManager")
    except Exception as exc:
        errors.append(f"JobManager: {type(exc).__name__}: {exc}")

    # 3. Reset SettingsManager (already has a public helper).
    try:
        from ..settings_manager import reset_settings_manager

        reset_settings_manager()
        cleared.append("SettingsManager")
    except Exception as exc:
        errors.append(f"SettingsManager: {type(exc).__name__}: {exc}")

    # 4. Reset JobGate.
    try:
        from ..job_gate import reset_job_gate

        reset_job_gate()
        cleared.append("JobGate")
    except Exception as exc:
        errors.append(f"JobGate: {type(exc).__name__}: {exc}")

    # 5. Delete on-disk state files from CONFIG_DIR. Each wizard test
    #    expects a pristine first-run state; the reset endpoint
    #    nukes everything that persists between requests.
    config_dir = os.environ.get("CONFIG_DIR", "/config")
    files_to_delete = (
        "jobs.db",
        "jobs.db-shm",
        "jobs.db-wal",
        "scheduler.db",
        "scheduler.db-shm",
        "scheduler.db-wal",
        "settings.json",
        "setup_state.json",
        # NOT auth.json — the Flask session cookie used by the test
        # client is signed with the Flask secret key (which doesn't
        # change), so the cookie stays valid across resets. Deleting
        # auth.json was forcing the session_cookie_wizard fixture to
        # re-POST /login per test, which tripped Flask-Limiter's rate
        # limit (HTTP 429) within seconds.
        "webhook_history.json",
    )
    files_deleted: list[str] = []
    for name in files_to_delete:
        path = os.path.join(config_dir, name)
        if os.path.exists(path):
            try:
                os.remove(path)
                files_deleted.append(name)
            except OSError as exc:
                errors.append(f"unlink {name}: {exc}")

    logger.debug(
        "__test_reset: cleared={} files_deleted={} errors={}",
        cleared,
        files_deleted,
        errors,
    )
    return jsonify(
        {
            "cleared_singletons": cleared,
            "deleted_files": files_deleted,
            "errors": errors,
        }
    ), 200
