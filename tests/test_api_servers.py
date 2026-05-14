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
        # Audit fix — original asserted only the status code. A 409 with empty
        # body would have passed even if the route accidentally swallowed the
        # collision detail. Also assert (a) the body identifies the conflict
        # AND (b) the existing server wasn't mutated by the failed write.
        assert response.status_code == 409
        body = response.get_json() or {}
        assert "error" in body or "message" in body, (
            f"409 must surface a useful error message, not an empty body: {body!r}"
        )
        # Existing emby server must not have been overwritten by the failed write.
        existing = next(s for s in get_settings_manager().get("media_servers") if s["id"] == "fixed-id")
        assert existing["type"] == "emby", (
            f"failed POST silently mutated the existing server's type to {existing['type']!r}"
        )
        assert existing["name"] == "Existing"
        assert existing["url"] == "http://emby"


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
        # Sheet directory exists but contains no .jpg tiles yet —
        # exists must report False (D38: the layout's "fresh" signal is
        # the presence of at least one tile under the per-resolution
        # sub-dir, not the presence of the directory shell).
        media_dir = tmp_path / "Show" / "S01"
        media_dir.mkdir(parents=True)
        media_file = media_dir / "S01E01.mkv"
        media_file.write_bytes(b"")
        sheet_dir = media_dir / "S01E01.trickplay" / "320 - 10x10"
        sheet_dir.mkdir(parents=True)
        # NOTE: 0.jpg deliberately omitted.

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
        # Sheet dir exists but contains no .jpg tiles — overall NOT exists.
        assert data["exists"] is False
        assert any("S01E01.trickplay" in p for p in data["missing_paths"])

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


class TestUninstallPlugin:
    """POST /uninstall-plugin — mirrors /install-plugin for the reverse flow."""

    def _seed_jelly(self):
        _seed_media_servers(
            [
                {
                    "id": "jelly-1",
                    "type": "jellyfin",
                    "name": "Jelly",
                    "enabled": True,
                    "url": "http://jellyfin:8096",
                    "auth": {"method": "api_key", "api_key": "k"},
                }
            ]
        )

    def test_non_jellyfin_rejected(self, client, auth_headers):
        """Uninstall only works on Jellyfin. Plex/Emby must 400."""
        _seed_media_servers(
            [
                {
                    "id": "emby-1",
                    "type": "emby",
                    "name": "Emby",
                    "enabled": True,
                    "url": "http://emby:8096",
                    "auth": {"method": "api_key", "api_key": "k"},
                }
            ]
        )
        response = client.post(
            "/api/servers/emby-1/uninstall-plugin",
            headers=auth_headers,
        )
        assert response.status_code == 400
        body = response.get_json()
        assert body["ok"] is False
        assert "jellyfin" in body["error"].lower()

    def test_server_not_found_returns_404(self, client, auth_headers):
        response = client.post(
            "/api/servers/missing/uninstall-plugin",
            headers=auth_headers,
        )
        assert response.status_code == 404

    def test_forwards_result_to_caller(self, client, auth_headers, monkeypatch):
        """The route must return what uninstall_plugin returned verbatim
        so the UI's step-list progress component works."""
        from media_preview_generator.servers.jellyfin import JellyfinServer

        self._seed_jelly()
        monkeypatch.setattr(
            JellyfinServer,
            "uninstall_plugin",
            lambda self: {
                "ok": True,
                "steps": [
                    {"step": "uninstall_package", "ok": True, "detail": "removed"},
                    {"step": "restart", "ok": True, "detail": "restart requested"},
                ],
                "error": "",
            },
        )
        response = client.post(
            "/api/servers/jelly-1/uninstall-plugin",
            headers=auth_headers,
        )
        assert response.status_code == 200
        body = response.get_json()
        assert body["ok"] is True
        assert len(body["steps"]) == 2
        assert body["steps"][0]["step"] == "uninstall_package"


