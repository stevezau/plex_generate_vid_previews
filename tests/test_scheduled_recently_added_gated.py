"""Tests for ``_start_recently_added_job_async`` — the gated helper that
replaced the inline ``_run_recently_added_multi_server`` call previously
fired straight from the APScheduler worker thread.

Pre-fix the inline path was the ONE remaining work source that bypassed
the JobGate and skipped Job-row creation entirely (no UI visibility,
no cancellation). The helper closes both gaps: gate-acquire before the
scan runs, real Job row in the JobManager, ``config.source =
"scheduled_recently_added"`` for the source badge.

These tests pin the contract:
  1. The helper creates a Job with the correct shape.
  2. The daemon thread acquires the JobGate before invoking the scan.
  3. The scan is invoked with the kwargs the operator configured.
  4. The gate slot is released on completion.
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _reset_singletons():
    """Drop process-wide singletons between tests so timers / threads
    from one test don't bleed into another."""
    import media_preview_generator.web.job_gate as gate_mod
    import media_preview_generator.web.jobs as jobs_mod
    import media_preview_generator.web.routes.job_runner as jr_mod

    with jobs_mod._job_lock:
        jobs_mod._job_manager = None
    gate_mod.reset_job_gate()
    with jr_mod._inflight_lock:
        jr_mod._inflight_jobs.clear()
    yield
    with jobs_mod._job_lock:
        jobs_mod._job_manager = None
    gate_mod.reset_job_gate()
    with jr_mod._inflight_lock:
        jr_mod._inflight_jobs.clear()


def _wait_for(predicate, timeout=3.0, interval=0.02):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


