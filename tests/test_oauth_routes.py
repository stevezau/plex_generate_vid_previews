"""
Tests for Plex OAuth API routes.

Tests the OAuth authentication flow and settings API endpoints.
"""

import json

import pytest


@pytest.fixture
def mock_auth_config(tmp_path, monkeypatch):
    """Mock auth module to use temp directory."""
    auth_file = str(tmp_path / "auth.json")
    monkeypatch.setattr("media_preview_generator.web.auth.AUTH_FILE", auth_file)
    monkeypatch.setattr("media_preview_generator.web.auth.get_config_dir", lambda: str(tmp_path))

    # Reset the global settings manager singleton
    from media_preview_generator.web.settings_manager import reset_settings_manager

    reset_settings_manager()

    return str(tmp_path)


@pytest.fixture
def flask_app(tmp_path, mock_auth_config):
    """Create Flask app for testing."""
    from media_preview_generator.web.app import create_app
    from media_preview_generator.web.settings_manager import get_settings_manager

    app = create_app(config_dir=str(tmp_path))
    app.config["TESTING"] = True
    # Mark setup as complete so @setup_or_auth_required enforces auth
    settings = get_settings_manager()
    settings.set("setup_complete", True)
    return app


@pytest.fixture
def client(flask_app):
    """Create test client."""
    return flask_app.test_client()


@pytest.fixture
def auth_headers(flask_app):
    """Get auth headers for API calls."""
    from media_preview_generator.web.auth import get_auth_token

    token = get_auth_token()
    return {"X-Auth-Token": token}


class TestSettingsAPIRoutes:
    """Tests for settings API endpoints."""

    def test_get_settings(self, client, auth_headers):
        """Test getting current settings."""
        response = client.get("/api/settings", headers=auth_headers)

        assert response.status_code == 200
        data = json.loads(response.data)
        # Check that settings fields are present
        assert "plex_url" in data
        assert "gpu_threads" in data
        assert "thumbnail_interval" in data

    def test_update_settings(self, client, auth_headers):
        """Test updating settings."""
        # Pre-seed gpu_config so gpu_threads setter can distribute workers
        client.post(
            "/api/settings",
            headers={**auth_headers, "Content-Type": "application/json"},
            data=json.dumps(
                {
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
                }
            ),
        )

        response = client.post(
            "/api/settings",
            headers={**auth_headers, "Content-Type": "application/json"},
            data=json.dumps({"gpu_threads": 4, "thumbnail_interval": 5}),
        )

        assert response.status_code == 200
        data = json.loads(response.data)
        assert data["success"] is True

        # Verify settings were saved
        response = client.get("/api/settings", headers=auth_headers)
        data = json.loads(response.data)
        assert data["gpu_threads"] == 4
        assert data["thumbnail_interval"] == 5

    def test_update_plex_url(self, client, auth_headers):
        """Test updating plex_url setting."""
        response = client.post(
            "/api/settings",
            headers={**auth_headers, "Content-Type": "application/json"},
            data=json.dumps({"plex_url": "http://192.168.1.100:32400"}),
        )

        assert response.status_code == 200
        data = json.loads(response.data)
        assert data["success"] is True


class TestSetupRoutes:
    """Tests for setup wizard API endpoints."""

    def test_get_setup_status(self, client):
        """Test getting setup status (no auth required)."""
        response = client.get("/api/setup/status")

        assert response.status_code == 200
        data = json.loads(response.data)
        # Check actual API response fields
        assert "configured" in data
        assert "setup_complete" in data
        assert "current_step" in data
        assert "plex_authenticated" in data

    def test_save_setup_state(self, client, auth_headers):
        """Test saving setup wizard state."""
        response = client.post(
            "/api/setup/state",
            headers={**auth_headers, "Content-Type": "application/json"},
            data=json.dumps({"step": 2, "data": {"server_name": "Test Server"}}),
        )

        assert response.status_code == 200
        data = json.loads(response.data)
        assert data["success"] is True

        # Verify state was saved - check via get_setup_state
        response = client.get("/api/setup/state", headers=auth_headers)
        data = json.loads(response.data)
        assert data["step"] == 2

    def test_complete_setup(self, client, auth_headers):
        """Test completing the setup wizard."""
        # First save some settings
        client.post(
            "/api/settings",
            headers={**auth_headers, "Content-Type": "application/json"},
            data=json.dumps({"plex_url": "http://localhost:32400", "plex_token": "test-token"}),
        )

        response = client.post("/api/setup/complete", headers=auth_headers)

        assert response.status_code == 200
        data = json.loads(response.data)
        assert data["success"] is True

        # Verify setup is marked complete
        response = client.get("/api/setup/status")
        data = json.loads(response.data)
        assert data["setup_complete"] is True


