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

    def test_chain_jobs_are_ephemeral_not_persisted_to_disk(self, jm, tmp_path):
        """Regression for 5,387-orphan production incident 2026-05-09:
        my 9fc29dd commit persisted retry-chain Jobs to ``jobs.db``.
        Each library scan / webhook burst created hundreds of chains;
        on container restart they were "revived" by the
        interrupted-jobs logic and hammered the dashboard until the
        Plex previews-readiness probe slowed to 30+ seconds.

        Fix: retry-chain Jobs are EPHEMERAL — they live only in the
        in-memory ``_jobs`` dict and disappear with the container.
        The underlying ``threading.Timer`` instances in the retry
        queue don't survive restart either, so the chain itself is
        gone — the user-visible Job row would be orphaned anyway.

        This test pins that contract: upserting a retry-chain Job
        does NOT touch the SQLite store. A new ``JobManager`` started
        against the same config dir doesn't see the chain.
        """
        from media_preview_generator.web.jobs import JobManager

        path = "/data/Movies/Foo.mkv"
        jm.upsert_retry_chain_job(
            canonical_path=path,
            basename="Foo.mkv",
            attempt=1,
            max_attempts=5,
            next_run_at=None,
            wait_seconds=30,
            outcome="scheduled",
        )
        # The chain Job IS visible in this JobManager instance...
        assert any(j.id.startswith("retry-") for j in jm.get_all_jobs())

        # ...but a brand-new JobManager (simulating a container
        # restart) loading the same config dir MUST NOT see it.
        import media_preview_generator.web.jobs as jobs_mod

        with jobs_mod._job_lock:
            jobs_mod._job_manager = None
        jm2 = JobManager(config_dir=jm.config_dir)
        assert not any(j.id.startswith("retry-") for j in jm2.get_all_jobs()), (
            "Retry-chain Jobs MUST NOT survive a JobManager restart — they are "
            "ephemeral by design (the underlying retry timer is gone after restart, "
            "so a persisted Job row would be orphaned). Pre-fix this test would "
            "fail because 9fc29dd called self._persist_job() on every upsert, "
            "leaving the chain in jobs.db forever."
        )

    def test_load_drops_legacy_retry_chain_orphans(self, tmp_path):
        """Belt-and-braces: even if a legacy ``jobs.db`` from a pre-fix
        deployment contains persisted retry-chain rows, ``_load_jobs``
        drops them at startup AND removes them from the store. This is
        the recovery path for users who upgrade from 9fc29dd → the
        next release without the fix; without this pruning, their
        existing 5K+ orphans would still hammer the dashboard on first
        boot of the fixed code.
        """
        import media_preview_generator.web.jobs as jobs_mod
        from media_preview_generator.web.jobs import JobManager, JobStorage

        with jobs_mod._job_lock:
            jobs_mod._job_manager = None

        config_dir = tmp_path / "legacy_config"
        config_dir.mkdir()
        # Hand-write the legacy persisted state by upserting directly
        # via JobStorage (bypassing the new ephemeral upsert path).
        storage = JobStorage(str(config_dir / "jobs.db"))
        from media_preview_generator.web.jobs import Job

        legacy_chain = Job(id="retry-deadbeef00000000", library_name="Old Chain")
        storage.upsert(legacy_chain)
        # Also stash a normal pending job so we can verify the load
        # only drops chain Jobs, not other pending ones.
        normal = Job(id="normal-1", library_name="Normal")
        storage.upsert(normal)
        storage.close()

        # Fresh JobManager loads from the same config dir.
        jm = JobManager(config_dir=str(config_dir))
        all_jobs = jm.get_all_jobs()
        chain_ids = [j.id for j in all_jobs if j.id.startswith("retry-")]
        normal_ids = [j.id for j in all_jobs if not j.id.startswith("retry-")]
        assert chain_ids == [], f"Legacy retry-chain Job(s) {chain_ids} survived load — pruning regression"
        assert "normal-1" in normal_ids, "Pruning is over-aggressive — non-chain Jobs MUST survive load"

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


