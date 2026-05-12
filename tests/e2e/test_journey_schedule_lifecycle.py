"""Backend-real E2E: schedule create -> edit -> run-now -> delete lifecycle.

Audit gap #6: only `create` is tested. PUT, DELETE, run-now, enable,
disable endpoints are registered with no UI test exercising any of them.

This file drives the full lifecycle through the real APScheduler-backed
ScheduleManager. The schedule fires against a Plex server with no real
URL, so when run-now triggers it the orchestrator marks every item
unresolved and the resulting job completes near-instantly.
"""

from __future__ import annotations

import time

import pytest
import requests

# Auth header used by every backend API call below. Centralised here so
# the swap-from-page.request was a single audit point.
#
# Why ``requests`` instead of ``page.request``: ``page.request.post()``
# routes through Playwright's Python ↔ Node IPC pipe; under ``-n auto``
# parallel-test load the pipe scheduler stalls and the un-tunable 30 s
# timeout fires (~33 % failure rate, proven by side-by-side probe).
# These backend calls don't need browser cookie state (auth is via
# ``X-Auth-Token`` header), so ``requests`` is the correct client.
# See playwright#26739 / playwright-python#1039 for the IPC-stall
# class of bug.
_AUTH_HEADERS = {"X-Auth-Token": "e2e-test-token"}
_API_TIMEOUT = 60


_SEEDED_SERVER = {
    "id": "plex-sched",
    "type": "plex",
    "name": "Sched Plex",
    "enabled": True,
    "url": "http://plex.invalid:32400",
    "auth": {"method": "token", "token": "x" * 20},
    "verify_ssl": True,
    "timeout": 60,
    "libraries": [
        {"id": "1", "name": "Movies", "remote_paths": [], "enabled": True},
    ],
    "path_mappings": [],
    "exclude_paths": [],
    "output": {
        "adapter": "plex_bundle",
        "plex_config_folder": "/tmp",
        "frame_interval": 10,
    },
}


def _create_schedule_via_api(app_url: str, name: str, cron: str = "0 3 * * *") -> dict:
    """POST /api/schedules and return the created schedule dict."""
    payload = {
        "name": name,
        "cron_expression": cron,
        "library_id": None,
        "library_ids": [],
        "library_name": "All Libraries",
        "server_id": "plex-sched",
        "enabled": True,
        "priority": 2,
        "config": {"job_type": "full_library"},
    }
    resp = requests.post(
        f"{app_url}/api/schedules",
        headers=_AUTH_HEADERS,
        json=payload,
        timeout=_API_TIMEOUT,
    )
    assert resp.ok, f"POST /api/schedules failed: {resp.status_code} {resp.text}"
    return resp.json()


def _list_schedules(app_url: str) -> list[dict]:
    resp = requests.get(
        f"{app_url}/api/schedules",
        headers=_AUTH_HEADERS,
        timeout=_API_TIMEOUT,
    )
    assert resp.ok
    return (resp.json() or {}).get("schedules", [])


