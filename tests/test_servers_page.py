"""Smoke test for the /servers HTML page."""

from __future__ import annotations

import pytest

from media_preview_generator.web.settings_manager import get_settings_manager


@pytest.fixture
def mock_auth_config(tmp_path, monkeypatch):
    auth_file = str(tmp_path / "auth.json")
    monkeypatch.setattr("media_preview_generator.web.auth.AUTH_FILE", auth_file)
    monkeypatch.setattr("media_preview_generator.web.auth.get_config_dir", lambda: str(tmp_path))
    from media_preview_generator.web.settings_manager import reset_settings_manager

    reset_settings_manager()
    from media_preview_generator.web.routes import clear_gpu_cache

    clear_gpu_cache()
    return str(tmp_path)


@pytest.fixture
def flask_app(tmp_path, mock_auth_config):
    from media_preview_generator.web.app import create_app

    app = create_app(config_dir=str(tmp_path))
    app.config["TESTING"] = True
    return app


@pytest.fixture
def client(flask_app):
    return flask_app.test_client()


@pytest.fixture
def authenticated_client(flask_app):
    """A test client with a valid session cookie."""
    from media_preview_generator.web.auth import get_auth_token

    token = get_auth_token()
    sm = get_settings_manager()
    sm.set("setup_complete", True)

    client = flask_app.test_client()
    # Login to get a session cookie.
    client.post("/login", data={"token": token}, follow_redirects=False)
    return client


class TestServersPage:
    def test_unauthenticated_redirects_to_login(self, client):
        response = client.get("/servers", follow_redirects=False)
        # Flask's redirect() defaults to 302 — pinning the status code
        # ensures we don't silently accept e.g. a 308 (permanent route change)
        # or a 303 (POST redirect) when neither is what the auth gate emits.
        assert response.status_code == 302
        assert "/login" in response.headers.get("Location", "")

    def test_authenticated_renders(self, authenticated_client):
        response = authenticated_client.get("/servers")
        assert response.status_code == 200
        body = response.get_data(as_text=True)
        # Key landmarks of the rendered page.
        assert "Media Servers" in body
        assert "Add Server" in body
        assert "/api/webhooks/incoming" in body or "webhookUrl" in body
        assert 'data-type="plex"' in body
        assert 'data-type="emby"' in body
        assert 'data-type="jellyfin"' in body

    def test_navbar_includes_servers_link(self, authenticated_client):
        # Wrapping the assertion in `if status_code == 200:` made this test
        # silently pass even when /  bounces to /setup or /login. Demand 200
        # unconditionally so we actually verify the navbar contents.
        response = authenticated_client.get("/")
        assert response.status_code == 200
        body = response.get_data(as_text=True)
        assert "/servers" in body
