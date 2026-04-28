"""Tests for the vendor auth-flow API endpoints used by the Add Server wizard."""

from __future__ import annotations

import pytest

from plex_generate_previews.servers.emby_auth import EmbyAuthResult
from plex_generate_previews.servers.jellyfin_auth import (
    JellyfinAuthResult,
    QuickConnectInitiation,
)
from plex_generate_previews.web.settings_manager import get_settings_manager


@pytest.fixture
def mock_auth_config(tmp_path, monkeypatch):
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
    from plex_generate_previews.web.app import create_app

    app = create_app(config_dir=str(tmp_path))
    app.config["TESTING"] = True
    return app


@pytest.fixture
def client(flask_app):
    return flask_app.test_client()


@pytest.fixture
def auth_headers():
    from plex_generate_previews.web.auth import get_auth_token

    return {"X-Auth-Token": get_auth_token()}


class TestEmbyPasswordAuth:
    def test_success_returns_token(self, client, auth_headers, monkeypatch):
        monkeypatch.setattr(
            "plex_generate_previews.web.routes.api_server_auth.authenticate_emby_with_password",
            lambda **kw: EmbyAuthResult(
                ok=True,
                access_token="emby-tok",
                user_id="user-1",
                server_id="srv-emby",
                server_name="Office Emby",
                message="OK",
            ),
        )
        response = client.post(
            "/api/servers/auth/emby/password",
            headers=auth_headers,
            json={"url": "http://emby:8096", "username": "admin", "password": "pw"},
        )
        assert response.status_code == 200
        body = response.get_json()
        assert body["ok"] is True
        assert body["access_token"] == "emby-tok"
        assert body["user_id"] == "user-1"
        assert body["server_id"] == "srv-emby"

    def test_invalid_creds_surfaces_message(self, client, auth_headers, monkeypatch):
        monkeypatch.setattr(
            "plex_generate_previews.web.routes.api_server_auth.authenticate_emby_with_password",
            lambda **kw: EmbyAuthResult(ok=False, message="Emby rejected the username/password (401)"),
        )
        response = client.post(
            "/api/servers/auth/emby/password",
            headers=auth_headers,
            json={"url": "http://emby:8096", "username": "admin", "password": "wrong"},
        )
        body = response.get_json()
        assert body["ok"] is False
        assert "401" in body["message"]

    def test_missing_url_400(self, client, auth_headers):
        response = client.post(
            "/api/servers/auth/emby/password",
            headers=auth_headers,
            json={"username": "x", "password": "y"},
        )
        assert response.status_code == 400

    def test_missing_username_400(self, client, auth_headers):
        response = client.post(
            "/api/servers/auth/emby/password",
            headers=auth_headers,
            json={"url": "http://x", "password": "y"},
        )
        assert response.status_code == 400

    def test_unexpected_error_returns_500(self, client, auth_headers, monkeypatch):
        def boom(**kw):
            raise RuntimeError("kaboom")

        monkeypatch.setattr(
            "plex_generate_previews.web.routes.api_server_auth.authenticate_emby_with_password",
            boom,
        )
        response = client.post(
            "/api/servers/auth/emby/password",
            headers=auth_headers,
            json={"url": "http://x", "username": "a", "password": "b"},
        )
        assert response.status_code == 500


class TestJellyfinPasswordAuth:
    def test_success(self, client, auth_headers, monkeypatch):
        monkeypatch.setattr(
            "plex_generate_previews.web.routes.api_server_auth.authenticate_jellyfin_with_password",
            lambda **kw: JellyfinAuthResult(
                ok=True,
                access_token="jf-tok",
                user_id="jf-u",
                server_id="jf-srv",
                server_name="Family Jellyfin",
                message="OK",
            ),
        )
        response = client.post(
            "/api/servers/auth/jellyfin/password",
            headers=auth_headers,
            json={"url": "http://jellyfin:8096", "username": "admin", "password": "pw"},
        )
        body = response.get_json()
        assert body["ok"] is True
        assert body["access_token"] == "jf-tok"


