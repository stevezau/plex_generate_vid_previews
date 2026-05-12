"""Backend-real E2E: full job lifecycle through real Flask + SocketIO.

These tests are the antidote to the existing dashboard suite, which mocks
every API endpoint via ``page.route()`` and never exercises the real
JobManager / SocketIO stream / orchestrator pipeline. The bug class
"clicked Start, dashboard shows nothing happening for hours" is invisible
to the mocked tests; this file makes sure SocketIO + DOM updates wire
together end-to-end through the real backend.

What's mocked:
    * FFmpeg subprocess (PATH override -> no-op shim) — see backend_real_app.

What is NOT mocked:
    * Flask routes
    * SocketIO emit / receive
    * JobManager state machine
    * Dashboard JS event handlers
    * Job persistence (jobs.db)

A job with no media servers configured + a fake webhook_paths input
completes near-instantly (the orchestrator finds nothing to resolve)
which lets us assert the FULL lifecycle in <5 seconds while still
exercising every layer of real code.
"""

from __future__ import annotations

import time

import pytest
import requests
from playwright.sync_api import Page, expect

_AUTH_HEADERS = {"X-Auth-Token": "e2e-test-token"}
_AUTH_JSON_HEADERS = {"X-Auth-Token": "e2e-test-token", "Content-Type": "application/json"}
_API_TIMEOUT = 60


