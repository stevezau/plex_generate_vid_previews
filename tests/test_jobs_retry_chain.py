"""Tests for ``JobManager.upsert_retry_chain_job`` — the method that
MUTATES the originating dispatch Job into chain mode.

After the chain rewrite (PLAN at
``.claude/plans/check-the-last-30-40-binary-engelbart.md``):
- There is NO separate ``retry-<sha256(path)[:16]>`` Job.
- The originating dispatch's Job (the worker-pool Job that ran the
  initial FFmpeg + Plex/Emby publish) IS the chain Job. Its UUID is
  the chain identity.
- ``upsert_retry_chain_job`` adds/updates retry-chain state on that
  Job — flips status, sets retry chip aliases, bumps created_at,
  etc. It does NOT create a new row.

Matrix coverage per .claude/rules/testing.md:
  * outcome state machine: scheduled / running / completed / exhausted
  * first mutation (originating Job hasn't been chain-ified yet)
    vs. subsequent mutations
  * server attribution: late-arriving wins over empty
  * title-cleanup heuristic (extension-aware)
  * source persistence (first-writer-wins)
  * CANCELLED is sticky (TOCTOU race guard)
  * legacy ``retry-<hash>`` rows are dropped at load
  * chain Job (regular UUID with is_retry_chain config) survives
    restart with mark-failed for non-terminal interruption
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

from media_preview_generator.web.jobs import Job, JobManager, JobStatus, JobStorage


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


def _seed_originating_job(jm, library_name="Foo"):
    """Create a real worker-pool dispatch Job to use as the chain origin.

    Real chains spawn when ``process_canonical_path`` sees a publisher
    return ``PUBLISHED_PENDING_REGISTRATION``; at that point the
    originating Job already exists. Tests need a stand-in.
    """
    job = jm.create_job(library_name=library_name, config={})
    # The originating dispatch typically completes its initial work
    # before the chain takes over the lifecycle. Mimic that.
    jm.complete_job(job.id)
    return jm.get_job(job.id)


class TestUpsertMutatesOriginatingJob:
    def test_no_originating_job_returns_none(self, jm):
        """If the originating Job doesn't exist (CLI smoke test or
        deleted before retry fired), the method must NOT crash — it
        returns None and the caller falls through cleanly."""
        job = jm.upsert_retry_chain_job(
            canonical_path="/data/Foo.mkv",
            basename="Foo.mkv",
            attempt=1,
            max_attempts=5,
            next_run_at=None,
            wait_seconds=30,
            outcome="scheduled",
            originating_job_id="does-not-exist",
        )
        assert job is None

    def test_first_mutation_stamps_chain_init_bits(self, jm):
        """First call to ``upsert_retry_chain_job`` for a Job that
        hasn't been chain-ified yet must stamp the canonical-path,
        is_retry_chain flag, retry_started_at, etc."""
        original = _seed_originating_job(jm, library_name="Foo")
        path = "/data/Foo.mkv"
        job = jm.upsert_retry_chain_job(
            canonical_path=path,
            basename="Foo",
            attempt=1,
            max_attempts=5,
            next_run_at=(datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat(),
            wait_seconds=30,
            outcome="scheduled",
            originating_job_id=original.id,
        )
        # SAME UUID — no new row was created
        assert job.id == original.id
        assert job.config["is_retry_chain"] is True
        assert job.config["retry_chain_for"] == path
        assert "retry_started_at" in job.config
        assert job.config["retry_attempt"] == 1
        assert job.config["retry_max_attempts"] == 5
        # status flipped to PENDING (chain re-armed the lifecycle)
        assert job.status == JobStatus.PENDING
        # Only ONE Job row exists (originating == chain)
        all_jobs = jm.get_all_jobs()
        retry_chain_rows = [j for j in all_jobs if j.config.get("is_retry_chain")]
        assert len(retry_chain_rows) == 1
        assert retry_chain_rows[0].id == original.id

    def test_no_separate_retry_hash_row_created(self, jm):
        """Pre-rewrite a ``retry-<sha256(path)[:16]>`` row was created.
        Post-rewrite the chain IS the originating Job — never a
        separate row. Pinned so a regression that re-introduces the
        synthetic row gets caught."""
        original = _seed_originating_job(jm)
        jm.upsert_retry_chain_job(
            canonical_path="/data/Foo.mkv",
            basename="Foo",
            attempt=1,
            max_attempts=5,
            next_run_at=None,
            wait_seconds=30,
            outcome="scheduled",
            originating_job_id=original.id,
        )
        # No retry- prefixed Jobs anywhere in the JobManager.
        for job in jm.get_all_jobs():
            assert not job.id.startswith("retry-"), (
                f"Legacy retry-<hash> row leaked: {job.id}. Post-rewrite the "
                f"chain is the originating Job's UUID; no separate row should exist."
            )

    def test_subsequent_mutations_update_in_place(self, jm):
        """Each subsequent ``upsert_retry_chain_job`` call (attempt 2,
        3, ...) must mutate the SAME Job — not create a new one."""
        original = _seed_originating_job(jm)
        jm.upsert_retry_chain_job(
            canonical_path="/data/Foo.mkv",
            basename="Foo",
            attempt=1,
            max_attempts=5,
            next_run_at=None,
            wait_seconds=30,
            outcome="scheduled",
            originating_job_id=original.id,
        )
        job_v2 = jm.upsert_retry_chain_job(
            canonical_path="/data/Foo.mkv",
            basename="Foo",
            attempt=2,
            max_attempts=5,
            next_run_at=None,
            wait_seconds=120,
            outcome="scheduled",
            originating_job_id=original.id,
        )
        assert job_v2.id == original.id
        assert job_v2.config["retry_attempt"] == 2
        assert len([j for j in jm.get_all_jobs() if j.config.get("is_retry_chain")]) == 1


class TestStateMachine:
    def test_outcome_scheduled_pending_with_countdown(self, jm):
        original = _seed_originating_job(jm)
        eta = (datetime.now(timezone.utc) + timedelta(seconds=120)).isoformat()
        job = jm.upsert_retry_chain_job(
            canonical_path="/x.mkv",
            basename="x",
            attempt=2,
            max_attempts=5,
            next_run_at=eta,
            wait_seconds=120,
            outcome="scheduled",
            originating_job_id=original.id,
        )
        assert job.status == JobStatus.PENDING
        assert job.progress.retry_eta == eta
        assert job.progress.retry_wait_total == 120
        assert job.error is None
        # completed_at was set by _seed_originating_job's complete_job
        # call — the chain re-arm MUST clear it so the row reads as
        # "still in progress" not "finished in the past".
        assert job.completed_at is None, f"completed_at must be cleared when chain re-arms; got {job.completed_at!r}"

    def test_outcome_running_clears_completed_at(self, jm):
        """Defensive: even if a callback lands directly on
        outcome="running" as the FIRST chain mutation on an
        already-COMPLETED originating Job (skipping the "scheduled"
        leg), ``completed_at`` must clear. Otherwise sort/filter/
        duration logic that reads ``completed_at`` while status=RUNNING
        misbehaves.
        """
        original = _seed_originating_job(jm)
        # _seed_originating_job calls complete_job, so completed_at is set.
        assert original.completed_at is not None
        job = jm.upsert_retry_chain_job(
            canonical_path="/x.mkv",
            basename="x",
            attempt=1,
            max_attempts=5,
            next_run_at=None,
            wait_seconds=None,
            outcome="running",
            originating_job_id=original.id,
        )
        assert job.status == JobStatus.RUNNING
        assert job.completed_at is None, (
            f"Running outcome must clear completed_at on the first mutation; got {job.completed_at!r}"
        )

    def test_outcome_scheduled_clears_current_item(self, jm):
        """A chain transitioning back to PENDING/scheduled MUST clear
        ``progress.current_item``. The dashboard's Active panel treats
        PENDING + non-empty ``current_item`` as "Active" (pre-dispatch
        work in progress), so a chain that just finished a firing and
        is now waiting for the next backoff would otherwise show up
        under Active instead of Queue.

        Production bug: every chain row in the DB had
        ``current_item='[Webhook Targets] 1/1 completed'`` lingering
        from the worker's last status push. Chains waiting on backoff
        appeared in Active until they fired again, confusing users
        ("why is it Active if it's waiting?"). Queue-side countdown
        renderer expects to own these rows.
        """
        original = _seed_originating_job(jm)
        # Worker set a current_item during the firing — emulate.
        jm.update_progress(original.id, current_item="[Webhook Targets] 1/1 completed")
        assert jm.get_job(original.id).progress.current_item == "[Webhook Targets] 1/1 completed"

        eta = (datetime.now(timezone.utc) + timedelta(seconds=120)).isoformat()
        job = jm.upsert_retry_chain_job(
            canonical_path="/x.mkv",
            basename="x",
            attempt=2,
            max_attempts=5,
            next_run_at=eta,
            wait_seconds=120,
            outcome="scheduled",
            originating_job_id=original.id,
        )
        assert job.progress.current_item == "", (
            f"Scheduled chain must clear current_item so it doesn't show in the dashboard's "
            f"Active panel; got {job.progress.current_item!r}"
        )

    def test_outcome_exhausted_clears_current_item(self, jm):
        """Cleanliness: an exhausted chain row goes terminal (FAILED),
        but a stale ``current_item`` from the last firing would still
        render in the failed-row UI. Clear it for the same reason."""
        original = _seed_originating_job(jm)
        jm.update_progress(original.id, current_item="[Webhook Targets] 1/1 completed")

        job = jm.upsert_retry_chain_job(
            canonical_path="/x.mkv",
            basename="x",
            attempt=5,
            max_attempts=5,
            next_run_at=None,
            wait_seconds=None,
            outcome="exhausted",
            reason="Exhausted after 5 attempts",
            originating_job_id=original.id,
        )
        assert job.progress.current_item == "", (
            f"Exhausted chain must clear current_item; got {job.progress.current_item!r}"
        )

    def test_outcome_running_clears_countdown(self, jm):
        original = _seed_originating_job(jm)
        jm.upsert_retry_chain_job(
            canonical_path="/x.mkv",
            basename="x",
            attempt=1,
            max_attempts=5,
            next_run_at=(datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat(),
            wait_seconds=30,
            outcome="scheduled",
            originating_job_id=original.id,
        )
        job = jm.upsert_retry_chain_job(
            canonical_path="/x.mkv",
            basename="x",
            attempt=1,
            max_attempts=5,
            next_run_at=None,
            wait_seconds=None,
            outcome="running",
            originating_job_id=original.id,
        )
        assert job.status == JobStatus.RUNNING
        assert job.progress.retry_eta is None
        assert job.progress.retry_wait_total is None

    def test_outcome_completed_sets_terminal_green(self, jm):
        original = _seed_originating_job(jm)
        jm.upsert_retry_chain_job(
            canonical_path="/x.mkv",
            basename="x",
            attempt=1,
            max_attempts=5,
            next_run_at=None,
            wait_seconds=None,
            outcome="running",
            originating_job_id=original.id,
        )
        job = jm.upsert_retry_chain_job(
            canonical_path="/x.mkv",
            basename="x",
            attempt=2,
            max_attempts=5,
            next_run_at=None,
            wait_seconds=None,
            outcome="completed",
            originating_job_id=original.id,
        )
        assert job.status == JobStatus.COMPLETED
        assert job.completed_at is not None
        assert job.error is None
        assert job.progress.retry_eta is None

    def test_outcome_exhausted_sets_failed_with_reason(self, jm):
        original = _seed_originating_job(jm)
        job = jm.upsert_retry_chain_job(
            canonical_path="/x.mkv",
            basename="x",
            attempt=5,
            max_attempts=5,
            next_run_at=None,
            wait_seconds=None,
            outcome="exhausted",
            reason="Server still hadn't indexed after 5 attempts",
            originating_job_id=original.id,
        )
        assert job.status == JobStatus.FAILED
        assert "5 attempts" in (job.error or "")
        assert job.progress.retry_eta is None

    def test_completed_outcome_refreshes_publishers_from_attempt(self, jm):
        """Pin the contract: when a retry chain completes, the chain Job's
        ``publishers`` row MUST be replaced with the firing's per-server
        outcomes — NOT left as the originating dispatch's snapshot.

        Production bug: a Family Guy chain (9c50cd99) completed
        successfully on attempt 3 (Bridge plugin registered Jellyfin's
        trickplay; verified in jellyfin logs). The chain status flipped
        to COMPLETED but the modal kept rendering "JellyTest → Generated
        (auto-retrying) × 1" because the publishers field on the chain
        Job was set at the originating dispatch (when JellyTest was
        still pending_registration) and never refreshed. Users saw a
        green-completed row with a publisher tile claiming something
        was still retrying — confusing and wrong.
        """
        original = _seed_originating_job(jm)
        # Originating dispatch's snapshot — Jelly is pending.
        jm.set_publishers(
            original.id,
            [
                {"server_id": "plex", "server_name": "Plex", "server_type": "plex", "counts": {"published": 1}},
                {
                    "server_id": "jelly",
                    "server_name": "JellyTest",
                    "server_type": "jellyfin",
                    "counts": {"published_pending_registration": 1},
                },
            ],
        )

        # Retry attempt 3 succeeded — JellyTest now reports
        # skipped_output_exists (tiles already on disk; bridge plugin
        # registered the row). The retry callback hands these final
        # rows to upsert_retry_chain_job via the new ``publishers``
        # kwarg.
        final_rows = [
            {"server_id": "plex", "server_name": "Plex", "server_type": "plex", "counts": {"skipped_output_exists": 1}},
            {
                "server_id": "jelly",
                "server_name": "JellyTest",
                "server_type": "jellyfin",
                "counts": {"skipped_output_exists": 1},
            },
        ]

        job = jm.upsert_retry_chain_job(
            canonical_path="/x.mkv",
            basename="x",
            attempt=3,
            max_attempts=5,
            next_run_at=None,
            wait_seconds=None,
            outcome="completed",
            originating_job_id=original.id,
            publishers=final_rows,
        )
        assert job.status == JobStatus.COMPLETED
        # The chain's publishers MUST now reflect the firing's final
        # outcomes. Without this refresh, the dashboard renders stale
        # pending_registration on a successfully-completed row.
        jelly = next(p for p in job.publishers if p["server_id"] == "jelly")
        assert jelly["counts"] == {"skipped_output_exists": 1}, (
            f"Chain publishers must be refreshed to the firing's outcome on completion; "
            f"got {jelly['counts']!r}. Stale pending_registration on a completed chain is "
            f"the exact production bug this regression test guards against."
        )

    def test_exhausted_outcome_refreshes_publishers_from_attempt(self, jm):
        """Same principle for exhausted chains: the publishers row should
        reflect the LAST attempt's per-server state, not the originating
        dispatch. An exhausted Jellyfin still showing pending_registration
        IS the accurate diagnostic — but the chain may also include rows
        whose state advanced (e.g., Plex transitioned from skipped_not_indexed
        to skipped_output_exists between attempts)."""
        original = _seed_originating_job(jm)
        jm.set_publishers(
            original.id,
            [
                {
                    "server_id": "plex",
                    "server_name": "Plex",
                    "server_type": "plex",
                    "counts": {"skipped_not_indexed": 1},
                },
                {
                    "server_id": "jelly",
                    "server_name": "JellyTest",
                    "server_type": "jellyfin",
                    "counts": {"published_pending_registration": 1},
                },
            ],
        )

        # Last attempt's outcome — Plex resolved, Jelly still pending.
        final_rows = [
            {"server_id": "plex", "server_name": "Plex", "server_type": "plex", "counts": {"skipped_output_exists": 1}},
            {
                "server_id": "jelly",
                "server_name": "JellyTest",
                "server_type": "jellyfin",
                "counts": {"published_pending_registration": 1},
            },
        ]

        job = jm.upsert_retry_chain_job(
            canonical_path="/x.mkv",
            basename="x",
            attempt=5,
            max_attempts=5,
            next_run_at=None,
            wait_seconds=None,
            outcome="exhausted",
            reason="exhausted after 5",
            originating_job_id=original.id,
            publishers=final_rows,
        )
        plex = next(p for p in job.publishers if p["server_id"] == "plex")
        assert plex["counts"] == {"skipped_output_exists": 1}, (
            f"Plex should have transitioned to skipped_output_exists on the last attempt; got {plex['counts']!r}"
        )

    def test_running_outcome_does_not_clobber_publishers(self, jm):
        """Sibling of the scheduled-preserve test — closes the matrix
        on the ``outcome`` variable. running is fired when a Timer
        callback transitions the chain to "attempt N executing now";
        publishers belongs to the FINAL outcome (completed/exhausted),
        not the in-flight transition. Without this row a future
        consolidation that accidentally applied publishers on running
        too would wipe the originating snapshot mid-firing."""
        original = _seed_originating_job(jm)
        seed = [
            {"server_id": "plex", "server_name": "Plex", "server_type": "plex", "counts": {"published": 1}},
        ]
        jm.set_publishers(original.id, seed)
        job = jm.upsert_retry_chain_job(
            canonical_path="/x.mkv",
            basename="x",
            attempt=2,
            max_attempts=5,
            next_run_at=None,
            wait_seconds=None,
            outcome="running",
            originating_job_id=original.id,
            # No publishers kwarg — running transitions don't have
            # firing-result data to apply.
        )
        assert job.publishers == seed, f"running outcome must preserve publishers; got {job.publishers!r}"

    def test_scheduled_outcome_does_not_clobber_publishers(self, jm):
        """When a chain re-arms (outcome='scheduled', waiting for next attempt),
        ``publishers`` must NOT be touched — the originating snapshot still
        applies until a firing replaces it. Pre-fix concern: if upsert
        blindly took ``publishers`` from every outcome, a scheduled call
        without publishers data would wipe the originating snapshot."""
        original = _seed_originating_job(jm)
        seed = [
            {"server_id": "plex", "server_name": "Plex", "server_type": "plex", "counts": {"published": 1}},
        ]
        jm.set_publishers(original.id, seed)

        job = jm.upsert_retry_chain_job(
            canonical_path="/x.mkv",
            basename="x",
            attempt=2,
            max_attempts=5,
            next_run_at=None,
            wait_seconds=120,
            outcome="scheduled",
            originating_job_id=original.id,
            # No publishers passed — scheduled path doesn't have firing data.
        )
        # Publishers MUST survive the re-arm — the originating snapshot is
        # still the most-recent reality between firings.
        assert job.publishers == seed, f"scheduled outcome must preserve publishers; got {job.publishers!r}"


class TestSortToTop:
    def test_created_at_bumps_on_each_update(self, jm):
        """Each mutation refreshes created_at so the row pops to top
        of newest-first list. retry_started_at preserved in config
        for chain-age display."""
        import time

        original = _seed_originating_job(jm)
        jm.upsert_retry_chain_job(
            canonical_path="/x.mkv",
            basename="x",
            attempt=1,
            max_attempts=5,
            next_run_at=None,
            wait_seconds=30,
            outcome="scheduled",
            originating_job_id=original.id,
        )
        first_created = jm.get_job(original.id).created_at
        time.sleep(0.01)
        v2 = jm.upsert_retry_chain_job(
            canonical_path="/x.mkv",
            basename="x",
            attempt=2,
            max_attempts=5,
            next_run_at=None,
            wait_seconds=120,
            outcome="scheduled",
            originating_job_id=original.id,
        )
        assert v2.created_at > first_created
        # retry_started_at stamped on first mutation, preserved after
        assert v2.config["retry_started_at"] is not None


class TestServerAttribution:
    def test_server_attribution_set_when_missing(self, jm):
        """Originating Job created without server attribution; chain
        upsert fills it in."""
        original = _seed_originating_job(jm)
        assert original.server_id is None
        job = jm.upsert_retry_chain_job(
            canonical_path="/x.mkv",
            basename="x",
            attempt=1,
            max_attempts=5,
            next_run_at=None,
            wait_seconds=30,
            outcome="scheduled",
            server_id="jelly-1",
            server_name="JellyTest",
            server_type="jellyfin",
            originating_job_id=original.id,
        )
        assert job.server_id == "jelly-1"
        assert job.server_name == "JellyTest"
        assert job.server_type == "jellyfin"

    def test_existing_server_attribution_not_overwritten(self, jm):
        """Originating Job already pinned to Plex; chain upsert with
        different server context must NOT overwrite (defensive
        against multi-server fan-out coalescing incorrectly)."""

        # Create a Job with server attribution explicitly set
        original = jm.create_job(
            library_name="Foo",
            server_id="plex-default",
            server_name="Plex",
            server_type="plex",
        )
        jm.complete_job(original.id)

        job = jm.upsert_retry_chain_job(
            canonical_path="/x.mkv",
            basename="x",
            attempt=1,
            max_attempts=5,
            next_run_at=None,
            wait_seconds=30,
            outcome="scheduled",
            server_id="jelly-1",  # different
            server_name="JellyTest",
            server_type="jellyfin",
            originating_job_id=original.id,
        )
        assert job.server_id == "plex-default", (
            f"Existing server attribution must not be overwritten; got {job.server_id!r}"
        )


class TestUIAliases:
    def test_chip_aliases_present(self, jm):
        """app.js retry chip reads ``config.is_retry`` and
        ``config.max_retries``. The mutation must stamp both."""
        original = _seed_originating_job(jm)
        job = jm.upsert_retry_chain_job(
            canonical_path="/x.mkv",
            basename="x",
            attempt=2,
            max_attempts=6,
            next_run_at=None,
            wait_seconds=60,
            outcome="scheduled",
            originating_job_id=original.id,
        )
        assert job.config["is_retry"] is True
        assert job.config["max_retries"] == 6
        assert job.config["is_retry_chain"] is True
        assert job.config["retry_attempt"] == 2


class TestPersistenceAndRestart:
    def test_chain_job_persists_pending_across_restart_for_resume(self, jm, tmp_path):
        """The chain Job (originating dispatch with is_retry_chain
        config) MUST survive a restart so the modal Attempts dropdown
        can show history.

        Pre-fix: non-terminal chains were marked FAILED on load with
        'Retry interrupted by container restart' because the in-memory
        ``threading.Timer`` driving the chain didn't survive the
        restart. The user then had to manually re-trigger the source
        webhook to resume. That was hostile UX — a chain mid-backoff
        when DEV_RELOAD reloaded the container died for no reason
        the user could control.

        Post-fix: chains in PENDING (waiting on backoff) keep their
        PENDING state + retry_eta, and the JobManager collects them
        into ``interrupted_retry_chains()`` so the app boot phase can
        re-arm the Timer via ``schedule_retry_for_unindexed`` once
        the registry + config are loaded.
        """
        import media_preview_generator.web.jobs as jobs_mod

        original = _seed_originating_job(jm)
        eta = (datetime.now(timezone.utc) + timedelta(seconds=120)).isoformat()
        jm.upsert_retry_chain_job(
            canonical_path="/data/Foo.mkv",
            basename="Foo",
            attempt=1,
            max_attempts=5,
            next_run_at=eta,
            wait_seconds=120,
            outcome="scheduled",
            server_id="jelly-1",
            server_name="JellyTest",
            server_type="jellyfin",
            source="sonarr",
            originating_job_id=original.id,
        )
        live = jm.get_job(original.id)
        assert live.status == JobStatus.PENDING
        assert live.config["is_retry_chain"] is True

        # Restart simulation
        with jobs_mod._job_lock:
            jobs_mod._job_manager = None
        jm2 = JobManager(config_dir=jm.config_dir)
        recovered = jm2.get_job(original.id)
        assert recovered is not None
        assert recovered.status == JobStatus.PENDING, (
            f"Chain Job must remain PENDING on restart so the resume path can re-arm "
            f"the Timer; got {recovered.status}. If this asserts FAILED, the regression "
            f"would make every container reload nuke in-flight retry chains."
        )
        assert recovered.config["is_retry_chain"] is True
        assert recovered.progress.retry_eta == eta, (
            "retry_eta must survive restart so the resume path knows when this attempt was due"
        )

        # The interrupted-chains collection MUST contain it so the
        # boot-time resume callback can pick it up.
        interrupted = jm2.interrupted_retry_chains()
        ids = [j.id for j in interrupted]
        assert original.id in ids, (
            f"Chain Job {original.id} must be in interrupted_retry_chains() so the app's "
            f"post-config-load resume path can re-arm it; got {ids}"
        )

    def test_running_chain_job_recovered_as_pending_for_resume(self, jm, tmp_path):
        """A RUNNING chain Job (the worker was mid-firing when the
        process died) should also be picked up by the resume path
        rather than left dead. Restart converts RUNNING → PENDING
        so the same re-arm logic applies."""
        import media_preview_generator.web.jobs as jobs_mod

        original = _seed_originating_job(jm)
        # Force the chain into RUNNING — emulates "worker had just
        # picked up attempt #2 when the container restarted"
        jm.upsert_retry_chain_job(
            canonical_path="/data/Foo.mkv",
            basename="Foo",
            attempt=2,
            max_attempts=5,
            next_run_at=None,
            wait_seconds=None,
            outcome="running",
            server_id="jelly-1",
            server_name="JellyTest",
            server_type="jellyfin",
            originating_job_id=original.id,
        )
        assert jm.get_job(original.id).status == JobStatus.RUNNING

        with jobs_mod._job_lock:
            jobs_mod._job_manager = None
        jm2 = JobManager(config_dir=jm.config_dir)
        recovered = jm2.get_job(original.id)
        assert recovered is not None
        # RUNNING → PENDING on restart, so the resume path can re-arm.
        assert recovered.status == JobStatus.PENDING, (
            f"RUNNING chain must be recovered as PENDING for resume; got {recovered.status}"
        )
        assert original.id in [j.id for j in jm2.interrupted_retry_chains()]

    def test_terminal_chain_job_loads_as_is(self, tmp_path):
        """A chain Job that COMPLETED before restart must load
        unchanged. Its history is the user's record."""
        import media_preview_generator.web.jobs as jobs_mod

        with jobs_mod._job_lock:
            jobs_mod._job_manager = None
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        storage = JobStorage(str(config_dir / "jobs.db"))
        terminal_chain = Job(
            id="abc-original-uuid",
            library_name="Already Done",
            status=JobStatus.COMPLETED,
            config={
                "is_retry_chain": True,
                "retry_chain_for": "/data/Foo.mkv",
                "retry_attempt": 3,
                "retry_max_attempts": 5,
            },
        )
        storage.upsert(terminal_chain)
        storage.close()

        jm = JobManager(config_dir=str(config_dir))
        recovered = jm.get_job("abc-original-uuid")
        assert recovered is not None
        assert recovered.status == JobStatus.COMPLETED

    def test_legacy_retry_hash_rows_dropped_at_load(self, tmp_path):
        """Pre-rewrite chain rows had IDs like
        ``retry-deadbeef00000000``. These are obsolete after the
        rewrite — the chain identity is now the originating UUID.
        ``_load_jobs`` must drop them at startup AND remove them
        from disk so they don't re-appear on the next graceful exit.
        """
        import media_preview_generator.web.jobs as jobs_mod

        with jobs_mod._job_lock:
            jobs_mod._job_manager = None
        config_dir = tmp_path / "legacy_config"
        config_dir.mkdir()
        storage = JobStorage(str(config_dir / "jobs.db"))

        legacy_chain = Job(
            id="retry-deadbeef00000000",
            library_name="Old Schema",
            status=JobStatus.COMPLETED,
            config={"is_retry_chain": True},
        )
        normal_job = Job(id="normal-uuid", library_name="Normal")
        storage.upsert(legacy_chain)
        storage.upsert(normal_job)
        storage.close()

        jm = JobManager(config_dir=str(config_dir))
        loaded_ids = [j.id for j in jm.get_all_jobs()]
        assert "retry-deadbeef00000000" not in loaded_ids, f"Legacy retry-<hash> row survived load; got {loaded_ids}"
        assert "normal-uuid" in loaded_ids, "Cleanup is over-aggressive — non-retry rows must survive."


