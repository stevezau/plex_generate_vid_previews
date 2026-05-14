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
import requests

_AUTH_HEADERS = {"X-Auth-Token": "e2e-test-token"}
_AUTH_JSON_HEADERS = {"X-Auth-Token": "e2e-test-token", "Content-Type": "application/json"}
_API_TIMEOUT = 60


def _post_sonarr_webhook(app_url: str, file_path: str, series_title: str = "Test Series"):
    """POST a minimal valid Sonarr Download payload."""
    payload = {
        "eventType": "Download",
        "series": {"title": series_title, "path": "/data/TV/Test Series"},
        "episodes": [{"seasonNumber": 1, "episodeNumber": 2, "title": "Test Episode"}],
        "episodeFile": {"path": file_path, "relativePath": "S01E02.mkv"},
    }
    # Webhook endpoints use _authenticate_webhook which accepts X-Auth-Token
    # against either the configured webhook_secret OR the app auth token.
    # Use requests (not page.request) to avoid the Playwright IPC stall.
    resp = requests.post(
        f"{app_url}/api/webhooks/sonarr",
        headers=_AUTH_JSON_HEADERS,
        data=json.dumps(payload),
        timeout=_API_TIMEOUT,
    )
    return resp


@pytest.mark.e2e
class TestWebhookToDashboard:
    # webhook_delay=30 (override default seed of 1s) — this test polls
    # /api/webhooks/pending and races the debounce Timer. With delay=1s
    # the batch can fire + pop from _pending_batches before the test's
    # first GET sees it (Playwright roundtrip + JSON parse can exceed
    # 1s on a loaded runner), turning the assertion into a Heisenbug.
    # The test calls fire-now to skip the wait anyway, so 30s is harmless.
    @pytest.mark.parametrize("backend_real_app", [{"webhook_delay": 30}], indirect=True)
    def test_sonarr_webhook_creates_pending_batch_then_fire_now_dispatches(
        self,
        backend_real_app: tuple[str, str],
    ) -> None:
        app_url, _ = backend_real_app

        # Observe job creation via /api/jobs polling instead of browser-side
        # SocketIO subscription. Each page.evaluate() roundtrip uses the
        # Playwright Python↔Node IPC pipe; polling that pipe in a loop is
        # what crashed xdist workers under -n auto. The SocketIO emit
        # itself is verified in test_journey_live_job_lifecycle.py — here
        # we only need to observe that a job materialised, which the
        # backend's /api/jobs response gives us directly.

        # 1. POST a real Sonarr-shaped payload to the real webhook endpoint.
        webhook_path = "/data/TV/Test Series/S01E02.mkv"
        resp = _post_sonarr_webhook(app_url, webhook_path)
        assert resp.ok, f"Sonarr webhook returned {resp.status_code}: {resp.text}"

        # 2. The batch should appear in /api/webhooks/pending within a few
        #    ticks (this test uses webhook_delay=30s via parametrize so the
        #    natural Timer can't race the polling loop).
        deadline = time.monotonic() + 5
        pending_batch = None
        while time.monotonic() < deadline:
            r = requests.get(
                f"{app_url}/api/webhooks/pending",
                headers=_AUTH_HEADERS,
                timeout=_API_TIMEOUT,
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

        fire_resp = requests.post(
            f"{app_url}/api/webhooks/pending/{quote(debounce_key, safe='')}/fire-now",
            headers=_AUTH_HEADERS,
            timeout=_API_TIMEOUT,
        )
        assert fire_resp.status_code in (200, 202), f"fire-now: {fire_resp.status_code} {fire_resp.text}"

        # 4. A new job should appear in /api/jobs (polling, no SocketIO).
        # Filter is_retry=False — when no servers are configured the
        # dispatcher fast-skips the path and the retry-queue spawns a
        # child retry job with the same webhook_paths. The contract we're
        # verifying is the PARENT (the one fire-now spawned), not its
        # retry child.
        deadline = time.monotonic() + 15
        job_id = None
        while time.monotonic() < deadline:
            jobs_resp = requests.get(
                f"{app_url}/api/jobs?page=0",
                headers=_AUTH_HEADERS,
                timeout=_API_TIMEOUT,
            )
            if jobs_resp.ok:
                for job in jobs_resp.json().get("jobs", []):
                    cfg = job.get("config") or {}
                    if webhook_path in cfg.get("webhook_paths", []) and not cfg.get("is_retry"):
                        job_id = job.get("id")
                        break
            if job_id:
                break
            time.sleep(0.2)

        assert job_id is not None, (
            "fire-now dispatched but no parent Job ever appeared in /api/jobs for the webhook path. "
            "Either the webhook batch dispatcher silently dropped the job, or JobManager "
            "didn't record it."
        )

        # 5. Job should reach a terminal state (no servers configured -> the
        #    orchestrator marks unresolved and completes/fails fast).
        # 10s budget — global pytest timeout is 30s; steps 1-4 already
        # consumed ~5-10s. Empty-server jobs reach terminal in <1s typically.
        deadline = time.monotonic() + 10
        terminal = False
        while time.monotonic() < deadline:
            r = requests.get(
                f"{app_url}/api/jobs/{job_id}",
                headers=_AUTH_HEADERS,
                timeout=_API_TIMEOUT,
            )
            if r.ok and r.json().get("status") in ("completed", "failed", "cancelled"):
                terminal = True
                break
            time.sleep(0.25)
        assert terminal, f"Job {job_id} created from webhook never reached terminal state."

    def test_natural_debounce_fires_without_explicit_fire_now(
        self,
        backend_real_app: tuple[str, str],
    ) -> None:
        """Without clicking fire-now, the 1s debounce timer must still fire.

        Catches the bug class where the threading.Timer is created but never
        triggers (callback raises silently inside the timer thread).
        """
        app_url, _ = backend_real_app

        # POST the webhook and DO NOT call fire-now. Observe via /api/jobs
        # polling — no browser SocketIO subscription (would crash xdist
        # workers under -n auto via Playwright IPC).
        webhook_path = "/data/TV/Show Two/S02E03.mkv"
        resp = _post_sonarr_webhook(
            app_url,
            webhook_path,
            series_title="Show Two",
        )
        assert resp.ok

        # With webhook_delay=1 (seeded), the natural timer should fire
        # within ~3s and produce a parent Job visible in /api/jobs.
        # is_retry=False filter — see test above for rationale.
        deadline = time.monotonic() + 8
        observed = False
        while time.monotonic() < deadline:
            jobs_resp = requests.get(
                f"{app_url}/api/jobs?page=0",
                headers=_AUTH_HEADERS,
                timeout=_API_TIMEOUT,
            )
            if jobs_resp.ok:
                for job in jobs_resp.json().get("jobs", []):
                    cfg = job.get("config") or {}
                    if webhook_path in cfg.get("webhook_paths", []) and not cfg.get("is_retry"):
                        observed = True
                        break
            if observed:
                break
            time.sleep(0.2)

        assert observed, (
            "Real debounce timer (webhook_delay=1s) never fired and created a job. "
            "This is the silent-failure bug class: webhook accepted, batch queued, "
            "Timer.start() called, but the callback never ran."
        )
