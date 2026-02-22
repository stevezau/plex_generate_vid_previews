"""Tests for @setup_or_auth_required decorator in web.auth."""

import json
import pytest

from plex_generate_previews.web.settings_manager import get_settings_manager


@pytest.fixture
def mock_auth_config(tmp_path, monkeypatch):
    """Mock auth module to use temp directory."""
    auth_file = str(tmp_path / "auth.json")
    monkeypatch.setattr("plex_generate_previews.web.auth.AUTH_FILE", auth_file)
    monkeypatch.setattr(
        "plex_generate_previews.web.auth.get_config_dir", lambda: str(tmp_path)
    )
    from plex_generate_previews.web.settings_manager import reset_settings_manager

    reset_settings_manager()
    from plex_generate_previews.web.routes import clear_gpu_cache

    clear_gpu_cache()
    return str(tmp_path)


@pytest.fixture
def flask_app(tmp_path, mock_auth_config):
    """Create Flask test app with temp directory."""
    from plex_generate_previews.web.app import create_app

    app = create_app(config_dir=str(tmp_path))
    app.config["TESTING"] = True
    return app


@pytest.fixture
def client(flask_app):
    """Create Flask test client."""
    return flask_app.test_client()


@pytest.fixture
def auth_headers():
    """Generate valid auth headers with token."""
    from plex_generate_previews.web.auth import get_auth_token

    token = get_auth_token()
    return {"X-Auth-Token": token}


class TestSetupNotComplete:
    """Tests for unauthenticated access when setup is not complete."""

    def test_unauthenticated_get_settings(self, client):
        """Test unauthenticated GET /api/settings when setup incomplete."""
        settings = get_settings_manager()
        assert not settings.is_setup_complete()

        response = client.get("/api/settings")
        assert response.status_code == 200

    def test_unauthenticated_post_setup_set_token(self, client):
        """Test unauthenticated POST /api/setup/set-token when setup incomplete."""
        settings = get_settings_manager()
        assert not settings.is_setup_complete()

        data = {"token": "test-tok-12345678", "confirm_token": "test-tok-12345678"}
        response = client.post(
            "/api/setup/set-token",
            data=json.dumps(data),
            content_type="application/json",
        )
        assert response.status_code == 200

    def test_unauthenticated_get_plex_servers(self, client):
        """Test unauthenticated GET /api/plex/servers when setup incomplete.

        The auth decorator should not block the request (no 401 from
        authentication).  A downstream 401 for missing Plex token is
        acceptable.
        """
        settings = get_settings_manager()
        assert not settings.is_setup_complete()

        response = client.get("/api/plex/servers")
        # The endpoint itself may return 401 for "No Plex token available" —
        # that is NOT from the auth decorator.  Check the error message.
        if response.status_code == 401:
            data = response.get_json()
            assert data.get("error") != "Authentication required"

    def test_unauthenticated_get_system_status(self, client):
        """Test unauthenticated GET /api/system/status when setup incomplete."""
        settings = get_settings_manager()
        assert not settings.is_setup_complete()

        response = client.get("/api/system/status")
        assert response.status_code == 200

    def test_unauthenticated_get_setup_state(self, client):
        """Test unauthenticated GET /api/setup/state when setup incomplete."""
        settings = get_settings_manager()
        assert not settings.is_setup_complete()

        response = client.get("/api/setup/state")
        assert response.status_code == 200

    def test_unauthenticated_post_setup_state(self, client):
        """Test unauthenticated POST /api/setup/state when setup incomplete."""
        settings = get_settings_manager()
        assert not settings.is_setup_complete()

        data = {"step": 2, "data": {"foo": "bar"}}
        response = client.post(
            "/api/setup/state",
            data=json.dumps(data),
            content_type="application/json",
        )
        assert response.status_code == 200

    def test_unauthenticated_get_token_info(self, client):
        """Test unauthenticated GET /api/setup/token-info when setup incomplete."""
        settings = get_settings_manager()
        assert not settings.is_setup_complete()

        response = client.get("/api/setup/token-info")
        assert response.status_code == 200


