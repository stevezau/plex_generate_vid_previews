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
    # Clear GPU detection cache
    from plex_generate_previews.web.routes import clear_gpu_cache

    clear_gpu_cache()
    yield
    reset_settings_manager()
    with jobs_mod._job_lock:
        jobs_mod._job_manager = None
    with sched_mod._schedule_lock:
        sched_mod._schedule_manager = None
    clear_gpu_cache()


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
        resp = client.post(
            "/login", data={"token": "test-token-12345678"}, follow_redirects=False
        )
        # Should redirect to dashboard
        assert resp.status_code in (302, 308)
        assert "/login" not in resp.headers.get("Location", "")

    def test_login_post_invalid_token(self, client):
        resp = client.post("/login", data={"token": "wrong"})
        assert resp.status_code == 200
        assert b"Invalid" in resp.data or b"invalid" in resp.data

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
        assert resp.get_json()["jobs"] == []

    def test_create_job(self, client):
        """Test creating a job (mocking _start_job_async to avoid real processing)."""
        with patch("plex_generate_previews.web.routes._start_job_async"):
            resp = client.post(
                "/api/jobs", headers=_api_headers(), json={"library_name": "Movies"}
            )
        assert resp.status_code == 201
        data = resp.get_json()
        assert data["status"] == "pending"
        assert "id" in data

    def test_create_job_ignores_credential_overrides(self, client):
        """Job creation accepts request with credential-like keys but allow-list prevents applying them."""
        with patch("plex_generate_previews.web.routes._start_job_async") as mock_start:
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
        with patch("plex_generate_previews.web.routes._start_job_async"):
            create_resp = client.post(
                "/api/jobs", headers=_api_headers(), json={"library_name": "TV"}
            )
        job_id = create_resp.get_json()["id"]
        resp = client.get(f"/api/jobs/{job_id}", headers=_api_headers())
        assert resp.status_code == 200
        assert resp.get_json()["id"] == job_id

    def test_get_nonexistent_job(self, client):
        resp = client.get("/api/jobs/nonexistent", headers=_api_headers())
        assert resp.status_code == 404

    def test_cancel_job(self, client):
        with patch("plex_generate_previews.web.routes._start_job_async"):
            create_resp = client.post("/api/jobs", headers=_api_headers(), json={})
        job_id = create_resp.get_json()["id"]
        resp = client.post(f"/api/jobs/{job_id}/cancel", headers=_api_headers())
        assert resp.status_code == 200
        logs_resp = client.get(f"/api/jobs/{job_id}/logs", headers=_api_headers())
        logs = logs_resp.get_json()["logs"]
        assert any("Cancellation requested by user" in line for line in logs)

    def test_complete_job_does_not_override_cancelled_status(self, client):
        with patch("plex_generate_previews.web.routes._start_job_async"):
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
        with patch("plex_generate_previews.web.routes._start_job_async"):
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
        with patch("plex_generate_previews.web.routes._start_job_async"):
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
        with patch("plex_generate_previews.web.routes._start_job_async"):
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
        with patch("plex_generate_previews.web.routes._start_job_async"):
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
        with patch("plex_generate_previews.web.routes._start_job_async"):
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
        with patch("plex_generate_previews.web.routes._start_job_async"):
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
        with patch("plex_generate_previews.web.routes._start_job_async"):
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
        with patch("plex_generate_previews.web.routes._start_job_async"):
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
            json={"worker_type": "CPU_FALLBACK", "count": 2},
        )
        assert remove_resp.status_code == 200
        data = remove_resp.get_json()
        assert data["removed"] == 1
        assert data["scheduled_removal"] == 0
        assert data["unavailable"] == 1

    def test_workers_add_global_no_job_returns_400(self, client):
        """POST /api/workers/add returns 400 when no job is running."""
        resp = client.post(
            "/api/workers/add",
            headers=_api_headers(),
            json={"worker_type": "CPU", "count": 1},
        )
        assert resp.status_code == 400
        assert "running" in resp.get_json().get("error", "").lower()

    def test_workers_remove_global_no_job_returns_400(self, client):
        """POST /api/workers/remove returns 400 when no job is running."""
        resp = client.post(
            "/api/workers/remove",
            headers=_api_headers(),
            json={"worker_type": "CPU", "count": 1},
        )
        assert resp.status_code == 400
        assert "running" in resp.get_json().get("error", "").lower()

    def test_workers_add_global_success(self, client):
        """POST /api/workers/add delegates to running job pool."""
        with patch("plex_generate_previews.web.routes._start_job_async"):
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
        with patch("plex_generate_previews.web.routes._start_job_async"):
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
        assert "cpu_fallback_threads" in data

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

    def test_save_settings(self, client):
        resp = client.post(
            "/api/settings",
            headers=_api_headers(),
            json={"gpu_threads": 2, "cpu_threads": 4, "cpu_fallback_threads": 1},
        )
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True

        # Verify setting persisted
        resp2 = client.get("/api/settings", headers=_api_headers())
        data = resp2.get_json()
        assert data["gpu_threads"] == 2
        assert data["cpu_threads"] == 4
        assert data["cpu_fallback_threads"] == 1

    def test_save_settings_ignores_unknown_fields(self, client):
        resp = client.post(
            "/api/settings",
            headers=_api_headers(),
            json={"gpu_threads": 1, "unknown_field": "ignored"},
        )
        assert resp.status_code == 200

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


