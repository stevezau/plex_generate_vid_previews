"""Tests for AUTH_METHOD=external (external authentication bypass)."""

import json

import pytest

from plex_generate_previews.web.auth import (
    AUTH_METHOD_EXTERNAL,
    AUTH_METHOD_INTERNAL,
    get_auth_method,
    is_auth_external,
)
from plex_generate_previews.web.settings_manager import get_settings_manager


@pytest.fixture
def mock_auth_config(tmp_path, monkeypatch):
    """Mock auth module to use temp directory."""
    auth_file = str(tmp_path / "auth.json")
    monkeypatch.setattr("plex_generate_previews.web.auth.AUTH_FILE", auth_file)
    monkeypatch.setattr("plex_generate_previews.web.auth.get_config_dir", lambda: str(tmp_path))
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


class TestGetAuthMethod:
    """Tests for get_auth_method() env var parsing."""

    def test_default_is_internal(self, monkeypatch):
        monkeypatch.delenv("AUTH_METHOD", raising=False)
        assert get_auth_method() == AUTH_METHOD_INTERNAL

    def test_external_lowercase(self, monkeypatch):
        monkeypatch.setenv("AUTH_METHOD", "external")
        assert get_auth_method() == AUTH_METHOD_EXTERNAL

    def test_external_uppercase(self, monkeypatch):
        monkeypatch.setenv("AUTH_METHOD", "EXTERNAL")
        assert get_auth_method() == AUTH_METHOD_EXTERNAL

    def test_external_mixed_case(self, monkeypatch):
        monkeypatch.setenv("AUTH_METHOD", "External")
        assert get_auth_method() == AUTH_METHOD_EXTERNAL

    def test_external_with_whitespace(self, monkeypatch):
        monkeypatch.setenv("AUTH_METHOD", "  external  ")
        assert get_auth_method() == AUTH_METHOD_EXTERNAL

    def test_internal_explicit(self, monkeypatch):
        monkeypatch.setenv("AUTH_METHOD", "internal")
        assert get_auth_method() == AUTH_METHOD_INTERNAL

    def test_invalid_value_falls_back_to_internal(self, monkeypatch):
        monkeypatch.setenv("AUTH_METHOD", "bogus")
        assert get_auth_method() == AUTH_METHOD_INTERNAL

    def test_empty_string_falls_back_to_internal(self, monkeypatch):
        monkeypatch.setenv("AUTH_METHOD", "")
        assert get_auth_method() == AUTH_METHOD_INTERNAL


class TestIsAuthExternal:
    """Tests for is_auth_external() helper."""

    def test_false_by_default(self, monkeypatch):
        monkeypatch.delenv("AUTH_METHOD", raising=False)
        assert is_auth_external() is False

    def test_true_when_external(self, monkeypatch):
        monkeypatch.setenv("AUTH_METHOD", "external")
        assert is_auth_external() is True

    def test_false_when_internal(self, monkeypatch):
        monkeypatch.setenv("AUTH_METHOD", "internal")
        assert is_auth_external() is False


class TestExternalAuthRouteBehavior:
    """Test that protected routes pass through when AUTH_METHOD=external."""

    @pytest.fixture(autouse=True)
    def _set_external(self, monkeypatch):
        monkeypatch.setenv("AUTH_METHOD", "external")

    def test_dashboard_accessible_without_token(self, client):
        """GET / should not redirect to login when external auth is active."""
        settings = get_settings_manager()
        settings.set("setup_complete", True)

        response = client.get("/", follow_redirects=False)
        assert response.status_code == 200

    def test_api_jobs_accessible_without_token(self, client):
        """GET /api/jobs should succeed without any token."""
        settings = get_settings_manager()
        settings.set("setup_complete", True)

        response = client.get("/api/jobs")
        assert response.status_code == 200

    def test_api_settings_accessible_without_token(self, client):
        """GET /api/settings should succeed without any token."""
        settings = get_settings_manager()
        settings.set("setup_complete", True)

        response = client.get("/api/settings")
        assert response.status_code == 200

    def test_api_auth_status_reports_external(self, client):
        """GET /api/auth/status should report auth_method='external'."""
        response = client.get("/api/auth/status")
        assert response.status_code == 200
        data = response.get_json()
        assert data["authenticated"] is True
        assert data["auth_method"] == "external"

    def test_login_page_redirects_to_dashboard(self, client):
        """GET /login should redirect to dashboard when external auth is active."""
        settings = get_settings_manager()
        settings.set("setup_complete", True)

        response = client.get("/login", follow_redirects=False)
        assert response.status_code == 302
        assert response.headers["Location"].endswith("/")


