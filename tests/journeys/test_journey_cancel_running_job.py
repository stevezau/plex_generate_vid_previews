"""TEST_AUDIT P1.9 — cancel a running Job mid-flight.

User journey: a job is dispatched, ``run_processing`` is in the middle
of work (we model this by having the fake orchestrator block on a
threading Event), the user clicks Cancel on the Jobs page → POST
``/api/jobs/<id>/cancel`` → cancellation flag must propagate to the
orchestrator's ``cancel_check`` and the job ends in ``CANCELLED`` state
(NOT ``FAILED``).

Pinned regressions:
  - ``cancel_check`` callable wired through to ``run_processing`` (a
    regression that drops it leaves cancel as a no-op until the job
    finishes naturally — symptom: "Cancel button does nothing for
    8-hour scans")
  - Final state ``CANCELLED`` not ``FAILED``: the dashboard renders
    these very differently (yellow vs red), and a "cancelled because
    user clicked stop" should not look like a bug-class hard failure
  - Cancel cleans up: ``is_cancellation_requested`` flag is cleared
    from the JobManager so the next job for this id (e.g. retry) doesn't
    inherit the stale cancel signal
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


@pytest.fixture()
def app(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    monkeypatch.setenv("CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("WEB_AUTH_TOKEN", "test-token-12345678")
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
                        "name": "Plex Main",
                        "enabled": True,
                        "url": "http://plex:32400",
                        "auth": {"token": "tok"},
                        "libraries": [{"id": "1", "name": "Movies", "enabled": True}],
                    }
                ],
            }
        )
    )
    auth_path = config_dir / "auth.json"
    auth_path.write_text(json.dumps({"token": "test-token-12345678"}))
    return create_app(config_dir=str(config_dir))


def _auth_headers():
    return {"X-Auth-Token": "test-token-12345678"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _wait_until(predicate, timeout=3.0, interval=0.02):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


# ---------------------------------------------------------------------------
# Cancel mid-flight: real wiring
# ---------------------------------------------------------------------------


class TestCancelRunningJob:
    """Drive a real job through ``_start_job_async``; the fake orchestrator
    blocks until either the cancel_check returns True or a hard timeout
    fires. This is the only branch where the `cancel_check` callable
    actually matters in production — long-running scans poll it every few
    seconds."""

    @pytest.mark.real_job_async
    def test_cancel_propagates_to_cancel_check_and_terminates_job_as_cancelled(self, app):
        from media_preview_generator.web.jobs import JobStatus, get_job_manager
        from media_preview_generator.web.routes.job_runner import _start_job_async

        # Used by the test to know that the orchestrator started running
        # so we can fire the cancel API call against a job that's actually
        # in flight.
        run_started = threading.Event()
        cancel_seen = threading.Event()
        cancel_check_polls: list[bool] = []

        def fake_run_processing(config, selected_gpus, **kwargs):
            cancel_check = kwargs.get("cancel_check")
            assert callable(cancel_check), (
                "run_processing must receive a callable cancel_check kwarg "
                "from _start_job_async; otherwise no cancel API request can stop a job. "
                f"Got cancel_check={cancel_check!r}"
            )
            run_started.set()
            # Poll cancel_check on a tight loop, just like the real
            # orchestrator does between items.
            deadline = time.time() + 5.0
            while time.time() < deadline:
                value = bool(cancel_check())
                cancel_check_polls.append(value)
                if value:
                    cancel_seen.set()
                    return {"cancelled": True, "outcome": {"generated": 0}}
                time.sleep(0.02)
            # Should never get here in this test
            raise AssertionError("cancel_check never returned True within 5s")

        client = app.test_client()
        with (
            app.app_context(),
            patch(
                "media_preview_generator.jobs.orchestrator.run_processing",
                side_effect=fake_run_processing,
            ),
        ):
            # The @real_job_async marker on this test opts out of the
            # synchronous _start_job_async shim — so _start_job_async()
            # spawns a real daemon thread for run_job and returns. We
            # can then fire the cancel API while run_job is still in
            # flight inside the orchestrator's polling loop.
            job = get_job_manager().create_job(library_name="Movies", config={})
            _start_job_async(job.id, None)

            # Wait for the orchestrator to be actively polling cancel_check.
            assert run_started.wait(timeout=3.0), (
                "Orchestrator was never invoked within 3s — _start_job_async wiring or "
                "the synchronous shim leaked into a test that needs real async dispatch."
            )

            # NOW fire the cancel — orchestrator must observe it on its next poll.
            cancel_response = client.post(
                f"/api/jobs/{job.id}/cancel",
                headers=_auth_headers(),
            )
            assert cancel_response.status_code == 200, (
                f"Cancel API must return 200; got {cancel_response.status_code}. "
                f"Body: {cancel_response.get_data(as_text=True)!r}"
            )

            # Cancel propagated to the orchestrator within 2s.
            assert cancel_seen.wait(timeout=2.0), (
                "cancel_check callable did not return True within 2s of the cancel API call. "
                "The cancellation flag set by request_cancellation isn't reaching the "
                "lambda passed to run_processing. Production symptom: 'Cancel button does "
                "nothing for long-running scans'."
            )

            # Give the daemon run_job thread time to drain after cancel.
            assert _wait_until(
                lambda: get_job_manager().get_job(job.id).status.value == "cancelled",
                timeout=5.0,
            ), "Job did not transition to CANCELLED within 5s of cancel API call"

        # Final job state: CANCELLED, not FAILED. The dashboard renders
        # these very differently (yellow vs red) and a user-cancelled job
        # is not a bug-class failure.
        final_job = get_job_manager().get_job(job.id)
        assert final_job is not None
        assert final_job.status is JobStatus.CANCELLED, (
            f"Cancelled job must end in JobStatus.CANCELLED, not {final_job.status}. "
            f"A regression here surfaces every user-cancel as a red 'Failed' badge — "
            f"misleading the operator into investigating a bug that was actually a deliberate stop."
        )

        # cancel_check polled at least once and observed True at the end.
        # An empty list means the orchestrator never even called cancel_check.
        assert cancel_check_polls, "cancel_check was never polled — wiring contract broken"
        assert cancel_check_polls[-1] is True, (
            f"Last poll of cancel_check must be True (cancel signal was set); polls trail = {cancel_check_polls[-5:]!r}"
        )

        # Cleanup: cancellation flag cleared so a subsequent job for this id
        # (e.g. a retry) doesn't inherit the stale cancel signal. The flag
        # is cleared in run_job's finally block, which runs after our cancel
        # propagation observed True — give the daemon a brief moment to
        # finish that cleanup.
        assert _wait_until(
            lambda: not get_job_manager().is_cancellation_requested(job.id),
            timeout=3.0,
        ), (
            "After cancel completes, is_cancellation_requested must clear so any "
            "subsequent retry for this job id doesn't immediately self-cancel. "
            "Production: run_job's finally block calls clear_cancellation_flag."
        )