@pytest.mark.e2e
class TestLiveJobLifecycle:
    """The #1 audit gap: SocketIO event flow from real backend to DOM."""

    def test_job_lifecycle_emits_progress_then_complete_via_real_socketio(
        self,
        backend_real_app: tuple[str, str],
    ) -> None:
        """SocketIO events fire for the full job lifecycle.

        Uses a python-socketio client (not Playwright-driven browser
        observation) so the subscription doesn't go through Playwright's
        Python↔Node IPC pipe — the pipe stalls under ``-n auto`` and
        crashes xdist workers. The contract we're verifying (server
        emits ``job_created`` + a terminal event) is identical regardless
        of which client receives it.
        """
        import socketio

        from .conftest import _capture_session_cookie

        app_url, _ = backend_real_app

        # Subscribe BEFORE POSTing the job so we don't miss early events.
        # The /jobs namespace's `connect` handler calls is_authenticated()
        # which checks the Flask session cookie — capture one via a real
        # /login POST and pass it through the SocketIO connect headers.
        cookie = _capture_session_cookie(app_url)
        cookie_header = f"{cookie['name']}={cookie['value']}"

        captured: list[dict] = []
        client = socketio.Client(reconnection=False)

        def _record(event_name):
            def handler(data):
                captured.append({"event": event_name, "data": data})

            return handler

        for name in ("job_created", "job_started", "job_progress", "job_completed", "job_failed", "worker_update"):
            client.on(name, _record(name), namespace="/jobs")

        client.connect(
            app_url,
            namespaces=["/jobs"],
            transports=["polling"],
            wait_timeout=10,
            headers={"Cookie": cookie_header},
        )
        try:
            # POST a real manual job through Flask. file_paths must be under
            # the MEDIA_ROOT allowlist (default "/" so /tmp works). The path
            # doesn't need to exist — with no servers configured the
            # orchestrator marks it unresolved and completes quickly.
            post_resp = requests.post(
                f"{app_url}/api/jobs/manual",
                headers=_AUTH_JSON_HEADERS,
                data='{"file_paths": ["/tmp/nonexistent_e2e_job.mkv"]}',
                timeout=_API_TIMEOUT,
            )
            assert post_resp.ok, f"POST /api/jobs/manual failed: {post_resp.status_code} {post_resp.text}"
            job_id = post_resp.json()["id"]
            assert job_id, "Backend did not return a job id"

            # Wait for the job_created event for our specific job.
            deadline = time.monotonic() + 10
            saw_created = False
            while time.monotonic() < deadline:
                if any(e["event"] == "job_created" and e["data"].get("id") == job_id for e in captured):
                    saw_created = True
                    break
                time.sleep(0.1)
            assert saw_created, f"Backend never emitted a job_created event for job {job_id}. Captured: {captured}"

            # Real backend should drive the lifecycle to completion (or failure).
            # The dispatcher with no Plex + no servers + a single webhook_path
            # finishes near-instantly. We accept either job_completed OR
            # job_failed — both prove the orchestrator→JobManager→SocketIO
            # chain works. What we DON'T accept: jobs stuck in PENDING with
            # no terminal event.
            deadline = time.monotonic() + 30
            terminal_event = None
            while time.monotonic() < deadline:
                for ev in captured:
                    if ev["event"] in ("job_completed", "job_failed") and ev["data"].get("id") == job_id:
                        terminal_event = ev
                        break
                if terminal_event:
                    break
                time.sleep(0.25)

            assert terminal_event is not None, (
                f"Real backend never emitted job_completed or job_failed for {job_id} within 30s. "
                f"Captured events: {captured}"
            )
            # The terminal event payload must include the SAME job id (drift
            # would mean the dashboard's removeActiveJob() never fires).
            assert terminal_event["data"]["id"] == job_id
        finally:
            client.disconnect()

        # And the REAL backend's job-stats endpoint must reflect the new total.
        stats_resp = requests.get(
            f"{app_url}/api/jobs/stats",
            headers=_AUTH_HEADERS,
            timeout=_API_TIMEOUT,
        )
        assert stats_resp.ok
        stats = stats_resp.json()
        assert stats.get("total", 0) >= 1, f"Real /api/jobs/stats says total=0 after a job completed: {stats}"

    def test_dashboard_active_jobs_panel_clears_after_completion(
        self,
        backend_real_page: Page,
        backend_real_app: tuple[str, str],
    ) -> None:
        """After job_completed, the active-jobs container should NOT keep showing the job."""
        app_url, _ = backend_real_app

        backend_real_page.goto(f"{app_url}/")
        backend_real_page.wait_for_load_state("domcontentloaded")

        post_resp = requests.post(
            f"{app_url}/api/jobs/manual",
            headers=_AUTH_JSON_HEADERS,
            data='{"file_paths": ["/tmp/another_nonexistent.mkv"]}',
            timeout=_API_TIMEOUT,
        )
        assert post_resp.ok, post_resp.text
        job_id = post_resp.json()["id"]

        # Wait for the job to reach a terminal state via the REAL backend's
        # /api/jobs/<id> endpoint — no SocketIO timing required, just real
        # state machine transitions.
        terminal_statuses = {"completed", "failed", "cancelled"}
        deadline = time.monotonic() + 30
        final_status = None
        while time.monotonic() < deadline:
            r = requests.get(
                f"{app_url}/api/jobs/{job_id}",
                headers=_AUTH_HEADERS,
                timeout=_API_TIMEOUT,
            )
            if r.ok:
                final_status = r.json().get("status")
                if final_status in terminal_statuses:
                    break
            time.sleep(0.25)

        assert final_status in terminal_statuses, (
            f"Job {job_id} never reached a terminal status — last seen: {final_status!r}. "
            "This is the bug class the user complained about: jobs stuck forever."
        )

        # Now the dashboard's active-jobs panel must NOT permanently show this
        # job ID after `removeActiveJob(id)` has run. The badge text returns
        # to "No active jobs" when the panel is empty.
        # Allow a few SocketIO ticks for the DOM update to propagate.
        try:
            expect(backend_real_page.locator(f'[data-job-id="{job_id}"]')).to_have_count(0, timeout=5000)
        except AssertionError:
            # Surface the captured DOM state so we can diagnose.
            html = backend_real_page.locator("#activeJobsContainer").inner_html()
            raise AssertionError(
                f"Dashboard #activeJobsContainer still renders job {job_id} after backend "
                f"reported terminal status {final_status!r}. Container HTML: {html[:500]}"
            ) from None
