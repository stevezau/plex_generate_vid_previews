"""
Unit tests for Flask web routes.

Covers: login flow, auth API, settings CRUD, job management,
setup wizard, token endpoints, health check, and schedule endpoints.
Uses Flask's test client with an in-memory config dir.
"""

import json
import os
from unittest.mock import patch

import pytest

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
        assert "workers" in resp.get_json()

    def test_get_job_logs(self, client):
        with patch("plex_generate_previews.web.routes._start_job_async"):
            create_resp = client.post("/api/jobs", headers=_api_headers(), json={})
        job_id = create_resp.get_json()["id"]
        resp = client.get(f"/api/jobs/{job_id}/logs", headers=_api_headers())
        assert resp.status_code == 200
        assert "logs" in resp.get_json()


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

    def test_save_settings(self, client):
        resp = client.post(
            "/api/settings",
            headers=_api_headers(),
            json={"gpu_threads": 2, "cpu_threads": 4},
        )
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True

        # Verify setting persisted
        resp2 = client.get("/api/settings", headers=_api_headers())
        data = resp2.get_json()
        assert data["gpu_threads"] == 2
        assert data["cpu_threads"] == 4

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
