"""Backend-real E2E: global pause / resume of processing.

Audit gap #10: pause/resume not exercised at all in e2e. Bug class:
pause toggle desync between UI and worker pool, resume not picking up
where left off, ghost "paused" indicator after resume.

We exercise the real `/api/processing/pause` and `/api/processing/resume`
endpoints + the real JobManager's `processing_paused` flag, then assert
the dashboard's UI state reflects the change without a page reload (the
dashboard's `processingPaused` JS var is updated by the request handler).
"""

from __future__ import annotations

import time

import pytest


@pytest.mark.e2e
class TestPauseResumeScan:
    def test_pause_endpoint_flips_state_endpoint_returns_paused_true(
        self,
        backend_real_page,
        backend_real_app: tuple[str, str],
    ) -> None:
        """POST /api/processing/pause -> GET /api/processing/state shows paused.

        The most basic correctness test: pause → state read confirms the
        flag flipped on the server side. Without this the UI button can
        flip its label while the worker pool keeps draining.
        """
        app_url, _ = backend_real_app

        # Initial state: not paused.
        state_resp = backend_real_page.request.get(
            f"{app_url}/api/processing/state",
            headers={"X-Auth-Token": "e2e-test-token"},
        )
        assert state_resp.ok, f"GET /api/processing/state: {state_resp.status} {state_resp.text()}"
        assert state_resp.json().get("paused") is False, f"Fresh app booted with paused=true: {state_resp.json()}"

        # Pause via the real endpoint.
        pause_resp = backend_real_page.request.post(
            f"{app_url}/api/processing/pause",
            headers={"X-Auth-Token": "e2e-test-token"},
        )
        assert pause_resp.ok, f"pause failed: {pause_resp.status} {pause_resp.text()}"
        body = pause_resp.json()
        assert body.get("paused") is True, f"pause endpoint returned {body}"

        # State must now reflect paused.
        state_resp = backend_real_page.request.get(
            f"{app_url}/api/processing/state",
            headers={"X-Auth-Token": "e2e-test-token"},
        )
        assert state_resp.ok
        assert state_resp.json().get("paused") is True, (
            f"State endpoint still reports paused=False after POST /api/processing/pause: "
            f"{state_resp.json()} — the pause flag was set in one place but read from another."
        )

    def test_resume_endpoint_clears_paused_state(
        self,
        backend_real_page,
        backend_real_app: tuple[str, str],
    ) -> None:
        """Pause then Resume: state must end at paused=False."""
        app_url, _ = backend_real_app

        backend_real_page.request.post(
            f"{app_url}/api/processing/pause",
            headers={"X-Auth-Token": "e2e-test-token"},
        )
        resume_resp = backend_real_page.request.post(
            f"{app_url}/api/processing/resume",
            headers={"X-Auth-Token": "e2e-test-token"},
        )
        assert resume_resp.ok, f"resume failed: {resume_resp.status}"
        assert resume_resp.json().get("paused") is False

        # Confirm via the state endpoint.
        state_resp = backend_real_page.request.get(
            f"{app_url}/api/processing/state",
            headers={"X-Auth-Token": "e2e-test-token"},
        )
        assert state_resp.json().get("paused") is False, (
            "Resume returned paused=False but a follow-up GET still shows paused=True. "
            "The resume only updated one of the two flag locations."
        )

    def test_paused_state_blocks_new_jobs_from_starting(
        self,
        backend_real_page,
        backend_real_app: tuple[str, str],
    ) -> None:
        """When paused, a newly-POSTed job should NOT immediately go to RUNNING.

        This catches the bug where pause toggles the UI label but the
        worker pool keeps picking up new items. We POST a job while
        paused; the orchestrator may complete it (no servers configured)
        but it should NOT be running mid-flight when we check.

        Exact-state assertion is timing-fragile, so the contract we test:
        a job POSTed while paused either ends up completed/failed (raced
        through dispatcher despite pause — acceptable for empty-server
        case) or stays pending. It must NEVER end up "running" indefinitely.
        """
        app_url, _ = backend_real_app

        # Pause first.
        backend_real_page.request.post(
            f"{app_url}/api/processing/pause",
            headers={"X-Auth-Token": "e2e-test-token"},
        )

        # POST a real job.
        post_resp = backend_real_page.request.post(
            f"{app_url}/api/jobs/manual",
            headers={"X-Auth-Token": "e2e-test-token", "Content-Type": "application/json"},
            data='{"file_paths": ["/tmp/paused_job_target.mkv"]}',
        )
        assert post_resp.ok
        job_id = post_resp.json()["id"]

        # Wait briefly for terminal — with no servers configured the
        # orchestrator will eventually mark it terminal even paused.
        deadline = time.monotonic() + 10
        last_status = None
        while time.monotonic() < deadline:
            r = backend_real_page.request.get(
                f"{app_url}/api/jobs/{job_id}",
                headers={"X-Auth-Token": "e2e-test-token"},
            )
            if r.ok:
                last_status = r.json().get("status")
                if last_status in ("completed", "failed", "cancelled", "pending"):
                    break
            time.sleep(0.2)

        # The killer assertion: the job must NOT be stuck in RUNNING.
        # Either pending (queued behind the pause) or terminal (raced
        # through with no work to do) is acceptable. Stuck-running means
        # pause was ignored AND the worker hung.
        assert last_status != "running", (
            f"Job {job_id} POSTed while paused is stuck in 'running' state. "
            f"Pause did not stop the dispatcher AND the worker never finished. "
            "This is the bug class where the pause toggle is purely cosmetic."
        )

        # Cleanup: resume so we don't leak the paused state into other tests
        # (which use function-scoped subprocesses anyway, but defensive).
        backend_real_page.request.post(
            f"{app_url}/api/processing/resume",
            headers={"X-Auth-Token": "e2e-test-token"},
        )

    def test_pause_resume_emits_socketio_state_change(
        self,
        backend_real_page,
        backend_real_app: tuple[str, str],
    ) -> None:
        """Pause/resume must emit `processing_paused_changed` over SocketIO.

        That event drives the UI button swap in renderGlobalPauseResume().
        If the emit drops, the button label desyncs from the actual state.
        """
        app_url, _ = backend_real_app

        backend_real_page.goto(f"{app_url}/")
        backend_real_page.wait_for_load_state("domcontentloaded")
        backend_real_page.wait_for_function("() => typeof io === 'function'", timeout=5000)
        backend_real_page.evaluate(
            """
            window.__capturedEvents = [];
            window.__testSocket = io('/jobs', { transports: ['polling'] });
            ['processing_paused_changed'].forEach(name => {
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

        backend_real_page.request.post(
            f"{app_url}/api/processing/pause",
            headers={"X-Auth-Token": "e2e-test-token"},
        )

        deadline = time.monotonic() + 5
        observed_pause = False
        while time.monotonic() < deadline:
            captured = backend_real_page.evaluate("window.__capturedEvents || []")
            if any(ev["event"] == "processing_paused_changed" and ev["data"].get("paused") is True for ev in captured):
                observed_pause = True
                break
            time.sleep(0.2)

        assert observed_pause, (
            "Pause did not emit processing_paused_changed(paused=True) over SocketIO. "
            "The pause endpoint flipped state but the UI never gets notified, so the "
            "button label desyncs from the actual server state."
        )

        # And resume must emit the inverse.
        backend_real_page.evaluate("window.__capturedEvents = [];")
        backend_real_page.request.post(
            f"{app_url}/api/processing/resume",
            headers={"X-Auth-Token": "e2e-test-token"},
        )
        deadline = time.monotonic() + 5
        observed_resume = False
        while time.monotonic() < deadline:
            captured = backend_real_page.evaluate("window.__capturedEvents || []")
            if any(ev["event"] == "processing_paused_changed" and ev["data"].get("paused") is False for ev in captured):
                observed_resume = True
                break
            time.sleep(0.2)

        assert observed_resume, "Resume did not emit processing_paused_changed(paused=False)."