class TestSetupComplete:
    """Tests for auth enforcement when setup is complete."""

    def test_unauthenticated_get_settings_requires_auth(self, client):
        """Test unauthenticated GET /api/settings returns 401 after setup."""
        settings = get_settings_manager()
        settings.set("setup_complete", True)

        response = client.get("/api/settings")
        assert response.status_code == 401
        assert response.get_json()["error"] == "Authentication required"

    def test_unauthenticated_post_settings_requires_auth(self, client):
        """Test unauthenticated POST /api/settings returns 401 after setup."""
        settings = get_settings_manager()
        settings.set("setup_complete", True)

        response = client.post(
            "/api/settings",
            data=json.dumps({"plex_url": "http://test:32400"}),
            content_type="application/json",
        )
        assert response.status_code == 401
        assert response.get_json()["error"] == "Authentication required"

    def test_unauthenticated_post_set_token_requires_auth(self, client):
        """Test unauthenticated POST /api/setup/set-token returns 401 after setup."""
        settings = get_settings_manager()
        settings.set("setup_complete", True)

        response = client.post(
            "/api/setup/set-token",
            data=json.dumps({"token": "test12345", "confirm_token": "test12345"}),
            content_type="application/json",
        )
        assert response.status_code == 401

    def test_unauthenticated_get_token_info_requires_auth(self, client):
        """Test unauthenticated GET /api/setup/token-info returns 401 after setup."""
        settings = get_settings_manager()
        settings.set("setup_complete", True)

        response = client.get("/api/setup/token-info")
        assert response.status_code == 401

    def test_valid_bearer_token_get_settings(self, client, auth_headers):
        """Test valid Bearer token allows GET /api/settings after setup."""
        settings = get_settings_manager()
        settings.set("setup_complete", True)

        headers = {"Authorization": f"Bearer {auth_headers['X-Auth-Token']}"}
        response = client.get("/api/settings", headers=headers)
        assert response.status_code == 200

    def test_valid_x_auth_token_get_settings(self, client, auth_headers):
        """Test valid X-Auth-Token allows GET /api/settings after setup."""
        settings = get_settings_manager()
        settings.set("setup_complete", True)

        response = client.get("/api/settings", headers=auth_headers)
        assert response.status_code == 200

    def test_invalid_token_get_settings(self, client):
        """Test invalid X-Auth-Token returns 401 after setup."""
        settings = get_settings_manager()
        settings.set("setup_complete", True)

        headers = {"X-Auth-Token": "invalid-token-xyz"}
        response = client.get("/api/settings", headers=headers)
        assert response.status_code == 401

    def test_invalid_bearer_token_get_settings(self, client):
        """Test invalid Bearer token returns 401 after setup."""
        settings = get_settings_manager()
        settings.set("setup_complete", True)

        headers = {"Authorization": "Bearer invalid-token-xyz"}
        response = client.get("/api/settings", headers=headers)
        assert response.status_code == 401

    def test_authenticated_post_setup_complete(self, client, auth_headers):
        """Test authenticated POST /api/setup/complete succeeds after setup."""
        settings = get_settings_manager()
        settings.set("setup_complete", True)

        response = client.post("/api/setup/complete", headers=auth_headers)
        assert response.status_code == 200

    def test_authenticated_get_system_status(self, client, auth_headers):
        """Test authenticated GET /api/system/status succeeds after setup."""
        settings = get_settings_manager()
        settings.set("setup_complete", True)

        response = client.get("/api/system/status", headers=auth_headers)
        assert response.status_code == 200


class TestEdgeCases:
    """Tests for edge cases and state transitions."""

    def test_setup_completes_mid_session(self, client, auth_headers):
        """Test that completing setup mid-session enforces auth on next request."""
        settings = get_settings_manager()
        assert not settings.is_setup_complete()

        # First request: setup incomplete — no auth required
        response = client.get("/api/settings")
        assert response.status_code == 200

        # Mark setup as complete
        settings.set("setup_complete", True)

        # Second request: setup complete — auth required
        response = client.get("/api/settings")
        assert response.status_code == 401

        # Third request: with valid auth — succeeds
        response = client.get("/api/settings", headers=auth_headers)
        assert response.status_code == 200

    def test_empty_bearer_prefix(self, client):
        """Test 'Bearer ' without token body is rejected after setup."""
        settings = get_settings_manager()
        settings.set("setup_complete", True)

        headers = {"Authorization": "Bearer "}
        response = client.get("/api/settings", headers=headers)
        assert response.status_code == 401

    def test_malformed_authorization_header(self, client):
        """Test non-Bearer Authorization header is rejected after setup."""
        settings = get_settings_manager()
        settings.set("setup_complete", True)

        headers = {"Authorization": "NotBearer some-token"}
        response = client.get("/api/settings", headers=headers)
        assert response.status_code == 401

    def test_empty_x_auth_token(self, client):
        """Test empty X-Auth-Token string is rejected after setup."""
        settings = get_settings_manager()
        settings.set("setup_complete", True)

        headers = {"X-Auth-Token": ""}
        response = client.get("/api/settings", headers=headers)
        assert response.status_code == 401
