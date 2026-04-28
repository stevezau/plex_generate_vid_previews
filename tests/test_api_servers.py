"""Tests for the read-only multi-server API (``/api/servers``)."""

from __future__ import annotations

import pytest

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


def _seed_media_servers(servers: list[dict]) -> None:
    """Write the given list to settings.json's ``media_servers`` key."""
    sm = get_settings_manager()
    sm.set("media_servers", servers)


class TestListServers:
    def test_empty_when_no_servers_configured(self, client, auth_headers):
        response = client.get("/api/servers", headers=auth_headers)
        assert response.status_code == 200
        assert response.get_json() == {"servers": []}

    def test_returns_configured_servers(self, client, auth_headers):
        _seed_media_servers(
            [
                {
                    "id": "plex-default",
                    "type": "plex",
                    "name": "Home Plex",
                    "enabled": True,
                    "url": "http://plex:32400",
                    "auth": {"method": "token", "token": "supersecret"},
                    "libraries": [
                        {
                            "id": "1",
                            "name": "Movies",
                            "remote_paths": ["/m"],
                            "enabled": True,
                        }
                    ],
                }
            ]
        )

        response = client.get("/api/servers", headers=auth_headers)
        assert response.status_code == 200
        data = response.get_json()
        assert len(data["servers"]) == 1
        server = data["servers"][0]
        assert server["id"] == "plex-default"
        assert server["type"] == "plex"
        assert server["url"] == "http://plex:32400"

    def test_redacts_token(self, client, auth_headers):
        _seed_media_servers(
            [
                {
                    "id": "plex-default",
                    "type": "plex",
                    "name": "Plex",
                    "enabled": True,
                    "url": "http://plex:32400",
                    "auth": {"method": "token", "token": "should-be-hidden"},
                }
            ]
        )

        response = client.get("/api/servers", headers=auth_headers)
        server = response.get_json()["servers"][0]
        assert server["auth"]["token"] == "***REDACTED***"
        assert server["auth"]["method"] == "token"

    def test_redacts_emby_api_key_and_password(self, client, auth_headers):
        _seed_media_servers(
            [
                {
                    "id": "emby-1",
                    "type": "emby",
                    "name": "Emby",
                    "enabled": True,
                    "url": "http://emby:8096",
                    "auth": {
                        "method": "password",
                        "access_token": "secret-token",
                        "api_key": "secret-key",
                        "password": "secret-password",
                    },
                }
            ]
        )

        response = client.get("/api/servers", headers=auth_headers)
        server = response.get_json()["servers"][0]
        assert server["auth"]["access_token"] == "***REDACTED***"
        assert server["auth"]["api_key"] == "***REDACTED***"
        assert server["auth"]["password"] == "***REDACTED***"

    def test_skips_servers_with_unknown_type(self, client, auth_headers):
        _seed_media_servers(
            [
                {"id": "plex", "type": "plex", "name": "P", "url": "http://p"},
                {"id": "kodi", "type": "kodi", "name": "K", "url": "http://k"},
            ]
        )
        response = client.get("/api/servers", headers=auth_headers)
        ids = [s["id"] for s in response.get_json()["servers"]]
        assert ids == ["plex"]

    def test_handles_legacy_settings_without_media_servers(self, client, auth_headers):
        # No media_servers seeded → empty list, 200.
        response = client.get("/api/servers", headers=auth_headers)
        assert response.status_code == 200
        assert response.get_json()["servers"] == []


class TestGetServer:
    def test_returns_individual_server(self, client, auth_headers):
        _seed_media_servers(
            [
                {
                    "id": "plex-default",
                    "type": "plex",
                    "name": "Home Plex",
                    "enabled": True,
                    "url": "http://plex:32400",
                    "auth": {"token": "secret"},
                }
            ]
        )

        response = client.get("/api/servers/plex-default", headers=auth_headers)
        assert response.status_code == 200
        server = response.get_json()
        assert server["id"] == "plex-default"
        assert server["auth"]["token"] == "***REDACTED***"

    def test_404_when_server_id_missing(self, client, auth_headers):
        _seed_media_servers([])
        response = client.get("/api/servers/nope", headers=auth_headers)
        assert response.status_code == 404


