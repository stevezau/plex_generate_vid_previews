"""TEST_AUDIT P1.8 — webhook → debounce → JobManager → run_processing.

Drives the full chain on real production wiring. Mocks ONLY:
  - the debounce delay (collapsed to 0s via webhook_delay setting)
  - ``run_processing`` at the orchestrator boundary

Everything in between (HTTP route → ``_authenticate_webhook`` →
``_schedule_webhook_job`` → ``threading.Timer`` → ``_execute_webhook_job``
→ ``JobManager.create_job`` → ``_start_job_async`` → ``run_processing``)
runs unmocked.

Three cases pinned:
  1. Single POST → 1 Job → orchestrator gets the right ``webhook_paths``
  2. Three POSTs for the same path within the dedup window → still 1 Job
     (NOT 3) — pins ``_check_and_record_dedup``
  3. Fire-now: POST that schedules a 60s debounce, then POST to
     ``/api/webhooks/pending/<key>/fire-now`` → timer cancelled and the
     job runs immediately (not 60 s later)
"""

from __future__ import annotations

import json
import threading
import time
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


def _settings_with_debounce(config_dir, debounce_seconds: int, plex_cfg_path: str) -> None:
    settings_path = config_dir / "settings.json"
    settings_path.write_text(
        json.dumps(
            {
                "setup_complete": True,
                "webhook_enabled": True,
                "webhook_delay": debounce_seconds,
                "webhook_retry_count": 3,
                "webhook_retry_delay": 30,
                "media_servers": [
                    {
                        "id": "plex-1",
                        "type": "plex",
                        "name": "Plex Main",
                        "enabled": True,
                        "url": "http://plex:32400",
                        "auth": {"token": "tok"},
                        "libraries": [{"id": "1", "name": "TV", "enabled": True}],
                        "output": {"adapter": "plex_bundle", "plex_config_folder": plex_cfg_path},
                    }
                ],
            }
        )
    )
    auth_path = config_dir / "auth.json"
    auth_path.write_text(json.dumps({"token": "test-token-12345678"}))


def _make_plex_cfg(tmp_path) -> str:
    """Create a plex_config_folder layout that passes _validate_plex_config.

    Without a real plex_config_folder + Media/ subdir, load_config raises
    ConfigValidationError("PLEX_CONFIG_FOLDER is required") and the journey's
    _start_job_async bails before invoking run_processing. The dev .env masks
    this locally; CI has no .env and trips on every push.
    """
    plex_cfg = tmp_path / "plex_cfg"
    (plex_cfg / "Media" / "localhost").mkdir(parents=True, exist_ok=True)
    return str(plex_cfg)


@pytest.fixture()
def app_immediate(tmp_path, monkeypatch):
    """App with debounce = 0s — Timer fires immediately."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    monkeypatch.setenv("CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("WEB_AUTH_TOKEN", "test-token-12345678")
    _settings_with_debounce(config_dir, 0, _make_plex_cfg(tmp_path))
    return create_app(config_dir=str(config_dir))


@pytest.fixture()
def app_debounced(tmp_path, monkeypatch):
    """App with debounce = 60s — used to verify fire-now actually skips the wait."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    monkeypatch.setenv("CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("WEB_AUTH_TOKEN", "test-token-12345678")
    _settings_with_debounce(config_dir, 60, _make_plex_cfg(tmp_path))
    return create_app(config_dir=str(config_dir))


def _auth_headers():
    return {"X-Auth-Token": "test-token-12345678"}


# ---------------------------------------------------------------------------
# Case 1: single Sonarr POST → exactly 1 job → orchestrator invoked
# ---------------------------------------------------------------------------


