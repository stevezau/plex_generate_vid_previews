"""Tests for the read-only multi-server API (``/api/servers``)."""

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
def auth_headers():
    from media_preview_generator.web.auth import get_auth_token

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
        from media_preview_generator.servers import Library
        from media_preview_generator.servers.jellyfin import JellyfinServer

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
        from media_preview_generator.servers import Library

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
        from media_preview_generator.servers.plex import PlexServer

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

        from media_preview_generator.servers.plex import PlexServer

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

    def test_plex_multi_add_persists_server_identity_from_discovery(self, client, auth_headers, tmp_path):
        """Plex multi-server auto-discovery: each server posts its
        ``server_identity`` (plex.tv ``clientIdentifier``) directly,
        without a separate connection-test probe. Verifies the
        identity round-trips into media_servers so the webhook router
        can disambiguate later."""
        # plex_config_folder validation requires the directory to exist on
        # disk — the dev box has /config/plex but CI doesn't, so use a
        # tmp_path-backed dir that resolves the same way in both envs.
        plex_config = tmp_path / "plex_config"
        plex_config.mkdir()
        plex_token = "shared-oauth-token"
        for ms in [
            {
                "name": "Plex Home",
                "url": "http://192.168.1.5:32400",
                "machine_id": "machine-A",
            },
            {
                "name": "Plex Office",
                "url": "https://office.example.com",
                "machine_id": "machine-B",
            },
        ]:
            response = client.post(
                "/api/servers",
                headers=auth_headers,
                json={
                    "type": "plex",
                    "name": ms["name"],
                    "url": ms["url"],
                    "auth": {"method": "token", "token": plex_token},
                    "server_identity": ms["machine_id"],
                    "output": {
                        "adapter": "plex_bundle",
                        "plex_config_folder": str(plex_config),
                        "frame_interval": 10,
                    },
                },
            )
            assert response.status_code == 201, response.get_data(as_text=True)

        # Both persisted with distinct identities.
        servers = [s for s in get_settings_manager().get("media_servers") if s["type"] == "plex"]
        identities = {s["server_identity"] for s in servers}
        assert "machine-A" in identities
        assert "machine-B" in identities
        # Both share the same auth token (single OAuth → multiple servers).
        tokens = {s["auth"]["token"] for s in servers}
        assert tokens == {plex_token}

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

    def test_exclude_paths_round_trip(self, client, auth_headers, tmp_path):
        """PUT exclude_paths persists, GET surfaces them, second PUT preserves
        when omitted from the payload."""
        local_dir = tmp_path / "media"
        local_dir.mkdir()
        _seed_media_servers(
            [
                {
                    "id": "s1",
                    "type": "emby",
                    "name": "Emby",
                    "enabled": True,
                    "url": "http://emby",
                    "auth": {"api_key": "k"},
                    "exclude_paths": [],
                }
            ]
        )

        # First PUT — set two rules, one path-prefix and one regex.
        rules = [
            {"value": str(local_dir), "type": "path"},
            {"value": r".*\.iso$", "type": "regex"},
        ]
        response = client.put(
            "/api/servers/s1",
            headers=auth_headers,
            json={"exclude_paths": rules},
        )
        assert response.status_code == 200
        servers = get_settings_manager().get("media_servers")
        assert servers[0]["exclude_paths"] == rules

        # GET returns the same rules.
        response = client.get("/api/servers/s1", headers=auth_headers)
        assert response.status_code == 200
        assert response.get_json()["exclude_paths"] == rules

        # Second PUT without exclude_paths must preserve them, not wipe.
        response = client.put(
            "/api/servers/s1",
            headers=auth_headers,
            json={"name": "Renamed Emby"},
        )
        assert response.status_code == 200
        servers = get_settings_manager().get("media_servers")
        assert servers[0]["exclude_paths"] == rules

    def test_400_when_exclude_paths_regex_invalid(self, client, auth_headers):
        _seed_media_servers(
            [
                {
                    "id": "s1",
                    "type": "emby",
                    "name": "Emby",
                    "enabled": True,
                    "url": "http://emby",
                    "auth": {"api_key": "k"},
                }
            ]
        )
        response = client.put(
            "/api/servers/s1",
            headers=auth_headers,
            json={"exclude_paths": [{"value": "[unclosed", "type": "regex"}]},
        )
        assert response.status_code == 400
        body = response.get_json()
        assert "regex" in body["error"].lower()

    def test_400_when_path_mapping_local_prefix_missing(self, client, auth_headers, tmp_path):
        _seed_media_servers(
            [
                {
                    "id": "s1",
                    "type": "emby",
                    "name": "Emby",
                    "enabled": True,
                    "url": "http://emby",
                    "auth": {"api_key": "k"},
                }
            ]
        )
        nonexistent = tmp_path / "nope-not-here"
        response = client.put(
            "/api/servers/s1",
            headers=auth_headers,
            json={
                "path_mappings": [{"plex_prefix": "/data", "local_prefix": str(nonexistent), "webhook_prefixes": []}]
            },
        )
        assert response.status_code == 400
        assert "does not exist" in response.get_json()["error"]

    def test_put_accepts_modern_remote_prefix_path_mapping(self, client, auth_headers, tmp_path):
        """Persisted path_mappings use ``remote_prefix`` (modern multi-vendor key).

        Without this support, any PUT/PATCH to a server saved with the
        modern key would 400 even when the body is unchanged — since
        the validator re-runs against the existing path_mappings.
        Repro of the bug: GET → PUT round trip on a server with
        ``remote_prefix``-shaped mappings.
        """
        local_dir = tmp_path / "media"
        local_dir.mkdir()
        _seed_media_servers(
            [
                {
                    "id": "e1",
                    "type": "emby",
                    "name": "Emby",
                    "enabled": True,
                    "url": "http://emby",
                    "auth": {"api_key": "k"},
                    "path_mappings": [{"remote_prefix": "/em-media", "local_prefix": str(local_dir)}],
                }
            ]
        )

        # Touching only a non-mapping field used to fail because the
        # existing remote_prefix mappings would re-validate as missing.
        response = client.put(
            "/api/servers/e1",
            headers=auth_headers,
            json={"name": "Emby Renamed"},
        )
        assert response.status_code == 200, response.get_json()
        assert response.get_json()["name"] == "Emby Renamed"

        # Sending an explicit remote_prefix payload also succeeds.
        response = client.put(
            "/api/servers/e1",
            headers=auth_headers,
            json={"path_mappings": [{"remote_prefix": "/em-media2", "local_prefix": str(local_dir)}]},
        )
        assert response.status_code == 200, response.get_json()

    def test_400_when_plex_config_folder_missing(self, client, auth_headers, tmp_path):
        _seed_media_servers(
            [
                {
                    "id": "p1",
                    "type": "plex",
                    "name": "Plex",
                    "enabled": True,
                    "url": "http://plex:32400",
                    "auth": {"method": "token", "token": "t"},
                    "output": {"adapter": "plex_bundle", "plex_config_folder": "/plex"},
                }
            ]
        )
        bogus = tmp_path / "nope-not-here-either"
        response = client.put(
            "/api/servers/p1",
            headers=auth_headers,
            json={"output": {"adapter": "plex_bundle", "plex_config_folder": str(bogus)}},
        )
        assert response.status_code == 400
        assert "does not exist" in response.get_json()["error"]


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
        from media_preview_generator.servers import ConnectionResult
        from media_preview_generator.servers.emby import EmbyServer

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
        from media_preview_generator.servers import ConnectionResult
        from media_preview_generator.servers.jellyfin import JellyfinServer

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
        from media_preview_generator.servers import ConnectionResult
        from media_preview_generator.servers.emby import EmbyServer

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