class TestUpsertRetryChainJobSourceAndDisplay:
    """Cross-feature: ``source`` (trigger pill) + ``basename`` cleanup
    on subsequent upserts (cleaner title wins).

    Pre-fix the chain row dropped both the cleaned title (showed the raw
    filename instead of "Deadliest Catch S22E01") and the colored source
    pill (no Sonarr/Radarr/Sportarr/Plex chip), so it visually
    disconnected from its parent dispatch row in the queue table.
    """

    def test_source_is_persisted_on_create(self, jm):
        job = jm.upsert_retry_chain_job(
            canonical_path="/data/x.mkv",
            basename="x.mkv",
            attempt=1,
            max_attempts=5,
            next_run_at=None,
            wait_seconds=30,
            outcome="scheduled",
            source="sonarr",
        )
        assert job.config.get("source") == "sonarr", (
            "Without config['source'], _serverBadge() in app.js falls through to "
            "an unlabelled chain row — the user can't tell at a glance which "
            "trigger spawned the chain."
        )

    def test_source_back_fills_on_subsequent_upsert(self, jm):
        """Some retry-chain call sites don't have ``source`` in scope yet
        (e.g. a re-schedule after a non-deterministic exception). When a
        later upsert DOES carry it, fill in the gap rather than
        clobbering an already-set value with None.
        """
        path = "/data/x.mkv"
        jm.upsert_retry_chain_job(
            canonical_path=path,
            basename="x.mkv",
            attempt=1,
            max_attempts=5,
            next_run_at=None,
            wait_seconds=30,
            outcome="scheduled",
        )
        job = jm.upsert_retry_chain_job(
            canonical_path=path,
            basename="x.mkv",
            attempt=2,
            max_attempts=5,
            next_run_at=None,
            wait_seconds=120,
            outcome="scheduled",
            source="radarr",
        )
        assert job.config["source"] == "radarr"

    def test_source_does_not_overwrite_existing(self, jm):
        """First write wins: don't clobber a real source with a later
        ``None`` from a context that lost it (e.g. a worker callback
        rebuilt without the original webhook payload in scope).
        """
        path = "/data/x.mkv"
        jm.upsert_retry_chain_job(
            canonical_path=path,
            basename="x.mkv",
            attempt=1,
            max_attempts=5,
            next_run_at=None,
            wait_seconds=30,
            outcome="scheduled",
            source="sonarr",
        )
        job = jm.upsert_retry_chain_job(
            canonical_path=path,
            basename="x.mkv",
            attempt=2,
            max_attempts=5,
            next_run_at=None,
            wait_seconds=120,
            outcome="scheduled",
            source=None,
        )
        assert job.config["source"] == "sonarr"

    def test_cleaner_title_replaces_raw_basename_on_subsequent_upsert(self, jm):
        """Live regression (2026-05-10): retry-queue's first call comes
        from a path-only context and uses ``os.path.basename`` (raw
        filename like 'Deadliest Catch (2005) - S22E01 - Kings of the
        Frozen North [WEBDL-1080p][EAC3 2.0][h264]-SNAKE.mkv'); a later
        call from the worker carries the dispatcher's cleaned
        ``library_name`` ('Deadliest Catch S22E01'). Prefer the cleaner
        title so the chain row matches its parent dispatch row.
        """
        path = (
            "/data/Deadliest Catch (2005) - S22E01 - Kings of the Frozen North [WEBDL-1080p][EAC3 2.0][h264]-SNAKE.mkv"
        )
        raw = "Deadliest Catch (2005) - S22E01 - Kings of the Frozen North [WEBDL-1080p][EAC3 2.0][h264]-SNAKE.mkv"
        clean = "Deadliest Catch S22E01"
        jm.upsert_retry_chain_job(
            canonical_path=path,
            basename=raw,
            attempt=1,
            max_attempts=5,
            next_run_at=None,
            wait_seconds=30,
            outcome="scheduled",
        )
        job = jm.upsert_retry_chain_job(
            canonical_path=path,
            basename=clean,
            attempt=2,
            max_attempts=5,
            next_run_at=None,
            wait_seconds=120,
            outcome="scheduled",
        )
        assert job.library_name == clean, (
            f"Cleaner title should win; got {job.library_name!r}. "
            "Without this, the chain row stays glued to the raw filename "
            "even after a later caller passes the dispatcher's cleaned title."
        )
        assert job.config["retry_basename"] == clean

    def test_raw_basename_does_not_overwrite_cleaner_existing(self, jm):
        """Reverse direction: don't regress a clean title to a raw
        basename if a later upsert hits the path-only fallback.
        """
        path = "/data/Foo.mkv"
        jm.upsert_retry_chain_job(
            canonical_path=path,
            basename="Foo (Cleaned)",
            attempt=1,
            max_attempts=5,
            next_run_at=None,
            wait_seconds=30,
            outcome="scheduled",
        )
        job = jm.upsert_retry_chain_job(
            canonical_path=path,
            basename="Foo.mkv.with.much.longer.raw.label",
            attempt=2,
            max_attempts=5,
            next_run_at=None,
            wait_seconds=120,
            outcome="scheduled",
        )
        assert job.library_name == "Foo (Cleaned)"


class TestRetryChainSynthesizedLogs:
    """The chain Job's ``View Logs`` modal used to show the misleading
    "Log file was cleared due to log retention policy." sentinel because
    chain Jobs never write a per-attempt log file (they're UI-only).
    Synthesize a meaningful status block instead.
    """

    def test_get_logs_returns_synthesized_status_for_chain_job(self, jm):
        path = "/data/X.mkv"
        jm.upsert_retry_chain_job(
            canonical_path=path,
            basename="X.mkv",
            attempt=2,
            max_attempts=5,
            next_run_at=None,
            wait_seconds=60,
            outcome="running",
            source="sonarr",
        )
        chain_id = "retry-" + __import__("hashlib").sha256(path.encode()).hexdigest()[:16]
        logs = jm.get_logs(chain_id)

        assert logs, "Synthesized log lines must not be empty for a chain Job"
        joined = "\n".join(logs)
        assert "no per-attempt logs" in joined.lower(), (
            "Synthesized log must explain to the user why per-attempt logs are missing here."
        )
        assert "X.mkv" in joined, "Synthesized log must surface the source file name"
        assert "2 of 5" in joined, "Synthesized log must surface attempt N of M"
        assert "Log file was cleared" not in joined, (
            "Pre-fix the retention sentinel was returned — that's misleading; "
            "no log was ever written for a chain Job to clear."
        )

    def test_get_logs_paginated_returns_synthesized_status_for_chain_job(self, jm):
        path = "/data/Y.mkv"
        jm.upsert_retry_chain_job(
            canonical_path=path,
            basename="Y.mkv",
            attempt=1,
            max_attempts=5,
            next_run_at=None,
            wait_seconds=30,
            outcome="scheduled",
        )
        chain_id = "retry-" + __import__("hashlib").sha256(path.encode()).hexdigest()[:16]
        result = jm.get_logs_paginated(chain_id)
        assert result["total_lines"] >= 6
        assert all("Log file was cleared" not in line for line in result["lines"])