class TestJellyfinQuickConnect:
    def test_initiate_returns_code_and_secret(self, client, auth_headers, monkeypatch):
        monkeypatch.setattr(
            "plex_generate_previews.web.routes.api_server_auth.initiate_quick_connect",
            lambda **kw: (QuickConnectInitiation(code="ABC123", secret="abc-secret"), "Initiated"),
        )
        response = client.post(
            "/api/servers/auth/jellyfin/quick-connect/initiate",
            headers=auth_headers,
            json={"url": "http://jellyfin:8096"},
        )
        body = response.get_json()
        assert body["ok"] is True
        assert body["code"] == "ABC123"
        assert body["secret"] == "abc-secret"

    def test_initiate_failure_surfaces_message(self, client, auth_headers, monkeypatch):
        monkeypatch.setattr(
            "plex_generate_previews.web.routes.api_server_auth.initiate_quick_connect",
            lambda **kw: (None, "Quick Connect not enabled by admin"),
        )
        response = client.post(
            "/api/servers/auth/jellyfin/quick-connect/initiate",
            headers=auth_headers,
            json={"url": "http://jellyfin:8096"},
        )
        body = response.get_json()
        assert body["ok"] is False
        assert "Quick Connect" in body["message"]

    def test_poll_pending(self, client, auth_headers, monkeypatch):
        monkeypatch.setattr(
            "plex_generate_previews.web.routes.api_server_auth.poll_quick_connect",
            lambda **kw: (False, "Pending"),
        )
        response = client.post(
            "/api/servers/auth/jellyfin/quick-connect/poll",
            headers=auth_headers,
            json={"url": "http://jellyfin:8096", "secret": "abc-secret"},
        )
        body = response.get_json()
        assert body["ok"] is True
        assert body["authenticated"] is False

    def test_poll_approved(self, client, auth_headers, monkeypatch):
        monkeypatch.setattr(
            "plex_generate_previews.web.routes.api_server_auth.poll_quick_connect",
            lambda **kw: (True, "Approved"),
        )
        response = client.post(
            "/api/servers/auth/jellyfin/quick-connect/poll",
            headers=auth_headers,
            json={"url": "http://jellyfin:8096", "secret": "abc-secret"},
        )
        body = response.get_json()
        assert body["authenticated"] is True

    def test_exchange_success(self, client, auth_headers, monkeypatch):
        monkeypatch.setattr(
            "plex_generate_previews.web.routes.api_server_auth.exchange_quick_connect",
            lambda **kw: JellyfinAuthResult(
                ok=True,
                access_token="qc-tok",
                user_id="u1",
                server_id="srv",
                server_name="JF",
                message="Authenticated via Quick Connect",
            ),
        )
        response = client.post(
            "/api/servers/auth/jellyfin/quick-connect/exchange",
            headers=auth_headers,
            json={"url": "http://jellyfin:8096", "secret": "abc-secret"},
        )
        body = response.get_json()
        assert body["ok"] is True
        assert body["access_token"] == "qc-tok"

    def test_exchange_missing_secret(self, client, auth_headers):
        response = client.post(
            "/api/servers/auth/jellyfin/quick-connect/exchange",
            headers=auth_headers,
            json={"url": "http://jellyfin:8096"},
        )
        assert response.status_code == 400

    def test_endpoints_dont_persist_anything(self, client, auth_headers, monkeypatch):
        # No matter which auth endpoint runs, settings.media_servers stays empty.
        monkeypatch.setattr(
            "plex_generate_previews.web.routes.api_server_auth.authenticate_emby_with_password",
            lambda **kw: EmbyAuthResult(ok=True, access_token="t", user_id="u"),
        )
        client.post(
            "/api/servers/auth/emby/password",
            headers=auth_headers,
            json={"url": "http://emby", "username": "x", "password": "y"},
        )
        # Auth is stateless; media_servers stays empty.
        assert get_settings_manager().get("media_servers") in (None, [])
