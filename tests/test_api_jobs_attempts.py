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


def _seed_chain_with_attempts(jm, *, canonical_path: str, basename: str, num_attempts: int):
    """Seed a chain row + N per-attempt child Jobs.

    Returns ``(chain_id, [attempt_ids])`` so tests can assert on shape.
    """
    chain = jm.upsert_retry_chain_job(
        canonical_path=canonical_path,
        basename=basename,
        attempt=num_attempts,
        max_attempts=5,
        next_run_at=None,
        wait_seconds=30,
        outcome="scheduled",
    )
    attempt_ids = []
    for i in range(1, num_attempts + 1):
        attempt = jm.create_job(
            library_name=basename,
            config={
                "is_retry_attempt": True,
                "parent_chain_id": chain.id,
                "retry_attempt": i,
                "retry_max_attempts": 5,
                "is_retry": True,
                "max_retries": 5,
            },
        )
        attempt_ids.append(attempt.id)
        jm.complete_job(attempt.id)
    return chain.id, attempt_ids


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
        assert any(j["id"].startswith("retry-") for j in data["jobs"]), (
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
    def test_returns_sorted_attempts_for_chain(self, client):
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
        # Sorted ascending by retry_attempt.
        nums = [a["retry_attempt"] for a in data["attempts"]]
        assert nums == [1, 2, 3, 4], f"Attempts must sort by retry_attempt ascending; got {nums}"
        # Each entry's FIELD VALUES (not just key presence) are what
        # the modal dropdown reads to render its option labels — pin
        # actual values so a regression that returns ``status=None``
        # or ``completed_at=None`` for every attempt fails the test
        # instead of silently passing (D34-shape bug-blind gap).
        for a in data["attempts"]:
            assert set(["id", "retry_attempt", "status", "completed_at", "duration_sec"]).issubset(a.keys())
            assert a["status"] == "completed", (
                f"Seeded attempts called complete_job → status should be 'completed'; got {a['status']!r}"
            )
            assert a["completed_at"] is not None, (
                f"Completed attempts must surface completed_at so the dropdown can show duration; "
                f"got None for {a['id']}"
            )
            assert a["duration_sec"] is not None and a["duration_sec"] >= 0
            assert a["id"] in set(attempt_ids), f"Attempt id {a['id']!r} doesn't match any seeded child"

    def test_returns_404_for_non_chain_job(self, client):
        from media_preview_generator.web.jobs import get_job_manager

        jm = get_job_manager()
        # Regular (non-chain) Job
        regular = jm.create_job(library_name="Not a chain", config={})

        resp = client.get(f"/api/jobs/{regular.id}/attempts", headers=_headers())
        assert resp.status_code == 404, f"Non-chain jobs must 404 on /attempts; got {resp.status_code}"

    def test_returns_404_for_unknown_id(self, client):
        resp = client.get("/api/jobs/retry-doesnotexist0000/attempts", headers=_headers())
        assert resp.status_code == 404

    def test_returns_empty_attempts_for_chain_with_no_firings(self, client):
        """A chain row can exist before its first firing has spawned an
        attempt Job — the endpoint must still 200 with an empty list
        rather than 404 so the dropdown can render 'No attempts yet'.
        """
        from media_preview_generator.web.jobs import get_job_manager

        jm = get_job_manager()
        chain = jm.upsert_retry_chain_job(
            canonical_path="/data/NoFiringsYet.mkv",
            basename="NoFiringsYet",
            attempt=1,
            max_attempts=5,
            next_run_at=None,
            wait_seconds=30,
            outcome="scheduled",
        )

        resp = client.get(f"/api/jobs/{chain.id}/attempts", headers=_headers())
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["attempts"] == []
        assert data["max_attempts"] == 5

    def test_requires_auth(self, client):
        """The endpoint sits behind @api_token_required — calls without
        a valid token MUST be rejected.
        """
        resp = client.get("/api/jobs/retry-anything/attempts")
        assert resp.status_code in (401, 403), (
            f"Endpoint must require auth; unauthenticated request got {resp.status_code}"
        )