class TestPlexServerRoutes:
    """Tests for Plex server discovery routes."""

    def test_get_servers_without_token(self, client, auth_headers, monkeypatch):
        """No Plex token configured -> /api/plex/servers must return 401.

        The route returns 401 specifically (see api_plex.py:get_plex_servers
        line 132-133). Accepting [400, 401, 500] here lets a regression flip
        to 500 silently — the UI can't distinguish "missing token" from
        "server crashed" if any 4xx/5xx passes.
        """
        from media_preview_generator.web.settings_manager import get_settings_manager

        monkeypatch.delenv("PLEX_TOKEN", raising=False)
        monkeypatch.delenv("PLEX_URL", raising=False)
        sm = get_settings_manager()
        sm.delete("plex_token")
        sm.delete("plex_url")

        response = client.get("/api/plex/servers", headers=auth_headers)

        assert response.status_code == 401
        body = json.loads(response.data)
        assert body["servers"] == []
        assert "token" in body["error"].lower()

    def test_get_libraries_without_server(self, client, auth_headers, monkeypatch):
        """No Plex URL/token -> /api/plex/libraries must return 400 (bad request).

        The route validates url+token *before* attempting any I/O, so the
        error class is "client request is missing data" -> 400. Accepting
        [400, 500] would let an unhandled exception path slip through.
        """
        from media_preview_generator.web.settings_manager import get_settings_manager

        monkeypatch.delenv("PLEX_TOKEN", raising=False)
        monkeypatch.delenv("PLEX_URL", raising=False)
        sm = get_settings_manager()
        sm.delete("plex_token")
        sm.delete("plex_url")

        response = client.get("/api/plex/libraries", headers=auth_headers)

        assert response.status_code == 400
        body = json.loads(response.data)
        assert body["libraries"] == []
        assert "Plex" in body["error"]

    def test_check_pin_returns_auth_token(self, client, auth_headers, monkeypatch):
        """check_plex_pin must return the auth_token to the client.

        Regression test: the multi-server "Add Plex Server" wizard captures
        the token from this response to populate the per-server entry's
        auth.token field. Without it, addSelectedPlexServers fans out with
        auth.token=null and every saved Plex server fails to authenticate.

        The legacy single-Plex setup.html flow also benefits — it ignores
        the token client-side but the saved settings.plex_token is still
        set server-side, so back-compat is preserved.
        """
        from unittest.mock import MagicMock

        # Stub the upstream plex.tv /api/v2/pins/<id> call.
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "id": 12345,
            "code": "ABCD",
            "authToken": "secret-plex-token-from-plextv",
        }
        # api_plex.py imports `requests` locally inside the route, so patch
        # the module's `get` method directly.
        import requests as _requests

        monkeypatch.setattr(_requests, "get", lambda *a, **kw: mock_response)

        response = client.get("/api/plex/auth/pin/12345", headers=auth_headers)
        assert response.status_code == 200
        data = json.loads(response.data)
        assert data["authenticated"] is True
        assert data["auth_token"] == "secret-plex-token-from-plextv"

    def test_check_pin_pending_returns_null_auth_token(self, client, auth_headers, monkeypatch):
        """When plex.tv hasn't authenticated the PIN yet, auth_token is None."""
        from unittest.mock import MagicMock

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "id": 12345,
            "code": "ABCD",
            "authToken": None,
        }
        # api_plex.py imports `requests` locally inside the route, so patch
        # the module's `get` method directly.
        import requests as _requests

        monkeypatch.setattr(_requests, "get", lambda *a, **kw: mock_response)

        response = client.get("/api/plex/auth/pin/12345", headers=auth_headers)
        assert response.status_code == 200
        data = json.loads(response.data)
        assert data["authenticated"] is False
        assert data["auth_token"] is None


class TestAuthRequired:
    """Tests for authentication requirement on API endpoints."""

    def test_settings_requires_auth(self, client):
        """Test that settings endpoint requires authentication."""
        response = client.get("/api/settings")
        assert response.status_code == 401

    def test_save_settings_requires_auth(self, client):
        """Test that save settings endpoint requires authentication."""
        response = client.post(
            "/api/settings",
            headers={"Content-Type": "application/json"},
            data=json.dumps({"gpu_threads": 4}),
        )
        assert response.status_code == 401

    def test_invalid_token_rejected(self, client):
        """Test that invalid token is rejected."""
        response = client.get("/api/settings", headers={"X-Auth-Token": "invalid-token-12345"})
        assert response.status_code == 401


