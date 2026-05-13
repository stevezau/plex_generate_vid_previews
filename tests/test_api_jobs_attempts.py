"""Tests for the retry-chain Attempts API surface (post-2026-05-13 refactor).

* ``GET /api/jobs`` default-hides retry-child rows (``is_retry: True``
  with no ``is_retry_chain`` flag) so the dashboard shows ONE row per
  dispatch — the chain head — regardless of how many retry attempts
  are in flight.

* ``GET /api/jobs?include_retry_attempts=1`` opts in (debug, scripting).

* ``GET /api/jobs/<chain_id>/attempts`` walks children by
  ``parent_job_id == chain_id`` and returns metadata sorted by
  ``retry_attempt`` ascending — powers the Job Details modal's
  Attempts dropdown.

The legacy ``is_retry_attempt`` flag (set by the deleted per-file retry
queue) is still honoured by the filter for back-compat with any
persisted rows from older versions.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from media_preview_generator.web.app import create_app


@pytest.fixture()
def app(tmp_path):
    config_dir = str(tmp_path / "cfg")
    os.makedirs(config_dir, exist_ok=True)
    with patch.dict(
        os.environ,
        {
            "CONFIG_DIR": config_dir,
            "WEB_AUTH_TOKEN": "test-token-12345678",
            "WEB_PORT": "8099",
        },
    ):
        flask_app = create_app(config_dir=config_dir)
        flask_app.config["TESTING"] = True
        flask_app.config["WTF_CSRF_ENABLED"] = False
        yield flask_app


@pytest.fixture()
def client(app):
    return app.test_client()


def _headers():
    return {"Authorization": "Bearer test-token-12345678"}


def _seed_chain_with_attempts(
    jm,
    *,
    canonical_path: str,
    basename: str,
    num_attempts: int,
    originating_job_id: str | None = None,
):
    """Seed an originating dispatch Job mutated into chain mode +
    N per-attempt child Jobs.

    Post-rewrite the chain IS the originating Job — same UUID. If
    ``originating_job_id`` isn't provided, create a fresh originating
    Job and use its id.

    Returns ``(chain_id, [attempt_ids])`` where chain_id is the
    originating Job's UUID.
    """
    if originating_job_id is None:
        origin = jm.create_job(library_name=basename, config={})
        jm.complete_job(origin.id)
        originating_job_id = origin.id

    chain = jm.upsert_retry_chain_job(
        canonical_path=canonical_path,
        basename=basename,
        attempt=num_attempts,
        max_attempts=5,
        next_run_at=None,
        wait_seconds=30,
        outcome="scheduled",
        originating_job_id=originating_job_id,
    )
    # chain might be None if originating_job_id didn't exist (e.g.
    # caller passed a fake UUID for the orphan-original test). Return
    # the originating_job_id either way so callers can pin behavior.
    if chain is None:
        # Edge case for the orphan test: the chain Job is gone. Use a
        # synthetic chain ID by creating a fresh Job manually (still
        # not via upsert) — we need SOME chain to anchor child
        # attempts so the orphan test can assert deleted-sentinel.
        chain_id = originating_job_id
    else:
        chain_id = chain.id
    attempt_ids = []
    for i in range(1, num_attempts + 1):
        # Post-2026-05-13 retry children carry is_retry=True with
        # parent_job_id pointing at the chain head's UUID. The legacy
        # is_retry_attempt + parent_chain_id pair (set by the deleted
        # per-file retry queue) is gone.
        attempt = jm.create_job(
            library_name=basename,
            config={
                "is_retry": True,
                "parent_job_id": chain_id,
                "retry_attempt": i,
                "retry_max_attempts": 5,
                "max_retries": 5,
            },
        )
        attempt_ids.append(attempt.id)
        jm.complete_job(attempt.id)
    return chain_id, attempt_ids


class TestJobsListFilter:
    def test_jobs_list_hides_attempts_by_default(self, client):
        from media_preview_generator.web.jobs import get_job_manager

        jm = get_job_manager()
        _, attempt_ids = _seed_chain_with_attempts(
            jm,
            canonical_path="/data/Foo.mkv",
            basename="Foo",
            num_attempts=3,
        )

        resp = client.get("/api/jobs?page=0", headers=_headers())
        assert resp.status_code == 200
        data = resp.get_json()
        returned_ids = {j["id"] for j in data["jobs"]}
        assert returned_ids.isdisjoint(set(attempt_ids)), (
            f"Attempt rows must be hidden from /api/jobs by default; got {returned_ids & set(attempt_ids)}"
        )
        # Chain row IS visible.
        # Chain Job IS the originating dispatch's UUID (no retry-<hash> prefix).
        # The chain is recognised by config.is_retry_chain.
        assert any(j.get("config", {}).get("is_retry_chain") for j in data["jobs"]), (
            "Chain row must remain visible in the default response."
        )

    def test_jobs_list_includes_attempts_with_opt_in_flag(self, client):
        from media_preview_generator.web.jobs import get_job_manager

        jm = get_job_manager()
        _, attempt_ids = _seed_chain_with_attempts(
            jm,
            canonical_path="/data/Foo.mkv",
            basename="Foo",
            num_attempts=3,
        )

        resp = client.get("/api/jobs?page=0&include_retry_attempts=1", headers=_headers())
        assert resp.status_code == 200
        data = resp.get_json()
        returned_ids = {j["id"] for j in data["jobs"]}
        for aid in attempt_ids:
            assert aid in returned_ids, (
                f"Attempt {aid} missing when include_retry_attempts=1 — opt-in must return them all"
            )

    def test_jobs_list_pagination_total_reflects_filter(self, client):
        """`total` and `pages` in the paginated response must reflect
        the FILTERED list — otherwise the dashboard renders empty
        pages because total counts hidden rows.
        """
        from media_preview_generator.web.jobs import get_job_manager

        jm = get_job_manager()
        _seed_chain_with_attempts(
            jm,
            canonical_path="/data/Foo.mkv",
            basename="Foo",
            num_attempts=5,
        )

        default_resp = client.get("/api/jobs?page=0", headers=_headers())
        opt_in_resp = client.get("/api/jobs?page=0&include_retry_attempts=1", headers=_headers())

        default_total = default_resp.get_json()["total"]
        opt_in_total = opt_in_resp.get_json()["total"]
        assert opt_in_total - default_total == 5, (
            f"Opt-in must add exactly 5 attempt rows; got delta = {opt_in_total - default_total}"
        )


class TestAttemptsEndpoint:
    def test_returns_attempts_with_original_prepended(self, client):
        """Post-rewrite the chain IS the originating dispatch. The
        endpoint returns the original as Attempt 0 (is_originating=True)
        followed by each retry firing in ascending order.
        """
        from media_preview_generator.web.jobs import get_job_manager

        jm = get_job_manager()
        chain_id, attempt_ids = _seed_chain_with_attempts(
            jm,
            canonical_path="/data/Foo.mkv",
            basename="Foo",
            num_attempts=4,
        )

        resp = client.get(f"/api/jobs/{chain_id}/attempts", headers=_headers())
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["chain_id"] == chain_id
        assert data["max_attempts"] == 5
        # First entry: originating dispatch (is_originating=True, retry_attempt=0).
        # Subsequent entries: retry firings sorted ascending.
        nums = [a["retry_attempt"] for a in data["attempts"]]
        assert nums == [0, 1, 2, 3, 4], f"Expected [0,1,2,3,4] (original + 4 retries); got {nums}"
        # First entry IS the chain itself (originating dispatch)
        assert data["attempts"][0]["is_originating"] is True
        assert data["attempts"][0]["id"] == chain_id
        # Subsequent entries are the per-firing attempt Jobs
        for a in data["attempts"][1:]:
            assert a["is_originating"] is False
            assert a["id"] in set(attempt_ids), f"Attempt {a['id']!r} not in seeded ids"
            assert a["status"] == "completed"
            assert a["completed_at"] is not None
            assert a["duration_sec"] is not None and a["duration_sec"] >= 0

    def test_returns_404_for_non_chain_job(self, client):
        from media_preview_generator.web.jobs import get_job_manager

        jm = get_job_manager()
        # Regular (non-chain) Job
        regular = jm.create_job(library_name="Not a chain", config={})

        resp = client.get(f"/api/jobs/{regular.id}/attempts", headers=_headers())
        assert resp.status_code == 404, f"Non-chain jobs must 404 on /attempts; got {resp.status_code}"

    def test_returns_404_for_unknown_id(self, client):
        # Use a UUID-shaped id that doesn't exist
        resp = client.get("/api/jobs/00000000-0000-0000-0000-000000000000/attempts", headers=_headers())
        assert resp.status_code == 404

    def test_returns_only_original_for_chain_with_no_firings(self, client):
        """A chain with no retry firings yet still has one entry:
        the originating dispatch itself (Attempt 0). Dropdown renders
        that as the only option until the first firing spawns."""
        from media_preview_generator.web.jobs import get_job_manager

        jm = get_job_manager()
        chain_id, _ = _seed_chain_with_attempts(
            jm,
            canonical_path="/data/NoFiringsYet.mkv",
            basename="NoFiringsYet",
            num_attempts=0,
        )
        resp = client.get(f"/api/jobs/{chain_id}/attempts", headers=_headers())
        assert resp.status_code == 200
        data = resp.get_json()
        # Even with zero retry firings, the original dispatch is
        # always present (chain IS the original).
        assert len(data["attempts"]) == 1
        assert data["attempts"][0]["is_originating"] is True
        assert data["attempts"][0]["id"] == chain_id
        assert data["max_attempts"] == 5

    def test_requires_auth(self, client):
        """The endpoint sits behind @api_token_required — calls without
        a valid token MUST be rejected.
        """
        resp = client.get("/api/jobs/00000000-0000-0000-0000-000000000000/attempts")
        assert resp.status_code in (401, 403), (
            f"Endpoint must require auth; unauthenticated request got {resp.status_code}"
        )

    def test_attempts_endpoint_includes_pending_servers(self, client):
        """Each retry entry's ``pending_servers`` lists the servers
        that still had files in ``published_pending_registration`` /
        ``skipped_not_indexed`` / ``skipped_not_in_library`` state at
        the moment that retry completed. The frontend renders one
        vendor icon per pending server on the matching pill — without
        this data the modal can't tell the operator WHY a chain ran 4
        runs (e.g. "Jellyfin was still indexing for 16 minutes").

        Contract:
          * Originating-dispatch entry (chain head) ALWAYS has
            ``pending_servers: []`` regardless of state. The chain
            head's ``publishers`` snapshot is refreshed at terminal
            so reconstructing the initial pending state isn't worth
            the complexity.
          * Each retry-child entry has its own ``pending_servers``
            derived from THAT child's ``publishers`` field (which is
            the per-run snapshot, not the chain aggregate).
        """
        from media_preview_generator.web.jobs import get_job_manager

        jm = get_job_manager()
        # Seed: chain head + 3 retry children covering the three cells
        # that matter:
        #   1. one server pending (single chip)
        #   2. two servers pending (multi chip — verifies the helper
        #      emits one entry per pending publisher, not just the
        #      first one)
        #   3. zero servers pending (clean attempt; helper returns [])
        chain_id, attempt_ids = _seed_chain_with_attempts(
            jm,
            canonical_path="/data/Foo.mkv",
            basename="Foo",
            num_attempts=3,
        )
        # Patch each retry child's publishers field to simulate the
        # per-run snapshot the orchestrator writes via set_publishers.
        first_child = jm.get_job(attempt_ids[0])
        first_child.publishers = [
            {
                "server_id": "jelly-1",
                "server_name": "JellyTest",
                "server_type": "jellyfin",
                "counts": {"published_pending_registration": 4},
            },
            {
                "server_id": "plex-1",
                "server_name": "Plex",
                "server_type": "plex",
                "counts": {"published": 4},
            },
        ]
        # Retry child 2: TWO servers pending — Jellyfin (4 files) AND
        # Emby (1 file). Exercises the multi-publisher path so a
        # regression that emits only the first pending server (e.g. a
        # stray ``break`` in the helper) fails this row.
        second_child = jm.get_job(attempt_ids[1])
        second_child.publishers = [
            {
                "server_id": "jelly-1",
                "server_name": "JellyTest",
                "server_type": "jellyfin",
                "counts": {"published_pending_registration": 4},
            },
            {
                "server_id": "emby-1",
                "server_name": "EmbyTest",
                "server_type": "emby",
                "counts": {"skipped_not_indexed": 1, "published": 3},
            },
            {
                "server_id": "plex-1",
                "server_name": "Plex",
                "server_type": "plex",
                "counts": {"published": 4},
            },
        ]
        # Retry child 3: nothing pending — chain succeeded on attempt 3.
        third_child = jm.get_job(attempt_ids[2])
        third_child.publishers = [
            {
                "server_id": "jelly-1",
                "server_name": "JellyTest",
                "server_type": "jellyfin",
                "counts": {"skipped_output_exists": 4},
            },
            {
                "server_id": "plex-1",
                "server_name": "Plex",
                "server_type": "plex",
                "counts": {"skipped_output_exists": 4},
            },
        ]
        with jm._lock:  # noqa: SLF001
            jm._persist_job(first_child)  # noqa: SLF001
            jm._persist_job(second_child)  # noqa: SLF001
            jm._persist_job(third_child)  # noqa: SLF001

        resp = client.get(f"/api/jobs/{chain_id}/attempts", headers=_headers())
        assert resp.status_code == 200
        attempts = resp.get_json()["attempts"]

        # Originating-dispatch entry: pending_servers ALWAYS empty.
        # Contract: chain head's publishers is post-retry truth, not
        # initial state.
        originating = next(a for a in attempts if a["is_originating"])
        assert originating["pending_servers"] == [], (
            f"Originating entry must have empty pending_servers regardless of "
            f"chain state; got {originating['pending_servers']!r}"
        )

        # Retry child 1: Jellyfin still pending → exactly 1 row,
        # carrying the correct server attribution + count.
        child1 = next(a for a in attempts if a["id"] == attempt_ids[0])
        assert len(child1["pending_servers"]) == 1, (
            f"Retry 1 must show Jellyfin as the lone blocker; got {child1['pending_servers']!r}"
        )
        assert child1["pending_servers"][0] == {
            "server_id": "jelly-1",
            "server_name": "JellyTest",
            "server_type": "jellyfin",
            "count": 4,
        }

        # Retry child 2: TWO servers pending → exactly 2 rows with
        # the correct per-server counts. The order isn't contractually
        # specified (the helper preserves Job.publishers iteration
        # order), so assert on the set/contents, not position.
        child2 = next(a for a in attempts if a["id"] == attempt_ids[1])
        child2_by_id = {p["server_id"]: p for p in child2["pending_servers"]}
        assert set(child2_by_id.keys()) == {"jelly-1", "emby-1"}, (
            f"Retry 2 must surface BOTH Jellyfin and Emby as pending; got {child2['pending_servers']!r}"
        )
        assert child2_by_id["jelly-1"]["count"] == 4, (
            f"Jellyfin pending count must reflect the per-server total (4 files in "
            f"published_pending_registration); got {child2_by_id['jelly-1']!r}"
        )
        assert child2_by_id["emby-1"]["count"] == 1, (
            f"Emby pending count must reflect only the pending statuses (1 skipped_not_indexed; "
            f"the 3 published files are not pending); got {child2_by_id['emby-1']!r}"
        )

        # Retry child 3: no pending state → empty list.
        child3 = next(a for a in attempts if a["id"] == attempt_ids[2])
        assert child3["pending_servers"] == [], (
            f"Retry 3 cleared everything; must have empty pending_servers; got {child3['pending_servers']!r}"
        )


class TestRetryNowEndpoint:
    """``POST /api/jobs/<id>/retry-now`` — operator action that fires the
    pending back-off retry immediately. Powers the modal footer's
    "Retry now" button.

    The endpoint's contract:
        200 + ``{"fired": True, ...}`` on successful fire
        404 when ``job_id`` is unknown
        400 when the job exists but is not a chain head
        409 when nothing is scheduled (chain terminal/cancelled/mid-firing)
        401/403 without auth
    """

    def test_fires_pending_retry_for_chain_head(self, client):
        """Post-2026-05-13: retry-now finds the chain's pending retry
        child Job (is_retry=True, parent_job_id=chain, status=PENDING)
        and sets ``force_fire_now`` on its config. The child's backoff-
        wait loop polls this flag each tick and breaks out early.
        """
        from media_preview_generator.web.jobs import JobStatus, get_job_manager

        jm = get_job_manager()
        # Seed with 1 attempt — the helper marks it COMPLETED by
        # default. We need a PENDING one for retry-now to find, so
        # override the status manually after seeding.
        chain_id, attempt_ids = _seed_chain_with_attempts(
            jm,
            canonical_path="/data/Beast (2026)/Beast.mkv",
            basename="Beast (2026)",
            num_attempts=1,
        )
        # Flip the seeded child back to PENDING so retry-now has a
        # target. The seed helper marks them COMPLETED to mirror the
        # general "history" pattern; here we need a live pending child.
        child = jm.get_job(attempt_ids[0])
        child.status = JobStatus.PENDING
        child.completed_at = None
        with jm._lock:  # noqa: SLF001
            jm._persist_job(child)  # noqa: SLF001

        resp = client.post(f"/api/jobs/{chain_id}/retry-now", headers=_headers())

        assert resp.status_code == 200, (
            f"Successful retry-now must return 200; got {resp.status_code} {resp.get_data(as_text=True)!r}"
        )
        body = resp.get_json()
        assert body["fired"] is True
        assert body["job_id"] == chain_id
        # The endpoint surfaces the retry Job's ID so the UI can drill
        # into the firing's logs immediately.
        assert body["retry_job_id"] == attempt_ids[0]
        # Verify force_fire_now was set on the pending child's config —
        # this is the signal the backoff-wait loop polls to skip the
        # remaining countdown.
        refreshed = jm.get_job(attempt_ids[0])
        assert (refreshed.config or {}).get("force_fire_now") is True, (
            f"force_fire_now must be set so the backoff loop bails; got config={refreshed.config!r}"
        )

    def test_returns_400_for_non_chain_job(self, client):
        from media_preview_generator.web.jobs import get_job_manager

        jm = get_job_manager()
        job = jm.create_job(library_name="Regular Job", config={})
        try:
            resp = client.post(f"/api/jobs/{job.id}/retry-now", headers=_headers())
            assert resp.status_code == 400
            assert resp.get_json()["error"] == "Not a retry-chain job"
        finally:
            jm.delete_job(job.id)

    def test_returns_404_for_unknown_id(self, client):
        resp = client.post(
            "/api/jobs/00000000-0000-0000-0000-000000000000/retry-now",
            headers=_headers(),
        )
        assert resp.status_code == 404

    def test_force_fire_now_reader_helper(self, app):
        """The polling-loop reader of ``force_fire_now`` lives in
        ``job_runner._is_force_fire_now_set`` and the writer lives in
        ``api_jobs.retry_now``. They must agree on the exact config
        key. This test pins the reader's behavior so a typo on either
        side (e.g. writing ``force_fire`` instead of ``force_fire_now``)
        fails loudly instead of silently disabling the "Retry now"
        button.
        """
        from media_preview_generator.web.jobs import get_job_manager
        from media_preview_generator.web.routes.job_runner import _is_force_fire_now_set

        with app.app_context():
            jm = get_job_manager()
            # Job without the flag → reader returns False.
            job = jm.create_job(library_name="Foo", config={})
            assert _is_force_fire_now_set(jm, job.id) is False, "Reader must return False when force_fire_now is absent"
            # Set the flag via the same path the writer uses (the
            # update_job_config call api_jobs.retry_now makes).
            new_cfg = dict(job.config or {})
            new_cfg["force_fire_now"] = True
            jm.update_job_config(job.id, new_cfg)
            assert _is_force_fire_now_set(jm, job.id) is True, (
                "Reader must return True once force_fire_now is set in config"
            )
            # Missing-Job race: reader must return False, not raise.
            assert _is_force_fire_now_set(jm, "nonexistent-uuid") is False, (
                "Reader must return False (not raise) when the Job has been deleted"
            )

    def test_attempts_endpoint_surfaces_legacy_children(self, client):
        """Pre-2026-05-13 retry children carry ``is_retry_attempt=True``
        and ``parent_chain_id=<chain>`` (not the new ``is_retry`` +
        ``parent_job_id`` pair). The /attempts endpoint MUST walk both
        flag pairs so legacy chains in jobs.db continue to surface
        their attempts after the refactor.

        Without this back-compat, the user's modal Attempts dropdown
        would be empty for every chain created before the refactor.
        Verified live against production data — 66 legacy chains have
        children using the old flags.
        """
        from media_preview_generator.web.jobs import get_job_manager

        jm = get_job_manager()
        # Create a chain head + legacy-style children manually so we can
        # pin the back-compat selector independently of the new flag pair.
        original = jm.create_job(library_name="Legacy Foo", config={})
        jm.complete_job(original.id)
        jm.upsert_retry_chain_job(
            canonical_path="/legacy/foo.mkv",
            basename="Legacy Foo",
            attempt=2,
            max_attempts=5,
            next_run_at=None,
            wait_seconds=30,
            outcome="scheduled",
            originating_job_id=original.id,
        )
        # Two legacy children — the old flags.
        legacy_child_ids = []
        for i in (1, 2):
            child = jm.create_job(
                library_name="Legacy Foo",
                config={
                    "is_retry_attempt": True,
                    "parent_chain_id": original.id,
                    "retry_attempt": i,
                    "retry_max_attempts": 5,
                },
            )
            jm.complete_job(child.id)
            legacy_child_ids.append(child.id)

        resp = client.get(f"/api/jobs/{original.id}/attempts", headers=_headers())
        assert resp.status_code == 200, resp.get_data(as_text=True)
        data = resp.get_json()
        # Originating dispatch is prepended as retry_attempt=0; legacy
        # children come after.
        returned_ids = [a["id"] for a in data["attempts"]]
        for cid in legacy_child_ids:
            assert cid in returned_ids, (
                f"Legacy child {cid} (is_retry_attempt + parent_chain_id) must surface in /attempts; "
                f"got ids={returned_ids}"
            )

    def test_returns_409_when_no_pending_retry(self, client):
        """A chain head whose retry children are all terminal (COMPLETED/
        FAILED/CANCELLED) has no pending child to fire. The endpoint
        surfaces this as 409 so the UI can show "Already running" /
        "Chain terminal" instead of silently no-op'ing.
        """
        from media_preview_generator.web.jobs import get_job_manager

        jm = get_job_manager()
        # The seed helper marks all children COMPLETED — no pending
        # child to fire, so retry-now should 409.
        chain_id, _ = _seed_chain_with_attempts(
            jm,
            canonical_path="/data/done.mkv",
            basename="Done",
            num_attempts=1,
        )

        resp = client.post(f"/api/jobs/{chain_id}/retry-now", headers=_headers())

        assert resp.status_code == 409
        body = resp.get_json()
        assert body["error"] == "No pending retry to fire"
        assert "hint" in body

    def test_requires_auth(self, client):
        resp = client.post("/api/jobs/00000000-0000-0000-0000-000000000000/retry-now")
        assert resp.status_code in (401, 403), f"retry-now must require auth; got {resp.status_code}"


class TestOriginatingDispatchFilter:
    """Single-row-per-file UX: when a chain row references an
    ``originating_job_id``, that worker-pool dispatch Job is hidden
    from the default /api/jobs list so the user sees ONE row per file.
    The original is accessible via the chain's modal Attempts dropdown
    (where it appears as "Original dispatch"), and via direct
    /api/jobs/<uuid> for power-user / debugging use.
    """

    def test_chain_is_the_originating_dispatch_uuid(self, client):
        """Post-rewrite the chain Job IS the originating dispatch
        (same UUID — chain identity = dispatch UUID). No separate
        retry-<hash> row. The chain row is visible in /api/jobs.
        """
        from media_preview_generator.web.jobs import get_job_manager

        jm = get_job_manager()
        chain_id, _ = _seed_chain_with_attempts(
            jm,
            canonical_path="/data/Marshals.mkv",
            basename="Marshals S01E11",
            num_attempts=2,
        )

        resp = client.get("/api/jobs?page=0", headers=_headers())
        assert resp.status_code == 200
        ids = {j["id"] for j in resp.get_json()["jobs"]}
        assert chain_id in ids, (
            f"Chain Job (originating dispatch's UUID) MUST be visible in the default list. Found ids: {ids}"
        )
        # No retry-<hash>-prefixed ids should appear (legacy rows are dropped at load)
        assert not any(i.startswith("retry-") for i in ids)

    def test_attempt_rows_hidden_default_visible_with_opt_in(self, client):
        from media_preview_generator.web.jobs import get_job_manager

        jm = get_job_manager()
        _, attempt_ids = _seed_chain_with_attempts(
            jm,
            canonical_path="/data/Marshals.mkv",
            basename="Marshals S01E11",
            num_attempts=3,
        )

        # Default: attempts hidden
        resp = client.get("/api/jobs?page=0", headers=_headers())
        ids_default = {j["id"] for j in resp.get_json()["jobs"]}
        assert ids_default.isdisjoint(set(attempt_ids)), (
            f"is_retry retry-child rows must be hidden from default list; found {ids_default & set(attempt_ids)}"
        )

        # Opt-in: attempts visible
        resp = client.get("/api/jobs?page=0&include_retry_attempts=1", headers=_headers())
        ids_opt = {j["id"] for j in resp.get_json()["jobs"]}
        for aid in attempt_ids:
            assert aid in ids_opt, f"Attempt {aid} missing with include_retry_attempts=1"