class TestSingleWebhookProducesOneJob:
    def test_single_sonarr_post_creates_one_job_and_invokes_run_processing(self, app_immediate):
        from media_preview_generator.web.jobs import get_job_manager

        run_calls: list[dict] = []
        run_event = threading.Event()

        def fake_run_processing(config, selected_gpus, **kwargs):
            run_calls.append(
                {
                    "webhook_paths": list(getattr(config, "webhook_paths", []) or []),
                    "job_id": kwargs.get("job_id"),
                }
            )
            run_event.set()
            return {"outcome": {"generated": 1}}

        client = app_immediate.test_client()
        with (
            app_immediate.app_context(),
            patch(
                "media_preview_generator.jobs.orchestrator.run_processing",
                side_effect=fake_run_processing,
            ),
        ):
            response = client.post(
                "/api/webhooks/sonarr",
                json={
                    "eventType": "Download",
                    "series": {"title": "Show A"},
                    "episodes": [{"seasonNumber": 1, "episodeNumber": 5}],
                    "episodeFile": {"path": "/data/tv/Show A/S01E05.mkv"},
                },
                headers=_auth_headers(),
            )
            assert response.status_code == 202
            assert run_event.wait(timeout=3.0), "run_processing was never invoked end-to-end"

        # Exactly one Job — the debounce → batch → create_job chain ran
        # exactly once. A regression that double-fires (e.g. two Timers
        # for one batch) would explode the queue on every Sonarr import.
        all_jobs = get_job_manager().get_all_jobs()
        assert len(all_jobs) == 1, (
            f"Single webhook POST must produce exactly 1 Job; got {len(all_jobs)}. "
            f"Job summaries: {[(j.id, j.library_name) for j in all_jobs]}"
        )

        # run_processing got the exact path from the webhook — pinned via
        # the orchestrator's Config.webhook_paths attribute set by
        # _start_job_async's overrides loop.
        assert len(run_calls) == 1, f"run_processing must be invoked exactly once per Job; got {len(run_calls)}"
        assert run_calls[0]["webhook_paths"] == ["/data/tv/Show A/S01E05.mkv"], (
            f"Webhook path must reach run_processing as Config.webhook_paths verbatim; "
            f"got {run_calls[0]['webhook_paths']!r}"
        )
        assert run_calls[0]["job_id"] == all_jobs[0].id, (
            f"run_processing must be called with the Job ID from JobManager; "
            f"got job_id={run_calls[0]['job_id']!r} but Job is {all_jobs[0].id!r}"
        )


# ---------------------------------------------------------------------------
# Case 2: three POSTs same path → 1 job (dedup wins)
# ---------------------------------------------------------------------------


class TestDuplicateWebhooksDedupToOneJob:
    def test_three_identical_sonarr_posts_collapse_to_single_job(self, app_immediate):
        """Plex re-fires library.new on metadata refresh; Sonarr re-fires
        when the user clicks 'Refresh Episode Info'. A regression that
        loses dedup creates 3+ duplicate Jobs per imported file — the
        Job queue UI would fill with redundant work.

        Pin: the SECOND and THIRD POSTs return 200 (deduped) and only
        the FIRST POST creates a Job. ``_recent_dispatches`` is the
        guard, ``_check_and_record_dedup`` is the function."""
        from media_preview_generator.web.jobs import get_job_manager

        run_event = threading.Event()
        run_count = {"n": 0}

        def fake_run_processing(config, selected_gpus, **kwargs):
            run_count["n"] += 1
            run_event.set()
            return {"outcome": {"generated": 1}}

        payload = {
            "eventType": "Download",
            "series": {"title": "Show B"},
            "episodes": [{"seasonNumber": 1, "episodeNumber": 5}],
            "episodeFile": {"path": "/data/tv/Show B/S01E05.mkv"},
        }
        client = app_immediate.test_client()
        with (
            app_immediate.app_context(),
            patch(
                "media_preview_generator.jobs.orchestrator.run_processing",
                side_effect=fake_run_processing,
            ),
        ):
            r1 = client.post("/api/webhooks/sonarr", json=payload, headers=_auth_headers())
            r2 = client.post("/api/webhooks/sonarr", json=payload, headers=_auth_headers())
            r3 = client.post("/api/webhooks/sonarr", json=payload, headers=_auth_headers())
            assert run_event.wait(timeout=3.0), "First webhook never produced a Job"
            # Brief settle so any second/third (incorrectly scheduled) timer would also fire.
            time.sleep(0.2)

        # First should queue (202); subsequent identical POSTs deduped (200).
        assert r1.status_code == 202, f"First identical POST must queue (202); got {r1.status_code}"
        assert r2.status_code == 200, (
            f"Second identical POST must dedup (200); got {r2.status_code}. "
            f"Without dedup, repeated Sonarr/Plex events fan out into duplicate Jobs."
        )
        assert r3.status_code == 200, f"Third identical POST must dedup (200); got {r3.status_code}"

        all_jobs = get_job_manager().get_all_jobs()
        assert len(all_jobs) == 1, (
            f"Three identical webhooks must collapse to exactly 1 Job (dedup); got {len(all_jobs)}. "
            f"Jobs: {[(j.id, j.library_name) for j in all_jobs]}"
        )
        assert run_count["n"] == 1, (
            f"run_processing must be invoked exactly once for the deduped batch; got {run_count['n']}"
        )


