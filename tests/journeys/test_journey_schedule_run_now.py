"""TEST_AUDIT P1.10 — schedule run-now produces a Job with the right wiring.

User journey:
  1. POST ``/api/schedules`` to create a schedule (cron-triggered).
  2. POST ``/api/schedules/<id>/run`` to trigger it immediately.
  3. A Job appears in JobManager with:
     - ``parent_schedule_id`` pointing at the schedule
     - the schedule's ``library_id`` projected onto job config
     - ``server_id`` pinned (when the schedule had one)
  4. The schedule's ``last_run`` is updated to "now".
  5. The schedule's ``next_run`` is recomputed (NOT stuck at last_run —
     a regression that fails to re-arm leaves the schedule appearing
     "last fired ages ago, next fires never").

Mocks ONLY at the orchestrator boundary (``run_processing``). All
schedule wiring, JobManager creation, and ``_start_job_async`` runs for
real.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from media_preview_generator.web.app import create_app
from media_preview_generator.web.settings_manager import reset_settings_manager

pytestmark = pytest.mark.journey


@pytest.fixture(autouse=True)
def _reset_singletons():
    reset_settings_manager()
    import media_preview_generator.web.jobs as jobs_mod
    import media_preview_generator.web.scheduler as sched_mod
    import media_preview_generator.web.webhooks as wh_mod

    with jobs_mod._job_lock:
        jobs_mod._job_manager = None
    with sched_mod._schedule_lock:
        if sched_mod._schedule_manager is not None:
            try:
                sched_mod._schedule_manager.stop()
            except Exception:
                pass
        sched_mod._schedule_manager = None
    wh_mod._recent_dispatches.clear()
    wh_mod._pending_batches.clear()
    for t in list(wh_mod._pending_timers.values()):
        try:
            t.cancel()
        except Exception:
            pass
    wh_mod._pending_timers.clear()
    yield
    reset_settings_manager()
    with jobs_mod._job_lock:
        jobs_mod._job_manager = None
    with sched_mod._schedule_lock:
        if sched_mod._schedule_manager is not None:
            try:
                sched_mod._schedule_manager.stop()
            except Exception:
                pass
            sched_mod._schedule_manager = None
    wh_mod._recent_dispatches.clear()
    wh_mod._pending_batches.clear()
    for t in list(wh_mod._pending_timers.values()):
        try:
            t.cancel()
        except Exception:
            pass
    wh_mod._pending_timers.clear()


@pytest.fixture()
def app(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    monkeypatch.setenv("CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("WEB_AUTH_TOKEN", "test-token-12345678")
    # See test_journey_cancel_running_job.app for why plex_config_folder
    # must be set + the path must contain Media/. CI lacks the dev .env
    # that masks this locally.
    plex_cfg = tmp_path / "plex_cfg"
    (plex_cfg / "Media" / "localhost").mkdir(parents=True, exist_ok=True)
    settings_path = config_dir / "settings.json"
    settings_path.write_text(
        json.dumps(
            {
                "setup_complete": True,
                "media_servers": [
                    {
                        "id": "plex-1",
                        "type": "plex",
                        "name": "Plex Main",
                        "enabled": True,
                        "url": "http://plex:32400",
                        "auth": {"token": "tok"},
                        "libraries": [{"id": "lib-1", "name": "Movies", "enabled": True}],
                        "output": {"adapter": "plex_bundle", "plex_config_folder": str(plex_cfg)},
                    }
                ],
            }
        )
    )
    auth_path = config_dir / "auth.json"
    auth_path.write_text(json.dumps({"token": "test-token-12345678"}))
    return create_app(config_dir=str(config_dir))


def _auth_headers():
    return {"X-Auth-Token": "test-token-12345678", "Content-Type": "application/json"}


# ---------------------------------------------------------------------------
# Run-now journey
# ---------------------------------------------------------------------------


class TestScheduleRunNow:
    def test_run_now_spawns_job_with_schedule_attribution_and_advances_last_run(self, app):
        """Pin: when the user clicks "Run now" on a schedule, a real Job
        materialises in JobManager carrying ``parent_schedule_id`` so the
        Jobs page can show "Triggered by schedule X". Without
        attribution, the operator can't tell scheduled-runs from
        manual-runs apart in History."""
        from media_preview_generator.web.jobs import get_job_manager
        from media_preview_generator.web.scheduler import get_schedule_manager

        run_calls: list[dict] = []

        def fake_run_processing(config, selected_gpus, **kwargs):
            run_calls.append({"job_id": kwargs.get("job_id")})
            return {"outcome": {"generated": 0}}

        client = app.test_client()
        with (
            app.app_context(),
            patch(
                "media_preview_generator.jobs.orchestrator.run_processing",
                side_effect=fake_run_processing,
            ),
        ):
            # Create the schedule via the real API.
            create_resp = client.post(
                "/api/schedules",
                data=json.dumps(
                    {
                        "name": "Nightly Movies",
                        "cron_expression": "0 3 * * *",
                        "library_id": "lib-1",
                        "library_name": "Movies",
                        "server_id": "plex-1",
                        "config": {},
                    }
                ),
                headers=_auth_headers(),
            )
            assert create_resp.status_code == 201, (
                f"Schedule create must return 201; got {create_resp.status_code}. "
                f"Body: {create_resp.get_data(as_text=True)!r}"
            )
            schedule_id = create_resp.get_json()["id"]

            # Capture next_run BEFORE the run-now so we can confirm it
            # didn't go backwards / wasn't reset to None.
            schedules_before = get_schedule_manager().get_all_schedules()
            schedule_before = next(s for s in schedules_before if s["id"] == schedule_id)
            next_run_before = schedule_before.get("next_run")
            assert next_run_before, (
                f"Just-created cron schedule must have a next_run; got {next_run_before!r}. "
                f"APScheduler trigger registration is broken."
            )
            assert schedule_before.get("last_run") is None, (
                f"Brand-new schedule must have last_run=None; got {schedule_before.get('last_run')!r}"
            )

            t_before_run = datetime.now(timezone.utc)

            # Fire run-now.
            run_resp = client.post(
                f"/api/schedules/{schedule_id}/run",
                headers=_auth_headers(),
            )
            assert run_resp.status_code == 200, (
                f"run-now must return 200 for an existing schedule; got {run_resp.status_code}. "
                f"Body: {run_resp.get_data(as_text=True)!r}"
            )

        # A Job must exist with parent_schedule_id pointing at this schedule.
        all_jobs = get_job_manager().get_all_jobs()
        scheduled_jobs = [j for j in all_jobs if j.parent_schedule_id == schedule_id]
        assert len(scheduled_jobs) == 1, (
            f"Run-now must create exactly 1 Job whose parent_schedule_id == {schedule_id!r}; "
            f"got {len(scheduled_jobs)}. All jobs: "
            f"{[(j.id, j.library_name, j.parent_schedule_id) for j in all_jobs]}. "
            f"Without parent_schedule_id, the Jobs page can't render the 'from schedule' chip."
        )
        job = scheduled_jobs[0]

        # The schedule's library_id is forwarded onto the Job's
        # selected_libraries so the orchestrator scopes the scan correctly.
        assert "lib-1" in (job.config.get("selected_libraries") or []), (
            f"Schedule's library_id='lib-1' must land in job.config['selected_libraries']; "
            f"got config.selected_libraries={job.config.get('selected_libraries')!r}. "
            f"A regression here fans every schedule out to every library."
        )

        # Server pin propagated.
        assert job.server_id == "plex-1", (
            f"Schedule pinned to server_id='plex-1' must produce a Job with server_id='plex-1'; "
            f"got job.server_id={job.server_id!r}"
        )

        # run_processing was called for the spawned Job.
        assert any(c["job_id"] == job.id for c in run_calls), (
            f"run_processing must be invoked with the spawned job_id={job.id!r}; got run_calls={run_calls!r}"
        )

        # last_run advanced past t_before_run.
        schedules_after = get_schedule_manager().get_all_schedules()
        schedule_after = next(s for s in schedules_after if s["id"] == schedule_id)
        last_run_after = schedule_after.get("last_run")
        assert last_run_after, (
            f"After run-now, schedule.last_run must be set; got {last_run_after!r}. "
            f"A regression here leaves the Schedules UI showing 'Last run: never' even after a manual fire."
        )
        last_run_dt = datetime.fromisoformat(last_run_after.replace("Z", "+00:00"))
        # Allow 1s slop for clock skew.
        assert last_run_dt >= t_before_run.replace(microsecond=0), (
            f"last_run={last_run_after!r} must be >= t_before_run={t_before_run.isoformat()!r}; "
            f"the timestamp wasn't actually updated to 'now'."
        )

        # next_run still anchored at the original cron firing time — the
        # APScheduler job wasn't dropped or rescheduled to "never". A
        # regression that nuked the trigger after run-now would leave
        # next_run=None and the schedule would never auto-fire again.
        next_run_after = schedule_after.get("next_run")
        assert next_run_after, (
            f"After run-now, schedule.next_run must STILL point at a future fire time; "
            f"got {next_run_after!r}. The schedule lost its trigger and won't auto-fire again — "
            f"silent sleep-mode bug class."
        )
        # And it shouldn't equal last_run (that would mean next_run is just
        # mirroring when we ran, not the actual next-cron-fire time).
        assert next_run_after != last_run_after, (
            f"next_run={next_run_after!r} must differ from last_run={last_run_after!r}; "
            f"if they're equal the trigger wasn't recomputed and the UI will show "
            f"'Next run: <same time as last>' which is misleading."
        )

    def test_run_now_unknown_schedule_returns_404(self, app):
        """Negative-case pin: a regression that returned 200 on missing
        ids would let the UI silently swallow stale schedule deletes."""
        client = app.test_client()
        with app.app_context():
            r = client.post(
                "/api/schedules/no-such-schedule/run",
                headers=_auth_headers(),
            )
        assert r.status_code == 404, (
            f"run-now on unknown schedule_id must return 404; got {r.status_code}. Body: {r.get_data(as_text=True)!r}"
        )
