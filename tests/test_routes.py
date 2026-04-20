"""
Unit tests for Flask web routes.

Covers: login flow, auth API, settings CRUD, job management,
setup wizard, token endpoints, health check, and schedule endpoints.
Uses Flask's test client with an in-memory config dir.
"""

import json
import os
import threading
from unittest.mock import MagicMock, patch

import pytest

from plex_generate_previews.config import normalize_path_mappings
from plex_generate_previews.web.app import create_app
from plex_generate_previews.web.settings_manager import reset_settings_manager

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_singletons():
    """Reset web singletons between tests to avoid cross-contamination."""
    reset_settings_manager()
    # Also reset the jobs singleton
    import plex_generate_previews.web.jobs as jobs_mod

    with jobs_mod._job_lock:
        jobs_mod._job_manager = None
    # Reset schedule singleton
    import plex_generate_previews.web.scheduler as sched_mod

    with sched_mod._schedule_lock:
        sched_mod._schedule_manager = None
    # Clear GPU detection and library caches
    from plex_generate_previews.web.routes import clear_gpu_cache, clear_library_cache

    clear_gpu_cache()
    clear_library_cache()
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
    clear_gpu_cache()
    clear_library_cache()


@pytest.fixture()
def app(tmp_path):
    """Create a Flask test app with a temporary config directory."""
    config_dir = str(tmp_path / "config")
    os.makedirs(config_dir, exist_ok=True)

    # Write a known auth token so tests can authenticate
    auth_file = os.path.join(config_dir, "auth.json")
    with open(auth_file, "w") as f:
        json.dump({"token": "test-token-12345678"}, f)

    # Mark setup as complete so before_request doesn't redirect to /setup
    settings_file = os.path.join(config_dir, "settings.json")
    with open(settings_file, "w") as f:
        json.dump({"setup_complete": True}, f)

    with patch.dict(
        os.environ,
        {
            "CONFIG_DIR": config_dir,
            "WEB_AUTH_TOKEN": "test-token-12345678",
            "WEB_PORT": "8099",
        },
    ):
        # Create app with threading async_mode (test client works with any mode)
        flask_app = create_app(config_dir=config_dir)
        flask_app.config["TESTING"] = True
        # Disable CSRF for test convenience (routes already exempt the api blueprint)
        flask_app.config["WTF_CSRF_ENABLED"] = False
        yield flask_app


@pytest.fixture()
def client(app):
    """Flask test client."""
    return app.test_client()


@pytest.fixture()
def authed_client(client):
    """Flask test client with an authenticated session."""
    with client.session_transaction() as sess:
        sess["authenticated"] = True
    return client


def _api_headers(token: str = "test-token-12345678") -> dict:
    """Return headers for Bearer-token-authenticated API calls."""
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


# ---------------------------------------------------------------------------
# Page Routes
# ---------------------------------------------------------------------------


class TestPageRoutes:
    """Test HTML page routes."""

    def test_index_redirects_to_login_when_unauthenticated(self, client):
        resp = client.get("/", follow_redirects=False)
        assert resp.status_code in (302, 308)
        assert "/login" in resp.headers.get("Location", "")

    def test_login_page_renders(self, client):
        resp = client.get("/login")
        assert resp.status_code == 200
        assert b"token" in resp.data.lower() or b"login" in resp.data.lower()

    def test_settings_requires_auth(self, client):
        resp = client.get("/settings", follow_redirects=False)
        assert resp.status_code in (302, 308)

    def test_settings_accessible_when_authenticated(self, authed_client):
        resp = authed_client.get("/settings")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Login / Logout
# ---------------------------------------------------------------------------


class TestLoginLogout:
    """Test login and logout flows."""

    def test_login_post_valid_token(self, client):
        resp = client.post("/login", data={"token": "test-token-12345678"}, follow_redirects=False)
        # Should redirect to dashboard
        assert resp.status_code in (302, 308)
        assert "/login" not in resp.headers.get("Location", "")

    def test_login_post_invalid_token(self, client):
        resp = client.post("/login", data={"token": "wrong"})
        assert resp.status_code == 200
        assert b"didn" in resp.data or b"invalid" in resp.data or b"Invalid" in resp.data

    def test_already_authenticated_redirects_from_login(self, authed_client):
        resp = authed_client.get("/login", follow_redirects=False)
        assert resp.status_code in (302, 308)

    def test_logout_clears_session(self, authed_client):
        resp = authed_client.get("/logout", follow_redirects=False)
        assert resp.status_code in (302, 308)
        # Subsequent request should require login
        resp2 = authed_client.get("/", follow_redirects=False)
        assert resp2.status_code in (302, 308)
        assert "/login" in resp2.headers.get("Location", "")

    def test_login_rate_limit_exceeded(self, client):
        """After 5 POSTs to /login, 6th returns 429."""
        for _ in range(5):
            client.post("/login", data={"token": "wrong"})
        resp = client.post("/login", data={"token": "wrong"})
        assert resp.status_code == 429

    def test_api_login_rate_limit_exceeded(self, client):
        """After 10 POSTs to /api/auth/login, 11th returns 429."""
        for _ in range(10):
            client.post("/api/auth/login", json={"token": "wrong"})
        resp = client.post("/api/auth/login", json={"token": "wrong"})
        assert resp.status_code == 429


# ---------------------------------------------------------------------------
# Auth API
# ---------------------------------------------------------------------------


class TestAuthAPI:
    """Test /api/auth/* endpoints."""

    def test_auth_status_unauthenticated(self, client):
        resp = client.get("/api/auth/status")
        data = resp.get_json()
        assert data["authenticated"] is False

    def test_auth_status_authenticated(self, authed_client):
        resp = authed_client.get("/api/auth/status")
        data = resp.get_json()
        assert data["authenticated"] is True

    def test_api_login_valid(self, client):
        resp = client.post("/api/auth/login", json={"token": "test-token-12345678"})
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True

    def test_api_login_invalid(self, client):
        resp = client.post("/api/auth/login", json={"token": "bad"})
        assert resp.status_code == 401
        assert resp.get_json()["success"] is False

    def test_api_logout(self, authed_client):
        resp = authed_client.post("/api/auth/logout")
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True


# ---------------------------------------------------------------------------
# Health Check
# ---------------------------------------------------------------------------


class TestHealthCheck:
    """Test /api/health endpoint."""

    def test_health_no_auth_required(self, client):
        resp = client.get("/api/health")
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "healthy"


# ---------------------------------------------------------------------------
# Token Endpoints
# ---------------------------------------------------------------------------


class TestTokenEndpoints:
    """Test token regeneration and info endpoints."""

    def test_regenerate_token_requires_auth(self, client):
        resp = client.post("/api/token/regenerate")
        assert resp.status_code == 401

    def test_regenerate_token_returns_masked(self, client):
        resp = client.post("/api/token/regenerate", headers=_api_headers())
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["token"].startswith("****")

    def test_regenerate_token_invalidates_session(self, client):
        """After token regeneration, session is cleared; request with old session gets 401."""
        client.post("/api/auth/login", json={"token": "test-token-12345678"})
        client.post("/api/token/regenerate", headers=_api_headers())
        resp = client.get("/api/jobs")
        assert resp.status_code == 401

    def test_setup_token_info(self, client):
        resp = client.get("/api/setup/token-info", headers=_api_headers())
        assert resp.status_code == 200
        data = resp.get_json()
        assert "token" in data
        assert data["token"].startswith("****")
        assert "source" in data


# ---------------------------------------------------------------------------
# Jobs API
# ---------------------------------------------------------------------------