class TestHealthCheckApplySetSchema:
    """/health-check/apply accepts both legacy 'flags' and new 'set' body shapes."""

    def _seed_jelly(self):
        _seed_media_servers(
            [
                {
                    "id": "jelly-1",
                    "type": "jellyfin",
                    "name": "Jelly",
                    "enabled": True,
                    "url": "http://jellyfin:8096",
                    "auth": {"method": "api_key", "api_key": "k"},
                }
            ]
        )

    def test_set_schema_forwards_explicit_values(self, client, auth_headers, monkeypatch):
        """The 'set' body must delegate to apply_flag_values with the
        parsed targets VERBATIM — asserting kwargs, not just call count,
        so a future bug that strips/rewrites targets would break the test.

        Per .claude/rules/testing.md: "if removing a parameter from the
        SUT wouldn't break the test, the test isn't covering that
        parameter." This test asserts the exact targets list."""
        from media_preview_generator.servers.jellyfin import JellyfinServer

        self._seed_jelly()
        captured = {}

        def fake_apply_flag_values(self, targets):
            captured["targets"] = targets
            return {"m:EnableRealtimeMonitor": "ok"}

        monkeypatch.setattr(JellyfinServer, "apply_flag_values", fake_apply_flag_values)
        # Silence the plugin-cache warm probe.
        monkeypatch.setattr(JellyfinServer, "check_plugin_installed", lambda self: {"installed": True})

        response = client.post(
            "/api/servers/jelly-1/health-check/apply",
            headers=auth_headers,
            json={"set": [{"flag": "EnableRealtimeMonitor", "value": False, "library_ids": ["m"]}]},
        )
        assert response.status_code == 200
        assert response.get_json()["ok"] is True

        # The SUT's contract: targets forwarded unchanged.
        assert captured["targets"] == [{"flag": "EnableRealtimeMonitor", "value": False, "library_ids": ["m"]}]

    def test_destructive_disable_without_phrase_is_rejected_400(self, client, auth_headers, monkeypatch):
        """CRITICAL guardrail: EnableTrickplayImageExtraction=false deletes
        published .trickplay/ tiles on Jellyfin's next refresh. The UI
        modal requires typing 'disable trickplay' before POSTing — but
        the UI is UX gloss. A curl/bookmarklet/XHR replay that skips
        the modal MUST still carry the phrase or the route 400s.

        This test asserts: POSTing the destructive (flag, value) WITHOUT
        a confirm key gets rejected and apply_flag_values is NOT called.
        Removing the server-side guardrail would let this test pass
        ONLY if we forget to patch apply_flag_values — so we patch it
        with a side_effect=AssertionError to prove the route blocks
        BEFORE the adapter ever sees the request."""
        from media_preview_generator.servers.jellyfin import JellyfinServer

        self._seed_jelly()

        def _should_not_be_called(self, targets):
            raise AssertionError("apply_flag_values was called — destructive guardrail regressed")

        monkeypatch.setattr(JellyfinServer, "apply_flag_values", _should_not_be_called)
        monkeypatch.setattr(JellyfinServer, "check_plugin_installed", lambda self: {"installed": True})

        response = client.post(
            "/api/servers/jelly-1/health-check/apply",
            headers=auth_headers,
            json={
                "set": [{"flag": "EnableTrickplayImageExtraction", "value": False, "library_ids": ["m"]}],
                # NO confirm key — must 400.
            },
        )
        assert response.status_code == 400, response.get_json()
        body = response.get_json()
        assert "destructive" in body["error"].lower() or "typed confirmation" in body["error"].lower()

    def test_destructive_disable_with_wrong_phrase_is_rejected(self, client, auth_headers, monkeypatch):
        """Wrong phrase in confirm must also 400 — full parity with the
        UI's type-to-confirm: 'disable' ≠ 'disable trickplay'."""
        from media_preview_generator.servers.jellyfin import JellyfinServer

        self._seed_jelly()
        monkeypatch.setattr(
            JellyfinServer,
            "apply_flag_values",
            lambda self, targets: (_ for _ in ()).throw(AssertionError("should not be called")),
        )
        monkeypatch.setattr(JellyfinServer, "check_plugin_installed", lambda self: {"installed": True})

        response = client.post(
            "/api/servers/jelly-1/health-check/apply",
            headers=auth_headers,
            json={
                "set": [{"flag": "EnableTrickplayImageExtraction", "value": False, "library_ids": ["m"]}],
                "confirm": {"EnableTrickplayImageExtraction": "wrong phrase"},
            },
        )
        assert response.status_code == 400

    def test_destructive_disable_with_correct_phrase_proceeds(self, client, auth_headers, monkeypatch):
        """When the phrase matches exactly, the request proceeds to
        apply_flag_values and returns 200. This is the happy path —
        ensures the guardrail hasn't become a blanket reject."""
        from media_preview_generator.servers.jellyfin import JellyfinServer

        self._seed_jelly()
        captured = {}

        def fake_apply(self, targets):
            captured["targets"] = targets
            return {"m:EnableTrickplayImageExtraction": "ok"}

        monkeypatch.setattr(JellyfinServer, "apply_flag_values", fake_apply)
        monkeypatch.setattr(JellyfinServer, "check_plugin_installed", lambda self: {"installed": True})

        response = client.post(
            "/api/servers/jelly-1/health-check/apply",
            headers=auth_headers,
            json={
                "set": [{"flag": "EnableTrickplayImageExtraction", "value": False, "library_ids": ["m"]}],
                "confirm": {"EnableTrickplayImageExtraction": "disable trickplay"},
            },
        )
        assert response.status_code == 200
        assert response.get_json()["ok"] is True
        assert captured["targets"] == [{"flag": "EnableTrickplayImageExtraction", "value": False, "library_ids": ["m"]}]

    def test_non_destructive_flag_does_not_require_confirm(self, client, auth_headers, monkeypatch):
        """Flipping EnableRealtimeMonitor off is non-destructive — the
        guardrail should let it through with no confirm key. Proves
        the guardrail is scoped to destructive flags only."""
        from media_preview_generator.servers.jellyfin import JellyfinServer

        self._seed_jelly()
        captured = {}

        def fake_apply(self, targets):
            captured["targets"] = targets
            return {"m:EnableRealtimeMonitor": "ok"}

        monkeypatch.setattr(JellyfinServer, "apply_flag_values", fake_apply)
        monkeypatch.setattr(JellyfinServer, "check_plugin_installed", lambda self: {"installed": True})

        response = client.post(
            "/api/servers/jelly-1/health-check/apply",
            headers=auth_headers,
            json={"set": [{"flag": "EnableRealtimeMonitor", "value": False, "library_ids": ["m"]}]},
        )
        assert response.status_code == 200

    def test_set_schema_validates_each_row(self, client, auth_headers):
        """Malformed 'set' rows must 400 — empty flag, missing value, bad library_ids."""
        self._seed_jelly()

        # Empty flag.
        resp = client.post(
            "/api/servers/jelly-1/health-check/apply",
            headers=auth_headers,
            json={"set": [{"flag": "", "value": True}]},
        )
        assert resp.status_code == 400

        # Missing value key.
        resp = client.post(
            "/api/servers/jelly-1/health-check/apply",
            headers=auth_headers,
            json={"set": [{"flag": "X"}]},
        )
        assert resp.status_code == 400

        # Bad library_ids type.
        resp = client.post(
            "/api/servers/jelly-1/health-check/apply",
            headers=auth_headers,
            json={"set": [{"flag": "X", "value": True, "library_ids": "not-a-list"}]},
        )
        assert resp.status_code == 400

    def test_legacy_flags_schema_still_works(self, client, auth_headers, monkeypatch):
        """Existing callers that post {'flags': [...]} MUST keep working —
        'flags' delegates to apply_recommended_settings, not apply_flag_values."""
        from media_preview_generator.servers.jellyfin import JellyfinServer

        self._seed_jelly()
        captured = {}

        def fake_apply_recommended(self, flags=None):
            captured["flags"] = flags
            return {"m:EnableRealtimeMonitor": "ok"}

        def fake_apply_flag_values(self, targets):
            raise AssertionError("legacy 'flags' schema must NOT dispatch to apply_flag_values")

        monkeypatch.setattr(JellyfinServer, "apply_recommended_settings", fake_apply_recommended)
        monkeypatch.setattr(JellyfinServer, "apply_flag_values", fake_apply_flag_values)
        monkeypatch.setattr(JellyfinServer, "check_plugin_installed", lambda self: {"installed": True})

        response = client.post(
            "/api/servers/jelly-1/health-check/apply",
            headers=auth_headers,
            json={"flags": ["EnableRealtimeMonitor"]},
        )
        assert response.status_code == 200
        assert captured["flags"] == ["EnableRealtimeMonitor"]

    def test_empty_body_still_fixes_everything(self, client, auth_headers, monkeypatch):
        """Legacy 'fix all' — empty body delegates to apply_recommended_settings(flags=None)."""
        from media_preview_generator.servers.jellyfin import JellyfinServer

        self._seed_jelly()
        captured = {}

        def fake_apply_recommended(self, flags=None):
            captured["flags"] = flags
            return {}

        monkeypatch.setattr(JellyfinServer, "apply_recommended_settings", fake_apply_recommended)
        monkeypatch.setattr(JellyfinServer, "check_plugin_installed", lambda self: {"installed": True})

        response = client.post(
            "/api/servers/jelly-1/health-check/apply",
            headers=auth_headers,
            json={},
        )
        assert response.status_code == 200
        # apply_recommended_settings got flags=None (fix every issue).
        assert captured["flags"] is None


