"""Backend-real E2E: Sonarr webhook -> debounce -> job appears in dashboard.

The audit calls webhook auto-fire "the most-shipped silent-failure surface."
This test POSTs a real Sonarr-shaped payload to the real /webhooks/sonarr
endpoint, then asserts the debounced batch appears under /api/webhooks/pending,
fires it via /api/webhooks/pending/<key>/fire-now, and verifies the resulting
job materialises end-to-end.

The seeded settings.json sets ``webhook_delay: 1`` so the natural debounce
window is short. We also exercise fire-now to skip the wait entirely.
"""

from __future__ import annotations

import json
import time

import pytest


def _post_sonarr_webhook(page, app_url: str, file_path: str, series_title: str = "Test Series") -> None:
    """POST a minimal valid Sonarr Download payload."""
    payload = {
        "eventType": "Download",
        "series": {"title": series_title, "path": "/data/TV/Test Series"},
        "episodes": [{"seasonNumber": 1, "episodeNumber": 2, "title": "Test Episode"}],
        "episodeFile": {"path": file_path, "relativePath": "S01E02.mkv"},
    }
    # Webhook endpoints use _authenticate_webhook which accepts X-Auth-Token
    # against either the configured webhook_secret OR the app auth token.
    resp = page.request.post(
        f"{app_url}/api/webhooks/sonarr",
        headers={
            "Content-Type": "application/json",
            "X-Auth-Token": "e2e-test-token",
        },
        data=json.dumps(payload),
    )
    return resp


@pytest.mark.e2e
class TestWebhookToDashboard:
    def test_sonarr_webhook_creates_pending_batch_then_fire_now_dispatches(
        self,
        backend_real_page,
        backend_real_app: tuple[str, str],
    ) -> None:
        app_url, _ = backend_real_app

        # Open dashboard so SocketIO is connected and we can observe job_created.
        # Open our own SocketIO connection — the dashboard's `socket` var is
        # module-scoped and inaccessible from the page evaluate context.
        backend_real_page.goto(f"{app_url}/")
        backend_real_page.wait_for_load_state("domcontentloaded")
        backend_real_page.wait_for_function("() => typeof io === 'function'", timeout=5000)
        backend_real_page.evaluate(
            """
            window.__capturedEvents = [];
            window.__testSocket = io('/jobs', { transports: ['polling'] });
            ['job_created', 'job_started', 'job_completed', 'job_failed'].forEach(name => {
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

        # 1. POST a real Sonarr-shaped payload to the real webhook endpoint.
        webhook_path = "/data/TV/Test Series/S01E02.mkv"
        resp = _post_sonarr_webhook(backend_real_page, app_url, webhook_path)
        assert resp.ok, f"Sonarr webhook returned {resp.status}: {resp.text()}"

        # 2. The batch should appear in /api/webhooks/pending within a few
        #    ticks (the debounce window is 1s by seeding).
        deadline = time.monotonic() + 5
        pending_batch = None
        while time.monotonic() < deadline:
            r = backend_real_page.request.get(
                f"{app_url}/api/webhooks/pending",
                headers={"X-Auth-Token": "e2e-test-token"},
            )
            if r.ok:
                body = r.json()
                if body.get("pending"):
                    pending_batch = body["pending"][0]
                    break
            time.sleep(0.1)

        assert pending_batch is not None, (
            "Real /webhooks/pending never showed the Sonarr batch. The webhook either "
            "rejected the payload or the debounce queue isn't recording it."
        )
        assert pending_batch["file_count"] == 1, f"Pending batch wrong shape: {pending_batch}"
        assert pending_batch["source"] == "sonarr", f"Wrong source: {pending_batch}"
        debounce_key = pending_batch["key"]

        # 3. Fire-now to skip the (already short) debounce wait.
        from urllib.parse import quote

        fire_resp = backend_real_page.request.post(
            f"{app_url}/api/webhooks/pending/{quote(debounce_key, safe='')}/fire-now",
            headers={"X-Auth-Token": "e2e-test-token"},
        )
        assert fire_resp.status in (200, 202), f"fire-now: {fire_resp.status} {fire_resp.text()}"

        # 4. A new job should appear via SocketIO + via the jobs list.
        deadline = time.monotonic() + 15
        job_id = None
        while time.monotonic() < deadline:
            captured = backend_real_page.evaluate("window.__capturedEvents || []")
            for ev in captured:
                if ev["event"] == "job_created":
                    job_id = ev["data"].get("id")
                    break
            if job_id:
                break
            time.sleep(0.2)

        assert job_id is not None, (
            "fire-now dispatched but the dashboard never observed a job_created event. "
            "Either the webhook batch dispatcher silently dropped the job, or SocketIO "
            "didn't relay it. Captured: " + str(backend_real_page.evaluate("window.__capturedEvents || []"))
        )

        # 5. Job should reach a terminal state (no servers configured -> the
        #    orchestrator marks unresolved and completes/fails fast).
        deadline = time.monotonic() + 30
        terminal = False
        while time.monotonic() < deadline:
            r = backend_real_page.request.get(
                f"{app_url}/api/jobs/{job_id}",
                headers={"X-Auth-Token": "e2e-test-token"},
            )
            if r.ok and r.json().get("status") in ("completed", "failed", "cancelled"):
                terminal = True
                break
            time.sleep(0.25)
        assert terminal, f"Job {job_id} created from webhook never reached terminal state."

    def test_natural_debounce_fires_without_explicit_fire_now(
        self,
        backend_real_page,
        backend_real_app: tuple[str, str],
    ) -> None:
        """Without clicking fire-now, the 1s debounce timer must still fire.

        Catches the bug class where the threading.Timer is created but never
        triggers (callback raises silently inside the timer thread).
        """
        app_url, _ = backend_real_app

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

        # POST the webhook and DO NOT call fire-now.
        resp = _post_sonarr_webhook(
            backend_real_page,
            app_url,
            "/data/TV/Show Two/S02E03.mkv",
            series_title="Show Two",
        )
        assert resp.ok

        # With webhook_delay=1 (seeded), the natural timer should fire within ~3s.
        deadline = time.monotonic() + 8
        observed = False
        while time.monotonic() < deadline:
            captured = backend_real_page.evaluate("window.__capturedEvents || []")
            if any(ev["event"] == "job_created" for ev in captured):
                observed = True
                break
            time.sleep(0.2)

        assert observed, (
            "Real debounce timer (webhook_delay=1s) never fired and created a job. "
            "This is the silent-failure bug class: webhook accepted, batch queued, "
            "Timer.start() called, but the callback never ran."
        )