class TestOutputStatus:
    def _seed_emby(self, *, libraries=None):
        _seed_media_servers(
            [
                {
                    "id": "emby-1",
                    "type": "emby",
                    "name": "Emby",
                    "enabled": True,
                    "url": "http://emby:8096",
                    "auth": {"method": "api_key", "api_key": "k"},
                    "libraries": libraries or [],
                    "output": {"adapter": "emby_sidecar", "width": 320, "frame_interval": 10},
                }
            ]
        )

    def test_emby_reports_missing_when_sidecar_absent(self, client, auth_headers):
        self._seed_emby()
        response = client.get(
            "/api/servers/emby-1/output-status",
            headers=auth_headers,
            query_string={"path": "/tmp/nonexistent/Foo.mkv"},
        )
        assert response.status_code == 200
        data = response.get_json()
        assert data["server_type"] == "emby"
        assert data["adapter"] == "emby_sidecar"
        assert data["exists"] is False
        # The expected sidecar path is reported even though the file is missing.
        assert any(p.endswith("Foo-320-10.bif") for p in data["paths"])
        assert any(p.endswith("Foo-320-10.bif") for p in data["missing_paths"])

    def test_emby_reports_exists_when_sidecar_present(self, client, auth_headers, tmp_path):
        media_dir = tmp_path / "Movies"
        media_dir.mkdir()
        media_file = media_dir / "Test.mkv"
        media_file.write_bytes(b"")
        # Pre-create the sidecar Emby would write.
        sidecar = media_dir / "Test-320-10.bif"
        sidecar.write_bytes(b"\x89BIF\x0d\x0a\x1a\x0a")

        self._seed_emby()
        response = client.get(
            "/api/servers/emby-1/output-status",
            headers=auth_headers,
            query_string={"path": str(media_file)},
        )
        data = response.get_json()
        assert data["exists"] is True
        assert data["missing_paths"] == []

    def test_plex_requires_item_id(self, client, auth_headers):
        _seed_media_servers(
            [
                {
                    "id": "plex-1",
                    "type": "plex",
                    "name": "Plex",
                    "enabled": True,
                    "url": "http://plex:32400",
                    "auth": {"token": "t"},
                    "output": {"adapter": "plex_bundle", "plex_config_folder": "/cfg"},
                }
            ]
        )
        response = client.get(
            "/api/servers/plex-1/output-status",
            headers=auth_headers,
            query_string={"path": "/m/foo.mkv"},
        )
        data = response.get_json()
        assert data["needs_item_id"] is True
        assert data["exists"] is False

    def test_jellyfin_reports_missing_sheets_dir(self, client, auth_headers, tmp_path):
        # Manifest exists but the tile-sheets directory doesn't yet —
        # exists must report False because the format requires both.
        media_dir = tmp_path / "Show" / "S01"
        media_dir.mkdir(parents=True)
        media_file = media_dir / "S01E01.mkv"
        media_file.write_bytes(b"")
        trickplay_dir = media_dir / "trickplay"
        trickplay_dir.mkdir()
        manifest = trickplay_dir / "S01E01-320.json"
        manifest.write_text("{}")
        # NOTE: matching tiles dir trickplay/S01E01-320/ deliberately omitted.

        _seed_media_servers(
            [
                {
                    "id": "jelly-1",
                    "type": "jellyfin",
                    "name": "Jelly",
                    "enabled": True,
                    "url": "http://jellyfin:8096",
                    "auth": {"method": "api_key", "api_key": "k"},
                    "output": {"adapter": "jellyfin_trickplay", "width": 320, "frame_interval": 10},
                }
            ]
        )

        response = client.get(
            "/api/servers/jelly-1/output-status",
            headers=auth_headers,
            query_string={"path": str(media_file), "item_id": "x"},
        )
        data = response.get_json()
        assert data["server_type"] == "jellyfin"
        # Manifest was present, but sheets dir wasn't — overall NOT exists.
        assert data["exists"] is False
        assert any("S01E01-320" in p for p in data["missing_paths"])

    def test_404_when_server_missing(self, client, auth_headers):
        _seed_media_servers([])
        response = client.get(
            "/api/servers/missing/output-status",
            headers=auth_headers,
            query_string={"path": "/x"},
        )
        assert response.status_code == 404

    def test_400_when_path_missing(self, client, auth_headers):
        self._seed_emby()
        response = client.get(
            "/api/servers/emby-1/output-status",
            headers=auth_headers,
        )
        assert response.status_code == 400