class TestJobsAPI:
    """Test /api/jobs/* endpoints."""

    def test_get_jobs_requires_auth(self, client):
        resp = client.get("/api/jobs")
        assert resp.status_code == 401

    def test_get_jobs_empty(self, client):
        resp = client.get("/api/jobs", headers=_api_headers())
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["jobs"] == []
        assert data["total"] == 0
        assert data["page"] == 1
        assert data["per_page"] == 50
        assert data["pages"] == 1

    def test_create_job(self, client):
        """Test creating a job (mocking _start_job_async to avoid real processing)."""
        with patch("plex_generate_previews.web.routes.api_jobs._start_job_async"):
            resp = client.post("/api/jobs", headers=_api_headers(), json={"library_name": "Movies"})
        assert resp.status_code == 201
        data = resp.get_json()
        assert data["status"] == "pending"
        assert "id" in data

    def test_create_job_ignores_credential_overrides(self, client):
        """Job creation accepts request with credential-like keys but allow-list prevents applying them."""
        with patch("plex_generate_previews.web.routes.api_jobs._start_job_async") as mock_start:
            resp = client.post(
                "/api/jobs",
                headers=_api_headers(),
                json={
                    "library_name": "Movies",
                    "config": {
                        "plex_token": "evil-token",
                        "plex_url": "http://evil.com",
                    },
                },
            )
        assert resp.status_code == 201
        mock_start.assert_called_once()
        assert mock_start.call_args[0][0]  # job_id present

    def test_get_specific_job(self, client):
        with patch("plex_generate_previews.web.routes.api_jobs._start_job_async"):
            create_resp = client.post("/api/jobs", headers=_api_headers(), json={"library_name": "TV"})
        job_id = create_resp.get_json()["id"]
        resp = client.get(f"/api/jobs/{job_id}", headers=_api_headers())
        assert resp.status_code == 200
        assert resp.get_json()["id"] == job_id

    def test_get_nonexistent_job(self, client):
        resp = client.get("/api/jobs/nonexistent", headers=_api_headers())
        assert resp.status_code == 404

    def test_cancel_job(self, client):
        with patch("plex_generate_previews.web.routes.api_jobs._start_job_async"):
            create_resp = client.post("/api/jobs", headers=_api_headers(), json={})
        job_id = create_resp.get_json()["id"]
        resp = client.post(f"/api/jobs/{job_id}/cancel", headers=_api_headers())
        assert resp.status_code == 200
        logs_resp = client.get(f"/api/jobs/{job_id}/logs", headers=_api_headers())
        logs = logs_resp.get_json()["logs"]
        assert any("Cancellation requested by user" in line for line in logs)

    def test_complete_job_does_not_override_cancelled_status(self, client):
        with patch("plex_generate_previews.web.routes.api_jobs._start_job_async"):
            create_resp = client.post("/api/jobs", headers=_api_headers(), json={})
        job_id = create_resp.get_json()["id"]

        from plex_generate_previews.web.jobs import get_job_manager

        jm = get_job_manager()
        jm.start_job(job_id)
        jm.request_cancellation(job_id)
        jm.cancel_job(job_id)
        jm.complete_job(job_id)

        resp = client.get(f"/api/jobs/{job_id}", headers=_api_headers())
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "cancelled"

    def test_delete_job(self, client):
        with patch("plex_generate_previews.web.routes.api_jobs._start_job_async"):
            create_resp = client.post("/api/jobs", headers=_api_headers(), json={})
        job_id = create_resp.get_json()["id"]
        # Cancel first so it can be deleted (only non-running jobs)
        client.post(f"/api/jobs/{job_id}/cancel", headers=_api_headers())
        resp = client.delete(f"/api/jobs/{job_id}", headers=_api_headers())
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True

    def test_delete_nonexistent_job(self, client):
        resp = client.delete("/api/jobs/nonexistent", headers=_api_headers())
        assert resp.status_code == 404

    def test_clear_jobs(self, client):
        resp = client.post("/api/jobs/clear", headers=_api_headers())
        assert resp.status_code == 200
        assert "cleared" in resp.get_json()

    def test_clear_jobs_with_status_filter(self, client):
        """Test clearing only specific statuses."""
        with patch("plex_generate_previews.web.routes.api_jobs._start_job_async"):
            r1 = client.post("/api/jobs", headers=_api_headers(), json={})
            r2 = client.post("/api/jobs", headers=_api_headers(), json={})
            r3 = client.post("/api/jobs", headers=_api_headers(), json={})
        id1 = r1.get_json()["id"]
        id2 = r2.get_json()["id"]
        id3 = r3.get_json()["id"]
        # Cancel all three, then mark one completed, one failed, one cancelled
        for jid in [id1, id2, id3]:
            client.post(f"/api/jobs/{jid}/cancel", headers=_api_headers())

        # Clear only cancelled jobs
        resp = client.post(
            "/api/jobs/clear",
            headers=_api_headers(),
            json={"statuses": ["cancelled"]},
        )
        assert resp.status_code == 200
        assert resp.get_json()["cleared"] == 3

    def test_clear_jobs_empty_statuses_clears_all_terminal(self, client):
        """Empty body clears all terminal (completed/failed/cancelled) jobs."""
        resp = client.post("/api/jobs/clear", headers=_api_headers(), json={})
        assert resp.status_code == 200
        assert "cleared" in resp.get_json()

    def test_get_jobs_pagination(self, client):
        """Pagination returns correct slices and metadata."""
        with patch("plex_generate_previews.web.routes.api_jobs._start_job_async"):
            for _ in range(5):
                client.post("/api/jobs", headers=_api_headers(), json={})

        resp = client.get("/api/jobs?page=1&per_page=2", headers=_api_headers())
        data = resp.get_json()
        assert resp.status_code == 200
        assert len(data["jobs"]) == 2
        assert data["total"] == 5
        assert data["page"] == 1
        assert data["per_page"] == 2
        assert data["pages"] == 3

    def test_get_jobs_pagination_last_page(self, client):
        """Last page may have fewer items than per_page."""
        with patch("plex_generate_previews.web.routes.api_jobs._start_job_async"):
            for _ in range(5):
                client.post("/api/jobs", headers=_api_headers(), json={})

        resp = client.get("/api/jobs?page=3&per_page=2", headers=_api_headers())
        data = resp.get_json()
        assert len(data["jobs"]) == 1
        assert data["page"] == 3

    def test_get_jobs_pagination_out_of_range(self, client):
        """Page beyond total returns empty jobs list."""
        resp = client.get("/api/jobs?page=99&per_page=10", headers=_api_headers())
        data = resp.get_json()
        assert resp.status_code == 200
        assert data["jobs"] == []
        assert data["page"] == 99

    def test_get_jobs_unpaginated(self, client):
        """page=0 returns all jobs without pagination."""
        with patch("plex_generate_previews.web.routes.api_jobs._start_job_async"):
            for _ in range(3):
                client.post("/api/jobs", headers=_api_headers(), json={})

        resp = client.get("/api/jobs?page=0", headers=_api_headers())
        data = resp.get_json()
        assert resp.status_code == 200
        assert len(data["jobs"]) == 3
        assert data["page"] == 0
        assert data["pages"] == 1

    def test_get_jobs_sort_order(self, client):
        """Jobs are sorted: running first, pending oldest-first, terminal newest-first."""
        from plex_generate_previews.web.jobs import get_job_manager

        jm = get_job_manager()
        with patch("plex_generate_previews.web.routes.api_jobs._start_job_async"):
            r1 = client.post("/api/jobs", headers=_api_headers(), json={})
            r2 = client.post("/api/jobs", headers=_api_headers(), json={})
            r3 = client.post("/api/jobs", headers=_api_headers(), json={})

        id1, id2, _id3 = r1.get_json()["id"], r2.get_json()["id"], r3.get_json()["id"]
        jm.start_job(id1)
        jm.complete_job(id1)
        jm.start_job(id2)

        resp = client.get("/api/jobs?page=0", headers=_api_headers())
        jobs = resp.get_json()["jobs"]
        statuses = [j["status"] for j in jobs]
        assert statuses[0] == "running"
        assert statuses[1] == "pending"
        assert statuses[2] == "completed"

    def test_get_job_stats(self, client):
        resp = client.get("/api/jobs/stats", headers=_api_headers())
        assert resp.status_code == 200
        data = resp.get_json()
        assert "total" in data

    def test_get_worker_statuses(self, client):
        resp = client.get("/api/jobs/workers", headers=_api_headers())
        assert resp.status_code == 200
        data = resp.get_json()
        assert "workers" in data
        assert isinstance(data["workers"], list)

    def test_get_job_logs(self, client):
        with patch("plex_generate_previews.web.routes.api_jobs._start_job_async"):
            create_resp = client.post("/api/jobs", headers=_api_headers(), json={})
        job_id = create_resp.get_json()["id"]
        resp = client.get(f"/api/jobs/{job_id}/logs", headers=_api_headers())
        assert resp.status_code == 200
        data = resp.get_json()
        assert "logs" in data
        assert "log_cleared_by_retention" in data
        assert data["log_cleared_by_retention"] is False

    def test_get_job_logs_returns_retention_flag_when_log_cleared(self, client, app):
        """When job exists but log file was removed by retention, API returns log_cleared_by_retention."""
        with patch("plex_generate_previews.web.routes.api_jobs._start_job_async"):
            create_resp = client.post("/api/jobs", headers=_api_headers(), json={})
        job_id = create_resp.get_json()["id"]
        from plex_generate_previews.web.jobs import get_job_manager

        jm = get_job_manager()
        jm.start_job(job_id)
        jm.complete_job(job_id)
        # Remove in-memory logs and delete log file to simulate retention cleanup
        jm.clear_logs(job_id)
        resp = client.get(f"/api/jobs/{job_id}/logs", headers=_api_headers())
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["log_cleared_by_retention"] is True
        assert data["logs"] == ["Log file was cleared due to log retention policy."]

    def test_pause_resume_job(self, client):
        """Per-job pause/resume routes delegate to global processing pause/resume."""
        with patch("plex_generate_previews.web.routes.api_jobs._start_job_async"):
            create_resp = client.post("/api/jobs", headers=_api_headers(), json={})
        job_id = create_resp.get_json()["id"]

        from plex_generate_previews.web.jobs import get_job_manager

        jm = get_job_manager()
        jm.start_job(job_id)

        pause_resp = client.post(f"/api/jobs/{job_id}/pause", headers=_api_headers())
        assert pause_resp.status_code == 200
        assert pause_resp.get_json().get("paused") is True

        resume_resp = client.post(f"/api/jobs/{job_id}/resume", headers=_api_headers())
        assert resume_resp.status_code == 200
        assert resume_resp.get_json().get("paused") is False

    def test_processing_state_get(self, client):
        """GET /api/processing/state returns global pause state."""
        resp = client.get("/api/processing/state", headers=_api_headers())
        assert resp.status_code == 200
        data = resp.get_json()
        assert "paused" in data
        assert isinstance(data["paused"], bool)

    def test_processing_pause_resume(self, client):
        """POST /api/processing/pause and resume set and return global state."""
        pause_resp = client.post("/api/processing/pause", headers=_api_headers())
        assert pause_resp.status_code == 200
        assert pause_resp.get_json()["paused"] is True

        state_resp = client.get("/api/processing/state", headers=_api_headers())
        assert state_resp.get_json()["paused"] is True

        resume_resp = client.post("/api/processing/resume", headers=_api_headers())
        assert resume_resp.status_code == 200
        assert resume_resp.get_json()["paused"] is False

        state_resp2 = client.get("/api/processing/state", headers=_api_headers())
        assert state_resp2.get_json()["paused"] is False

    def test_job_not_started_when_processing_paused(self, client):
        """When global processing is paused, new job remains pending (not started)."""
        import time

        from plex_generate_previews.web.settings_manager import get_settings_manager

        get_settings_manager().processing_paused = True
        try:
            create_resp = client.post("/api/jobs", headers=_api_headers(), json={})
            assert create_resp.status_code == 201
            job_id = create_resp.get_json()["id"]
            time.sleep(0.3)
            job_resp = client.get(f"/api/jobs/{job_id}", headers=_api_headers())
            assert job_resp.status_code == 200
            assert job_resp.get_json()["status"] == "pending"
        finally:
            get_settings_manager().processing_paused = False

    def test_scale_workers_add_remove(self, client):
        with patch("plex_generate_previews.web.routes.api_jobs._start_job_async"):
            create_resp = client.post("/api/jobs", headers=_api_headers(), json={})
        job_id = create_resp.get_json()["id"]

        from plex_generate_previews.web.jobs import get_job_manager

        jm = get_job_manager()
        jm.start_job(job_id)
        pool = MagicMock()
        pool.add_workers.return_value = 2
        pool.remove_workers.return_value = {
            "removed": 1,
            "scheduled": 1,
            "unavailable": 0,
        }
        jm.set_active_worker_pool(job_id, pool)

        add_resp = client.post(
            f"/api/jobs/{job_id}/workers/add",
            headers=_api_headers(),
            json={"worker_type": "CPU", "count": 2},
        )
        assert add_resp.status_code == 200
        assert add_resp.get_json()["added"] == 2
        pool.add_workers.assert_called_once_with("CPU", 2)

        remove_resp = client.post(
            f"/api/jobs/{job_id}/workers/remove",
            headers=_api_headers(),
            json={"worker_type": "CPU", "count": 2},
        )
        assert remove_resp.status_code == 200
        data = remove_resp.get_json()
        assert data["removed"] == 1
        assert data["scheduled_removal"] == 1
        assert data["unavailable"] == 0
        pool.remove_workers.assert_called_once_with("CPU", 2)

    def test_scale_workers_remove_busy_workers_returns_scheduled_removal(self, client):
        """Remove endpoint returns scheduled_removal when workers are busy (deferred removal)."""
        with patch("plex_generate_previews.web.routes.api_jobs._start_job_async"):
            create_resp = client.post("/api/jobs", headers=_api_headers(), json={})
        job_id = create_resp.get_json()["id"]

        from plex_generate_previews.web.jobs import get_job_manager

        jm = get_job_manager()
        jm.start_job(job_id)
        pool = MagicMock()
        pool.remove_workers.return_value = {
            "removed": 0,
            "scheduled": 2,
            "unavailable": 0,
        }
        jm.set_active_worker_pool(job_id, pool)

        remove_resp = client.post(
            f"/api/jobs/{job_id}/workers/remove",
            headers=_api_headers(),
            json={"worker_type": "GPU", "count": 2},
        )
        assert remove_resp.status_code == 200
        data = remove_resp.get_json()
        assert data["success"] is True
        assert data["removed"] == 0
        assert data["scheduled_removal"] == 2
        assert data["unavailable"] == 0
        pool.remove_workers.assert_called_once_with("GPU", 2)

    def test_scale_workers_remove_returns_unavailable_when_fewer_workers_exist(self, client):
        """Remove endpoint returns unavailable when requesting more than existing workers."""
        with patch("plex_generate_previews.web.routes.api_jobs._start_job_async"):
            create_resp = client.post("/api/jobs", headers=_api_headers(), json={})
        job_id = create_resp.get_json()["id"]

        from plex_generate_previews.web.jobs import get_job_manager

        jm = get_job_manager()
        jm.start_job(job_id)
        pool = MagicMock()
        pool.remove_workers.return_value = {
            "removed": 1,
            "scheduled": 0,
            "unavailable": 1,
        }
        jm.set_active_worker_pool(job_id, pool)

        remove_resp = client.post(
            f"/api/jobs/{job_id}/workers/remove",
            headers=_api_headers(),
            json={"worker_type": "CPU", "count": 2},
        )
        assert remove_resp.status_code == 200
        data = remove_resp.get_json()
        assert data["removed"] == 1
        assert data["scheduled_removal"] == 0
        assert data["unavailable"] == 1

    def test_workers_add_global_no_pool_returns_409(self, client):
        """POST /api/workers/add returns 409 when no worker pool exists."""
        resp = client.post(
            "/api/workers/add",
            headers=_api_headers(),
            json={"worker_type": "CPU", "count": 1},
        )
        assert resp.status_code == 409
        assert "not available" in resp.get_json().get("error", "").lower()

    def test_workers_remove_global_no_pool_returns_409(self, client):
        """POST /api/workers/remove returns 409 when no worker pool exists."""
        resp = client.post(
            "/api/workers/remove",
            headers=_api_headers(),
            json={"worker_type": "CPU", "count": 1},
        )
        assert resp.status_code == 409
        assert "not available" in resp.get_json().get("error", "").lower()

    def test_workers_add_global_success(self, client):
        """POST /api/workers/add delegates to running job pool."""
        with patch("plex_generate_previews.web.routes.api_jobs._start_job_async"):
            create_resp = client.post("/api/jobs", headers=_api_headers(), json={})
        job_id = create_resp.get_json()["id"]

        from plex_generate_previews.web.jobs import get_job_manager

        jm = get_job_manager()
        jm.start_job(job_id)
        pool = MagicMock()
        pool.add_workers.return_value = 2
        jm.set_active_worker_pool(job_id, pool)

        add_resp = client.post(
            "/api/workers/add",
            headers=_api_headers(),
            json={"worker_type": "GPU", "count": 2},
        )
        assert add_resp.status_code == 200
        data = add_resp.get_json()
        assert data["added"] == 2
        assert data["worker_type"] == "GPU"
        pool.add_workers.assert_called_once_with("GPU", 2)

    def test_workers_remove_global_success(self, client):
        """POST /api/workers/remove delegates to running job pool."""
        with patch("plex_generate_previews.web.routes.api_jobs._start_job_async"):
            create_resp = client.post("/api/jobs", headers=_api_headers(), json={})
        job_id = create_resp.get_json()["id"]

        from plex_generate_previews.web.jobs import get_job_manager

        jm = get_job_manager()
        jm.start_job(job_id)
        pool = MagicMock()
        pool.remove_workers.return_value = {
            "removed": 1,
            "scheduled": 0,
            "unavailable": 0,
        }
        jm.set_active_worker_pool(job_id, pool)

        remove_resp = client.post(
            "/api/workers/remove",
            headers=_api_headers(),
            json={"worker_type": "CPU", "count": 1},
        )
        assert remove_resp.status_code == 200
        data = remove_resp.get_json()
        assert data["removed"] == 1
        assert data["worker_type"] == "CPU"
        pool.remove_workers.assert_called_once_with("CPU", 1)


# ---------------------------------------------------------------------------
# Manual Trigger API
# ---------------------------------------------------------------------------