class TestJobLogsAndWorkers:
    """Tests for job logs and worker status endpoints."""

    def test_get_job_logs_not_found(self, client, auth_headers):
        """Test getting logs for non-existent job returns 404."""
        response = client.get("/api/jobs/nonexistent-job-id/logs", headers=auth_headers)
        assert response.status_code == 404

    def test_get_worker_statuses(self, client, auth_headers):
        """Worker-statuses endpoint returns the contract shape, not just a list.

        Audit fix — the original assertion (``isinstance(workers, list)``)
        is tautological: a response of ``{"workers": []}`` always passes
        even when there are real workers being silently dropped. Verify
        the route returns a well-shaped envelope so a regression that
        flipped the field name (e.g. ``worker_statuses``) or returned a
        bare list at top level would fail.
        """
        response = client.get("/api/jobs/workers", headers=auth_headers)
        assert response.status_code == 200
        data = json.loads(response.data)
        # Stable envelope shape — caller-side JS depends on this.
        assert isinstance(data, dict), f"expected dict envelope, got {type(data).__name__}"
        assert "workers" in data
        assert isinstance(data["workers"], list)
        # When workers ARE present, each must carry the dispatcher's
        # contract shape — but the test fixture has no workers, so just
        # assert the list shape doesn't accidentally include garbage.
        for entry in data["workers"]:
            assert isinstance(entry, dict)
            assert "worker_id" in entry, f"worker entry missing worker_id: {entry!r}"
            assert "status" in entry, f"worker entry missing status: {entry!r}"

    def test_job_logs_requires_auth(self, client):
        """Test that job logs endpoint requires authentication."""
        response = client.get("/api/jobs/some-job-id/logs")
        assert response.status_code == 401

    def test_workers_requires_auth(self, client):
        """Test that workers endpoint requires authentication."""
        response = client.get("/api/jobs/workers")
        assert response.status_code == 401


class TestAuthTokenFunctions:
    """Tests for authentication token management functions."""

    def test_is_token_env_controlled_false(self, mock_auth_config, monkeypatch):
        """Test is_token_env_controlled returns False when env var is not set."""
        monkeypatch.delenv("WEB_AUTH_TOKEN", raising=False)
        from media_preview_generator.web.auth import is_token_env_controlled

        assert is_token_env_controlled() is False

    def test_is_token_env_controlled_true(self, mock_auth_config, monkeypatch):
        """Test is_token_env_controlled returns True when env var is set."""
        monkeypatch.setenv("WEB_AUTH_TOKEN", "env-token-value")
        from media_preview_generator.web.auth import is_token_env_controlled

        assert is_token_env_controlled() is True

    def test_set_auth_token_success(self, mock_auth_config, monkeypatch):
        """Test setting a valid token succeeds."""
        monkeypatch.delenv("WEB_AUTH_TOKEN", raising=False)
        from media_preview_generator.web.auth import get_auth_token, set_auth_token

        result = set_auth_token("my-new-secure-token")
        assert result["success"] is True
        assert get_auth_token() == "my-new-secure-token"

    def test_set_auth_token_too_short(self, mock_auth_config, monkeypatch):
        """Test setting a token that's too short fails."""
        monkeypatch.delenv("WEB_AUTH_TOKEN", raising=False)
        from media_preview_generator.web.auth import set_auth_token

        result = set_auth_token("short")
        assert result["success"] is False
        assert "at least 8 characters" in result["error"]

    def test_set_auth_token_env_locked(self, mock_auth_config, monkeypatch):
        """Test setting token fails when WEB_AUTH_TOKEN env var is set."""
        monkeypatch.setenv("WEB_AUTH_TOKEN", "env-controlled-token")
        from media_preview_generator.web.auth import set_auth_token

        result = set_auth_token("my-new-token-123")
        assert result["success"] is False
        assert "environment variable" in result["error"]

    def test_set_auth_token_rejects_same_as_current(self, mock_auth_config, monkeypatch):
        """Setup wizard step 5 forces a NEW token away from the auto-generated
        one printed in Docker logs. The server enforces that by refusing to
        save a token equal to the current one."""
        monkeypatch.delenv("WEB_AUTH_TOKEN", raising=False)
        from media_preview_generator.web.auth import get_auth_token, set_auth_token

        current = get_auth_token()
        result = set_auth_token(current)
        assert result["success"] is False
        assert "different from the current" in result["error"]

    def test_get_token_info_structure(self, mock_auth_config, monkeypatch):
        """``get_token_info`` returns a well-shaped dict AND the values are right.

        Audit fix — original asserted only key presence. A regression
        returning ``{"env_controlled": "yes", "token": None, ...}`` would
        have passed. Now also assert types + that the token is masked
        (last-4 visible, rest replaced with ``*``).
        """
        monkeypatch.delenv("WEB_AUTH_TOKEN", raising=False)
        from media_preview_generator.web.auth import get_token_info

        info = get_token_info()
        # Shape assertions
        assert isinstance(info["env_controlled"], bool), (
            f"env_controlled must be bool, got {type(info['env_controlled']).__name__}"
        )
        assert isinstance(info["token"], str)
        assert isinstance(info["token_length"], int)
        assert info["source"] in ("config", "environment"), f"unexpected source: {info['source']!r}"
        # Token must be masked — never expose more than the last 4 chars.
        assert info["token"].startswith("*"), f"token leaked unmasked: {info['token']!r}"
        assert info["token_length"] >= 8, "auto-generated tokens are at least 8 chars"

    def test_get_token_info_config_source(self, mock_auth_config, monkeypatch):
        """Test get_token_info returns config source when not env controlled."""
        monkeypatch.delenv("WEB_AUTH_TOKEN", raising=False)
        from media_preview_generator.web.auth import get_token_info

        info = get_token_info()
        assert info["env_controlled"] is False
        assert info["source"] == "config"

    def test_get_token_info_env_source(self, mock_auth_config, monkeypatch):
        """Test get_token_info returns environment source when env var set."""
        monkeypatch.setenv("WEB_AUTH_TOKEN", "env-token-12345")
        from media_preview_generator.web.auth import get_token_info

        info = get_token_info()
        assert info["env_controlled"] is True
        assert info["source"] == "environment"
        # Token should be masked — only last 4 chars visible
        assert info["token"] == "****2345"