class TestCompleteJobChainActiveGuard:
    """When the worker's post-dispatch ``complete_job`` runs on a Job
    that has been mutated into chain mode, the status transition must
    be skipped — the chain's own state machine drives the lifecycle.
    But the worker-pool BOOKKEEPING must still be cleared (the dispatch
    IS done; that slot is released).
    """

    def test_complete_job_skips_status_when_chain_active(self, jm):
        original = _seed_originating_job(jm)
        # Chain mutates the Job to PENDING (mid-chain re-arm)
        jm.upsert_retry_chain_job(
            canonical_path="/x.mkv",
            basename="x",
            attempt=2,
            max_attempts=5,
            next_run_at=None,
            wait_seconds=30,
            outcome="scheduled",
            originating_job_id=original.id,
        )
        chain = jm.get_job(original.id)
        assert chain.status == JobStatus.PENDING

        # Worker's complete_job tries to mark COMPLETED — must be no-op
        # for the status transition.
        jm.complete_job(original.id)
        after = jm.get_job(original.id)
        assert after.status == JobStatus.PENDING, (
            f"complete_job must NOT overwrite chain-active status; got {after.status}"
        )
        assert after.config.get("is_retry_chain") is True
        # last_outcome stays "scheduled" (chain's view) — complete_job
        # didn't drive the state machine.
        assert after.config["last_outcome"] == "scheduled"

    def test_complete_job_clears_bookkeeping_when_chain_active(self, jm):
        """The guard skips the status transition but MUST still clear
        the worker-pool bookkeeping. Otherwise the Job leaks in
        ``_running_job_ids``, the pause/cancel flags remain, and a
        future retry firing's cancellation check would trip on a
        stale flag (architecture-review MED at commit-time).
        """
        original = _seed_originating_job(jm)
        # Simulate worker bookkeeping that the dispatch set up.
        jm._running_job_ids.add(original.id)
        jm.request_cancellation(original.id)
        # Chain mutation
        jm.upsert_retry_chain_job(
            canonical_path="/x.mkv",
            basename="x",
            attempt=1,
            max_attempts=5,
            next_run_at=None,
            wait_seconds=30,
            outcome="scheduled",
            originating_job_id=original.id,
        )
        # Worker completes its dispatch
        jm.complete_job(original.id)

        assert original.id not in jm._running_job_ids, (
            "Chain-active guard must still discard from _running_job_ids "
            "(the dispatch IS done; worker slot is released)."
        )
        assert not jm.is_cancellation_requested(original.id), (
            "Chain-active guard must still clear the cancellation flag — "
            "leaving it set would trip a future retry firing's cancel check."
        )