# ---------------------------------------------------------------------------
# Job config / path_mappings (settings vs config_overrides)
# ---------------------------------------------------------------------------


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
            patch("plex_generate_previews.cli.run_processing", side_effect=capture_run_processing),
            patch("plex_generate_previews.config.load_config", return_value=mock_config),
            patch("plex_generate_previews.web.routes._verify_tmp_folder_health", return_value=(True, [])),
            patch("plex_generate_previews.utils.setup_working_directory", return_value=str(tmp_path / "work")),
            patch("plex_generate_previews.gpu_detection.detect_all_gpus", return_value=[]),
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
            {"plex_prefix": "/override", "local_prefix": "/local_override", "webhook_prefixes": []}
        ]
        client.post(
            "/api/settings",
            headers=_api_headers(),
            json={
                "path_mappings": [
                    {"plex_prefix": "/from_settings", "local_prefix": "/local", "webhook_prefixes": []}
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
            patch("plex_generate_previews.cli.run_processing", side_effect=capture_run_processing),
            patch("plex_generate_previews.config.load_config", return_value=mock_config),
            patch("plex_generate_previews.web.routes._verify_tmp_folder_health", return_value=(True, [])),
            patch("plex_generate_previews.utils.setup_working_directory", return_value=str(tmp_path / "work")),
            patch("plex_generate_previews.gpu_detection.detect_all_gpus", return_value=[]),
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
            {"plex_prefix": "/data", "local_prefix": "/mnt/data", "webhook_prefixes": ["/data"]}
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
            patch("plex_generate_previews.cli.run_processing", side_effect=capture_run_processing),
            patch("plex_generate_previews.config.load_config", return_value=mock_config),
            patch("plex_generate_previews.web.routes._verify_tmp_folder_health", return_value=(True, [])),
            patch("plex_generate_previews.utils.setup_working_directory", return_value=str(tmp_path / "work")),
            patch("plex_generate_previews.gpu_detection.detect_all_gpus", return_value=[]),
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
            patch("plex_generate_previews.cli.run_processing", side_effect=capture_run_processing),
            patch("plex_generate_previews.config.load_config", return_value=mock_config),
            patch("plex_generate_previews.web.routes._verify_tmp_folder_health", return_value=(True, [])),
            patch("plex_generate_previews.utils.setup_working_directory", return_value=str(tmp_path / "work")),
            patch("plex_generate_previews.gpu_detection.detect_all_gpus", return_value=[]),
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
            patch("plex_generate_previews.cli.run_processing", side_effect=capture_run_processing),
            patch("plex_generate_previews.config.load_config", return_value=mock_config),
            patch("plex_generate_previews.web.routes._verify_tmp_folder_health", return_value=(True, [])),
            patch("plex_generate_previews.utils.setup_working_directory", return_value=str(tmp_path / "work")),
            patch("plex_generate_previews.gpu_detection.detect_all_gpus", return_value=[]),
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
        resp = client.post(
            "/api/schedules", headers=_api_headers(), json={"name": "Test"}
        )
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# System API
# ---------------------------------------------------------------------------


class TestSystemAPI:
    """Test /api/system/* endpoints."""

    def test_get_system_status(self, client):
        with patch(
            "plex_generate_previews.gpu_detection.detect_all_gpus", return_value=[]
        ):
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
        assert "cpu_fallback_threads" in data


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

    def test_validate_paths_path_mappings_new_format_local_not_found(
        self, client, tmp_path, monkeypatch
    ):
        """New-format path_mappings: invalid local_prefix returns validation error."""
        (tmp_path / "Media" / "localhost").mkdir(parents=True)
        monkeypatch.setattr(
            "plex_generate_previews.web.routes.PLEX_DATA_ROOT", str(tmp_path)
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
        assert any(
            "Folder not found" in e or "Path in this app" in e or "Row 1" in e
            for e in data["errors"]
        )

    def test_validate_paths_path_mappings_null_byte_in_local(
        self, client, tmp_path, monkeypatch
    ):
        """path_mappings row with null byte in local_prefix returns invalid path error."""
        (tmp_path / "Media" / "localhost").mkdir(parents=True)
        monkeypatch.setattr(
            "plex_generate_previews.web.routes.PLEX_DATA_ROOT", str(tmp_path)
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
        assert any("Invalid" in e for e in data["errors"])

    def test_validate_paths_legacy_plex_only_returns_error(
        self, client, tmp_path, monkeypatch
    ):
        """Legacy: only plex_videos_path_mapping set returns Local Media Path required."""
        (tmp_path / "Media" / "localhost").mkdir(parents=True)
        monkeypatch.setattr(
            "plex_generate_previews.web.routes.PLEX_DATA_ROOT", str(tmp_path)
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

    def test_validate_paths_legacy_local_only_returns_error(
        self, client, tmp_path, monkeypatch
    ):
        """Legacy: only plex_local_videos_path_mapping set returns Plex Media Path required."""
        (tmp_path / "Media" / "localhost").mkdir(parents=True)
        monkeypatch.setattr(
            "plex_generate_previews.web.routes.PLEX_DATA_ROOT", str(tmp_path)
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
        resp = client.get(
            "/api/jobs", headers={"Authorization": "Bearer test-token-12345678"}
        )
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