class TestPathOwners:
    def test_diagnoses_ownership(self, client, auth_headers):
        _seed_media_servers(
            [
                {
                    "id": "plex-default",
                    "type": "plex",
                    "name": "Plex",
                    "enabled": True,
                    "url": "http://plex:32400",
                    "auth": {},
                    "libraries": [
                        {
                            "id": "1",
                            "name": "Movies",
                            "remote_paths": ["/data/movies"],
                            "enabled": True,
                        }
                    ],
                }
            ]
        )

        response = client.get(
            "/api/servers/owners",
            headers=auth_headers,
            query_string={"path": "/data/movies/Foo (2024)/Foo (2024).mkv"},
        )
        assert response.status_code == 200
        data = response.get_json()
        assert data["path"].endswith(".mkv")
        owners = data["owners"]
        assert len(owners) == 1
        assert owners[0]["server_id"] == "plex-default"
        assert owners[0]["library_name"] == "Movies"

    def test_returns_empty_when_no_owners(self, client, auth_headers):
        _seed_media_servers(
            [
                {
                    "id": "plex",
                    "type": "plex",
                    "name": "P",
                    "enabled": True,
                    "url": "http://p",
                    "auth": {},
                    "libraries": [
                        {
                            "id": "1",
                            "name": "Movies",
                            "remote_paths": ["/data/movies"],
                            "enabled": True,
                        }
                    ],
                }
            ]
        )
        response = client.get(
            "/api/servers/owners",
            headers=auth_headers,
            query_string={"path": "/elsewhere/Foo.mkv"},
        )
        assert response.status_code == 200
        assert response.get_json()["owners"] == []

    def test_400_when_path_missing(self, client, auth_headers):
        response = client.get("/api/servers/owners", headers=auth_headers)
        assert response.status_code == 400


class TestRefreshLibraries:
    def test_404_when_server_missing(self, client, auth_headers):
        _seed_media_servers([])
        response = client.post(
            "/api/servers/nope/refresh-libraries",
            headers=auth_headers,
        )
        assert response.status_code == 404

    def test_jellyfin_refresh_succeeds(self, client, auth_headers, monkeypatch):
        # Jellyfin now ships a real client; the refresh route accepts it.
        from plex_generate_previews.servers import Library
        from plex_generate_previews.servers.jellyfin import JellyfinServer

        _seed_media_servers(
            [
                {
                    "id": "jelly-1",
                    "type": "jellyfin",
                    "name": "Jellyfin",
                    "enabled": True,
                    "url": "http://jellyfin:8096",
                    "auth": {"method": "api_key", "api_key": "k"},
                }
            ]
        )

        def fake_list_libraries(self):
            return [Library(id="1", name="Movies", remote_paths=("/jf/movies",), enabled=True)]

        monkeypatch.setattr(JellyfinServer, "list_libraries", fake_list_libraries)

        response = client.post(
            "/api/servers/jelly-1/refresh-libraries",
            headers=auth_headers,
        )
        assert response.status_code == 200
        assert response.get_json()["count"] == 1

    def test_persists_libraries_and_preserves_enabled_toggle(self, client, auth_headers, monkeypatch):
        from plex_generate_previews.servers import Library

        _seed_media_servers(
            [
                {
                    "id": "plex-default",
                    "type": "plex",
                    "name": "Plex",
                    "enabled": True,
                    "url": "http://plex:32400",
                    "auth": {"token": "t"},
                    "libraries": [
                        # User had previously disabled Movies; refresh must
                        # preserve that.
                        {
                            "id": "1",
                            "name": "Movies",
                            "remote_paths": ["/old"],
                            "enabled": False,
                        }
                    ],
                }
            ]
        )

        # Avoid touching real plexapi; stub list_libraries on the live client.
        from plex_generate_previews.servers.plex import PlexServer

        def fake_list_libraries(self):
            return [
                Library(id="1", name="Movies", remote_paths=("/data/movies",), enabled=True),
                Library(id="2", name="TV Shows", remote_paths=("/data/tv",), enabled=True),
            ]

        monkeypatch.setattr(PlexServer, "list_libraries", fake_list_libraries)

        response = client.post(
            "/api/servers/plex-default/refresh-libraries",
            headers=auth_headers,
        )
        assert response.status_code == 200

        data = response.get_json()
        assert data["count"] == 2
        libs_by_id = {lib["id"]: lib for lib in data["libraries"]}
        # Existing user toggle preserved.
        assert libs_by_id["1"]["enabled"] is False
        # New library defaults to whatever the server reported (here True).
        assert libs_by_id["2"]["enabled"] is True
        # remote_paths were updated.
        assert libs_by_id["1"]["remote_paths"] == ["/data/movies"]

        # Settings snapshot persisted.
        settings = get_settings_manager()
        persisted = settings.get("media_servers")[0]["libraries"]
        assert {lib["id"] for lib in persisted} == {"1", "2"}

    def test_502_when_server_unreachable(self, client, auth_headers, monkeypatch):
        _seed_media_servers(
            [
                {
                    "id": "plex-default",
                    "type": "plex",
                    "name": "Plex",
                    "enabled": True,
                    "url": "http://plex:32400",
                    "auth": {"token": "t"},
                }
            ]
        )

        from plex_generate_previews.servers.plex import PlexServer

        def boom(self):
            raise RuntimeError("network down")

        monkeypatch.setattr(PlexServer, "list_libraries", boom)

        response = client.post(
            "/api/servers/plex-default/refresh-libraries",
            headers=auth_headers,
        )
        assert response.status_code == 502