class TestManualTriggerAPI:
    """Test POST /api/jobs/manual endpoint."""

    def test_manual_trigger_valid_path(self, client, tmp_path):
        """Valid file path creates a job and returns 201."""
        test_file = tmp_path / "movie.mkv"
        test_file.touch()

        with patch("plex_generate_previews.web.routes.api_jobs._start_job_async") as mock_start:
            with patch("plex_generate_previews.web.routes.api_jobs.MEDIA_ROOT", str(tmp_path)):
                resp = client.post(
                    "/api/jobs/manual",
                    headers=_api_headers(),
                    json={"file_paths": [str(test_file)]},
                )
        assert resp.status_code == 201
        data = resp.get_json()
        assert "id" in data
        assert "Manual:" in data.get("library_name", "")
        mock_start.assert_called_once()
        config_overrides = mock_start.call_args[0][1]
        assert str(test_file) in config_overrides["webhook_paths"]

    def test_manual_trigger_no_paths(self, client):
        """Empty file_paths returns 400."""
        resp = client.post(
            "/api/jobs/manual",
            headers=_api_headers(),
            json={"file_paths": []},
        )
        assert resp.status_code == 400
        assert "required" in resp.get_json()["error"].lower()

    def test_manual_trigger_missing_body(self, client):
        """Missing JSON body returns 400."""
        resp = client.post(
            "/api/jobs/manual",
            headers=_api_headers(),
            json={},
        )
        assert resp.status_code == 400

    def test_manual_trigger_path_outside_media_root(self, client, tmp_path):
        """Path traversal outside MEDIA_ROOT returns 400."""
        media_root = tmp_path / "media"
        media_root.mkdir()

        with patch("plex_generate_previews.web.routes.api_jobs._start_job_async"):
            with patch("plex_generate_previews.web.routes.api_jobs.MEDIA_ROOT", str(media_root)):
                resp = client.post(
                    "/api/jobs/manual",
                    headers=_api_headers(),
                    json={"file_paths": ["/etc/passwd"]},
                )
        assert resp.status_code == 400
        assert "outside" in resp.get_json()["error"].lower()

    def test_manual_trigger_requires_auth(self, client):
        """Unauthenticated request returns 401."""
        resp = client.post(
            "/api/jobs/manual",
            json={"file_paths": ["/media/test.mkv"]},
        )
        assert resp.status_code == 401

    def test_manual_trigger_force_regenerate(self, client, tmp_path):
        """force_regenerate flag is passed through to config overrides."""
        test_file = tmp_path / "movie.mkv"
        test_file.touch()

        with patch("plex_generate_previews.web.routes.api_jobs._start_job_async") as mock_start:
            with patch("plex_generate_previews.web.routes.api_jobs.MEDIA_ROOT", str(tmp_path)):
                resp = client.post(
                    "/api/jobs/manual",
                    headers=_api_headers(),
                    json={
                        "file_paths": [str(test_file)],
                        "force_regenerate": True,
                    },
                )
        assert resp.status_code == 201
        config_overrides = mock_start.call_args[0][1]
        assert config_overrides["force_generate"] is True

    def test_manual_trigger_multiple_paths(self, client, tmp_path):
        """Multiple file paths produce a job labeled with file count."""
        f1 = tmp_path / "a.mkv"
        f2 = tmp_path / "b.mkv"
        f1.touch()
        f2.touch()

        with patch("plex_generate_previews.web.routes.api_jobs._start_job_async"):
            with patch("plex_generate_previews.web.routes.api_jobs.MEDIA_ROOT", str(tmp_path)):
                resp = client.post(
                    "/api/jobs/manual",
                    headers=_api_headers(),
                    json={"file_paths": [str(f1), str(f2)]},
                )
        assert resp.status_code == 201
        data = resp.get_json()
        assert "2 files" in data.get("library_name", "")


# ---------------------------------------------------------------------------
# Settings API
# ---------------------------------------------------------------------------


