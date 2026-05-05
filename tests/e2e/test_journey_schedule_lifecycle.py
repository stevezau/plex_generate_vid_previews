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


def _create_schedule_via_api(page, app_url: str, name: str, cron: str = "0 3 * * *") -> dict:
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
    import json

    resp = page.request.post(
        f"{app_url}/api/schedules",
        headers={"X-Auth-Token": "e2e-test-token", "Content-Type": "application/json"},
        data=json.dumps(payload),
    )
    assert resp.ok, f"POST /api/schedules failed: {resp.status} {resp.text()}"
    return resp.json()


def _list_schedules(page, app_url: str) -> list[dict]:
    resp = page.request.get(
        f"{app_url}/api/schedules",
        headers={"X-Auth-Token": "e2e-test-token"},
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
        created = _create_schedule_via_api(backend_real_page, app_url, "Original Name", cron="0 2 * * *")
        sched_id = created["id"]
        assert created["name"] == "Original Name"

        # Edit via real PUT.
        import json

        put_resp = backend_real_page.request.put(
            f"{app_url}/api/schedules/{sched_id}",
            headers={"X-Auth-Token": "e2e-test-token", "Content-Type": "application/json"},
            data=json.dumps(
                {
                    "name": "Edited Name",
                    "cron_expression": "30 4 * * *",
                    "library_id": None,
                    "library_ids": [],
                    "library_name": "All Libraries",
                    "server_id": "plex-sched",
                    "enabled": True,
                    "priority": 2,
                    "config": {"job_type": "full_library"},
                }
            ),
        )
        assert put_resp.ok, f"PUT failed: {put_resp.status} {put_resp.text()}"

        # GET to verify both fields persisted.
        schedules = _list_schedules(backend_real_page, app_url)
        target = next((s for s in schedules if s["id"] == sched_id), None)
        assert target is not None, f"Schedule {sched_id} disappeared after PUT"
        assert target["name"] == "Edited Name", f"Name didn't persist: {target}"
        assert target["trigger_value"] == "30 4 * * *", (
            f"Cron expression didn't persist: trigger_value={target['trigger_value']!r}"
        )

    def test_run_now_creates_job_in_active_panel(
        self,
        backend_real_page,
        backend_real_app: tuple[str, str],
    ) -> None:
        """Click run-now -> a job materialises in the dashboard active panel.

        This is the path that breaks silently when the scheduler dispatch
        chain is broken — POST returns 200, no job ever appears.
        """
        app_url, _ = backend_real_app

        # Create a schedule first so we have something to run-now.
        created = _create_schedule_via_api(backend_real_page, app_url, "Run Now Test")
        sched_id = created["id"]

        # Open dashboard so SocketIO is connected and we can observe job_created.
        backend_real_page.goto(f"{app_url}/")
        backend_real_page.wait_for_load_state("domcontentloaded")
        backend_real_page.wait_for_function("() => typeof io === 'function'", timeout=5000)
        backend_real_page.evaluate(
            """
            window.__capturedEvents = [];
            window.__testSocket = io('/jobs', { transports: ['polling'] });
            ['job_created'].forEach(name => {
                window.__testSocket.on(name, (data) => {
                    window.__capturedEvents.push({event: name, data: data});
                });
            });
            """
        )
        backend_real_page.wait_for_function(
            "() => window.__testSocket && window.__testSocket.connected === true",
            timeout=10000,
        )

        # Run-now via the real endpoint.
        run_resp = backend_real_page.request.post(
            f"{app_url}/api/schedules/{sched_id}/run",
            headers={"X-Auth-Token": "e2e-test-token"},
        )
        assert run_resp.ok, f"run-now failed: {run_resp.status} {run_resp.text()}"

        # The scheduler dispatches into the same JobManager pipeline. We
        # should observe a job_created event.
        deadline = time.monotonic() + 15
        observed = False
        while time.monotonic() < deadline:
            captured = backend_real_page.evaluate("window.__capturedEvents || []")
            if any(ev["event"] == "job_created" for ev in captured):
                observed = True
                break
            time.sleep(0.2)

        assert observed, (
            "Schedule run-now POSTed cleanly but no job_created event ever fired. "
            "The schedule fired into the void — its callback raised silently inside "
            "APScheduler, or the dispatch chain dropped the job."
        )

    def test_disable_then_enable_toggle_round_trips(
        self,
        backend_real_page,
        backend_real_app: tuple[str, str],
    ) -> None:
        """Disable -> GET shows enabled=false -> Enable -> GET shows enabled=true."""
        app_url, _ = backend_real_app

        created = _create_schedule_via_api(backend_real_page, app_url, "Toggle Test")
        sched_id = created["id"]
        assert created["enabled"] is True

        disable_resp = backend_real_page.request.post(
            f"{app_url}/api/schedules/{sched_id}/disable",
            headers={"X-Auth-Token": "e2e-test-token"},
        )
        assert disable_resp.ok, disable_resp.text()

        schedules = _list_schedules(backend_real_page, app_url)
        target = next(s for s in schedules if s["id"] == sched_id)
        assert target["enabled"] is False, f"Disable did not stick: {target}"

        enable_resp = backend_real_page.request.post(
            f"{app_url}/api/schedules/{sched_id}/enable",
            headers={"X-Auth-Token": "e2e-test-token"},
        )
        assert enable_resp.ok, enable_resp.text()

        schedules = _list_schedules(backend_real_page, app_url)
        target = next(s for s in schedules if s["id"] == sched_id)
        assert target["enabled"] is True, f"Enable did not stick: {target}"

    def test_delete_removes_schedule_from_list(
        self,
        backend_real_page,
        backend_real_app: tuple[str, str],
    ) -> None:
        """DELETE -> next GET no longer returns the schedule."""
        app_url, _ = backend_real_app

        created = _create_schedule_via_api(backend_real_page, app_url, "Delete Me")
        sched_id = created["id"]

        # Confirm it's there.
        before = _list_schedules(backend_real_page, app_url)
        assert any(s["id"] == sched_id for s in before)

        delete_resp = backend_real_page.request.delete(
            f"{app_url}/api/schedules/{sched_id}",
            headers={"X-Auth-Token": "e2e-test-token"},
        )
        assert delete_resp.ok, f"DELETE failed: {delete_resp.status} {delete_resp.text()}"

        after = _list_schedules(backend_real_page, app_url)
        assert not any(s["id"] == sched_id for s in after), (
            f"Schedule {sched_id} still present after DELETE: {[s['id'] for s in after]}"
        )

        # And a follow-up DELETE on the gone schedule should 404 (NOT 500
        # — that would mean the manager raised during the lookup).
        second_delete = backend_real_page.request.delete(
            f"{app_url}/api/schedules/{sched_id}",
            headers={"X-Auth-Token": "e2e-test-token"},
        )
        assert second_delete.status == 404, (
            f"Double-delete returned {second_delete.status} (expected 404). "
            "Either the manager forgot to handle the missing-id case, or "
            "the delete didn't actually delete."
        )