class TestCancelledIsSticky:
    def test_cancelled_chain_not_resurrected_by_late_upsert(self, jm):
        """TOCTOU race guard: if a Timer's _callback is mid-firing
        when the user clicks Cancel, the cascade marks the chain
        CANCELLED. The callback's post-process upsert must NOT
        overwrite that terminal state."""
        original = _seed_originating_job(jm)
        jm.upsert_retry_chain_job(
            canonical_path="/x.mkv",
            basename="x",
            attempt=1,
            max_attempts=5,
            next_run_at=None,
            wait_seconds=30,
            outcome="scheduled",
            originating_job_id=original.id,
        )
        # User cancels (sets status=CANCELLED)
        jm.cancel_job(original.id)
        assert jm.get_job(original.id).status == JobStatus.CANCELLED

        # Late callback tries to mark "completed" — must be ignored
        job = jm.upsert_retry_chain_job(
            canonical_path="/x.mkv",
            basename="x",
            attempt=2,
            max_attempts=5,
            next_run_at=None,
            wait_seconds=None,
            outcome="completed",
            originating_job_id=original.id,
        )
        assert job.status == JobStatus.CANCELLED, (
            f"CANCELLED must be sticky; got {job.status} after late-callback upsert"
        )


class TestTitleHeuristic:
    def test_extension_bearing_basename_loses_to_clean(self, jm):
        """Originating Job's clean title must NOT be clobbered by a
        shorter raw-.mkv basename arriving in a later upsert
        (heuristic: extension-bearing titles always lose)."""
        # Create with the clean Sonarr title
        original = jm.create_job(library_name="Ruqyah The Exorcism (2017)", config={})
        jm.complete_job(original.id)

        job = jm.upsert_retry_chain_job(
            canonical_path="/data/Foo.mkv",
            basename="Foo.mkv",  # shorter but dirty
            attempt=2,
            max_attempts=5,
            next_run_at=None,
            wait_seconds=120,
            outcome="scheduled",
            originating_job_id=original.id,
        )
        assert job.library_name == "Ruqyah The Exorcism (2017)", (
            f"Extension-bearing basename must not win; got {job.library_name!r}"
        )

    def test_clean_basename_wins_over_dirty_existing(self, jm):
        """Inverse: if the originating dispatch happened to be created
        with a raw .mkv title (rare but possible), and a later chain
        upsert has a clean title, the clean one must win."""
        original = jm.create_job(library_name="Foo.mkv", config={})
        jm.complete_job(original.id)

        job = jm.upsert_retry_chain_job(
            canonical_path="/data/Foo.mkv",
            basename="A Very Long Clean Movie Title (2024)",
            attempt=1,
            max_attempts=5,
            next_run_at=None,
            wait_seconds=30,
            outcome="scheduled",
            originating_job_id=original.id,
        )
        assert job.library_name == "A Very Long Clean Movie Title (2024)"


class TestSource:
    def test_source_persisted_on_first_mutation(self, jm):
        original = _seed_originating_job(jm)
        job = jm.upsert_retry_chain_job(
            canonical_path="/x.mkv",
            basename="x",
            attempt=1,
            max_attempts=5,
            next_run_at=None,
            wait_seconds=30,
            outcome="scheduled",
            source="sonarr",
            originating_job_id=original.id,
        )
        assert job.config["source"] == "sonarr"

    def test_source_first_writer_wins(self, jm):
        """A later upsert with source=None must not clobber the first
        upsert's source value."""
        original = _seed_originating_job(jm)
        jm.upsert_retry_chain_job(
            canonical_path="/x.mkv",
            basename="x",
            attempt=1,
            max_attempts=5,
            next_run_at=None,
            wait_seconds=30,
            outcome="scheduled",
            source="radarr",
            originating_job_id=original.id,
        )
        job = jm.upsert_retry_chain_job(
            canonical_path="/x.mkv",
            basename="x",
            attempt=2,
            max_attempts=5,
            next_run_at=None,
            wait_seconds=120,
            outcome="scheduled",
            source=None,
            originating_job_id=original.id,
        )
        assert job.config["source"] == "radarr"
