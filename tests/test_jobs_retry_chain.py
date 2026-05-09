"""Tests for JobManager.upsert_retry_chain_job — the user-visible
retry-chain Job row that surfaces the headless retry queue's progress.

Pre-fix the retry queue was ENTIRELY headless: retries fired in the
background with no UI representation. The user couldn't see "this is
still working" — they had to trust the logs. The retry-chain Job
machinery puts each in-flight retry chain on the Jobs panel as a
single row that updates in place across attempts and sorts to the top.

Matrix coverage per .claude/rules/testing.md:
  * outcome: scheduled / running / completed / exhausted
  * first call (creates) vs subsequent calls (updates same row)
  * stable ID derivation (same canonical_path → same chain ID)
  * created_at bump on each update (sort-to-top)
  * retry_eta + retry_wait_total set/cleared appropriately
  * server attribution forwarded
  * config aliases (is_retry, max_retries) for existing UI rendering
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

from media_preview_generator.web.jobs import JobManager, JobStatus


@pytest.fixture(autouse=True)
def _reset_job_manager():
    import media_preview_generator.web.jobs as jobs_mod

    with jobs_mod._job_lock:
        jobs_mod._job_manager = None
    yield
    with jobs_mod._job_lock:
        jobs_mod._job_manager = None


@pytest.fixture
def jm(tmp_path):
    config_dir = tmp_path / "config"
    os.makedirs(config_dir, exist_ok=True)
    return JobManager(config_dir=str(config_dir))


class TestUpsertRetryChainJob:
    def test_first_call_creates_row_with_stable_retry_prefixed_id(self, jm):
        path = "/data/Movies/Foo.mkv"
        job = jm.upsert_retry_chain_job(
            canonical_path=path,
            basename="Foo.mkv",
            attempt=1,
            max_attempts=5,
            next_run_at=(datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat(),
            wait_seconds=30,
            outcome="scheduled",
        )
        assert job.id.startswith("retry-")
        assert len(job.id) == len("retry-") + 16  # 16-char SHA prefix
        assert job.status == JobStatus.PENDING
        assert job.library_name == "Foo.mkv"
        assert job.config["is_retry_chain"] is True
        assert job.config["retry_chain_for"] == path
        assert job.config["retry_attempt"] == 1
        assert job.config["retry_max_attempts"] == 5

    def test_same_canonical_path_yields_same_chain_id(self, jm):
        """Different upsert calls for the same path must update the SAME row."""
        path = "/data/Movies/Foo.mkv"
        job1 = jm.upsert_retry_chain_job(
            canonical_path=path,
            basename="Foo.mkv",
            attempt=1,
            max_attempts=5,
            next_run_at=None,
            wait_seconds=30,
            outcome="scheduled",
        )
        job2 = jm.upsert_retry_chain_job(
            canonical_path=path,
            basename="Foo.mkv",
            attempt=2,
            max_attempts=5,
            next_run_at=None,
            wait_seconds=120,
            outcome="scheduled",
        )
        assert job1.id == job2.id, "Same canonical_path must derive the same chain ID"
        assert job2.config["retry_attempt"] == 2  # update in place
        # Only one Job row exists for this chain.
        all_jobs = jm.get_all_jobs()
        chain_jobs = [j for j in all_jobs if j.id.startswith("retry-")]
        assert len(chain_jobs) == 1

    def test_different_canonical_paths_yield_different_chain_ids(self, jm):
        a = jm.upsert_retry_chain_job(
            canonical_path="/data/A.mkv",
            basename="A.mkv",
            attempt=1,
            max_attempts=5,
            next_run_at=None,
            wait_seconds=30,
            outcome="scheduled",
        )
        b = jm.upsert_retry_chain_job(
            canonical_path="/data/B.mkv",
            basename="B.mkv",
            attempt=1,
            max_attempts=5,
            next_run_at=None,
            wait_seconds=30,
            outcome="scheduled",
        )
        assert a.id != b.id

    # ------------------------------------------------------------------
    # State machine
    # ------------------------------------------------------------------

    def test_outcome_scheduled_sets_pending_with_countdown(self, jm):
        eta = (datetime.now(timezone.utc) + timedelta(seconds=120)).isoformat()
        job = jm.upsert_retry_chain_job(
            canonical_path="/x.mkv",
            basename="x.mkv",
            attempt=2,
            max_attempts=5,
            next_run_at=eta,
            wait_seconds=120,
            outcome="scheduled",
        )
        assert job.status == JobStatus.PENDING
        assert job.progress.retry_eta == eta
        assert job.progress.retry_wait_total == 120
        assert job.error is None

    def test_outcome_running_clears_countdown(self, jm):
        # First scheduled, then running
        jm.upsert_retry_chain_job(
            canonical_path="/x.mkv",
            basename="x.mkv",
            attempt=1,
            max_attempts=5,
            next_run_at=(datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat(),
            wait_seconds=30,
            outcome="scheduled",
        )
        job = jm.upsert_retry_chain_job(
            canonical_path="/x.mkv",
            basename="x.mkv",
            attempt=1,
            max_attempts=5,
            next_run_at=None,
            wait_seconds=None,
            outcome="running",
        )
        assert job.status == JobStatus.RUNNING
        assert job.progress.retry_eta is None, (
            "Countdown must clear when retry fires — otherwise UI keeps "
            "showing 'next in Xs' while the dispatch is actually running."
        )
        assert job.progress.retry_wait_total is None
        assert job.started_at is not None

    def test_outcome_completed_sets_completed_status(self, jm):
        jm.upsert_retry_chain_job(
            canonical_path="/x.mkv",
            basename="x.mkv",
            attempt=1,
            max_attempts=5,
            next_run_at=None,
            wait_seconds=None,
            outcome="running",
        )
        job = jm.upsert_retry_chain_job(
            canonical_path="/x.mkv",
            basename="x.mkv",
            attempt=2,
            max_attempts=5,
            next_run_at=None,
            wait_seconds=None,
            outcome="completed",
        )
        assert job.status == JobStatus.COMPLETED
        assert job.completed_at is not None
        assert job.error is None
        assert job.progress.retry_eta is None

    def test_outcome_exhausted_sets_failed_with_reason(self, jm):
        job = jm.upsert_retry_chain_job(
            canonical_path="/x.mkv",
            basename="x.mkv",
            attempt=5,
            max_attempts=5,
            next_run_at=None,
            wait_seconds=None,
            outcome="exhausted",
            reason="Server still hadn't indexed after 5 attempts",
        )
        assert job.status == JobStatus.FAILED
        assert job.completed_at is not None
        assert "5 attempts" in (job.error or "")
        assert job.progress.retry_eta is None

    def test_exhausted_without_reason_falls_back_to_default(self, jm):
        job = jm.upsert_retry_chain_job(
            canonical_path="/x.mkv",
            basename="x.mkv",
            attempt=5,
            max_attempts=5,
            next_run_at=None,
            wait_seconds=None,
            outcome="exhausted",
        )
        assert job.error is not None
        assert "5" in job.error  # mentions attempt count

    # ------------------------------------------------------------------
    # Sort-to-top behaviour
    # ------------------------------------------------------------------

    def test_created_at_bumps_on_each_update(self, jm):
        """For sort-to-top: each upsert refreshes created_at so the row
        floats to the front of the 'newest first' Jobs list."""
        import time

        job_v1 = jm.upsert_retry_chain_job(
            canonical_path="/x.mkv",
            basename="x.mkv",
            attempt=1,
            max_attempts=5,
            next_run_at=None,
            wait_seconds=30,
            outcome="scheduled",
        )
        first_created = job_v1.created_at
        time.sleep(0.01)
        job_v2 = jm.upsert_retry_chain_job(
            canonical_path="/x.mkv",
            basename="x.mkv",
            attempt=2,
            max_attempts=5,
            next_run_at=None,
            wait_seconds=120,
            outcome="scheduled",
        )
        assert job_v2.created_at > first_created, (
            "created_at must bump on each upsert so the row sorts to top of "
            "newest-first Jobs list — otherwise it gets buried under newer jobs."
        )
        # Original chain start is preserved in config for chain-age display
        assert job_v2.config["retry_started_at"] == first_created

    def test_retry_started_at_preserved_across_updates(self, jm):
        job_v1 = jm.upsert_retry_chain_job(
            canonical_path="/x.mkv",
            basename="x.mkv",
            attempt=1,
            max_attempts=5,
            next_run_at=None,
            wait_seconds=30,
            outcome="scheduled",
        )
        original_start = job_v1.config["retry_started_at"]
        # Multiple state transitions
        for outcome in ("running", "scheduled", "running", "completed"):
            j = jm.upsert_retry_chain_job(
                canonical_path="/x.mkv",
                basename="x.mkv",
                attempt=2,
                max_attempts=5,
                next_run_at=None,
                wait_seconds=60,
                outcome=outcome,
            )
            assert j.config["retry_started_at"] == original_start

    # ------------------------------------------------------------------
    # Server attribution
    # ------------------------------------------------------------------

    def test_server_attribution_forwarded(self, jm):
        job = jm.upsert_retry_chain_job(
            canonical_path="/x.mkv",
            basename="x.mkv",
            attempt=1,
            max_attempts=5,
            next_run_at=None,
            wait_seconds=30,
            outcome="scheduled",
            server_id="jelly-1",
            server_name="JellyTest",
            server_type="jellyfin",
        )
        assert job.server_id == "jelly-1"
        assert job.server_name == "JellyTest"
        assert job.server_type == "jellyfin"

    def test_late_arriving_server_attribution_wins_over_empty(self, jm):
        # First call without server attribution
        jm.upsert_retry_chain_job(
            canonical_path="/x.mkv",
            basename="x.mkv",
            attempt=1,
            max_attempts=5,
            next_run_at=None,
            wait_seconds=30,
            outcome="scheduled",
        )
        # Second call WITH attribution should fill it in
        job = jm.upsert_retry_chain_job(
            canonical_path="/x.mkv",
            basename="x.mkv",
            attempt=2,
            max_attempts=5,
            next_run_at=None,
            wait_seconds=120,
            outcome="scheduled",
            server_id="jelly-1",
            server_name="JellyTest",
            server_type="jellyfin",
        )
        assert job.server_id == "jelly-1"

    # ------------------------------------------------------------------
    # UI integration — config aliases
    # ------------------------------------------------------------------

    def test_sets_existing_ui_aliases_for_existing_retry_renderer(self, jm):
        """The existing app.js retry-badge + countdown rendering reads
        ``config.is_retry`` and ``config.max_retries``. Our retry-chain
        Job MUST set both so the existing UI renders it correctly without
        a JS-side change.
        """
        job = jm.upsert_retry_chain_job(
            canonical_path="/x.mkv",
            basename="x.mkv",
            attempt=2,
            max_attempts=6,
            next_run_at=None,
            wait_seconds=60,
            outcome="scheduled",
        )
        assert job.config["is_retry"] is True, (
            "Without is_retry=True the existing app.js retry badge (line ~1641) won't render the 'Retry N/M' chip."
        )
        assert job.config["max_retries"] == 6, (
            "Without max_retries set, the existing badge won't show the M denominator."
        )
        assert job.config["is_retry_chain"] is True  # distinguisher
        assert job.config["retry_attempt"] == 2