class TestTokenAPIEndpoints:
    """Tests for token management API endpoints."""

    def test_setup_token_info_endpoint(self, client, auth_headers):
        """Test GET /api/setup/token-info returns token information."""
        response = client.get("/api/setup/token-info", headers=auth_headers)

        assert response.status_code == 200
        data = json.loads(response.data)
        assert "env_controlled" in data
        assert "token" in data
        assert "source" in data

    def test_setup_token_info_requires_auth(self, client):
        """Test token-info endpoint requires authentication."""
        response = client.get("/api/setup/token-info")
        assert response.status_code == 401

    def test_setup_set_token_success(self, client, auth_headers, monkeypatch):
        """Test POST /api/setup/set-token with valid data succeeds."""
        monkeypatch.delenv("WEB_AUTH_TOKEN", raising=False)

        response = client.post(
            "/api/setup/set-token",
            headers={**auth_headers, "Content-Type": "application/json"},
            data=json.dumps({"token": "my-new-password-123", "confirm_token": "my-new-password-123"}),
        )

        assert response.status_code == 200
        data = json.loads(response.data)
        assert data["success"] is True

    def test_setup_set_token_mismatch(self, client, auth_headers, monkeypatch):
        """Test set-token fails when tokens don't match."""
        monkeypatch.delenv("WEB_AUTH_TOKEN", raising=False)

        response = client.post(
            "/api/setup/set-token",
            headers={**auth_headers, "Content-Type": "application/json"},
            data=json.dumps({"token": "password-one-123", "confirm_token": "password-two-456"}),
        )

        assert response.status_code == 400
        data = json.loads(response.data)
        assert data["success"] is False
        assert "match" in data["error"].lower()

    def test_setup_set_token_too_short(self, client, auth_headers, monkeypatch):
        """Test set-token fails when token is too short."""
        monkeypatch.delenv("WEB_AUTH_TOKEN", raising=False)

        response = client.post(
            "/api/setup/set-token",
            headers={**auth_headers, "Content-Type": "application/json"},
            data=json.dumps({"token": "short", "confirm_token": "short"}),
        )

        assert response.status_code == 400
        data = json.loads(response.data)
        assert data["success"] is False
        assert "8 characters" in data["error"]

    def test_setup_set_token_requires_auth(self, client):
        """Test set-token endpoint requires authentication."""
        response = client.post(
            "/api/setup/set-token",
            headers={"Content-Type": "application/json"},
            data=json.dumps({"token": "test12345", "confirm_token": "test12345"}),
        )
        assert response.status_code == 401