# ---------------------------------------------------------------------------
# Case 3: Fire-now skips the 60s wait
# ---------------------------------------------------------------------------


class TestFireNowSkipsDebounce:
    def test_fire_now_cancels_timer_and_runs_job_immediately(self, app_debounced):
        """A 60 s debounce timer is queued. Then POST to
        ``/api/webhooks/pending/<key>/fire-now`` — must cancel the timer
        AND dispatch the batch synchronously so the Job appears in
        JobManager within the test's wait window (way under 60s).

        Pin: a regression that left the timer running OR called
        ``_execute_webhook_job`` twice (once via the timer, once via
        fire-now) would create extra duplicate Jobs."""
        from media_preview_generator.web.jobs import get_job_manager
        from media_preview_generator.web.webhooks import _pending_batches, _pending_timers

        run_event = threading.Event()
        run_calls: list[dict] = []

        def fake_run_processing(config, selected_gpus, **kwargs):
            run_calls.append({"webhook_paths": list(getattr(config, "webhook_paths", []) or [])})
            run_event.set()
            return {"outcome": {"generated": 1}}

        client = app_debounced.test_client()
        with (
            app_debounced.app_context(),
            patch(
                "media_preview_generator.jobs.orchestrator.run_processing",
                side_effect=fake_run_processing,
            ),
        ):
            r = client.post(
                "/api/webhooks/sonarr",
                json={
                    "eventType": "Download",
                    "series": {"title": "Show C"},
                    "episodes": [{"seasonNumber": 1, "episodeNumber": 5}],
                    "episodeFile": {"path": "/data/tv/Show C/S01E05.mkv"},
                },
                headers=_auth_headers(),
            )
            assert r.status_code == 202

            # Verify the debounce queued a timer and a batch under "sonarr" key.
            assert "sonarr" in _pending_timers, (
                f"After webhook POST, _pending_timers must contain key 'sonarr'; "
                f"got keys={list(_pending_timers.keys())!r}. Debounce-schedule path is broken."
            )
            assert "sonarr" in _pending_batches

            # Fire-now: skip the 60s wait.
            fire_response = client.post(
                "/api/webhooks/pending/sonarr/fire-now",
                headers=_auth_headers(),
            )
            assert fire_response.status_code == 202, (
                f"fire-now should return 202 when the batch was found; got {fire_response.status_code}"
            )

            # Job must materialise way under 60 s (we wait at most 3 s).
            assert run_event.wait(timeout=3.0), (
                "fire-now did not result in run_processing being invoked within 3s. "
                "Either the timer wasn't cancelled and the synchronous dispatch path is broken, "
                "OR the fire-now endpoint failed silently."
            )

        all_jobs = get_job_manager().get_all_jobs()
        # Critical: only ONE Job. If the original 60s timer ALSO fired
        # (because fire-now didn't cancel it), we'd get 2 jobs.
        assert len(all_jobs) == 1, (
            f"fire-now must cancel the original timer so only 1 Job is created; got {len(all_jobs)}. "
            f"Jobs: {[(j.id, j.library_name) for j in all_jobs]}"
        )
        assert len(run_calls) == 1, (
            f"run_processing must be invoked exactly once after fire-now; got {len(run_calls)}. "
            f"More than 1 means the cancelled timer ALSO fired."
        )

        # Pending state cleared after fire-now drained it.
        assert "sonarr" not in _pending_batches, (
            "After fire-now, the batch must be removed from _pending_batches; "
            f"got keys={list(_pending_batches.keys())!r}"
        )
        assert "sonarr" not in _pending_timers, (
            f"After fire-now, the timer must be removed from _pending_timers; got keys={list(_pending_timers.keys())!r}"
        )

    def test_fire_now_unknown_key_returns_404(self, app_debounced):
        """Pin the negative case: fire-now on a non-existent key returns
        404 (NOT 500, NOT 202). A regression that swallows the missing
        key and returns success would let the UI think a job ran."""
        client = app_debounced.test_client()
        with app_debounced.app_context():
            r = client.post(
                "/api/webhooks/pending/no-such-key/fire-now",
                headers=_auth_headers(),
            )
        assert r.status_code == 404, (
            f"fire-now for unknown key must return 404; got {r.status_code}. Body: {r.get_data(as_text=True)!r}"
        )