# ---------------------------------------------------------------------------
# CRUD endpoints
# ---------------------------------------------------------------------------


class TestCreateServer:
    def test_creates_emby_server_and_assigns_id(self, client, auth_headers):
        response = client.post(
            "/api/servers",
            headers=auth_headers,
            json={
                "type": "emby",
                "name": "Test Emby",
                "url": "http://emby:8096",
                "auth": {"method": "api_key", "api_key": "secret"},
            },
        )
        assert response.status_code == 201
        body = response.get_json()
        assert body["type"] == "emby"
        assert body["name"] == "Test Emby"
        assert body["id"]  # generated server-side
        # Returned auth is redacted.
        assert body["auth"]["api_key"] == "***REDACTED***"

        # Persisted to settings.
        servers = get_settings_manager().get("media_servers")
        assert any(s["id"] == body["id"] for s in servers)

    def test_400_when_type_missing(self, client, auth_headers):
        response = client.post(
            "/api/servers",
            headers=auth_headers,
            json={"name": "X", "url": "http://x"},
        )
        assert response.status_code == 400

    def test_400_when_unknown_type(self, client, auth_headers):
        response = client.post(
            "/api/servers",
            headers=auth_headers,
            json={"type": "kodi", "name": "K", "url": "http://k"},
        )
        assert response.status_code == 400

    def test_400_when_name_missing(self, client, auth_headers):
        response = client.post(
            "/api/servers",
            headers=auth_headers,
            json={"type": "emby", "url": "http://emby"},
        )
        assert response.status_code == 400

    def test_400_when_url_missing(self, client, auth_headers):
        response = client.post(
            "/api/servers",
            headers=auth_headers,
            json={"type": "emby", "name": "Emby"},
        )
        assert response.status_code == 400

    def test_409_when_id_collides(self, client, auth_headers):
        _seed_media_servers(
            [
                {
                    "id": "fixed-id",
                    "type": "emby",
                    "name": "Existing",
                    "enabled": True,
                    "url": "http://emby",
                    "auth": {},
                }
            ]
        )
        response = client.post(
            "/api/servers",
            headers=auth_headers,
            json={
                "id": "fixed-id",
                "type": "jellyfin",
                "name": "New",
                "url": "http://jelly",
            },
        )
        assert response.status_code == 409


class TestUpdateServer:
    def test_renames_server(self, client, auth_headers):
        _seed_media_servers(
            [
                {
                    "id": "s1",
                    "type": "emby",
                    "name": "Old Name",
                    "enabled": True,
                    "url": "http://emby",
                    "auth": {"api_key": "k"},
                }
            ]
        )
        response = client.put(
            "/api/servers/s1",
            headers=auth_headers,
            json={"name": "New Name"},
        )
        assert response.status_code == 200
        assert response.get_json()["name"] == "New Name"
        # Other fields untouched.
        servers = get_settings_manager().get("media_servers")
        assert servers[0]["url"] == "http://emby"
        assert servers[0]["auth"]["api_key"] == "k"

    def test_redacted_auth_does_not_clobber_secret(self, client, auth_headers):
        _seed_media_servers(
            [
                {
                    "id": "s1",
                    "type": "emby",
                    "name": "Emby",
                    "enabled": True,
                    "url": "http://emby",
                    "auth": {"api_key": "real-secret"},
                }
            ]
        )
        # Client GETs the redacted form, edits 'name', sends it all back.
        response = client.put(
            "/api/servers/s1",
            headers=auth_headers,
            json={
                "name": "Renamed",
                "auth": {"api_key": "***REDACTED***"},
            },
        )
        assert response.status_code == 200
        # Real secret preserved despite the redacted echo.
        servers = get_settings_manager().get("media_servers")
        assert servers[0]["auth"]["api_key"] == "real-secret"

    def test_id_field_immutable(self, client, auth_headers):
        _seed_media_servers(
            [
                {
                    "id": "s1",
                    "type": "emby",
                    "name": "Emby",
                    "enabled": True,
                    "url": "http://emby",
                    "auth": {},
                }
            ]
        )
        response = client.put(
            "/api/servers/s1",
            headers=auth_headers,
            json={"id": "hacked-id", "name": "X"},
        )
        assert response.status_code == 200
        servers = get_settings_manager().get("media_servers")
        assert servers[0]["id"] == "s1"  # unchanged

    def test_404_when_unknown_id(self, client, auth_headers):
        response = client.put(
            "/api/servers/missing",
            headers=auth_headers,
            json={"name": "X"},
        )
        assert response.status_code == 404