class TestInternalAuthUnchanged:
    """Verify default (internal) auth still requires tokens."""

    @pytest.fixture(autouse=True)
    def _set_internal(self, monkeypatch):
        monkeypatch.setenv("AUTH_METHOD", "internal")

    def test_api_jobs_requires_auth(self, client):
        """GET /api/jobs should return 401 without token."""
        settings = get_settings_manager()
        settings.set("setup_complete", True)

        response = client.get("/api/jobs")
        assert response.status_code == 401

    def test_api_jobs_with_token_succeeds(self, client, auth_headers):
        """GET /api/jobs should succeed with valid token."""
        settings = get_settings_manager()
        settings.set("setup_complete", True)

        response = client.get("/api/jobs", headers=auth_headers)
        assert response.status_code == 200

    def test_api_auth_status_reports_internal(self, client):
        """GET /api/auth/status should report auth_method='internal'."""
        response = client.get("/api/auth/status")
        data = response.get_json()
        assert data["auth_method"] == "internal"


class TestWebhookAuthNotBypassed:
    """Webhook auth uses its own decorator and must NOT be bypassed by external auth."""

    @pytest.fixture(autouse=True)
    def _set_external(self, monkeypatch):
        monkeypatch.setenv("AUTH_METHOD", "external")

    def test_radarr_webhook_requires_token(self, client):
        """POST /api/webhooks/radarr still requires webhook token when external."""
        response = client.post(
            "/api/webhooks/radarr",
            data=json.dumps({"eventType": "Test"}),
            content_type="application/json",
        )
        assert response.status_code == 401

    def test_sonarr_webhook_requires_token(self, client):
        """POST /api/webhooks/sonarr still requires webhook token when external."""
        response = client.post(
            "/api/webhooks/sonarr",
            data=json.dumps({"eventType": "Test"}),
            content_type="application/json",
        )
        assert response.status_code == 401

    def test_custom_webhook_not_auto_authenticated(self, client):
        """POST /api/webhooks/custom is not bypassed by external auth.

        The custom endpoint is not CSRF-exempt (pre-existing), so without a
        browser session it returns 302 rather than 401.  Either way, the
        request must NOT succeed (200) -- proving _authenticate_webhook is
        independent from is_authenticated().
        """
        response = client.post(
            "/api/webhooks/custom",
            data=json.dumps({"eventType": "Test"}),
            content_type="application/json",
        )
        assert response.status_code != 200

    def test_radarr_webhook_succeeds_with_valid_token(self, client, auth_headers):
        """POST /api/webhooks/radarr succeeds with valid token even when external."""
        response = client.post(
            "/api/webhooks/radarr",
            data=json.dumps({"eventType": "Test"}),
            content_type="application/json",
            headers=auth_headers,
        )
        assert response.status_code == 200


class TestExternalAuthReenableInternal:
    """Test that unsetting AUTH_METHOD re-enables internal auth."""

    def test_switch_from_external_to_internal(self, client, monkeypatch):
        """Removing AUTH_METHOD should require tokens again."""
        settings = get_settings_manager()
        settings.set("setup_complete", True)

        monkeypatch.setenv("AUTH_METHOD", "external")
        response = client.get("/api/jobs")
        assert response.status_code == 200

        monkeypatch.setenv("AUTH_METHOD", "internal")
        response = client.get("/api/jobs")
        assert response.status_code == 401


class TestTokenInfoIncludesAuthMethod:
    """Test that get_token_info() includes auth_method field."""

    def test_token_info_internal(self, mock_auth_config, monkeypatch):
        monkeypatch.setenv("AUTH_METHOD", "internal")
        from plex_generate_previews.web.auth import get_token_info

        info = get_token_info()
        assert info["auth_method"] == "internal"

    def test_token_info_external(self, mock_auth_config, monkeypatch):
        monkeypatch.setenv("AUTH_METHOD", "external")
        from plex_generate_previews.web.auth import get_token_info

        info = get_token_info()
        assert info["auth_method"] == "external"