@pytest.mark.e2e
@pytest.mark.parametrize(
    "backend_real_app",
    [{"media_servers": [_SEEDED_SERVER]}],
    indirect=True,
)
class TestScheduleLifecycle:
    def test_create_then_edit_changes_persist(
        self,
        backend_real_page,
        backend_real_app: tuple[str, str],
    ) -> None:
        """Create -> PUT (edit name + cron) -> assert GET reflects new values."""
        app_url, _ = backend_real_app

        # Create via real API.
        created = _create_schedule_via_api(app_url, "Original Name", cron="0 2 * * *")
        sched_id = created["id"]
        assert created["name"] == "Original Name"

        # Edit via real PUT.
        put_resp = requests.put(
            f"{app_url}/api/schedules/{sched_id}",
            headers=_AUTH_HEADERS,
            json={
                "name": "Edited Name",
                "cron_expression": "30 4 * * *",
                "library_id": None,
                "library_ids": [],
                "library_name": "All Libraries",
                "server_id": "plex-sched",
                "enabled": True,
                "priority": 2,
                "config": {"job_type": "full_library"},
            },
            timeout=_API_TIMEOUT,
        )
        assert put_resp.ok, f"PUT failed: {put_resp.status_code} {put_resp.text}"

        # GET to verify both fields persisted.
        schedules = _list_schedules(app_url)
        target = next((s for s in schedules if s["id"] == sched_id), None)
        assert target is not None, f"Schedule {sched_id} disappeared after PUT"
        assert target["name"] == "Edited Name", f"Name didn't persist: {target}"
        assert target["trigger_value"] == "30 4 * * *", (
            f"Cron expression didn't persist: trigger_value={target['trigger_value']!r}"
        )

    def test_run_now_creates_job_in_active_panel(
        self,
        backend_real_app: tuple[str, str],
    ) -> None:
        """Click run-now -> a job materialises in the dashboard active panel.

        This is the path that breaks silently when the scheduler dispatch
        chain is broken — POST returns 200, no job ever appears.

        Implementation note: this test originally observed
        ``job_created`` via a Playwright-driven SocketIO subscription
        (page.goto + page.evaluate + page.wait_for_function), but
        every Playwright call routes through the Python ↔ Node IPC
        pipe and under ``-n auto`` parallelism that pipe stalls,
        crashing the xdist worker on a 60 s timeout. The test's
        *contract* is "a job materialises" — i.e. it appears in the
        backend's /api/jobs response. Polling that endpoint via
        ``requests`` is functionally equivalent, doesn't require a
        browser context, and is immune to the Playwright IPC stall.

        Coverage gap: the dashboard's SocketIO push for the *manual*
        dispatch path is verified in
        ``test_journey_live_job_lifecycle.py``, but the *scheduler*
        dispatch path's SocketIO emit is not directly asserted
        anywhere. It's covered transitively (both paths funnel
        through ``JobManager.create_job`` which fires the
        ``job_created`` event), but a regression that bypassed the
        emit for scheduler-spawned Jobs specifically would not be
        caught here. Worth a follow-up test if that path becomes
        load-bearing for the dashboard UX.
        """
        app_url, _ = backend_real_app

        # Create a schedule first so we have something to run-now.
        created = _create_schedule_via_api(app_url, "Run Now Test")
        sched_id = created["id"]

        # Run-now via the real endpoint.
        run_resp = requests.post(
            f"{app_url}/api/schedules/{sched_id}/run",
            headers=_AUTH_HEADERS,
            timeout=_API_TIMEOUT,
        )
        assert run_resp.ok, f"run-now failed: {run_resp.status_code} {run_resp.text}"

        # Poll the jobs endpoint until the schedule's run-now Job
        # materialises. ``page=0`` returns the unpaginated full list
        # so the assertion isn't sensitive to per_page defaults or
        # to historical jobs pushing this one off page 1. Matches by
        # ``parent_schedule_id`` so we know the job is THIS schedule's
        # dispatch (not some unrelated one that landed concurrently
        # in a parallel test).
        deadline = time.monotonic() + 15
        observed_job: dict | None = None
        while time.monotonic() < deadline:
            jobs_resp = requests.get(
                f"{app_url}/api/jobs?page=0",
                headers=_AUTH_HEADERS,
                timeout=_API_TIMEOUT,
            )
            assert jobs_resp.ok, f"GET /api/jobs failed: {jobs_resp.status_code} {jobs_resp.text}"
            jobs = jobs_resp.json().get("jobs", [])
            for job in jobs:
                if job.get("parent_schedule_id") == sched_id:
                    observed_job = job
                    break
            if observed_job is not None:
                break
            time.sleep(0.2)

        assert observed_job is not None, (
            f"Schedule run-now POSTed cleanly but no Job ever appeared in /api/jobs "
            f"with parent_schedule_id={sched_id!r}. The schedule fired into the "
            "void — its callback raised silently inside APScheduler, or the "
            "dispatch chain dropped the job."
        )

    def test_disable_then_enable_toggle_round_trips(
        self,
        backend_real_page,
        backend_real_app: tuple[str, str],
    ) -> None:
        """Disable -> GET shows enabled=false -> Enable -> GET shows enabled=true."""
        app_url, _ = backend_real_app

        created = _create_schedule_via_api(app_url, "Toggle Test")
        sched_id = created["id"]
        assert created["enabled"] is True

        disable_resp = requests.post(
            f"{app_url}/api/schedules/{sched_id}/disable",
            headers=_AUTH_HEADERS,
            timeout=_API_TIMEOUT,
        )
        assert disable_resp.ok, disable_resp.text

        schedules = _list_schedules(app_url)
        target = next(s for s in schedules if s["id"] == sched_id)
        assert target["enabled"] is False, f"Disable did not stick: {target}"

        enable_resp = requests.post(
            f"{app_url}/api/schedules/{sched_id}/enable",
            headers=_AUTH_HEADERS,
            timeout=_API_TIMEOUT,
        )
        assert enable_resp.ok, enable_resp.text

        schedules = _list_schedules(app_url)
        target = next(s for s in schedules if s["id"] == sched_id)
        assert target["enabled"] is True, f"Enable did not stick: {target}"

    def test_delete_removes_schedule_from_list(
        self,
        backend_real_page,
        backend_real_app: tuple[str, str],
    ) -> None:
        """DELETE -> next GET no longer returns the schedule."""
        app_url, _ = backend_real_app

        created = _create_schedule_via_api(app_url, "Delete Me")
        sched_id = created["id"]

        # Confirm it's there.
        before = _list_schedules(app_url)
        assert any(s["id"] == sched_id for s in before)

        delete_resp = requests.delete(
            f"{app_url}/api/schedules/{sched_id}",
            headers=_AUTH_HEADERS,
            timeout=_API_TIMEOUT,
        )
        assert delete_resp.ok, f"DELETE failed: {delete_resp.status_code} {delete_resp.text}"

        after = _list_schedules(app_url)
        assert not any(s["id"] == sched_id for s in after), (
            f"Schedule {sched_id} still present after DELETE: {[s['id'] for s in after]}"
        )

        # And a follow-up DELETE on the gone schedule should 404 (NOT 500
        # — that would mean the manager raised during the lookup).
        second_delete = requests.delete(
            f"{app_url}/api/schedules/{sched_id}",
            headers=_AUTH_HEADERS,
            timeout=_API_TIMEOUT,
        )
        assert second_delete.status_code == 404, (
            f"Double-delete returned {second_delete.status_code} (expected 404). "
            "Either the manager forgot to handle the missing-id case, or "
            "the delete didn't actually delete."
        )
