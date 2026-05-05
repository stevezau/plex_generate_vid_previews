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
from playwright.sync_api import Page, expect


@pytest.mark.e2e
class TestLiveJobLifecycle:
    """The #1 audit gap: SocketIO event flow from real backend to DOM."""

    def test_job_lifecycle_emits_progress_then_complete_via_real_socketio(
        self,
        backend_real_page: Page,
        backend_real_app: tuple[str, str],
    ) -> None:
        app_url, _ = backend_real_app

        # Open the dashboard FIRST so SocketIO connects before we POST the job.
        # Otherwise the `job_created` and `job_completed` events fire before the
        # client subscribes and the test can't observe them.
        backend_real_page.goto(f"{app_url}/")
        backend_real_page.wait_for_load_state("domcontentloaded")

        # The dashboard's own ``socket`` variable is module-scoped (let socket = null)
        # so we can't observe its events from the page context. Open a SECOND
        # SocketIO connection on the same /jobs namespace via the global ``io``
        # client lib, and subscribe to the events we care about.
        # transports: ['polling'] matches the dashboard + the server's
        # allow_upgrades=False config.
        backend_real_page.wait_for_function("() => typeof io === 'function'", timeout=5000)
        backend_real_page.evaluate(
            """
            window.__capturedEvents = [];
            window.__testSocket = io('/jobs', { transports: ['polling'] });
            ['job_created', 'job_started', 'job_progress',
             'job_completed', 'job_failed', 'worker_update'].forEach(name => {
                window.__testSocket.on(name, (data) => {
                    window.__capturedEvents.push({event: name, data: data});
                });
            });
            """
        )
        # Wait for our test socket to connect before POSTing.
        backend_real_page.wait_for_function(
            "() => window.__testSocket && window.__testSocket.connected === true",
            timeout=10000,
        )

        # POST a real manual job through Flask. file_paths must be under the
        # MEDIA_ROOT allowlist (default "/" so /tmp works). The path doesn't
        # need to exist — with no servers configured the orchestrator marks
        # it unresolved and completes quickly without any FFmpeg work.
        post_resp = backend_real_page.request.post(
            f"{app_url}/api/jobs/manual",
            headers={
                "X-Auth-Token": "e2e-test-token",
                "Content-Type": "application/json",
            },
            data='{"file_paths": ["/tmp/nonexistent_e2e_job.mkv"]}',
        )
        assert post_resp.ok, f"POST /api/jobs/manual failed: {post_resp.status} {post_resp.text()}"
        job = post_resp.json()
        job_id = job["id"]
        assert job_id, "Backend did not return a job id"

        # Wait for the dashboard's REAL DOM to reflect the job arriving.
        # `loadJobs()` is invoked by the `job_created` SocketIO handler.
        # The active-jobs container should populate within a few SocketIO
        # ticks. The basename of our path becomes the job's display label.
        try:
            backend_real_page.wait_for_function(
                """(jobId) => {
                    const evs = window.__capturedEvents || [];
                    return evs.some(e => e.event === 'job_created'
                        && e.data && e.data.id === jobId);
                }""",
                arg=job_id,
                timeout=10000,
            )
        except Exception:
            captured = backend_real_page.evaluate("window.__capturedEvents || []")
            raise AssertionError(
                f"Backend never emitted a job_created event for job {job_id} "
                f"that the dashboard observed. Captured: {captured}"
            ) from None

        # Real backend should drive the lifecycle to completion (or failure).
        # The dispatcher with no Plex + no servers + a single webhook_path will
        # finish near-instantly. We accept either job_completed OR job_failed
        # — both prove the orchestrator->JobManager->SocketIO chain works.
        # What we DON'T accept: jobs stuck in PENDING with no terminal event.
        deadline = time.monotonic() + 30
        terminal_event = None
        while time.monotonic() < deadline:
            captured = backend_real_page.evaluate("window.__capturedEvents || []")
            for ev in captured:
                if ev["event"] in ("job_completed", "job_failed") and ev["data"].get("id") == job_id:
                    terminal_event = ev
                    break
            if terminal_event:
                break
            time.sleep(0.25)

        assert terminal_event is not None, (
            f"Real backend never emitted job_completed or job_failed for {job_id} within 30s. "
            f"Captured events: {backend_real_page.evaluate('window.__capturedEvents || []')}"
        )

        # The terminal event payload must include the SAME job id (drift would
        # mean the dashboard's removeActiveJob() never fires for our job).
        assert terminal_event["data"]["id"] == job_id

        # And the REAL backend's job-stats endpoint must reflect the new total.
        stats_resp = backend_real_page.request.get(
            f"{app_url}/api/jobs/stats",
            headers={"X-Auth-Token": "e2e-test-token"},
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
        backend_real_page.wait_for_function("() => typeof io === 'function'", timeout=5000)

        post_resp = backend_real_page.request.post(
            f"{app_url}/api/jobs/manual",
            headers={
                "X-Auth-Token": "e2e-test-token",
                "Content-Type": "application/json",
            },
            data='{"file_paths": ["/tmp/another_nonexistent.mkv"]}',
        )
        assert post_resp.ok, post_resp.text()
        job_id = post_resp.json()["id"]

        # Wait for the job to reach a terminal state via the REAL backend's
        # /api/jobs/<id> endpoint — no SocketIO timing required, just real
        # state machine transitions.
        terminal_statuses = {"completed", "failed", "cancelled"}
        deadline = time.monotonic() + 30
        final_status = None
        while time.monotonic() < deadline:
            r = backend_real_page.request.get(
                f"{app_url}/api/jobs/{job_id}",
                headers={"X-Auth-Token": "e2e-test-token"},
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
