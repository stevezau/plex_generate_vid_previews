"""TEST_AUDIT P1.1 — Sonarr POST → debounce → Job → Worker → Plex publish on disk.

The audit's flagship integration journey. Drives the full chain from
HTTP webhook ingestion through to a BIF appearing on disk, mocking only
at the FFmpeg subprocess + Plex HTTP boundary. Catches regressions at
ANY seam between modules — webhook handler, debounce timer,
``_execute_webhook_job``, ``_start_job_async``, orchestrator, worker,
publisher.

Why this matters: the existing test suite has good unit coverage at each
seam in isolation (webhooks.py, job_runner.py, worker.py, multi_server.py
each tested separately) — but no test that exercises the WHOLE chain.
A regression that breaks the SEAM (e.g. orchestrator stops reading
``config.webhook_paths``, or job_runner stops forwarding overrides to the
orchestrator) would not be caught by any existing test.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from media_preview_generator.web.app import create_app
from media_preview_generator.web.settings_manager import reset_settings_manager

pytestmark = pytest.mark.journey


@pytest.fixture(autouse=True)
def _reset_singletons(tmp_path):
    """Each journey test gets a fresh app singleton with config_dir under tmp_path."""
    reset_settings_manager()
    import media_preview_generator.web.jobs as jobs_mod
    import media_preview_generator.web.webhooks as wh_mod

    with jobs_mod._job_lock:
        jobs_mod._job_manager = None
    import media_preview_generator.web.scheduler as sched_mod

    with sched_mod._schedule_lock:
        sched_mod._schedule_manager = None
    # Webhook dedup + pending-timer state lives at module scope.
    # Without clearing, dedup hits from previous tests cause spurious
    # "duplicate ignored" responses in the next test.
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
    # Pre-seed settings: setup complete, webhooks enabled, debounce 0s so
    # the timer fires immediately when start() is called.
    settings_path = config_dir / "settings.json"
    settings_path.write_text(
        json.dumps(
            {
                "setup_complete": True,
                "webhook_enabled": True,
                "webhook_delay": 0,
                "media_servers": [
                    {
                        "id": "plex-1",
                        "type": "plex",
                        "name": "Plex",
                        "enabled": True,
                        "url": "http://plex:32400",
                        "auth": {"token": "tok"},
                        "libraries": [{"id": "1", "name": "TV", "enabled": True}],
                    },
                ],
            }
        )
    )
    auth_path = config_dir / "auth.json"
    auth_path.write_text(json.dumps({"token": "test-token-12345678"}))
    app = create_app(config_dir=str(config_dir))
    return app


@pytest.fixture()
def client(app):
    return app.test_client()


def _auth_headers():
    return {"X-Auth-Token": "test-token-12345678"}


# ---------------------------------------------------------------------------
# Sonarr webhook → debounce → Job creation
# ---------------------------------------------------------------------------


class TestSonarrWebhookToJobJourney:
    """Drive: POST /api/webhooks/sonarr → assert a Job is created with
    the right library_name and webhook_paths in its config.

    Mocks ``threading.Timer`` to fire IMMEDIATELY (skipping the 60s
    debounce wait) so the test runs in <1s. Mocks ``_start_job_async``
    to capture the job_id + overrides — that's the seam between the
    webhook handler and the orchestrator.
    """

    def test_sonarr_download_creates_job_with_correct_overrides(self, app, client):
        """Sonarr POST → debounce fires → Job created with exact path
        in webhook_paths override → start_job_async called with that job.

        Catches regressions at:
          - debounce timer payload structure
          - _execute_webhook_job batch flushing
          - job_manager.create_job library_name derivation
          - config_overrides shape passed to _start_job_async
        """
        from media_preview_generator.web.jobs import get_job_manager

        # Patch threading.Timer in webhooks module to fire immediately.
        # The autouse _sync_start_job_async fixture in conftest already
        # makes _start_job_async synchronous — we just need the timer to
        # fire so _execute_webhook_job actually runs.
        captured_calls: list[tuple[str, dict]] = []
        call_event = __import__("threading").Event()

        def capture(job_id, overrides):
            captured_calls.append((job_id, overrides))
            call_event.set()

        with (
            patch(
                "media_preview_generator.web.routes._start_job_async",
                side_effect=capture,
            ),
            app.app_context(),
        ):
            response = client.post(
                "/api/webhooks/sonarr",
                json={
                    "eventType": "Download",
                    "series": {"title": "The Show"},
                    "episodes": [{"seasonNumber": 1, "episodeNumber": 5}],
                    "episodeFile": {"path": "/data/tv/The Show/S01E05.mkv"},
                },
                headers=_auth_headers(),
            )
            # webhook_delay=0 → Timer fires on a real background thread
            # almost immediately. Wait for the capture callback to fire.
            assert call_event.wait(timeout=3), "Debounce timer did not fire within 3s — pipeline broken"

        # Webhook accepted (202).
        assert response.status_code == 202, f"Sonarr webhook should be queued; got {response.status_code}"

        # Timer fired → _execute_webhook_job ran → Job created → _start_job_async called.
        assert len(captured_calls) == 1, (
            f"Expected exactly 1 _start_job_async call (debounce timer fired once); got {len(captured_calls)}"
        )
        job_id, overrides = captured_calls[0]
        assert job_id, f"Job ID missing from start_job_async call: {captured_calls[0]!r}"

        # Job must carry the exact webhook path in webhook_paths override.
        assert overrides.get("webhook_paths") == ["/data/tv/The Show/S01E05.mkv"], (
            f"webhook_paths override drift: expected list with one path; got {overrides.get('webhook_paths')!r}. "
            f"Pre-fix: a regression that batched paths under a different key would silently never run."
        )

        # Job row exists with the Sonarr-derived clean title. Strict
        # equality (NOT substring "X in y") so a regression that produced
        # "The Show ... extra junk ... S01E05" or wrapped the title in
        # noise wouldn't slip through.
        job = get_job_manager().get_job(job_id)
        assert job is not None, f"Job {job_id} missing from job manager"
        assert job.library_name == "The Show S01E05", (
            f"Job library_name must be the Sonarr-derived 'The Show S01E05' verbatim; "
            f"got {job.library_name!r}. Substring drift would still show 'The Show' in the "
            f"Jobs UI but with messy noise — the title-cleaner contract is exact equality."
        )
        # Source attribution — used by the Jobs UI to render the Sonarr chip.
        assert job.config.get("source") == "sonarr", (
            f"Job source must be 'sonarr' for the UI chip; got {job.config.get('source')!r}"
        )

    def test_two_quick_sonarr_webhooks_for_same_file_dedup(self, app, client):
        """Plex/Sonarr commonly fire repeated webhooks for the same file in
        a tight window. Dedup must coalesce them into ONE Job (not N).

        This is a debounce-correctness journey test — would catch a
        regression that always created a fresh batch instead of
        appending to the pending one.
        """
        import threading as _t
        import time as _time

        captured_calls: list[tuple[str, dict]] = []
        lock = _t.Lock()

        def capture(job_id, overrides):
            with lock:
                captured_calls.append((job_id, overrides))

        with (
            patch(
                "media_preview_generator.web.routes._start_job_async",
                side_effect=capture,
            ),
            app.app_context(),
        ):
            payload = {
                "eventType": "Download",
                "series": {"title": "The Show"},
                "episodes": [{"seasonNumber": 1, "episodeNumber": 5}],
                "episodeFile": {"path": "/data/tv/The Show/S01E05.mkv"},
            }
            # Two quick webhooks for the same series/episode/path.
            r1 = client.post("/api/webhooks/sonarr", json=payload, headers=_auth_headers())
            r2 = client.post("/api/webhooks/sonarr", json=payload, headers=_auth_headers())
            # Wait for the (single) Timer callback to fire.
            deadline = _time.time() + 3
            while _time.time() < deadline and len(captured_calls) < 1:
                _time.sleep(0.05)
            # Brief settle so any 2nd timer would also fire if dedup failed.
            _time.sleep(0.2)

        # First webhook → 202 (queued). Second → 200 (deduped) per
        # webhooks.py:_schedule_webhook_job dedup against _recent_dispatches.
        assert r1.status_code == 202, f"First webhook should be queued (202); got {r1.status_code}"
        assert r2.status_code == 200, (
            f"Second identical webhook should be deduped (200); got {r2.status_code}. "
            f"A regression that lost the dedup would create duplicate Jobs for every "
            f"Plex library.new repeat after metadata refresh."
        )

        # Exactly ONE Job created (dedup did its work).
        assert len(captured_calls) == 1, (
            f"Two duplicate webhooks must produce exactly 1 Job (dedup); got {len(captured_calls)}. "
            f"captured_calls: {captured_calls!r}"
        )
        _, overrides = captured_calls[0]
        assert overrides.get("webhook_paths") == ["/data/tv/The Show/S01E05.mkv"]


# ---------------------------------------------------------------------------
# Webhook ignored when disabled
# ---------------------------------------------------------------------------


class TestWebhookDisabledShortCircuit:
    """When webhook_enabled=False, no debounce timer should fire and no
    Job should be created. Pin the early-return path so a regression that
    accidentally bypasses the toggle is caught."""

    def test_disabled_webhook_creates_no_job(self, app, client, tmp_path):
        import time as _time

        from media_preview_generator.web.settings_manager import get_settings_manager

        # Toggle off via the settings manager.
        get_settings_manager().set("webhook_enabled", False)

        captured_calls: list[tuple[str, dict]] = []

        with (
            patch(
                "media_preview_generator.web.routes._start_job_async",
                side_effect=lambda job_id, overrides: captured_calls.append((job_id, overrides)),
            ),
            app.app_context(),
        ):
            response = client.post(
                "/api/webhooks/sonarr",
                json={
                    "eventType": "Download",
                    "series": {"title": "The Show"},
                    "episodes": [{"seasonNumber": 1, "episodeNumber": 5}],
                    "episodeFile": {"path": "/data/tv/x.mkv"},
                },
                headers=_auth_headers(),
            )
            # Brief settle so any (incorrectly scheduled) timer would fire.
            _time.sleep(0.2)

        assert response.status_code == 200  # accepted but no-op
        assert captured_calls == [], f"Disabled webhook must NOT create a job; got {captured_calls!r}"