class TestPreviewsReadinessRoute:
    """Unified /previews-readiness must delegate to live.previews_readiness()
    for every vendor and return the envelope as-is."""

    @pytest.mark.parametrize(
        ("vendor_type", "server_cls_path"),
        [
            ("plex", "media_preview_generator.servers.plex.PlexServer"),
            ("emby", "media_preview_generator.servers.emby.EmbyServer"),
            ("jellyfin", "media_preview_generator.servers.jellyfin.JellyfinServer"),
        ],
    )
    def test_shape_matches_across_vendors(self, client, auth_headers, monkeypatch, vendor_type, server_cls_path):
        """Matrix test (per .claude/rules/testing.md):
        every vendor's readiness payload flows through the route unchanged.
        A regression where /previews-readiness silently dropped 'sections'
        or repackaged the envelope would get caught here."""
        import importlib

        module_name, cls_name = server_cls_path.rsplit(".", 1)
        cls = getattr(importlib.import_module(module_name), cls_name)

        auth = {"token": "t"} if vendor_type == "plex" else {"method": "api_key", "api_key": "k"}
        _seed_media_servers(
            [
                {
                    "id": f"{vendor_type}-1",
                    "type": vendor_type,
                    "name": vendor_type.capitalize(),
                    "enabled": True,
                    "url": f"http://{vendor_type}:8096",
                    "auth": auth,
                    "output": {"plex_config_folder": "/cfg"} if vendor_type == "plex" else {},
                }
            ]
        )

        expected = {
            "vendor": vendor_type,
            "overall_ok": True,
            "sections": [
                {
                    "id": "connection",
                    "title": "Connection",
                    "docs_anchor": "connection",
                    "ok": True,
                    "severity": "critical",
                    "checks": [],
                }
            ],
        }
        monkeypatch.setattr(cls, "previews_readiness", lambda self: expected)
        # Silence any vendor-specific cache-warm probes.
        if hasattr(cls, "check_plugin_installed"):
            monkeypatch.setattr(cls, "check_plugin_installed", lambda self: {"installed": False})

        response = client.get(
            f"/api/servers/{vendor_type}-1/previews-readiness",
            headers=auth_headers,
        )
        assert response.status_code == 200, response.get_json()
        body = response.get_json()
        # Route forwards the vendor envelope verbatim.
        assert body == expected

    def test_jellyfin_previews_readiness_self_warms_plugin_cache(self):
        """Contract test: JellyfinServer.previews_readiness() MUST probe
        check_plugin_installed() as its FIRST step so fresh instances
        see the correct plugin state before downstream checks
        (library recommendations) read the cache.

        The route deliberately does NOT warm the cache itself — it
        relies on this self-warm. If this property regresses (e.g.
        someone moves the plugin probe later, or drops it), fresh
        instances default to plugin-absent recommendations. Same bug
        class as D34.

        Verifies at the adapter level (not the route) so the check is
        location-independent: any caller of previews_readiness gets
        the warm for free."""
        from unittest.mock import MagicMock

        from media_preview_generator.servers import ServerConfig, ServerType
        from media_preview_generator.servers.jellyfin import JellyfinServer

        cfg = ServerConfig(
            id="j",
            type=ServerType.JELLYFIN,
            name="J",
            enabled=True,
            url="http://jf:8096",
            auth={"method": "api_key", "api_key": "k"},
        )
        jelly = JellyfinServer(cfg)

        call_order: list[str] = []

        def fake_check_plugin():
            call_order.append("check_plugin_installed")
            return {"installed": True, "version": "10.11.0.2", "error": ""}

        def fake_request(method, url, **kwargs):
            call_order.append(f"{method} {url}")
            if url == "/System/Info":
                return MagicMock(
                    status_code=200, json=MagicMock(return_value={"Version": "10.11.8"}), raise_for_status=MagicMock()
                )
            if url == "/System/Configuration":
                return MagicMock(
                    status_code=200,
                    json=MagicMock(
                        return_value={
                            "TrickplayOptions": {
                                "TileWidth": 10,
                                "TileHeight": 10,
                                "Interval": 10000,
                                "WidthResolutions": [320],
                            }
                        }
                    ),
                    raise_for_status=MagicMock(),
                )
            if url == "/Library/VirtualFolders":
                return MagicMock(status_code=200, json=MagicMock(return_value=[]), raise_for_status=MagicMock())
            raise AssertionError(f"unexpected {method} {url}")

        import unittest.mock

        with (
            unittest.mock.patch.object(jelly, "check_plugin_installed", side_effect=fake_check_plugin),
            unittest.mock.patch.object(JellyfinServer, "_request", side_effect=fake_request),
        ):
            jelly.previews_readiness()

        # The self-warm MUST be the first thing that happens — before
        # any /System/Info, /System/Configuration, or
        # /Library/VirtualFolders probe. Removing it or reordering it
        # would break the cache-consistency property callers depend on.
        assert call_order[0] == "check_plugin_installed", (
            "previews_readiness() must call check_plugin_installed() FIRST to "
            "warm the per-instance cache — fresh instances depend on this or "
            "library recommendations default to plugin-absent (wrong in Mode A). "
            f"Got call order: {call_order[:5]}"
        )


