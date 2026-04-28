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

    def test_501_for_jellyfin_until_phase_3(self, client, auth_headers):
        _seed_media_servers(
            [
                {
                    "id": "jelly-1",
                    "type": "jellyfin",
                    "name": "Jellyfin",
                    "enabled": True,
                    "url": "http://jellyfin:8096",
                    "auth": {},
                }
            ]
        )
        response = client.post(
            "/api/servers/jelly-1/refresh-libraries",
            headers=auth_headers,
        )
        assert response.status_code == 501

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