class TestDeleteServer:
    def test_removes_server(self, client, auth_headers):
        _seed_media_servers(
            [
                {
                    "id": "s1",
                    "type": "emby",
                    "name": "Emby",
                    "enabled": True,
                    "url": "http://emby",
                    "auth": {},
                },
                {
                    "id": "s2",
                    "type": "jellyfin",
                    "name": "Jellyfin",
                    "enabled": True,
                    "url": "http://jellyfin",
                    "auth": {},
                },
            ]
        )
        response = client.delete("/api/servers/s1", headers=auth_headers)
        assert response.status_code == 200
        assert response.get_json()["deleted"] == "s1"
        servers = get_settings_manager().get("media_servers")
        assert {s["id"] for s in servers} == {"s2"}

    def test_404_when_unknown(self, client, auth_headers):
        response = client.delete("/api/servers/nope", headers=auth_headers)
        assert response.status_code == 404


class TestTestConnection:
    def test_emby_test_connection_succeeds(self, client, auth_headers, monkeypatch):
        from plex_generate_previews.servers import ConnectionResult
        from plex_generate_previews.servers.emby import EmbyServer

        def fake_test(self):
            return ConnectionResult(
                ok=True,
                server_id="abc",
                server_name="Test Emby",
                version="4.9",
                message="OK",
            )

        monkeypatch.setattr(EmbyServer, "test_connection", fake_test)
        response = client.post(
            "/api/servers/test-connection",
            headers=auth_headers,
            json={
                "type": "emby",
                "name": "Test",
                "url": "http://emby:8096",
                "auth": {"method": "api_key", "api_key": "k"},
            },
        )
        assert response.status_code == 200
        body = response.get_json()
        assert body["ok"] is True
        assert body["server_id"] == "abc"
        assert body["server_name"] == "Test Emby"

    def test_jellyfin_test_connection_failure_reported(self, client, auth_headers, monkeypatch):
        from plex_generate_previews.servers import ConnectionResult
        from plex_generate_previews.servers.jellyfin import JellyfinServer

        def fake_test(self):
            return ConnectionResult(ok=False, message="connection refused")

        monkeypatch.setattr(JellyfinServer, "test_connection", fake_test)
        response = client.post(
            "/api/servers/test-connection",
            headers=auth_headers,
            json={
                "type": "jellyfin",
                "name": "JF",
                "url": "http://jellyfin:8096",
                "auth": {"method": "api_key", "api_key": "k"},
            },
        )
        assert response.status_code == 200
        body = response.get_json()
        assert body["ok"] is False
        assert "refused" in body["message"]

    def test_test_connection_does_not_persist(self, client, auth_headers, monkeypatch):
        from plex_generate_previews.servers import ConnectionResult
        from plex_generate_previews.servers.emby import EmbyServer

        monkeypatch.setattr(
            EmbyServer,
            "test_connection",
            lambda self: ConnectionResult(ok=True, message="OK"),
        )
        response = client.post(
            "/api/servers/test-connection",
            headers=auth_headers,
            json={
                "type": "emby",
                "name": "Test",
                "url": "http://emby:8096",
                "auth": {"method": "api_key", "api_key": "k"},
            },
        )
        assert response.status_code == 200
        # Nothing persisted.
        assert get_settings_manager().get("media_servers") in (None, [])

    def test_invalid_payload_returns_400(self, client, auth_headers):
        response = client.post(
            "/api/servers/test-connection",
            headers=auth_headers,
            json={"type": "emby"},  # no name / url
        )
        assert response.status_code == 400
        assert response.get_json()["ok"] is False
