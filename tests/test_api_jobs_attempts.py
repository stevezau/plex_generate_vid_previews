"""Tests for the retry-chain Attempts API surface:

* ``GET /api/jobs`` default-hides ``is_retry_attempt`` rows so the
  dashboard shows ONE row per file (the chain row) regardless of how
  many retry firings are in flight. Pre-PLAN-collapse attempt rows
  appeared in the main list, producing ~600/day during JellyTest
  backfill.

* ``GET /api/jobs?include_retry_attempts=1`` opts in (debug, scripting).

* ``GET /api/jobs/<chain_id>/attempts`` returns per-attempt child
  metadata sorted by ``retry_attempt`` ascending — powers the Job
  Details modal's Attempts dropdown.
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
        attempt = jm.create_job(
            library_name=basename,
            config={
                "is_retry_attempt": True,
                "parent_chain_id": chain_id,
                "retry_attempt": i,
                "retry_max_attempts": 5,
                "is_retry": True,
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


class TestLogsStreamEndpoint:
    """``GET /api/jobs/<id>/logs/stream`` — Tier 3.11 SSE endpoint.

    Verifies the response shape (mimetype + headers + initial flush
    behaviour) without trying to assert long-lived streaming, which
    would block the test client. The endpoint's 60-s connection cap
    means a full integration test would hang; instead we exercise
    the first burst (initial flush of existing log lines + the
    streaming framing) and let the server-side generator close on
    its own deadline.
    """

    def test_404_for_unknown_job(self, client):
        resp = client.get(
            "/api/jobs/00000000-0000-0000-0000-000000000000/logs/stream",
            headers=_headers(),
        )
        assert resp.status_code == 404
        assert resp.get_json()["error"] == "Job not found"

    def test_requires_auth(self, client):
        resp = client.get("/api/jobs/x/logs/stream")
        assert resp.status_code in (401, 403)

    def test_sets_text_event_stream_mimetype_and_no_buffering_headers(self, client):
        """Proxies (nginx, Cloudflare) default to buffering responses,
        which collapses SSE into a single chunk at end-of-connection.
        These headers defeat that behaviour — without them, the modal
        would see all log lines arrive in one burst when the 60-s
        connection cap closes the stream.
        """
        from media_preview_generator.web.jobs import get_job_manager

        jm = get_job_manager()
        job = jm.create_job(library_name="SSE smoke", config={})
        try:
            # Patch ``time.monotonic`` directly — the endpoint does
            # ``import time`` locally, so module-level patching of
            # ``api_jobs.time`` doesn't work. Each call: 0 (set
            # deadline) -> 100 (past deadline → loop exits without
            # sleep).
            with (
                patch("time.monotonic", side_effect=[0.0, 100.0, 100.0, 100.0]),
                patch("time.sleep", lambda *a, **kw: None),
            ):
                resp = client.get(
                    f"/api/jobs/{job.id}/logs/stream",
                    headers=_headers(),
                )
                # Drain the generator so any deferred work happens
                # while the test holds the test-client context.
                data = resp.get_data(as_text=True)
            assert resp.status_code == 200
            assert resp.mimetype == "text/event-stream", (
                f"SSE endpoint must return mimetype text/event-stream; got {resp.mimetype}"
            )
            assert resp.headers.get("X-Accel-Buffering") == "no", (
                "Must set X-Accel-Buffering: no so nginx doesn't buffer the stream."
            )
            cache_control = resp.headers.get("Cache-Control", "")
            assert "no-cache" in cache_control and "no-transform" in cache_control, (
                f"Cache-Control must include no-cache + no-transform; got {cache_control!r}"
            )
            # Connection ends with a reconnect event so EventSource
            # can re-open without the 3 s default backoff.
            assert "event: reconnect" in data, (
                f"Stream must close with 'event: reconnect' so the client re-opens cleanly. Got payload: {data!r}"
            )
        finally:
            jm.delete_job(job.id)


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
        from media_preview_generator.web.jobs import get_job_manager

        jm = get_job_manager()
        chain_id, _ = _seed_chain_with_attempts(
            jm,
            canonical_path="/data/Beast (2026)/Beast.mkv",
            basename="Beast (2026)",
            num_attempts=1,
        )

        # The seed helper doesn't actually schedule a Timer; mock
        # fire_now so the endpoint thinks one was pending. This is the
        # right boundary to mock — the endpoint forwards to
        # RetryScheduler and reports the result; the scheduler's own
        # behaviour is exercised in TestRetrySchedulerFireNow.
        with patch("media_preview_generator.processing.retry_queue.get_retry_scheduler") as gs:
            gs.return_value.fire_now.return_value = True
            resp = client.post(f"/api/jobs/{chain_id}/retry-now", headers=_headers())

        assert resp.status_code == 200, (
            f"Successful retry-now must return 200; got {resp.status_code} {resp.get_data(as_text=True)!r}"
        )
        body = resp.get_json()
        assert body["fired"] is True
        assert body["job_id"] == chain_id
        # canonical_path round-trips so the operator's UI can confirm
        # the right file was kicked.
        assert body["canonical_path"] == "/data/Beast (2026)/Beast.mkv"
        # Verify the SUT called fire_now with the chain's canonical_path
        # (not the job_id) — the scheduler keys by path, not UUID.
        gs.return_value.fire_now.assert_called_once_with("/data/Beast (2026)/Beast.mkv")

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

    def test_returns_409_when_no_pending_retry(self, client):
        """A chain head whose timer has already fired (or was cancelled)
        has nothing for ``fire_now`` to invoke. The endpoint surfaces
        this as 409 so the UI can show "Already running" / "Chain
        terminal" instead of silently no-op'ing.
        """
        from media_preview_generator.web.jobs import get_job_manager

        jm = get_job_manager()
        chain_id, _ = _seed_chain_with_attempts(
            jm,
            canonical_path="/data/done.mkv",
            basename="Done",
            num_attempts=1,
        )

        with patch("media_preview_generator.processing.retry_queue.get_retry_scheduler") as gs:
            gs.return_value.fire_now.return_value = False
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
            f"is_retry_attempt rows must be hidden from default list; found {ids_default & set(attempt_ids)}"
        )

        # Opt-in: attempts visible
        resp = client.get("/api/jobs?page=0&include_retry_attempts=1", headers=_headers())
        ids_opt = {j["id"] for j in resp.get_json()["jobs"]}
        for aid in attempt_ids:
            assert aid in ids_opt, f"Attempt {aid} missing with include_retry_attempts=1"