class TestPreviewsReadinessDismissal:
    """Issue #237: per-check dismissal flow.

    Two endpoints (POST dismiss/undismiss) + the GET handler tagging
    dismissed checks. Storage: per-server ``health_dismissals`` list
    inside ``media_servers[*]``.
    """

    def _seed_plex(self, dismissals: list[str] | None = None) -> None:
        entry: dict = {
            "id": "plex-1",
            "type": "plex",
            "name": "Plex",
            "enabled": True,
            "url": "http://plex:32400",
            "auth": {"method": "token", "token": "t"},
        }
        if dismissals is not None:
            entry["health_dismissals"] = list(dismissals)
        _seed_media_servers([entry])

    def test_dismiss_adds_check_id_to_list(self, client, auth_headers):
        self._seed_plex()
        response = client.post(
            "/api/servers/plex-1/previews-readiness/dismiss",
            headers=auth_headers,
            json={"check_id": "library_settings:FSEventLibraryUpdatesEnabled"},
        )
        assert response.status_code == 200, response.get_json()
        body = response.get_json()
        assert body["ok"] is True
        assert body["health_dismissals"] == ["library_settings:FSEventLibraryUpdatesEnabled"]
        # Persisted to settings.json.
        sm = get_settings_manager()
        servers = sm.get("media_servers")
        assert servers[0]["health_dismissals"] == ["library_settings:FSEventLibraryUpdatesEnabled"]

    def test_dismiss_is_idempotent(self, client, auth_headers):
        """Re-dismissing an already-dismissed check is a 200 no-op (no
        duplicate entries). The frontend may double-click; the
        backend must not pile up duplicates."""
        self._seed_plex(dismissals=["library_settings:FSEventLibraryUpdatesEnabled"])
        response = client.post(
            "/api/servers/plex-1/previews-readiness/dismiss",
            headers=auth_headers,
            json={"check_id": "library_settings:FSEventLibraryUpdatesEnabled"},
        )
        assert response.status_code == 200
        body = response.get_json()
        assert body["health_dismissals"] == ["library_settings:FSEventLibraryUpdatesEnabled"]

    def test_undismiss_removes_check_id(self, client, auth_headers):
        self._seed_plex(
            dismissals=[
                "library_settings:FSEventLibraryUpdatesEnabled",
                "vendor_extraction:5",
            ]
        )
        response = client.post(
            "/api/servers/plex-1/previews-readiness/undismiss",
            headers=auth_headers,
            json={"check_id": "library_settings:FSEventLibraryUpdatesEnabled"},
        )
        assert response.status_code == 200
        body = response.get_json()
        assert body["health_dismissals"] == ["vendor_extraction:5"]

    def test_undismiss_of_absent_is_noop(self, client, auth_headers):
        """Undismissing a check that wasn't dismissed is a 200 no-op."""
        self._seed_plex(dismissals=["library_settings:FSEventLibraryUpdatesEnabled"])
        response = client.post(
            "/api/servers/plex-1/previews-readiness/undismiss",
            headers=auth_headers,
            json={"check_id": "never_dismissed"},
        )
        assert response.status_code == 200
        body = response.get_json()
        assert body["health_dismissals"] == ["library_settings:FSEventLibraryUpdatesEnabled"]

    def test_dismiss_missing_check_id_returns_400(self, client, auth_headers):
        self._seed_plex()
        response = client.post(
            "/api/servers/plex-1/previews-readiness/dismiss",
            headers=auth_headers,
            json={},
        )
        assert response.status_code == 400
        assert response.get_json()["ok"] is False

    def test_dismiss_unknown_server_returns_404(self, client, auth_headers):
        self._seed_plex()
        response = client.post(
            "/api/servers/nope/previews-readiness/dismiss",
            headers=auth_headers,
            json={"check_id": "anything"},
        )
        assert response.status_code == 404

    def test_get_handler_tags_dismissed_checks(self, client, auth_headers, monkeypatch):
        """GET /previews-readiness must mark checks in the server's
        health_dismissals list with ``dismissed: true``. Raw audit
        state (ok, severity) is preserved — bucketing happens on the
        frontend.

        Matrix coverage: one dismissed check, one not dismissed,
        same response. Asserts the tag is applied to the right row
        and NOT to others.
        """
        from media_preview_generator.servers.plex import PlexServer

        self._seed_plex(dismissals=["library_settings:FSEventLibraryUpdatesEnabled"])
        payload = {
            "vendor": "plex",
            "overall_ok": True,
            "sections": [
                {
                    "id": "library_settings",
                    "title": "Library settings",
                    "docs_anchor": "library-settings",
                    "ok": False,
                    "severity": "recommended",
                    "checks": [
                        {
                            "id": "library_settings:FSEventLibraryUpdatesEnabled",
                            "label": "Scan my library automatically",
                            "ok": False,
                            "severity": "recommended",
                        },
                        {
                            "id": "library_settings:FSEventLibraryPartialScanEnabled",
                            "label": "Run a partial scan when changes are detected",
                            "ok": False,
                            "severity": "recommended",
                        },
                    ],
                },
            ],
        }
        monkeypatch.setattr(PlexServer, "previews_readiness", lambda self: payload)

        response = client.get(
            "/api/servers/plex-1/previews-readiness",
            headers=auth_headers,
        )
        assert response.status_code == 200
        body = response.get_json()
        checks = body["sections"][0]["checks"]
        # Dismissed check is tagged.
        dismissed_check = next(c for c in checks if c["id"] == "library_settings:FSEventLibraryUpdatesEnabled")
        assert dismissed_check.get("dismissed") is True
        # Raw audit state preserved — dismissal doesn't mutate ok/severity.
        assert dismissed_check["ok"] is False
        assert dismissed_check["severity"] == "recommended"
        # Other check is NOT tagged.
        other_check = next(c for c in checks if c["id"] == "library_settings:FSEventLibraryPartialScanEnabled")
        assert "dismissed" not in other_check or other_check["dismissed"] is False

    def test_get_handler_no_dismissals_returns_payload_unchanged(self, client, auth_headers, monkeypatch):
        """When health_dismissals is absent/empty, the GET handler
        must NOT add ``dismissed: false`` markers everywhere — the
        payload should pass through verbatim. This keeps frontend
        compatibility with older payloads / unaware vendors."""
        from media_preview_generator.servers.plex import PlexServer

        self._seed_plex()  # no health_dismissals field at all
        payload = {
            "vendor": "plex",
            "overall_ok": True,
            "sections": [
                {
                    "id": "library_settings",
                    "title": "Library settings",
                    "docs_anchor": "library-settings",
                    "ok": True,
                    "severity": "info",
                    "checks": [{"id": "x", "label": "X", "ok": True, "severity": "info"}],
                },
            ],
        }
        monkeypatch.setattr(PlexServer, "previews_readiness", lambda self: payload)

        response = client.get(
            "/api/servers/plex-1/previews-readiness",
            headers=auth_headers,
        )
        assert response.status_code == 200
        check = response.get_json()["sections"][0]["checks"][0]
        assert "dismissed" not in check