class TestStartRecentlyAddedJobAsync:
    def test_creates_job_with_scheduled_recently_added_source(self, tmp_path):
        """The Job row must carry ``config.source =
        "scheduled_recently_added"`` so the source-badge palette in app.js
        renders the "Scheduled scan" pill — without this the row appears
        unlabelled in the Job Queue table and the operator can't tell
        what triggered it."""
        import media_preview_generator.web.jobs as jobs_mod
        from media_preview_generator.web.jobs import JobManager
        from media_preview_generator.web.routes.job_runner import (
            _start_recently_added_job_async,
        )

        with jobs_mod._job_lock:
            jobs_mod._job_manager = JobManager(config_dir=str(tmp_path / "config"))
        jm = jobs_mod._job_manager

        scan_called = threading.Event()
        scan_kwargs: dict = {}

        def fake_scan(*args, **kwargs):
            scan_kwargs.update(kwargs)
            scan_called.set()
            return {}

        with (
            patch(
                "media_preview_generator.jobs.orchestrator._run_recently_added_multi_server",
                side_effect=fake_scan,
            ),
            patch("media_preview_generator.config.load_config", return_value=MagicMock()),
            patch(
                "media_preview_generator.web.routes.job_runner._build_selected_gpus",
                return_value=[],
            ),
        ):
            job_id = _start_recently_added_job_async(
                schedule_id="sched-1",
                server_id="plex-1",
                library_ids=["2"],
                lookback_hours=2.0,
                library_name="Recently added: TV",
            )
            assert scan_called.wait(timeout=3.0), "scan must run after gate admission"

        job = jm.get_job(job_id)
        assert job is not None
        assert job.config.get("source") == "scheduled_recently_added", (
            f"Job.config.source must be 'scheduled_recently_added' so the source-badge "
            f"palette in app.js renders the pill; got {job.config.get('source')!r}"
        )
        assert job.config.get("parent_schedule_id") == "sched-1"
        assert job.library_name == "Recently added: TV"
        # Scan was forwarded the right kwargs.
        assert scan_kwargs.get("server_id_filter") == "plex-1"
        assert scan_kwargs.get("library_ids") == ["2"]
        assert scan_kwargs.get("lookback_hours") == 2.0
        assert scan_kwargs.get("job_id") == job_id, (
            "job_id must thread through to the scan so progress callbacks land on this Job's row"
        )

    def test_acquires_gate_before_running_scan(self, tmp_path):
        """The gate must be acquired BEFORE the scan runs — otherwise
        the cap doesn't actually bound concurrent work."""
        import media_preview_generator.web.jobs as jobs_mod
        from media_preview_generator.web.jobs import JobManager
        from media_preview_generator.web.routes.job_runner import (
            _start_recently_added_job_async,
        )

        with jobs_mod._job_lock:
            jobs_mod._job_manager = JobManager(config_dir=str(tmp_path / "config"))

        call_log: list[str] = []
        scan_done = threading.Event()

        gate_mock = MagicMock()
        gate_mock.acquire = MagicMock(side_effect=lambda **kw: call_log.append("acquire") or True)
        gate_mock.release = lambda: call_log.append("release")

        def fake_scan(*args, **kwargs):
            call_log.append("scan")
            scan_done.set()
            return {}

        with (
            patch(
                "media_preview_generator.jobs.orchestrator._run_recently_added_multi_server",
                side_effect=fake_scan,
            ),
            patch("media_preview_generator.config.load_config", return_value=MagicMock()),
            patch(
                "media_preview_generator.web.routes.job_runner._build_selected_gpus",
                return_value=[],
            ),
            patch(
                "media_preview_generator.web.job_gate.get_job_gate",
                return_value=gate_mock,
            ),
        ):
            _start_recently_added_job_async(
                schedule_id="sched-1",
                server_id=None,
                library_ids=None,
                lookback_hours=1.0,
                library_name="Recently added: all libraries",
            )
            assert scan_done.wait(timeout=3.0)
            # Give the finally clause a tick to run release().
            assert _wait_for(lambda: "release" in call_log, timeout=2.0)

        assert "acquire" in call_log and "scan" in call_log and "release" in call_log, (
            f"acquire / scan / release must all run; got {call_log!r}"
        )
        assert call_log.index("acquire") < call_log.index("scan"), (
            f"gate.acquire MUST happen before the scan runs — got {call_log!r}"
        )
        assert call_log.index("scan") < call_log.index("release"), (
            f"gate.release MUST happen after the scan returns — got {call_log!r}"
        )

    def test_cancel_during_gate_wait_skips_scan_and_does_not_release(self, tmp_path):
        """If the user cancels the Job while it's waiting for a gate
        slot, ``acquire`` returns False and the scan must NOT run. The
        Job transitions to CANCELLED; no slot was ever held so release
        must not fire (would leak a slot from the cap)."""
        import media_preview_generator.web.jobs as jobs_mod
        from media_preview_generator.web.jobs import JobManager
        from media_preview_generator.web.routes.job_runner import (
            _start_recently_added_job_async,
        )

        with jobs_mod._job_lock:
            jobs_mod._job_manager = JobManager(config_dir=str(tmp_path / "config"))
        jm = jobs_mod._job_manager

        scan_called = threading.Event()
        gate_mock = MagicMock()
        gate_mock.acquire = MagicMock(return_value=False)
        gate_mock.release = MagicMock()

        def fake_scan(*args, **kwargs):
            scan_called.set()
            return {}

        with (
            patch(
                "media_preview_generator.jobs.orchestrator._run_recently_added_multi_server",
                side_effect=fake_scan,
            ),
            patch("media_preview_generator.config.load_config", return_value=MagicMock()),
            patch(
                "media_preview_generator.web.routes.job_runner._build_selected_gpus",
                return_value=[],
            ),
            patch(
                "media_preview_generator.web.job_gate.get_job_gate",
                return_value=gate_mock,
            ),
        ):
            job_id = _start_recently_added_job_async(
                schedule_id="sched-1",
                server_id=None,
                library_ids=None,
                lookback_hours=1.0,
                library_name="Recently added: all libraries",
            )
            # Let the daemon settle.
            assert _wait_for(
                lambda: jm.get_job(job_id).status.value in ("cancelled", "completed", "failed"),
                timeout=3.0,
            ), f"Job did not settle; status={jm.get_job(job_id).status.value!r}"

        assert not scan_called.is_set(), "scan must NOT run when gate.acquire returns False (cancel-during-wait)"
        assert gate_mock.release.call_count == 0, (
            f"gate.release must NOT fire when no slot was acquired — got {gate_mock.release.call_count} calls"
        )
        # Job should land in CANCELLED, not COMPLETED.
        assert jm.get_job(job_id).status.value == "cancelled"