class TestSettingsAPI:
    """Test /api/settings endpoints."""

    def test_get_settings(self, client):
        resp = client.get("/api/settings", headers=_api_headers())
        assert resp.status_code == 200
        data = resp.get_json()
        assert "gpu_threads" in data
        assert "cpu_threads" in data
        assert data["plex_verify_ssl"] is True

    def test_get_settings_returns_path_mappings(self, client):
        """GET /api/settings includes path_mappings when present."""
        path_mappings = [
            {
                "plex_prefix": "/data",
                "local_prefix": "/mnt/data",
                "webhook_prefixes": [],
            }
        ]
        client.post(
            "/api/settings",
            headers=_api_headers(),
            json={"path_mappings": path_mappings},
        )
        resp = client.get("/api/settings", headers=_api_headers())
        assert resp.status_code == 200
        assert resp.get_json().get("path_mappings") == path_mappings

    def test_get_settings_returns_exclude_paths(self, client):
        """GET /api/settings includes exclude_paths; save and load round-trip."""
        exclude_paths = [
            {"value": "/mnt/media/archive", "type": "path"},
            {"value": r".*\.iso$", "type": "regex"},
        ]
        client.post(
            "/api/settings",
            headers=_api_headers(),
            json={"exclude_paths": exclude_paths},
        )
        resp = client.get("/api/settings", headers=_api_headers())
        assert resp.status_code == 200
        assert resp.get_json().get("exclude_paths") == exclude_paths

    def test_save_settings(self, client):
        # Pre-seed gpu_config so gpu_threads setter can distribute workers
        client.post(
            "/api/settings",
            headers=_api_headers(),
            json={
                "gpu_config": [
                    {
                        "device": "/dev/gpu0",
                        "name": "GPU 0",
                        "type": "vaapi",
                        "enabled": True,
                        "workers": 1,
                        "ffmpeg_threads": 2,
                    },
                ]
            },
        )
        resp = client.post(
            "/api/settings",
            headers=_api_headers(),
            json={
                "gpu_threads": 2,
                "cpu_threads": 4,
                "plex_verify_ssl": False,
            },
        )
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True

        # Verify setting persisted
        resp2 = client.get("/api/settings", headers=_api_headers())
        data = resp2.get_json()
        assert data["gpu_threads"] == 2
        assert data["cpu_threads"] == 4
        assert data["plex_verify_ssl"] is False

    def test_save_settings_ignores_unknown_fields(self, client):
        resp = client.post(
            "/api/settings",
            headers=_api_headers(),
            json={
                "gpu_threads": 1,
                "unknown_field": "ignored",
                "plex_verify_ssl": False,
            },
        )
        assert resp.status_code == 200

    def test_save_settings_warns_zero_cpu_and_zero_gpu(self, client):
        """Saving with both CPU and GPU workers at zero succeeds with a warning."""
        resp = client.post(
            "/api/settings",
            headers=_api_headers(),
            json={
                "cpu_threads": 0,
                "gpu_config": [
                    {
                        "device": "/dev/dri/renderD128",
                        "name": "Test GPU",
                        "type": "vaapi",
                        "enabled": True,
                        "workers": 0,
                        "ffmpeg_threads": 2,
                    },
                ],
            },
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert "warning" in data
        assert "pending" in data["warning"].lower()

    def test_save_gpu_config_validates_list(self, client):
        """gpu_config must be a list; non-list values are rejected."""
        resp = client.post(
            "/api/settings",
            headers=_api_headers(),
            json={"gpu_config": "not_a_list"},
        )
        assert resp.status_code == 400

    def test_save_gpu_config_filters_invalid_entries(self, client):
        """gpu_config entries without device key are filtered out."""
        resp = client.post(
            "/api/settings",
            headers=_api_headers(),
            json={
                "gpu_config": [
                    {
                        "device": "cuda",
                        "name": "GPU",
                        "type": "nvidia",
                        "enabled": True,
                        "workers": 1,
                        "ffmpeg_threads": 2,
                    },
                    {"name": "no_device"},
                    "string_entry",
                    None,
                ],
            },
        )
        assert resp.status_code == 200
        get_resp = client.get("/api/settings", headers=_api_headers())
        saved = get_resp.get_json()["gpu_config"]
        assert len(saved) == 1
        assert saved[0]["device"] == "cuda"

    def test_save_log_settings(self, client):
        """Test that log_level, log_rotation_size, log_retention_count are persisted."""
        resp = client.post(
            "/api/settings",
            headers=_api_headers(),
            json={
                "log_level": "DEBUG",
                "log_rotation_size": "5 MB",
                "log_retention_count": 4,
            },
        )
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True

    @patch("plex_generate_previews.logging_config.setup_logging")
    def test_update_log_level(self, mock_setup_logging, client):
        """Test PUT /api/settings/log-level hot-reloads logging."""
        resp = client.put(
            "/api/settings/log-level",
            headers=_api_headers(),
            json={"log_level": "DEBUG"},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["log_level"] == "DEBUG"
        mock_setup_logging.assert_called_once()

    def test_update_log_level_invalid(self, client):
        """Test invalid log level returns 400."""
        resp = client.put(
            "/api/settings/log-level",
            headers=_api_headers(),
            json={"log_level": "INVALID"},
        )
        assert resp.status_code == 400

    def test_get_settings_returns_webhook_retry_defaults(self, client):
        """GET /api/settings returns default webhook_retry_count and webhook_retry_delay."""
        resp = client.get("/api/settings", headers=_api_headers())
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["webhook_retry_count"] == 3
        assert data["webhook_retry_delay"] == 30

    def test_save_webhook_retry_settings(self, client):
        """POST /api/settings persists webhook_retry_count and webhook_retry_delay."""
        resp = client.post(
            "/api/settings",
            headers=_api_headers(),
            json={"webhook_retry_count": 5, "webhook_retry_delay": 60},
        )
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True

        resp2 = client.get("/api/settings", headers=_api_headers())
        data = resp2.get_json()
        assert data["webhook_retry_count"] == 5
        assert data["webhook_retry_delay"] == 60


# ---------------------------------------------------------------------------
# Job config / path_mappings (settings vs config_overrides)
# ---------------------------------------------------------------------------


@pytest.mark.real_job_async
class TestJobConfigPathMappings:
    """Test _start_job_async applies settings path_mappings and config_overrides correctly."""

    def test_start_job_applies_settings_path_mappings(self, client, tmp_path):
        """Settings-provided path_mappings are applied to config when starting a job."""
        settings_path_mappings = [
            {
                "plex_prefix": "/plex",
                "local_prefix": "/local",
                "webhook_prefixes": ["/webhook"],
            }
        ]
        client.post(
            "/api/settings",
            headers=_api_headers(),
            json={"path_mappings": settings_path_mappings},
        )

        captured_configs = []
        done = threading.Event()

        def capture_run_processing(config, *args, **kwargs):
            captured_configs.append(config)
            done.set()

        mock_config = MagicMock()
        mock_config.path_mappings = []
        mock_config.tmp_folder = str(tmp_path)
        mock_config.plex_url = "http://test"
        mock_config.plex_token = "token"

        with (
            patch(
                "plex_generate_previews.jobs.orchestrator.run_processing",
                side_effect=capture_run_processing,
            ),
            patch("plex_generate_previews.config.load_config", return_value=mock_config),
            patch(
                "plex_generate_previews.processing.orchestrator._verify_tmp_folder_health",
                return_value=(True, []),
            ),
            patch(
                "plex_generate_previews.utils.setup_working_directory",
                return_value=str(tmp_path / "work"),
            ),
            patch("plex_generate_previews.gpu.detect.detect_all_gpus", return_value=[]),
        ):
            resp = client.post("/api/jobs", headers=_api_headers(), json={})
            assert resp.status_code == 201
            assert done.wait(timeout=2.0), "run_processing was not called"
        assert len(captured_configs) == 1
        expected = normalize_path_mappings({"path_mappings": settings_path_mappings})
        assert captured_configs[0].path_mappings == expected

    def test_start_job_config_overrides_path_mappings(self, client, tmp_path):
        """config_overrides.path_mappings overrides settings path_mappings."""
        override_mappings = [
            {
                "plex_prefix": "/override",
                "local_prefix": "/local_override",
                "webhook_prefixes": [],
            }
        ]
        client.post(
            "/api/settings",
            headers=_api_headers(),
            json={
                "path_mappings": [
                    {
                        "plex_prefix": "/from_settings",
                        "local_prefix": "/local",
                        "webhook_prefixes": [],
                    }
                ]
            },
        )

        captured_configs = []
        done = threading.Event()

        def capture_run_processing(config, *args, **kwargs):
            captured_configs.append(config)
            done.set()

        mock_config = MagicMock()
        mock_config.path_mappings = []
        mock_config.tmp_folder = str(tmp_path)
        mock_config.plex_url = "http://test"
        mock_config.plex_token = "token"

        with (
            patch(
                "plex_generate_previews.jobs.orchestrator.run_processing",
                side_effect=capture_run_processing,
            ),
            patch("plex_generate_previews.config.load_config", return_value=mock_config),
            patch(
                "plex_generate_previews.processing.orchestrator._verify_tmp_folder_health",
                return_value=(True, []),
            ),
            patch(
                "plex_generate_previews.utils.setup_working_directory",
                return_value=str(tmp_path / "work"),
            ),
            patch("plex_generate_previews.gpu.detect.detect_all_gpus", return_value=[]),
        ):
            resp = client.post(
                "/api/jobs",
                headers=_api_headers(),
                json={"config": {"path_mappings": override_mappings}},
            )
            assert resp.status_code == 201
            assert done.wait(timeout=2.0), "run_processing was not called"
        assert len(captured_configs) == 1
        assert captured_configs[0].path_mappings == override_mappings

    def test_webhook_job_retains_path_mappings_and_webhook_paths(self, client, tmp_path):
        """Webhook job with config_overrides has webhook_paths and path_mappings from settings."""
        settings_path_mappings = [
            {
                "plex_prefix": "/data",
                "local_prefix": "/mnt/data",
                "webhook_prefixes": ["/data"],
            }
        ]
        client.post(
            "/api/settings",
            headers=_api_headers(),
            json={"path_mappings": settings_path_mappings},
        )

        captured_configs = []
        done = threading.Event()

        def capture_run_processing(config, *args, **kwargs):
            captured_configs.append(config)
            done.set()

        mock_config = MagicMock()
        mock_config.path_mappings = []
        mock_config.tmp_folder = str(tmp_path)
        mock_config.plex_url = "http://test"
        mock_config.plex_token = "token"
        mock_config.webhook_paths = None

        with (
            patch(
                "plex_generate_previews.jobs.orchestrator.run_processing",
                side_effect=capture_run_processing,
            ),
            patch("plex_generate_previews.config.load_config", return_value=mock_config),
            patch(
                "plex_generate_previews.processing.orchestrator._verify_tmp_folder_health",
                return_value=(True, []),
            ),
            patch(
                "plex_generate_previews.utils.setup_working_directory",
                return_value=str(tmp_path / "work"),
            ),
            patch("plex_generate_previews.gpu.detect.detect_all_gpus", return_value=[]),
        ):
            resp = client.post(
                "/api/jobs",
                headers=_api_headers(),
                json={"config": {"webhook_paths": ["/data/Movies/foo.mkv"]}},
            )
            assert resp.status_code == 201
            assert done.wait(timeout=2.0), "run_processing was not called"
        assert len(captured_configs) == 1
        cfg = captured_configs[0]
        assert cfg.webhook_paths == ["/data/Movies/foo.mkv"]
        expected_mappings = normalize_path_mappings({"path_mappings": settings_path_mappings})
        assert cfg.path_mappings == expected_mappings

    def test_start_job_library_ids_override_sets_plex_library_ids(self, client, tmp_path):
        """library_ids request field should filter by Plex section IDs."""
        captured_configs = []
        done = threading.Event()

        def capture_run_processing(config, *args, **kwargs):
            captured_configs.append(config)
            done.set()

        mock_config = MagicMock()
        mock_config.path_mappings = []
        mock_config.tmp_folder = str(tmp_path)
        mock_config.plex_url = "http://test"
        mock_config.plex_token = "token"
        mock_config.plex_library_ids = None
        mock_config.plex_libraries = []

        with (
            patch(
                "plex_generate_previews.jobs.orchestrator.run_processing",
                side_effect=capture_run_processing,
            ),
            patch("plex_generate_previews.config.load_config", return_value=mock_config),
            patch(
                "plex_generate_previews.processing.orchestrator._verify_tmp_folder_health",
                return_value=(True, []),
            ),
            patch(
                "plex_generate_previews.utils.setup_working_directory",
                return_value=str(tmp_path / "work"),
            ),
            patch("plex_generate_previews.gpu.detect.detect_all_gpus", return_value=[]),
        ):
            resp = client.post(
                "/api/jobs",
                headers=_api_headers(),
                json={"library_ids": ["1", "2"]},
            )
            assert resp.status_code == 201
            assert done.wait(timeout=2.0), "run_processing was not called"
        assert len(captured_configs) == 1
        cfg = captured_configs[0]
        assert cfg.plex_library_ids == ["1", "2"]
        assert cfg.plex_libraries == []

    def test_start_job_selected_libraries_ids_map_to_id_scope(self, client, tmp_path):
        """selected_libraries values that are IDs should populate plex_library_ids."""
        captured_configs = []
        done = threading.Event()

        def capture_run_processing(config, *args, **kwargs):
            captured_configs.append(config)
            done.set()

        mock_config = MagicMock()
        mock_config.path_mappings = []
        mock_config.tmp_folder = str(tmp_path)
        mock_config.plex_url = "http://test"
        mock_config.plex_token = "token"
        mock_config.plex_library_ids = None
        mock_config.plex_libraries = []

        with (
            patch(
                "plex_generate_previews.jobs.orchestrator.run_processing",
                side_effect=capture_run_processing,
            ),
            patch("plex_generate_previews.config.load_config", return_value=mock_config),
            patch(
                "plex_generate_previews.processing.orchestrator._verify_tmp_folder_health",
                return_value=(True, []),
            ),
            patch(
                "plex_generate_previews.utils.setup_working_directory",
                return_value=str(tmp_path / "work"),
            ),
            patch("plex_generate_previews.gpu.detect.detect_all_gpus", return_value=[]),
        ):
            resp = client.post(
                "/api/jobs",
                headers=_api_headers(),
                json={"library_names": ["1", "2"]},
            )
            assert resp.status_code == 201
            assert done.wait(timeout=2.0), "run_processing was not called"
        assert len(captured_configs) == 1
        cfg = captured_configs[0]
        assert cfg.plex_library_ids == ["1", "2"]
        assert cfg.plex_libraries == []


# ---------------------------------------------------------------------------
# Setup Wizard API
# ---------------------------------------------------------------------------


class TestSetupWizardAPI:
    """Test /api/setup/* endpoints."""

    def test_get_setup_status_no_auth(self, client):
        """Setup status should be accessible without auth."""
        resp = client.get("/api/setup/status")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "configured" in data
        assert "setup_complete" in data

    def test_get_setup_state(self, client):
        resp = client.get("/api/setup/state", headers=_api_headers())
        assert resp.status_code == 200
        data = resp.get_json()
        assert "step" in data

    def test_save_setup_state(self, client):
        resp = client.post(
            "/api/setup/state",
            headers=_api_headers(),
            json={"step": 2, "data": {"plex_url": "http://plex:32400"}},
        )
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True

        # Verify persistence
        resp2 = client.get("/api/setup/state", headers=_api_headers())
        assert resp2.get_json()["step"] == 2

    def test_setup_state_save_and_load_path_mappings(self, client):
        """Setup wizard state persists path_mappings in step data."""
        path_mappings = [
            {
                "plex_prefix": "/plex",
                "local_prefix": "/local",
                "webhook_prefixes": ["/webhook"],
            }
        ]
        resp = client.post(
            "/api/setup/state",
            headers=_api_headers(),
            json={"step": 3, "data": {"path_mappings": path_mappings}},
        )
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True

        resp2 = client.get("/api/setup/state", headers=_api_headers())
        assert resp2.status_code == 200
        state = resp2.get_json()
        assert state["step"] == 3
        assert state.get("data", {}).get("path_mappings") == path_mappings

    def test_complete_setup(self, client):
        resp = client.post("/api/setup/complete", headers=_api_headers())
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["redirect"] == "/"

    def test_set_setup_token_mismatch(self, client):
        resp = client.post(
            "/api/setup/set-token",
            headers=_api_headers(),
            json={"token": "new-token-1234", "confirm_token": "different"},
        )
        assert resp.status_code == 400
        assert "match" in resp.get_json()["error"].lower()

    def test_set_setup_token_too_short(self, client):
        resp = client.post(
            "/api/setup/set-token",
            headers=_api_headers(),
            json={"token": "short", "confirm_token": "short"},
        )
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Schedules API
# ---------------------------------------------------------------------------


class TestSchedulesAPI:
    """Test /api/schedules/* endpoints."""

    def test_get_schedules_empty(self, client):
        resp = client.get("/api/schedules", headers=_api_headers())
        assert resp.status_code == 200
        assert resp.get_json()["schedules"] == []

    def test_create_schedule_missing_name(self, client):
        resp = client.post(
            "/api/schedules",
            headers=_api_headers(),
            json={"cron_expression": "0 */6 * * *"},
        )
        assert resp.status_code == 400

    def test_create_schedule_missing_trigger(self, client):
        resp = client.post("/api/schedules", headers=_api_headers(), json={"name": "Test"})
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# System API
# ---------------------------------------------------------------------------


class TestSystemAPI:
    """Test /api/system/* endpoints."""

    def test_get_system_status(self, client):
        with patch("plex_generate_previews.gpu.detect.detect_all_gpus", return_value=[]):
            resp = client.get("/api/system/status", headers=_api_headers())
        assert resp.status_code == 200
        data = resp.get_json()
        assert "gpus" in data

    def test_get_config(self, client):
        with patch("plex_generate_previews.config.load_config", return_value=None):
            resp = client.get("/api/system/config", headers=_api_headers())
        assert resp.status_code == 200
        data = resp.get_json()
        assert "gpu_threads" in data
        assert "cpu_threads" in data


# ---------------------------------------------------------------------------
# Path Validation
# ---------------------------------------------------------------------------


class TestPathValidation:
    """Test /api/setup/validate-paths endpoint."""

    def test_validate_paths_empty(self, client):
        resp = client.post(
            "/api/setup/validate-paths",
            headers=_api_headers(),
            json={"plex_config_folder": ""},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["valid"] is False

    def test_validate_paths_null_bytes_rejected(self, client):
        resp = client.post(
            "/api/setup/validate-paths",
            headers=_api_headers(),
            json={"plex_config_folder": "/plex\x00evil"},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["valid"] is False

    def test_validate_paths_requires_auth_after_setup(self, client):
        """When setup is complete, validate_paths requires authentication."""
        resp = client.post(
            "/api/setup/validate-paths",
            headers={"Content-Type": "application/json"},
            json={"plex_config_folder": "/plex"},
        )
        assert resp.status_code == 401

    def test_validate_paths_path_mappings_new_format_local_not_found(self, client, tmp_path, monkeypatch):
        """New-format path_mappings: invalid local_prefix returns validation error."""
        (tmp_path / "Media" / "localhost").mkdir(parents=True)
        monkeypatch.setattr(
            "plex_generate_previews.web.routes.api_settings.PLEX_DATA_ROOT",
            str(tmp_path),
        )
        resp = client.post(
            "/api/setup/validate-paths",
            headers=_api_headers(),
            json={
                "plex_config_folder": str(tmp_path),
                "path_mappings": [
                    {
                        "plex_prefix": "/plex",
                        "local_prefix": "/nonexistent_xyz_path_123",
                        "webhook_prefixes": [],
                    }
                ],
            },
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["valid"] is False
        # Path mapping row validation: either folder not found or outside allowed root
        assert any("Folder not found" in e or "Path in this app" in e or "Row 1" in e for e in data["errors"])

    def test_validate_paths_path_mappings_null_byte_in_local(self, client, tmp_path, monkeypatch):
        """path_mappings row with null byte in local_prefix returns invalid path error."""
        (tmp_path / "Media" / "localhost").mkdir(parents=True)
        monkeypatch.setattr(
            "plex_generate_previews.web.routes.api_settings.PLEX_DATA_ROOT",
            str(tmp_path),
        )
        resp = client.post(
            "/api/setup/validate-paths",
            headers=_api_headers(),
            json={
                "plex_config_folder": str(tmp_path),
                "path_mappings": [
                    {
                        "plex_prefix": "/p",
                        "local_prefix": "/local\x00evil",
                        "webhook_prefixes": [],
                    }
                ],
            },
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["valid"] is False
        assert any("invalid path" in e for e in data["errors"])

    def test_validate_paths_legacy_plex_only_returns_error(self, client, tmp_path, monkeypatch):
        """Legacy: only plex_videos_path_mapping set returns Local Media Path required."""
        (tmp_path / "Media" / "localhost").mkdir(parents=True)
        monkeypatch.setattr(
            "plex_generate_previews.web.routes.api_settings.PLEX_DATA_ROOT",
            str(tmp_path),
        )
        resp = client.post(
            "/api/setup/validate-paths",
            headers=_api_headers(),
            json={
                "plex_config_folder": str(tmp_path),
                "plex_videos_path_mapping": "/plex",
                "plex_local_videos_path_mapping": "",
            },
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["valid"] is False
        assert any("Local Media Path is required" in e for e in data["errors"])

    def test_validate_paths_legacy_local_only_returns_error(self, client, tmp_path, monkeypatch):
        """Legacy: only plex_local_videos_path_mapping set returns Plex Media Path required."""
        (tmp_path / "Media" / "localhost").mkdir(parents=True)
        monkeypatch.setattr(
            "plex_generate_previews.web.routes.api_settings.PLEX_DATA_ROOT",
            str(tmp_path),
        )
        resp = client.post(
            "/api/setup/validate-paths",
            headers=_api_headers(),
            json={
                "plex_config_folder": str(tmp_path),
                "plex_videos_path_mapping": "",
                "plex_local_videos_path_mapping": "/local",
            },
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["valid"] is False
        assert any("Plex Media Path is required" in e for e in data["errors"])


# ---------------------------------------------------------------------------
# Bearer vs Session Auth
# ---------------------------------------------------------------------------


class TestAuthMethods:
    """Test that both Bearer token and session auth work for API."""

    def test_bearer_auth(self, client):
        resp = client.get("/api/jobs", headers={"Authorization": "Bearer test-token-12345678"})
        assert resp.status_code == 200

    def test_x_auth_token_header(self, client):
        resp = client.get("/api/jobs", headers={"X-Auth-Token": "test-token-12345678"})
        assert resp.status_code == 200

    def test_session_auth(self, authed_client):
        resp = authed_client.get("/api/jobs")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------


class TestAuthRejection:
    """Test that unauthenticated requests are rejected."""

    def test_no_auth_rejected(self, client):
        resp = client.get("/api/jobs")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Schedule CRUD
# ---------------------------------------------------------------------------


class TestSchedulesCRUD:
    """Test schedule create, read, update, delete, enable, disable, run."""

    def test_create_schedule_cron(self, client):
        resp = client.post(
            "/api/schedules",
            headers=_api_headers(),
            json={"name": "Nightly", "cron_expression": "0 3 * * *"},
        )
        assert resp.status_code == 201
        data = resp.get_json()
        assert data["name"] == "Nightly"
        assert "id" in data

    def test_create_schedule_interval(self, client):
        resp = client.post(
            "/api/schedules",
            headers=_api_headers(),
            json={"name": "Every 6h", "interval_minutes": 360},
        )
        assert resp.status_code == 201

    def test_get_specific_schedule(self, client):
        create_resp = client.post(
            "/api/schedules",
            headers=_api_headers(),
            json={"name": "Test", "cron_expression": "0 0 * * *"},
        )
        schedule_id = create_resp.get_json()["id"]
        resp = client.get(f"/api/schedules/{schedule_id}", headers=_api_headers())
        assert resp.status_code == 200
        assert resp.get_json()["id"] == schedule_id

    def test_get_nonexistent_schedule(self, client):
        resp = client.get("/api/schedules/nonexistent", headers=_api_headers())
        assert resp.status_code == 404

    def test_update_schedule(self, client):
        create_resp = client.post(
            "/api/schedules",
            headers=_api_headers(),
            json={"name": "Original", "cron_expression": "0 0 * * *"},
        )
        schedule_id = create_resp.get_json()["id"]
        resp = client.put(
            f"/api/schedules/{schedule_id}",
            headers=_api_headers(),
            json={"name": "Updated"},
        )
        assert resp.status_code == 200
        assert resp.get_json()["name"] == "Updated"

    def test_update_nonexistent_schedule(self, client):
        resp = client.put(
            "/api/schedules/nonexistent",
            headers=_api_headers(),
            json={"name": "Nope"},
        )
        assert resp.status_code == 404

    def test_delete_schedule(self, client):
        create_resp = client.post(
            "/api/schedules",
            headers=_api_headers(),
            json={"name": "ToDelete", "cron_expression": "0 0 * * *"},
        )
        schedule_id = create_resp.get_json()["id"]
        resp = client.delete(f"/api/schedules/{schedule_id}", headers=_api_headers())
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True

    def test_delete_nonexistent_schedule(self, client):
        resp = client.delete("/api/schedules/nonexistent", headers=_api_headers())
        assert resp.status_code == 404

    def test_enable_schedule(self, client):
        create_resp = client.post(
            "/api/schedules",
            headers=_api_headers(),
            json={"name": "S", "cron_expression": "0 0 * * *", "enabled": False},
        )
        schedule_id = create_resp.get_json()["id"]
        resp = client.post(f"/api/schedules/{schedule_id}/enable", headers=_api_headers())
        assert resp.status_code == 200
        assert resp.get_json()["enabled"] is True

    def test_enable_nonexistent_schedule(self, client):
        resp = client.post("/api/schedules/nonexistent/enable", headers=_api_headers())
        assert resp.status_code == 404

    def test_disable_schedule(self, client):
        create_resp = client.post(
            "/api/schedules",
            headers=_api_headers(),
            json={"name": "S", "cron_expression": "0 0 * * *"},
        )
        schedule_id = create_resp.get_json()["id"]
        resp = client.post(f"/api/schedules/{schedule_id}/disable", headers=_api_headers())
        assert resp.status_code == 200
        assert resp.get_json()["enabled"] is False

    def test_disable_nonexistent_schedule(self, client):
        resp = client.post("/api/schedules/nonexistent/disable", headers=_api_headers())
        assert resp.status_code == 404

    @pytest.mark.real_job_async
    def test_run_now(self, client):
        create_resp = client.post(
            "/api/schedules",
            headers=_api_headers(),
            json={"name": "RunMe", "cron_expression": "0 0 * * *"},
        )
        schedule_id = create_resp.get_json()["id"]
        resp = client.post(f"/api/schedules/{schedule_id}/run", headers=_api_headers())
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True

    def test_run_now_nonexistent(self, client):
        resp = client.post("/api/schedules/nonexistent/run", headers=_api_headers())
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Reprocess Job
# ---------------------------------------------------------------------------


class TestReprocessJob:
    """Test /api/jobs/<id>/reprocess endpoint."""

    def test_reprocess_completed_job(self, client):
        with patch("plex_generate_previews.web.routes.api_jobs._start_job_async"):
            create_resp = client.post("/api/jobs", headers=_api_headers(), json={"library_name": "Movies"})
        job_id = create_resp.get_json()["id"]

        from plex_generate_previews.web.jobs import get_job_manager

        jm = get_job_manager()
        jm.start_job(job_id)
        jm.complete_job(job_id)

        with patch("plex_generate_previews.web.routes.api_jobs._start_job_async"):
            resp = client.post(f"/api/jobs/{job_id}/reprocess", headers=_api_headers())
        assert resp.status_code == 201
        assert resp.get_json()["id"] != job_id

    def test_reprocess_nonexistent_job(self, client):
        resp = client.post("/api/jobs/nonexistent/reprocess", headers=_api_headers())
        assert resp.status_code == 404

    def test_reprocess_running_job_rejected(self, client):
        with patch("plex_generate_previews.web.routes.api_jobs._start_job_async"):
            create_resp = client.post("/api/jobs", headers=_api_headers(), json={})
        job_id = create_resp.get_json()["id"]

        from plex_generate_previews.web.jobs import get_job_manager

        jm = get_job_manager()
        jm.start_job(job_id)

        resp = client.post(f"/api/jobs/{job_id}/reprocess", headers=_api_headers())
        assert resp.status_code == 409


# ---------------------------------------------------------------------------
# Worker Scaling Validation
# ---------------------------------------------------------------------------


class TestWorkerScalingValidation:
    """Test worker scaling edge cases and validation."""

    def test_add_workers_zero_count_rejected(self, client):
        with patch("plex_generate_previews.web.routes.api_jobs._start_job_async"):
            create_resp = client.post("/api/jobs", headers=_api_headers(), json={})
        job_id = create_resp.get_json()["id"]

        from plex_generate_previews.web.jobs import get_job_manager

        jm = get_job_manager()
        jm.start_job(job_id)

        resp = client.post(
            f"/api/jobs/{job_id}/workers/add",
            headers=_api_headers(),
            json={"worker_type": "CPU", "count": 0},
        )
        assert resp.status_code == 400

    def test_add_workers_invalid_type_rejected(self, client):
        with patch("plex_generate_previews.web.routes.api_jobs._start_job_async"):
            create_resp = client.post("/api/jobs", headers=_api_headers(), json={})
        job_id = create_resp.get_json()["id"]

        from plex_generate_previews.web.jobs import get_job_manager

        jm = get_job_manager()
        jm.start_job(job_id)

        resp = client.post(
            f"/api/jobs/{job_id}/workers/add",
            headers=_api_headers(),
            json={"worker_type": "INVALID", "count": 1},
        )
        assert resp.status_code == 400

    def test_add_workers_no_pool_returns_409(self, client):
        with patch("plex_generate_previews.web.routes.api_jobs._start_job_async"):
            create_resp = client.post("/api/jobs", headers=_api_headers(), json={})
        job_id = create_resp.get_json()["id"]

        from plex_generate_previews.web.jobs import get_job_manager

        jm = get_job_manager()
        jm.start_job(job_id)

        resp = client.post(
            f"/api/jobs/{job_id}/workers/add",
            headers=_api_headers(),
            json={"worker_type": "CPU", "count": 1},
        )
        assert resp.status_code == 409

    def test_remove_workers_zero_count_rejected(self, client):
        with patch("plex_generate_previews.web.routes.api_jobs._start_job_async"):
            create_resp = client.post("/api/jobs", headers=_api_headers(), json={})
        job_id = create_resp.get_json()["id"]

        from plex_generate_previews.web.jobs import get_job_manager

        jm = get_job_manager()
        jm.start_job(job_id)

        resp = client.post(
            f"/api/jobs/{job_id}/workers/remove",
            headers=_api_headers(),
            json={"worker_type": "CPU", "count": 0},
        )
        assert resp.status_code == 400

    def test_global_add_workers_invalid_type(self, client):
        with patch("plex_generate_previews.web.routes.api_jobs._start_job_async"):
            create_resp = client.post("/api/jobs", headers=_api_headers(), json={})
        job_id = create_resp.get_json()["id"]

        from plex_generate_previews.web.jobs import get_job_manager

        jm = get_job_manager()
        jm.start_job(job_id)
        pool = MagicMock()
        jm.set_active_worker_pool(job_id, pool)

        resp = client.post(
            "/api/workers/add",
            headers=_api_headers(),
            json={"worker_type": "BADTYPE", "count": 1},
        )
        assert resp.status_code == 400

    def test_global_remove_workers_zero_count(self, client):
        with patch("plex_generate_previews.web.routes.api_jobs._start_job_async"):
            create_resp = client.post("/api/jobs", headers=_api_headers(), json={})
        job_id = create_resp.get_json()["id"]

        from plex_generate_previews.web.jobs import get_job_manager

        jm = get_job_manager()
        jm.start_job(job_id)

        resp = client.post(
            "/api/workers/remove",
            headers=_api_headers(),
            json={"worker_type": "CPU", "count": 0},
        )
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Validate Paths - Additional Branches
# ---------------------------------------------------------------------------


class TestValidatePathsBranches:
    """Test additional validate_paths branches."""

    def test_validate_paths_valid_structure(self, client, tmp_path, monkeypatch):
        """Valid Plex directory structure succeeds."""
        media_dir = tmp_path / "Media" / "localhost"
        media_dir.mkdir(parents=True)
        for h in "0123456789abcdef":
            (media_dir / h).mkdir()
        monkeypatch.setattr(
            "plex_generate_previews.web.routes.api_settings.PLEX_DATA_ROOT",
            str(tmp_path),
        )
        resp = client.post(
            "/api/setup/validate-paths",
            headers=_api_headers(),
            json={"plex_config_folder": str(tmp_path)},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["valid"] is True
        assert any("valid structure" in i for i in data["info"])

    def test_validate_paths_missing_media_subfolder(self, client, tmp_path, monkeypatch):
        """Plex directory missing Media subfolder."""
        monkeypatch.setattr(
            "plex_generate_previews.web.routes.api_settings.PLEX_DATA_ROOT",
            str(tmp_path),
        )
        resp = client.post(
            "/api/setup/validate-paths",
            headers=_api_headers(),
            json={"plex_config_folder": str(tmp_path)},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["valid"] is False
        assert any("Media" in e for e in data["errors"])

    def test_validate_paths_missing_localhost(self, client, tmp_path, monkeypatch):
        """Plex directory has Media but missing localhost subfolder."""
        (tmp_path / "Media").mkdir()
        monkeypatch.setattr(
            "plex_generate_previews.web.routes.api_settings.PLEX_DATA_ROOT",
            str(tmp_path),
        )
        resp = client.post(
            "/api/setup/validate-paths",
            headers=_api_headers(),
            json={"plex_config_folder": str(tmp_path)},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["valid"] is False
        assert any("localhost" in e for e in data["errors"])

    def test_validate_paths_incomplete_structure_warns(self, client, tmp_path, monkeypatch):
        """Plex directory with few hash dirs warns."""
        media_dir = tmp_path / "Media" / "localhost"
        media_dir.mkdir(parents=True)
        (media_dir / "a").mkdir()
        monkeypatch.setattr(
            "plex_generate_previews.web.routes.api_settings.PLEX_DATA_ROOT",
            str(tmp_path),
        )
        resp = client.post(
            "/api/setup/validate-paths",
            headers=_api_headers(),
            json={"plex_config_folder": str(tmp_path)},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert any("incomplete" in w for w in data["warnings"])

    def test_validate_paths_no_mapping_info(self, client, tmp_path, monkeypatch):
        """No path mapping gives informational message."""
        media_dir = tmp_path / "Media" / "localhost"
        media_dir.mkdir(parents=True)
        for h in "0123456789abcdef":
            (media_dir / h).mkdir()
        monkeypatch.setattr(
            "plex_generate_previews.web.routes.api_settings.PLEX_DATA_ROOT",
            str(tmp_path),
        )
        resp = client.post(
            "/api/setup/validate-paths",
            headers=_api_headers(),
            json={"plex_config_folder": str(tmp_path)},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert any("No path mapping" in i for i in data["info"])

    def test_validate_paths_traversal_rejected(self, client, tmp_path, monkeypatch):
        """Path traversal attempt is rejected."""
        (tmp_path / "Media" / "localhost").mkdir(parents=True)
        monkeypatch.setattr(
            "plex_generate_previews.web.routes.api_settings.PLEX_DATA_ROOT",
            str(tmp_path),
        )
        resp = client.post(
            "/api/setup/validate-paths",
            headers=_api_headers(),
            json={"plex_config_folder": str(tmp_path / ".." / ".." / "etc")},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["valid"] is False

    def test_validate_paths_legacy_null_byte_in_local_media(self, client, tmp_path, monkeypatch):
        """Legacy local_media_path with null byte is rejected via the path_mappings branch."""
        (tmp_path / "Media" / "localhost").mkdir(parents=True)
        monkeypatch.setattr(
            "plex_generate_previews.web.routes.api_settings.PLEX_DATA_ROOT",
            str(tmp_path),
        )
        resp = client.post(
            "/api/setup/validate-paths",
            headers=_api_headers(),
            json={
                "plex_config_folder": str(tmp_path),
                "plex_videos_path_mapping": "/plex",
                "plex_local_videos_path_mapping": "/local\x00evil",
            },
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["valid"] is False
        assert any("invalid path" in e for e in data["errors"])

    def test_validate_paths_plex_data_path_null_byte_rejected(self, client, tmp_path, monkeypatch):
        """Null byte in plex_config_folder is rejected."""
        monkeypatch.setattr(
            "plex_generate_previews.web.routes.api_settings.PLEX_DATA_ROOT",
            str(tmp_path),
        )
        resp = client.post(
            "/api/setup/validate-paths",
            headers=_api_headers(),
            json={"plex_config_folder": "/plex\x00evil"},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["valid"] is False
        assert any("Invalid Plex Data Path" in e for e in data["errors"])


# ---------------------------------------------------------------------------
# Page Routes - Additional
# ---------------------------------------------------------------------------


class TestPageRoutesAdditional:
    """Test additional page routes."""

    def test_automation_page_requires_auth(self, client):
        resp = client.get("/automation", follow_redirects=False)
        assert resp.status_code in (302, 308)

    def test_automation_page_renders_with_both_panes(self, authed_client):
        resp = authed_client.get("/automation")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert 'id="pane-triggers"' in body
        assert 'id="pane-schedules"' in body
        assert 'id="sidebar-group-triggers"' in body
        assert 'id="sidebar-group-schedules"' in body

    def test_webhooks_route_redirects_to_automation(self, authed_client):
        resp = authed_client.get("/webhooks", follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["Location"].endswith("/automation#webhooks")

    def test_schedules_route_redirects_to_automation(self, authed_client):
        resp = authed_client.get("/schedules", follow_redirects=False)
        assert resp.status_code == 302
        location = resp.headers["Location"]
        assert location.startswith("/automation?")
        assert "tab=schedules" in location
        assert location.endswith("#schedules")

    def test_schedules_route_redirect_preserves_edit_query(self, authed_client):
        resp = authed_client.get("/schedules?editSchedule=abc123", follow_redirects=False)
        assert resp.status_code == 302
        location = resp.headers["Location"]
        assert "editSchedule=abc123" in location
        assert "tab=schedules" in location
        assert location.endswith("#schedules")

    def test_webhooks_route_redirect_preserves_query(self, authed_client):
        resp = authed_client.get("/webhooks?foo=bar", follow_redirects=False)
        assert resp.status_code == 302
        location = resp.headers["Location"]
        assert "foo=bar" in location
        assert location.endswith("#webhooks")

    def test_logs_page_requires_auth(self, client):
        resp = client.get("/logs", follow_redirects=False)
        assert resp.status_code in (302, 308)

    def test_logs_page_accessible_when_authenticated(self, authed_client):
        resp = authed_client.get("/logs")
        assert resp.status_code == 200

    def test_setup_wizard_page_redirects_when_configured(self, authed_client):
        resp = authed_client.get("/setup", follow_redirects=False)
        assert resp.status_code in (200, 302, 308)

    def test_index_redirects_to_setup_when_not_configured(self, client, tmp_path):
        """When setup is incomplete, authenticated user is redirected to setup."""
        config_dir = str(tmp_path / "setup_test_config")
        os.makedirs(config_dir, exist_ok=True)
        auth_file = os.path.join(config_dir, "auth.json")
        with open(auth_file, "w") as f:
            json.dump({"token": "test-token-12345678"}, f)

        with patch.dict(
            os.environ,
            {"CONFIG_DIR": config_dir, "WEB_AUTH_TOKEN": "test-token-12345678"},
        ):
            from plex_generate_previews.web.settings_manager import (
                reset_settings_manager,
            )

            reset_settings_manager()
            flask_app = create_app(config_dir=config_dir)
            flask_app.config["TESTING"] = True
            test_client = flask_app.test_client()
            with test_client.session_transaction() as sess:
                sess["authenticated"] = True

            resp = test_client.get("/", follow_redirects=False)
            assert resp.status_code in (200, 302, 308)
            reset_settings_manager()


# ---------------------------------------------------------------------------
# Log History API
# ---------------------------------------------------------------------------


class TestLogHistoryAPI:
    """Test /api/logs/history endpoint.

    Each test patches get_app_log_path to an isolated file so results
    are not polluted by the app's own startup logs.
    """

    def _write_log(self, path, entries):
        """Write JSONL entries to the given path."""
        import json as _json

        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            for e in entries:
                f.write(_json.dumps(e) + "\n")

    def test_log_history_requires_auth(self, client):
        resp = client.get("/api/logs/history")
        assert resp.status_code in (401, 403)

    def test_log_history_empty_when_no_file(self, app, authed_client, tmp_path):
        fake = str(tmp_path / "nonexistent" / "app.log")
        with patch(
            "plex_generate_previews.web.routes.api_system.get_app_log_path",
            return_value=fake,
        ):
            resp = authed_client.get("/api/logs/history", headers=_api_headers())
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["lines"] == []
        assert data["has_more"] is False

    def test_log_history_returns_entries(self, app, authed_client, tmp_path):
        fake = str(tmp_path / "logs" / "app.log")
        entries = [
            {
                "ts": "2026-03-22 09:10:18.100",
                "level": "INFO",
                "msg": "first",
                "mod": "a",
                "func": "f",
                "line": 1,
            },
            {
                "ts": "2026-03-22 09:10:18.200",
                "level": "WARNING",
                "msg": "second",
                "mod": "b",
                "func": "g",
                "line": 2,
            },
            {
                "ts": "2026-03-22 09:10:18.300",
                "level": "ERROR",
                "msg": "third",
                "mod": "c",
                "func": "h",
                "line": 3,
            },
        ]
        self._write_log(fake, entries)

        with patch(
            "plex_generate_previews.web.routes.api_system.get_app_log_path",
            return_value=fake,
        ):
            resp = authed_client.get("/api/logs/history", headers=_api_headers())
        data = resp.get_json()
        assert len(data["lines"]) == 3
        assert data["lines"][0]["msg"] == "first"
        assert data["lines"][2]["msg"] == "third"

    def test_log_history_level_filter(self, app, authed_client, tmp_path):
        fake = str(tmp_path / "logs" / "app.log")
        entries = [
            {
                "ts": "2026-03-22 09:10:18.100",
                "level": "DEBUG",
                "msg": "debug",
                "mod": "a",
                "func": "f",
                "line": 1,
            },
            {
                "ts": "2026-03-22 09:10:18.200",
                "level": "INFO",
                "msg": "info",
                "mod": "a",
                "func": "f",
                "line": 2,
            },
            {
                "ts": "2026-03-22 09:10:18.300",
                "level": "ERROR",
                "msg": "error",
                "mod": "a",
                "func": "f",
                "line": 3,
            },
        ]
        self._write_log(fake, entries)

        with patch(
            "plex_generate_previews.web.routes.api_system.get_app_log_path",
            return_value=fake,
        ):
            resp = authed_client.get("/api/logs/history?level=WARNING", headers=_api_headers())
        data = resp.get_json()
        assert len(data["lines"]) == 1
        assert data["lines"][0]["msg"] == "error"

    def test_log_history_before_cursor(self, app, authed_client, tmp_path):
        fake = str(tmp_path / "logs" / "app.log")
        entries = [
            {
                "ts": "2026-03-22 09:10:18.100",
                "level": "INFO",
                "msg": "old",
                "mod": "a",
                "func": "f",
                "line": 1,
            },
            {
                "ts": "2026-03-22 09:10:18.200",
                "level": "INFO",
                "msg": "mid",
                "mod": "a",
                "func": "f",
                "line": 2,
            },
            {
                "ts": "2026-03-22 09:10:18.300",
                "level": "INFO",
                "msg": "new",
                "mod": "a",
                "func": "f",
                "line": 3,
            },
        ]
        self._write_log(fake, entries)

        with patch(
            "plex_generate_previews.web.routes.api_system.get_app_log_path",
            return_value=fake,
        ):
            resp = authed_client.get(
                "/api/logs/history?before=2026-03-22 09:10:18.200",
                headers=_api_headers(),
            )
        data = resp.get_json()
        assert len(data["lines"]) == 1
        assert data["lines"][0]["msg"] == "old"

    def test_log_history_limit(self, app, authed_client, tmp_path):
        fake = str(tmp_path / "logs" / "app.log")
        entries = [
            {
                "ts": f"2026-03-22 09:10:18.{i:03d}",
                "level": "INFO",
                "msg": f"line{i}",
                "mod": "a",
                "func": "f",
                "line": i,
            }
            for i in range(20)
        ]
        self._write_log(fake, entries)

        with patch(
            "plex_generate_previews.web.routes.api_system.get_app_log_path",
            return_value=fake,
        ):
            resp = authed_client.get("/api/logs/history?limit=5", headers=_api_headers())
        data = resp.get_json()
        assert len(data["lines"]) == 5
        assert data["lines"][-1]["msg"] == "line19"


# ---------------------------------------------------------------------------
# Libraries API
# ---------------------------------------------------------------------------


class TestLibrariesAPI:
    """Test /api/libraries endpoint."""

    @patch("plex_generate_previews.web.routes.api_system._fetch_libraries_via_http")
    def test_get_libraries_with_settings(self, mock_fetch, client):
        """Libraries are fetched via HTTP when plex_url/token are set in settings."""
        from plex_generate_previews.web.settings_manager import get_settings_manager

        sm = get_settings_manager()
        sm.set("plex_url", "http://plex:32400")
        sm.set("plex_token", "test-token")
        mock_fetch.return_value = [{"id": "1", "name": "Movies", "type": "movie", "count": 100}]
        resp = client.get("/api/libraries", headers=_api_headers())
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data["libraries"]) == 1
        assert data["libraries"][0]["name"] == "Movies"

    def test_get_libraries_no_config(self, client):
        """Libraries endpoint with no plex config falls back gracefully."""
        from plex_generate_previews.web.settings_manager import get_settings_manager

        sm = get_settings_manager()
        sm.delete("plex_url")
        sm.delete("plex_token")
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PLEX_URL", None)
            os.environ.pop("PLEX_TOKEN", None)
            with patch("plex_generate_previews.config.get_cached_config", return_value=None):
                resp = client.get("/api/libraries", headers=_api_headers())
        assert resp.status_code == 400
        assert "libraries" in resp.get_json()


# ---------------------------------------------------------------------------
# Plex Test Connection
# ---------------------------------------------------------------------------


class TestPlexTestConnection:
    """Test /api/plex/test endpoint."""

    @patch("requests.get")
    def test_plex_test_success(self, mock_get, client):
        from plex_generate_previews.web.settings_manager import get_settings_manager

        sm = get_settings_manager()
        sm.set("plex_url", "http://plex:32400")
        sm.set("plex_token", "test-token")
        mock_response = MagicMock()
        mock_response.json.return_value = {"MediaContainer": {"friendlyName": "My Plex"}}
        mock_response.raise_for_status = MagicMock()
        mock_get.return_value = mock_response
        resp = client.post(
            "/api/plex/test",
            headers=_api_headers(),
            json={
                "url": "http://plex:32400",
                "token": "test-token",
                "verify_ssl": False,
            },
        )
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True
        assert resp.get_json()["server_name"] == "My Plex"
        assert mock_get.call_args.kwargs["verify"] is False

    def test_plex_test_no_url_returns_400(self, client):
        from plex_generate_previews.web.settings_manager import get_settings_manager

        sm = get_settings_manager()
        sm.delete("plex_url")
        sm.delete("plex_token")
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PLEX_URL", None)
            os.environ.pop("PLEX_TOKEN", None)
            resp = client.post("/api/plex/test", headers=_api_headers(), json={})
        assert resp.status_code == 400

    @patch("requests.get")
    def test_plex_test_connection_failure(self, mock_get, client):
        import requests as req_mod

        mock_get.side_effect = req_mod.exceptions.ConnectionError("refused")
        from plex_generate_previews.web.settings_manager import get_settings_manager

        sm = get_settings_manager()
        sm.set("plex_url", "http://plex:32400")
        sm.set("plex_token", "test-token")
        resp = client.post(
            "/api/plex/test",
            headers=_api_headers(),
            json={"url": "http://plex:32400", "token": "test-token"},
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["success"] is False
        assert "Could not connect" in body["error"]
        assert "http://plex:32400" in body["error"]

    @patch("requests.get")
    def test_plex_test_timeout_returns_specific_message(self, mock_get, client):
        import requests as req_mod

        mock_get.side_effect = req_mod.exceptions.Timeout("timed out")
        from plex_generate_previews.web.settings_manager import get_settings_manager

        sm = get_settings_manager()
        sm.set("plex_url", "http://plex:32400")
        sm.set("plex_token", "test-token")
        resp = client.post(
            "/api/plex/test",
            headers=_api_headers(),
            json={"url": "http://plex:32400", "token": "test-token"},
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["success"] is False
        assert "timed out" in body["error"]

    @patch("requests.get")
    def test_plex_test_ssl_error_returns_specific_message(self, mock_get, client):
        import requests as req_mod

        mock_get.side_effect = req_mod.exceptions.SSLError("bad cert")
        from plex_generate_previews.web.settings_manager import get_settings_manager

        sm = get_settings_manager()
        sm.set("plex_url", "https://plex:32400")
        sm.set("plex_token", "test-token")
        resp = client.post(
            "/api/plex/test",
            headers=_api_headers(),
            json={"url": "https://plex:32400", "token": "test-token"},
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["success"] is False
        assert "SSL" in body["error"]
        assert "Verify SSL" in body["error"]

    @patch("requests.get")
    def test_plex_test_http_401_returns_auth_message(self, mock_get, client):
        import requests as req_mod

        mock_response = MagicMock()
        mock_response.status_code = 401
        err = req_mod.exceptions.HTTPError("401 Unauthorized")
        err.response = mock_response
        mock_response.raise_for_status.side_effect = err
        mock_get.return_value = mock_response

        from plex_generate_previews.web.settings_manager import get_settings_manager

        sm = get_settings_manager()
        sm.set("plex_url", "http://plex:32400")
        sm.set("plex_token", "bad-token")
        resp = client.post(
            "/api/plex/test",
            headers=_api_headers(),
            json={"url": "http://plex:32400", "token": "bad-token"},
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["success"] is False
        assert "401" in body["error"]
        assert "token" in body["error"].lower()

    @patch("requests.get")
    def test_plex_test_http_404_returns_not_plex_message(self, mock_get, client):
        import requests as req_mod

        mock_response = MagicMock()
        mock_response.status_code = 404
        err = req_mod.exceptions.HTTPError("404 Not Found")
        err.response = mock_response
        mock_response.raise_for_status.side_effect = err
        mock_get.return_value = mock_response

        from plex_generate_previews.web.settings_manager import get_settings_manager

        sm = get_settings_manager()
        sm.set("plex_url", "http://example.com")
        sm.set("plex_token", "test-token")
        resp = client.post(
            "/api/plex/test",
            headers=_api_headers(),
            json={"url": "http://example.com", "token": "test-token"},
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["success"] is False
        assert "404" in body["error"]

    @patch("requests.get")
    def test_plex_test_invalid_json_returns_not_plex_message(self, mock_get, client):
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.side_effect = ValueError("not json")
        mock_get.return_value = mock_response

        from plex_generate_previews.web.settings_manager import get_settings_manager

        sm = get_settings_manager()
        sm.set("plex_url", "http://example.com")
        sm.set("plex_token", "test-token")
        resp = client.post(
            "/api/plex/test",
            headers=_api_headers(),
            json={"url": "http://example.com", "token": "test-token"},
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["success"] is False
        assert "not a Plex server" in body["error"] or "Plex server" in body["error"]
        assert "valid Plex data" in body["error"]


# ---------------------------------------------------------------------------
# Plex Webhook Test Reachability — loopback-in-Docker guard
# ---------------------------------------------------------------------------


class TestPlexWebhookLoopbackGuard:
    """Ensure the webhook self-test short-circuits for localhost URLs in Docker.

    Inside a container, `localhost` is the container itself — so a scary
    ConnectionRefused stack trace is the usual symptom when users leave the
    auto-filled default URL unchanged. The guard produces actionable text
    instead of attempting the doomed network call.
    """

    @patch("plex_generate_previews.web.routes.api_settings.is_docker_environment")
    @patch("requests.post")
    def test_loopback_in_docker_short_circuits_with_guidance(self, mock_post, mock_is_docker, client):
        mock_is_docker.return_value = True

        from plex_generate_previews.web.settings_manager import get_settings_manager

        sm = get_settings_manager()
        sm.set("plex_token", "plex-token")
        sm.set("webhook_secret", "secret-value")

        resp = client.post(
            "/api/settings/plex_webhook/test",
            headers=_api_headers(),
            json={"public_url": "http://localhost:9191/api/webhooks/plex"},
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["success"] is False
        assert "localhost" in body["error"]
        assert "Docker" in body["error"]
        assert body["public_url"] == "http://localhost:9191/api/webhooks/plex"
        # Guard must prevent any actual outbound request.
        mock_post.assert_not_called()

    @patch("plex_generate_previews.web.routes.api_settings.is_docker_environment")
    @patch("requests.post")
    def test_loopback_outside_docker_proceeds_with_network_call(self, mock_post, mock_is_docker, client):
        mock_is_docker.return_value = False

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = "ok"
        mock_post.return_value = mock_response

        from plex_generate_previews.web.settings_manager import get_settings_manager

        sm = get_settings_manager()
        sm.set("plex_token", "plex-token")
        sm.set("webhook_secret", "secret-value")

        resp = client.post(
            "/api/settings/plex_webhook/test",
            headers=_api_headers(),
            json={"public_url": "http://localhost:9191/api/webhooks/plex"},
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["success"] is True
        mock_post.assert_called_once()

    @patch("plex_generate_previews.web.routes.api_settings.is_docker_environment")
    @patch("requests.post")
    def test_non_loopback_in_docker_proceeds(self, mock_post, mock_is_docker, client):
        mock_is_docker.return_value = True

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = "ok"
        mock_post.return_value = mock_response

        from plex_generate_previews.web.settings_manager import get_settings_manager

        sm = get_settings_manager()
        sm.set("plex_token", "plex-token")
        sm.set("webhook_secret", "secret-value")

        resp = client.post(
            "/api/settings/plex_webhook/test",
            headers=_api_headers(),
            json={"public_url": "http://192.168.1.50:9191/api/webhooks/plex"},
        )
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True
        mock_post.assert_called_once()

    @patch("plex_generate_previews.web.routes.api_settings.is_docker_environment")
    def test_loopback_guard_covers_ipv4_and_ipv6(self, mock_is_docker, client):
        mock_is_docker.return_value = True

        from plex_generate_previews.web.settings_manager import get_settings_manager

        sm = get_settings_manager()
        sm.set("plex_token", "plex-token")
        sm.set("webhook_secret", "secret-value")

        for url in (
            "http://127.0.0.1:9191/api/webhooks/plex",
            "http://[::1]:9191/api/webhooks/plex",
        ):
            resp = client.post(
                "/api/settings/plex_webhook/test",
                headers=_api_headers(),
                json={"public_url": url},
            )
            assert resp.status_code == 200
            body = resp.get_json()
            assert body["success"] is False, f"expected guard to trip for {url}"
            assert "Docker" in body["error"]


# ---------------------------------------------------------------------------
# Plex Libraries API
# ---------------------------------------------------------------------------


class TestPlexLibrariesAPI:
    """Test /api/plex/libraries endpoint."""

    @patch("plex_generate_previews.web.routes.api_system._fetch_libraries_via_http")
    def test_get_plex_libraries(self, mock_fetch, client):
        from plex_generate_previews.web.settings_manager import get_settings_manager

        sm = get_settings_manager()
        sm.set("plex_url", "http://plex:32400")
        sm.set("plex_token", "test-token")
        mock_fetch.return_value = [{"id": "1", "name": "Movies", "type": "movie"}]
        resp = client.get("/api/plex/libraries", headers=_api_headers())
        assert resp.status_code == 200
        assert len(resp.get_json()["libraries"]) == 1

    def test_get_plex_libraries_no_creds(self, client):
        from plex_generate_previews.web.settings_manager import get_settings_manager

        sm = get_settings_manager()
        sm.delete("plex_url")
        sm.delete("plex_token")
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PLEX_URL", None)
            os.environ.pop("PLEX_TOKEN", None)
            resp = client.get("/api/plex/libraries", headers=_api_headers())
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Fetch Libraries Via HTTP (unit)
# ---------------------------------------------------------------------------


class TestFetchLibrariesViaHTTP:
    """Test _fetch_libraries_via_http helper."""

    @patch("requests.get")
    def test_fetch_libraries_filters_movie_and_show(self, mock_get):
        from plex_generate_previews.web.routes import _fetch_libraries_via_http

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "MediaContainer": {
                "Directory": [
                    {"key": "1", "title": "Movies", "type": "movie", "totalSize": 50},
                    {"key": "2", "title": "TV", "type": "show", "totalSize": 20},
                    {"key": "3", "title": "Photos", "type": "photo"},
                ]
            }
        }
        mock_response.raise_for_status = MagicMock()
        mock_get.return_value = mock_response
        result = _fetch_libraries_via_http("http://plex:32400", "token")
        assert len(result) == 2
        assert result[0]["name"] == "Movies"
        assert result[0]["type"] == "movie"
        assert result[1]["name"] == "TV"
        assert result[1]["type"] == "show"
        assert "count" not in result[0]
        assert mock_get.call_args.kwargs["verify"] is True

    @patch("requests.get")
    def test_fetch_libraries_can_disable_ssl_verification(self, mock_get):
        from plex_generate_previews.web.routes import _fetch_libraries_via_http

        mock_response = MagicMock()
        mock_response.json.return_value = {"MediaContainer": {"Directory": []}}
        mock_response.raise_for_status = MagicMock()
        mock_get.return_value = mock_response

        _fetch_libraries_via_http(
            "https://plex.example:32400",
            "token",
            verify_ssl=False,
        )
        assert mock_get.call_args.kwargs["verify"] is False


class TestParamToBool:
    """Test _param_to_bool uses the same truthy set as config.py."""

    def test_none_returns_default(self):
        from plex_generate_previews.web.routes import _param_to_bool

        assert _param_to_bool(None, True) is True
        assert _param_to_bool(None, False) is False

    def test_bool_passthrough(self):
        from plex_generate_previews.web.routes import _param_to_bool

        assert _param_to_bool(True, False) is True
        assert _param_to_bool(False, True) is False

    def test_truthy_strings(self):
        from plex_generate_previews.web.routes import _param_to_bool

        for val in ("true", "1", "yes", "True", "YES", " true "):
            assert _param_to_bool(val, False) is True, f"Expected True for {val!r}"

    def test_falsy_strings(self):
        from plex_generate_previews.web.routes import _param_to_bool

        for val in ("false", "0", "no", "off", "anything", ""):
            assert _param_to_bool(val, False) is False, f"Expected False for {val!r}"

    @patch("requests.get")
    def test_get_plex_libraries_passes_verify_ssl_override(self, mock_get, client):
        """verify_ssl query param flows through to requests.get."""
        from plex_generate_previews.web.settings_manager import get_settings_manager

        sm = get_settings_manager()
        sm.set("plex_url", "https://plex:32400")
        sm.set("plex_token", "tok")

        mock_response = MagicMock()
        mock_response.json.return_value = {"MediaContainer": {"Directory": []}}
        mock_response.raise_for_status = MagicMock()
        mock_get.return_value = mock_response

        resp = client.get("/api/plex/libraries?verify_ssl=false", headers=_api_headers())
        assert resp.status_code == 200
        assert mock_get.call_args.kwargs["verify"] is False


# ---------------------------------------------------------------------------
# Library cache
# ---------------------------------------------------------------------------


class TestLibraryCache:
    """Test Plex library caching behaviour."""

    @patch("plex_generate_previews.web.routes.api_system._fetch_libraries_via_http")
    def test_libraries_cached_on_second_call(self, mock_fetch, client):
        """Second call to /api/libraries returns cached data without re-fetching."""
        from plex_generate_previews.web.settings_manager import get_settings_manager

        sm = get_settings_manager()
        sm.set("plex_url", "http://plex:32400")
        sm.set("plex_token", "test-token")
        mock_fetch.return_value = [{"id": "1", "name": "Movies", "type": "movie"}]

        resp1 = client.get("/api/libraries", headers=_api_headers())
        resp2 = client.get("/api/libraries", headers=_api_headers())

        assert resp1.status_code == 200
        assert resp2.status_code == 200
        assert resp2.get_json()["libraries"][0]["name"] == "Movies"
        # Only one fetch — second call served from cache
        assert mock_fetch.call_count == 1

    @patch("plex_generate_previews.web.routes.api_system._fetch_libraries_via_http")
    def test_cache_bypassed_with_explicit_url(self, mock_fetch, client):
        """Explicit url/token query params bypass the library cache."""
        from plex_generate_previews.web.settings_manager import get_settings_manager

        sm = get_settings_manager()
        sm.set("plex_url", "http://plex:32400")
        sm.set("plex_token", "test-token")
        mock_fetch.return_value = [{"id": "1", "name": "Movies", "type": "movie"}]

        # First call populates cache
        client.get("/api/libraries", headers=_api_headers())
        # Second call with explicit overrides should bypass cache
        client.get(
            "/api/libraries?url=http://other:32400&token=tok",
            headers=_api_headers(),
        )
        assert mock_fetch.call_count == 2

    @patch("plex_generate_previews.web.routes.api_system._fetch_libraries_via_http")
    def test_cache_invalidated_on_plex_url_change(self, mock_fetch, client):
        """Saving a new plex_url clears the library cache."""
        from plex_generate_previews.web.settings_manager import get_settings_manager

        sm = get_settings_manager()
        sm.set("plex_url", "http://plex:32400")
        sm.set("plex_token", "test-token")
        mock_fetch.return_value = [{"id": "1", "name": "Movies", "type": "movie"}]

        client.get("/api/libraries", headers=_api_headers())
        assert mock_fetch.call_count == 1

        # Changing plex_url should invalidate the cache
        client.post(
            "/api/settings",
            headers=_api_headers(),
            json={"plex_url": "http://new-plex:32400"},
        )
        client.get("/api/libraries", headers=_api_headers())
        assert mock_fetch.call_count == 2


# ---------------------------------------------------------------------------
# Library type classification (pure function)
# ---------------------------------------------------------------------------


class TestClassifyLibraryType:
    """Test classify_library_type() library-type derivation."""

    def test_movie_section_returns_movie(self):
        from plex_generate_previews.web.routes.api_system import classify_library_type

        assert classify_library_type("movie", "tv.plex.agents.movie") == "movie"

    def test_movie_with_none_agent_returns_other_videos(self):
        from plex_generate_previews.web.routes.api_system import classify_library_type

        assert classify_library_type("movie", "com.plexapp.agents.none") == "other_videos"

    def test_show_section_returns_show(self):
        from plex_generate_previews.web.routes.api_system import classify_library_type

        assert classify_library_type("show", "tv.plex.agents.series") == "show"

    def test_show_with_sportarr_agent_returns_sports(self):
        from plex_generate_previews.web.routes.api_system import classify_library_type

        assert classify_library_type("show", "dev.sportarr.agents.sports") == "sports"

    def test_show_with_sportscanner_agent_returns_sports(self):
        from plex_generate_previews.web.routes.api_system import classify_library_type

        assert classify_library_type("show", "com.plexapp.agents.sportscanner") == "sports"

    def test_show_sports_pattern_is_case_insensitive(self):
        from plex_generate_previews.web.routes.api_system import classify_library_type

        assert classify_library_type("show", "SportArr.Main") == "sports"

    def test_show_with_none_agent_falls_through_to_show(self):
        from plex_generate_previews.web.routes.api_system import classify_library_type

        # agent=None should not crash and should fall through to plain "show"
        assert classify_library_type("show", None) == "show"

    def test_unknown_section_type_passes_through(self):
        from plex_generate_previews.web.routes.api_system import classify_library_type

        assert classify_library_type("photo", "agent.photos") == "photo"


# ---------------------------------------------------------------------------
# Version info helper (_get_version_info) — install-type + TTL cache
# ---------------------------------------------------------------------------


class TestGetVersionInfo:
    """Test _get_version_info() install-type detection and TTL caching."""

    @pytest.fixture(autouse=True)
    def _reset_version_cache(self):
        """Reset module-level version cache around every test."""
        from plex_generate_previews.web.routes import api_system as api_sys

        api_sys._version_cache["result"] = None
        api_sys._version_cache["fetched_at"] = 0.0
        yield
        api_sys._version_cache["result"] = None
        api_sys._version_cache["fetched_at"] = 0.0

    def test_local_docker_when_git_env_is_unknown(self, monkeypatch):
        """GIT_BRANCH=unknown + GIT_SHA=unknown → local_docker install_type."""
        monkeypatch.setenv("GIT_BRANCH", "unknown")
        monkeypatch.setenv("GIT_SHA", "unknown")
        monkeypatch.setattr(
            "plex_generate_previews.version_check.get_latest_github_release",
            lambda: "3.4.1",
        )

        from plex_generate_previews.web.routes.api_system import _get_version_info

        result = _get_version_info()

        assert result["install_type"] == "local_docker"
        assert result["current_version"] == "local build"
        assert result["latest_version"] == "3.4.1"
        assert result["update_available"] is False

    def test_docker_release_with_update_available(self, monkeypatch):
        """GIT_BRANCH=3.4.0 + newer release on GitHub → update_available=True."""
        monkeypatch.setenv("GIT_BRANCH", "3.4.0")
        monkeypatch.setenv("GIT_SHA", "abc1234")
        monkeypatch.setattr(
            "plex_generate_previews.version_check.get_latest_github_release",
            lambda: "3.4.1",
        )

        from plex_generate_previews.web.routes.api_system import _get_version_info

        result = _get_version_info()

        assert result["install_type"] == "docker"
        assert result["current_version"] == "3.4.0"
        assert result["latest_version"] == "3.4.1"
        assert result["update_available"] is True

    def test_docker_release_no_update_when_current_is_latest(self, monkeypatch):
        """GIT_BRANCH equal to latest release → update_available=False."""
        monkeypatch.setenv("GIT_BRANCH", "3.4.1")
        monkeypatch.setenv("GIT_SHA", "def5678")
        monkeypatch.setattr(
            "plex_generate_previews.version_check.get_latest_github_release",
            lambda: "3.4.1",
        )

        from plex_generate_previews.web.routes.api_system import _get_version_info

        result = _get_version_info()

        assert result["install_type"] == "docker"
        assert result["update_available"] is False

    def test_dev_docker_when_branch_is_not_a_version(self, monkeypatch):
        """Non-version GIT_BRANCH + GIT_SHA → dev_docker, update when head differs."""
        monkeypatch.setenv("GIT_BRANCH", "dev")
        monkeypatch.setenv("GIT_SHA", "abc1234")
        monkeypatch.setattr(
            "plex_generate_previews.version_check.get_branch_head_sha",
            lambda _branch: "def5678901",
        )

        from plex_generate_previews.web.routes.api_system import _get_version_info

        result = _get_version_info()

        assert result["install_type"] == "dev_docker"
        assert result["current_version"] == "dev@abc1234"
        assert result["update_available"] is True

    def test_dev_docker_no_update_when_sha_matches_head(self, monkeypatch):
        """dev branch + current SHA is a prefix of head SHA → update_available=False."""
        monkeypatch.setenv("GIT_BRANCH", "dev")
        monkeypatch.setenv("GIT_SHA", "abc1234")
        # head starts with git_sha → already at HEAD
        monkeypatch.setattr(
            "plex_generate_previews.version_check.get_branch_head_sha",
            lambda _branch: "abc1234567890",
        )

        from plex_generate_previews.web.routes.api_system import _get_version_info

        result = _get_version_info()

        assert result["install_type"] == "dev_docker"
        assert result["update_available"] is False

    def test_dev_docker_when_branch_is_main(self, monkeypatch):
        """GIT_BRANCH=main routes through dev_docker, same as other non-version branches."""
        monkeypatch.setenv("GIT_BRANCH", "main")
        monkeypatch.setenv("GIT_SHA", "4078c5d")
        monkeypatch.setattr(
            "plex_generate_previews.version_check.get_branch_head_sha",
            lambda _branch: "ed07876fedcba",
        )

        from plex_generate_previews.web.routes.api_system import _get_version_info

        result = _get_version_info()

        assert result["install_type"] == "dev_docker"
        assert result["current_version"] == "main@4078c5d"
        assert result["latest_version"] == "main@ed07876"
        assert result["update_available"] is True

    def test_pr_build_when_branch_starts_with_pr(self, monkeypatch):
        """GIT_BRANCH=pr-123 routes through pr_build: PR-123 vs latest release, no update banner."""
        monkeypatch.setenv("GIT_BRANCH", "pr-123")
        monkeypatch.setenv("GIT_SHA", "abc1234")
        monkeypatch.setattr(
            "plex_generate_previews.version_check.get_latest_github_release",
            lambda: "3.7.2",
        )

        branch_head_calls: list[str] = []

        def _unexpected_branch_head(branch: str) -> str | None:
            branch_head_calls.append(branch)
            return None

        monkeypatch.setattr(
            "plex_generate_previews.version_check.get_branch_head_sha",
            _unexpected_branch_head,
        )

        from plex_generate_previews.web.routes.api_system import _get_version_info

        result = _get_version_info()

        assert result["install_type"] == "pr_build"
        assert result["current_version"] == "PR-123"
        assert result["latest_version"] == "3.7.2"
        assert result["update_available"] is False
        assert branch_head_calls == []

    def test_source_install_when_no_git_env(self, monkeypatch):
        """No GIT_BRANCH/GIT_SHA + not a docker env → source install_type."""
        monkeypatch.delenv("GIT_BRANCH", raising=False)
        monkeypatch.delenv("GIT_SHA", raising=False)
        monkeypatch.setattr(
            "plex_generate_previews.utils.is_docker_environment",
            lambda: False,
        )
        monkeypatch.setattr(
            "plex_generate_previews.version_check.get_current_version",
            lambda: "3.4.0",
        )
        monkeypatch.setattr(
            "plex_generate_previews.version_check.get_latest_github_release",
            lambda: "3.4.1",
        )

        from plex_generate_previews.web.routes.api_system import _get_version_info

        result = _get_version_info()

        assert result["install_type"] == "source"
        assert result["current_version"] == "3.4.0"
        assert result["update_available"] is True

    def test_cache_hit_returns_memoized_result(self, monkeypatch):
        """Second call within TTL returns cached result without re-invoking helpers."""
        monkeypatch.setenv("GIT_BRANCH", "unknown")
        monkeypatch.setenv("GIT_SHA", "unknown")

        call_count = [0]

        def counting_release():
            call_count[0] += 1
            return "3.4.1"

        monkeypatch.setattr(
            "plex_generate_previews.version_check.get_latest_github_release",
            counting_release,
        )

        from plex_generate_previews.web.routes.api_system import _get_version_info

        first = _get_version_info()
        second = _get_version_info()

        assert first == second
        assert call_count[0] == 1


# ---------------------------------------------------------------------------
# /api/plex/servers — connection-list transformation
# ---------------------------------------------------------------------------


class TestGetPlexServersConnectionList:
    """Test /api/plex/servers resource filtering and connection normalization."""

    def _configure_token(self):
        from plex_generate_previews.web.settings_manager import get_settings_manager

        sm = get_settings_manager()
        sm.set("plex_token", "test-plex-token")

    def _mock_resources(self, mock_get, resources):
        mock_response = MagicMock()
        mock_response.json.return_value = resources
        mock_response.raise_for_status = MagicMock()
        mock_get.return_value = mock_response

    @patch("requests.get")
    def test_multi_connection_server_builds_normalized_list(self, mock_get, client):
        self._configure_token()
        self._mock_resources(
            mock_get,
            [
                {
                    "name": "Home Plex",
                    "clientIdentifier": "abc123",
                    "provides": "server",
                    "owned": True,
                    "connections": [
                        {
                            "protocol": "https",
                            "address": "192.168.1.10",
                            "port": 32400,
                            "uri": "https://192-168-1-10.hash.plex.direct:32400",
                            "local": True,
                            "relay": False,
                        },
                        {
                            "protocol": "https",
                            "address": "plex.example.com",
                            "port": 443,
                            "uri": "https://plex.example.com",
                            "local": False,
                            "relay": True,
                        },
                    ],
                }
            ],
        )

        resp = client.get("/api/plex/servers", headers=_api_headers())

        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data["servers"]) == 1
        server = data["servers"][0]
        # Best connection is the local one
        assert server["host"] == "192.168.1.10"
        assert server["local"] is True
        assert server["ssl"] is True
        # All connections are preserved in the normalized list
        assert len(server["connections"]) == 2
        assert server["connections"][0]["local"] is True
        assert server["connections"][1]["relay"] is True

    @patch("requests.get")
    def test_missing_uri_falls_back_to_protocol_host_port(self, mock_get, client):
        self._configure_token()
        self._mock_resources(
            mock_get,
            [
                {
                    "name": "Plex",
                    "clientIdentifier": "xyz",
                    "provides": "server",
                    "owned": True,
                    "connections": [
                        {
                            "protocol": "http",
                            "address": "10.0.0.5",
                            "port": 32400,
                            "local": True,
                            "relay": False,
                        }
                    ],
                }
            ],
        )

        resp = client.get("/api/plex/servers", headers=_api_headers())

        data = resp.get_json()
        conn = data["servers"][0]["connections"][0]
        assert conn["uri"] == "http://10.0.0.5:32400"
        assert conn["ssl"] is False

    @patch("requests.get")
    def test_non_server_resources_are_filtered_out(self, mock_get, client):
        self._configure_token()
        self._mock_resources(
            mock_get,
            [
                {"name": "MyTV", "provides": "player", "connections": []},
                {
                    "name": "Plex",
                    "clientIdentifier": "xyz",
                    "provides": "server",
                    "connections": [
                        {
                            "protocol": "http",
                            "address": "10.0.0.5",
                            "port": 32400,
                            "uri": "http://10.0.0.5:32400",
                        }
                    ],
                },
            ],
        )

        resp = client.get("/api/plex/servers", headers=_api_headers())

        data = resp.get_json()
        assert len(data["servers"]) == 1
        assert data["servers"][0]["name"] == "Plex"

    @patch("requests.get")
    def test_server_with_no_connections_is_skipped(self, mock_get, client):
        self._configure_token()
        self._mock_resources(
            mock_get,
            [{"name": "Offline", "provides": "server", "connections": []}],
        )

        resp = client.get("/api/plex/servers", headers=_api_headers())

        assert resp.get_json()["servers"] == []

    @patch("requests.get")
    def test_protocol_inferred_from_uri_when_absent(self, mock_get, client):
        self._configure_token()
        self._mock_resources(
            mock_get,
            [
                {
                    "name": "Plex",
                    "clientIdentifier": "xyz",
                    "provides": "server",
                    "connections": [
                        {
                            "address": "host.example.com",
                            "port": 32400,
                            "uri": "https://host.example.com",
                            "local": False,
                            "relay": False,
                        }
                    ],
                }
            ],
        )

        resp = client.get("/api/plex/servers", headers=_api_headers())

        conn = resp.get_json()["servers"][0]["connections"][0]
        assert conn["protocol"] == "https"
        assert conn["ssl"] is True

    def test_missing_token_returns_401(self, client):
        from plex_generate_previews.web.settings_manager import get_settings_manager

        sm = get_settings_manager()
        sm.delete("plex_token")
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PLEX_TOKEN", None)
            resp = client.get("/api/plex/servers", headers=_api_headers())

        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# /api/bif/search — multi-phase assembly
# ---------------------------------------------------------------------------


class TestBifSearchPhases:
    """Test /api/bif/search phase 1 (show expansion) and phase 2 (direct hits)."""

    def _configure_plex(self):
        from plex_generate_previews.web.settings_manager import get_settings_manager

        sm = get_settings_manager()
        sm.set("plex_url", "http://plex:32400")
        sm.set("plex_token", "test-plex-token")
        sm.set("plex_config_folder", "/config/plex")

    def _hub_response(self, hubs):
        resp = MagicMock()
        resp.json.return_value = {"MediaContainer": {"Hub": hubs}}
        resp.raise_for_status = MagicMock()
        return resp

    @patch("plex_generate_previews.web.routes.api_bif._item_to_result")
    @patch("plex_generate_previews.web.routes.api_bif._fetch_show_episodes")
    @patch("requests.get")
    def test_season_filter_skips_phase_2_and_passes_filters_to_fetch(self, mock_get, mock_fetch_eps, mock_item, client):
        """Query with ``S01E02`` expands the show hub but ignores episode/movie hubs."""
        self._configure_plex()
        mock_get.return_value = self._hub_response(
            [
                {"type": "show", "Metadata": [{"ratingKey": "100", "title": "Show"}]},
                # This episode hub would match phase 2 if season_filter were None,
                # but with a filter set it must be ignored.
                {"type": "episode", "Metadata": [{"key": "/library/metadata/999"}]},
            ]
        )
        mock_fetch_eps.return_value = [
            {"key": "/library/metadata/200", "title": "Ep1"},
            {"key": "/library/metadata/201", "title": "Ep2"},
        ]
        mock_item.side_effect = lambda item, *a, **kw: {"key": item.get("key", "")}

        resp = client.get("/api/bif/search?q=Show S01E02", headers=_api_headers())

        assert resp.status_code == 200
        keys = [r["key"] for r in resp.get_json()["results"]]
        assert "/library/metadata/200" in keys
        assert "/library/metadata/999" not in keys
        # Verify season/episode filters made it through to _fetch_show_episodes
        call_kwargs = mock_fetch_eps.call_args.kwargs
        assert call_kwargs["season_filter"] == 1
        assert call_kwargs["episode_filter"] == 2

    @patch("plex_generate_previews.web.routes.api_bif._item_to_result")
    @patch("requests.get")
    def test_plain_query_includes_movie_and_episode_hubs(self, mock_get, mock_item, client):
        self._configure_plex()
        mock_get.return_value = self._hub_response(
            [
                {"type": "movie", "Metadata": [{"key": "/library/metadata/10"}]},
                {"type": "episode", "Metadata": [{"key": "/library/metadata/11"}]},
            ]
        )
        mock_item.side_effect = lambda item, *a, **kw: {"key": item.get("key", "")}

        resp = client.get("/api/bif/search?q=Inception", headers=_api_headers())

        keys = [r["key"] for r in resp.get_json()["results"]]
        assert "/library/metadata/10" in keys
        assert "/library/metadata/11" in keys

    @patch("plex_generate_previews.web.routes.api_bif._item_to_result")
    @patch("requests.get")
    def test_duplicate_keys_are_deduped(self, mock_get, mock_item, client):
        self._configure_plex()
        mock_get.return_value = self._hub_response(
            [
                {
                    "type": "movie",
                    "Metadata": [
                        {"key": "/library/metadata/1"},
                        {"key": "/library/metadata/1"},  # duplicate
                        {"key": "/library/metadata/2"},
                    ],
                }
            ]
        )
        mock_item.side_effect = lambda item, *a, **kw: {"key": item.get("key", "")}

        resp = client.get("/api/bif/search?q=Movie", headers=_api_headers())

        results = resp.get_json()["results"]
        assert [r["key"] for r in results] == ["/library/metadata/1", "/library/metadata/2"]

    def test_short_query_returns_400(self, client):
        resp = client.get("/api/bif/search?q=a", headers=_api_headers())
        assert resp.status_code == 400
        assert "2 characters" in resp.get_json()["error"]

    def test_missing_plex_config_returns_400(self, client):
        from plex_generate_previews.web.settings_manager import get_settings_manager

        sm = get_settings_manager()
        sm.delete("plex_url")
        sm.delete("plex_token")
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PLEX_URL", None)
            os.environ.pop("PLEX_TOKEN", None)
            resp = client.get("/api/bif/search?q=Inception", headers=_api_headers())
        assert resp.status_code == 400

    @patch("requests.get")
    def test_plex_network_failure_returns_502(self, mock_get, client):
        import requests as req_mod

        self._configure_plex()
        mock_get.side_effect = req_mod.exceptions.ConnectionError("refused")

        resp = client.get("/api/bif/search?q=Inception", headers=_api_headers())

        assert resp.status_code == 502
