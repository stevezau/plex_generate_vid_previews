"""Tests for the Emby server client."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
import requests

from media_preview_generator.servers import (
    ConnectionResult,
    EmbyServer,
    Library,
    MediaItem,
    ServerConfig,
    ServerType,
    WebhookEvent,
)

_DEFAULT_AUTH = {"method": "api_key", "api_key": "k"}
_SENTINEL = object()


def _emby_config(
    *,
    server_id: str = "emby-1",
    name: str = "Test Emby",
    auth=_SENTINEL,
    libraries: list[Library] | None = None,
    url: str = "http://emby:8096",
) -> ServerConfig:
    """Build a ServerConfig with a sensible default auth dict.

    ``auth=_SENTINEL`` means "use the populated default"; passing an
    explicit ``{}`` is honoured so tests can assert behaviour with
    no credentials.
    """
    if auth is _SENTINEL:
        auth = dict(_DEFAULT_AUTH)
    return ServerConfig(
        id=server_id,
        type=ServerType.EMBY,
        name=name,
        enabled=True,
        url=url,
        auth=auth,
        libraries=libraries or [],
    )


@pytest.fixture
def emby():
    return EmbyServer(_emby_config())


class TestConstruction:
    def test_implements_media_server(self, emby):
        from media_preview_generator.servers import MediaServer

        assert isinstance(emby, MediaServer)

    def test_type_is_emby(self, emby):
        assert emby.type is ServerType.EMBY

    def test_id_and_name_propagate(self, emby):
        assert emby.id == "emby-1"
        assert emby.name == "Test Emby"


class TestTokenExtraction:
    def test_api_key(self):
        s = EmbyServer(_emby_config(auth={"method": "api_key", "api_key": "abc"}))
        assert s._token() == "abc"

    def test_access_token_from_password_flow(self):
        s = EmbyServer(_emby_config(auth={"method": "password", "access_token": "tok", "user_id": "u"}))
        assert s._token() == "tok"
        assert s._user_id() == "u"

    def test_legacy_token_field(self):
        s = EmbyServer(_emby_config(auth={"token": "legacy"}))
        assert s._token() == "legacy"

    def test_no_auth_returns_empty_string(self):
        s = EmbyServer(_emby_config(auth={}))
        assert s._token() == ""


class TestRequestUrlConstruction:
    """D31-class regression coverage: _request builds the HTTP URL by
    concatenating ``url.rstrip('/') + path``. Every test in TestTestConnection
    / TestListLibraries / TestResolveItemToRemotePath mocks _request itself,
    so a URL-construction bug inside _request (e.g. doubled prefix, missing
    slash, dropped path segment) would slip through every other test in this
    file. Mock at the next layer down — ``requests.request`` — and assert the
    URL we actually send.
    """

    def _capture_request(self, emby):
        """Patch requests.request and return the call list."""
        captured: list[dict] = []

        def fake_request(method, url, **kwargs):
            captured.append({"method": method, "url": url, **kwargs})
            response = MagicMock()
            response.json.return_value = {"Items": []}
            response.raise_for_status.return_value = None
            return response

        return captured, patch("media_preview_generator.servers._embyish.requests.request", side_effect=fake_request)

    def test_url_is_base_plus_path_no_doubled_prefix(self, emby):
        """url='http://emby:8096' + path='/System/Info' → exact base+path."""
        captured, ctx = self._capture_request(emby)
        with ctx:
            emby._request("GET", "/System/Info")
        assert len(captured) == 1
        assert captured[0]["url"] == "http://emby:8096/System/Info", (
            f"URL was {captured[0]['url']!r}; D31 doubled-prefix or rstrip regression"
        )
        assert "//" not in captured[0]["url"].split("://", 1)[1]

    def test_url_strips_trailing_slash_on_base(self):
        """url='http://emby:8096/' (trailing slash) must NOT produce '//' in path."""
        from media_preview_generator.servers.emby import EmbyServer

        srv = EmbyServer(_emby_config(url="http://emby:8096/"))
        captured, ctx = self._capture_request(srv)
        with ctx:
            srv._request("GET", "/Items")
        assert captured[0]["url"] == "http://emby:8096/Items", (
            f"trailing-slash url produced {captured[0]['url']!r} — strip regression"
        )

    def test_x_emby_token_header_is_set_from_config_token(self, emby):
        """The X-Emby-Token header MUST be present and equal to the configured token.
        A regression that drops the header would silently fail every authed call."""
        captured, ctx = self._capture_request(emby)
        with ctx:
            emby._request("GET", "/System/Info")
        headers = captured[0].get("headers") or {}
        assert headers.get("X-Emby-Token"), f"X-Emby-Token missing from headers: {headers!r}"
        assert headers.get("Accept") == "application/json"


class TestTestConnection:
    def test_success(self, emby):
        with patch.object(EmbyServer, "_request") as req:
            response = MagicMock()
            response.json.return_value = {"Id": "abc123", "ServerName": "Office Emby", "Version": "4.9.0"}
            response.raise_for_status.return_value = None
            req.return_value = response

            result = emby.test_connection()

        assert isinstance(result, ConnectionResult)
        assert result.ok is True
        assert result.server_id == "abc123"
        assert result.server_name == "Office Emby"
        assert result.version == "4.9.0"

    def test_missing_url(self):
        s = EmbyServer(_emby_config(url=""))
        result = s.test_connection()
        assert not result.ok
        assert "url" in result.message.lower()

    def test_missing_token(self):
        s = EmbyServer(_emby_config(auth={}))
        result = s.test_connection()
        assert not result.ok
        assert "token" in result.message.lower() or "api key" in result.message.lower()

    def test_unauthorized(self, emby):
        with patch.object(EmbyServer, "_request") as req:
            err_response = MagicMock(status_code=401)
            err = requests.exceptions.HTTPError(response=err_response)
            response = MagicMock()
            response.raise_for_status.side_effect = err
            req.return_value = response

            result = emby.test_connection()

        assert not result.ok
        assert "401" in result.message

    def test_timeout(self, emby):
        with patch.object(EmbyServer, "_request") as req:
            req.side_effect = requests.exceptions.Timeout()

            result = emby.test_connection()

        assert not result.ok
        assert "timed out" in result.message.lower()


class TestListLibraries:
    def test_maps_virtual_folders_to_library_objects(self, emby):
        with patch.object(EmbyServer, "_request") as req:
            response = MagicMock()
            response.json.return_value = [
                {
                    "Name": "Movies",
                    "ItemId": "1",
                    "Locations": ["/em-media/Movies"],
                    "CollectionType": "movies",
                },
                {
                    "Name": "TV Shows",
                    "ItemId": "2",
                    "Locations": ["/em-media/TV"],
                    "CollectionType": "tvshows",
                },
            ]
            response.raise_for_status.return_value = None
            req.return_value = response

            libs = emby.list_libraries()

        assert len(libs) == 2
        assert libs[0].name == "Movies"
        assert libs[0].kind == "movies"
        assert libs[0].remote_paths == ("/em-media/Movies",)
        assert all(isinstance(lib, Library) for lib in libs)

    def test_preserves_existing_enabled_toggles(self):
        emby = EmbyServer(
            _emby_config(
                libraries=[
                    Library(id="1", name="Movies", remote_paths=("/m",), enabled=False),
                ]
            )
        )

        with patch.object(EmbyServer, "_request") as req:
            response = MagicMock()
            response.json.return_value = [
                {"Name": "Movies", "ItemId": "1", "Locations": ["/em-media/Movies"]},
                {"Name": "TV Shows", "ItemId": "2", "Locations": ["/em-media/TV"]},
            ]
            response.raise_for_status.return_value = None
            req.return_value = response

            libs = emby.list_libraries()

        by_id = {lib.id: lib for lib in libs}
        # Existing user toggle preserved.
        assert by_id["1"].enabled is False
        # New library defaults to enabled.
        assert by_id["2"].enabled is True

    def test_empty_on_failure(self, emby):
        with patch.object(EmbyServer, "_request", side_effect=RuntimeError("boom")):
            assert emby.list_libraries() == []

    def test_empty_on_unexpected_shape(self, emby):
        with patch.object(EmbyServer, "_request") as req:
            response = MagicMock()
            response.json.return_value = {"not": "a list"}
            response.raise_for_status.return_value = None
            req.return_value = response

            assert emby.list_libraries() == []


class TestListItems:
    def test_yields_movies_and_episodes(self, emby):
        with patch.object(EmbyServer, "_request") as req:
            response = MagicMock()
            response.json.return_value = {
                "Items": [
                    {"Id": "100", "Type": "Movie", "Name": "Test Movie", "Path": "/m/movie.mkv"},
                    {
                        "Id": "200",
                        "Type": "Episode",
                        "Name": "Pilot",
                        "SeriesName": "Test Show",
                        "ParentIndexNumber": 1,
                        "IndexNumber": 1,
                        "Path": "/m/show.mkv",
                    },
                ]
            }
            response.raise_for_status.return_value = None
            req.return_value = response

            items = list(emby.list_items("lib-1"))

        assert len(items) == 2
        assert all(isinstance(i, MediaItem) for i in items)
        titles = [i.title for i in items]
        assert "Test Movie" in titles
        assert any("S01E01" in t for t in titles)

    def test_skips_items_without_paths(self, emby):
        with patch.object(EmbyServer, "_request") as req:
            response = MagicMock()
            response.json.return_value = {
                "Items": [
                    {"Id": "100", "Type": "Movie", "Name": "No Path"},
                    {"Id": "200", "Type": "Movie", "Name": "Good", "Path": "/m/g.mkv"},
                ]
            }
            response.raise_for_status.return_value = None
            req.return_value = response

            items = list(emby.list_items("lib-1"))

        assert [i.title for i in items] == ["Good"]


class TestResolveItemToRemotePath:
    """The default fixture uses api_key auth (no user_id) → the
    client hits ``/Items?Ids={id}`` which returns ``{"Items": [...]}``.
    """

    def test_prefers_media_sources_path(self, emby):
        with patch.object(EmbyServer, "_request") as req:
            response = MagicMock()
            response.json.return_value = {
                "Items": [{"Path": "/top.mkv", "MediaSources": [{"Path": "/media-source.mkv"}]}]
            }
            response.raise_for_status.return_value = None
            req.return_value = response

            assert emby.resolve_item_to_remote_path("42") == "/media-source.mkv"

            # Verify the URL — without user_id the universal /Items?Ids endpoint
            # is used (bare /Items/{id} returns 404 on Emby without user context).
            call_args = req.call_args
            assert call_args.args[0] == "GET"
            assert call_args.args[1] == "/Items"
            assert call_args.kwargs.get("params", {}).get("Ids") == "42"

    def test_falls_back_to_top_level_path(self, emby):
        with patch.object(EmbyServer, "_request") as req:
            response = MagicMock()
            response.json.return_value = {"Items": [{"Path": "/top.mkv", "MediaSources": []}]}
            response.raise_for_status.return_value = None
            req.return_value = response

            assert emby.resolve_item_to_remote_path("42") == "/top.mkv"

    def test_returns_none_on_failure(self, emby):
        with patch.object(EmbyServer, "_request", side_effect=RuntimeError("404")):
            assert emby.resolve_item_to_remote_path("42") is None

    def test_returns_none_when_no_path_anywhere(self, emby):
        with patch.object(EmbyServer, "_request") as req:
            response = MagicMock()
            response.json.return_value = {"Items": [{"MediaSources": [{"Type": "stream"}]}]}
            response.raise_for_status.return_value = None
            req.return_value = response

            assert emby.resolve_item_to_remote_path("42") is None

    def test_per_user_endpoint_when_user_id_present(self):
        """Password auth (with user_id) uses /Users/{id}/Items/{id}."""
        config = _emby_config(auth={"method": "password", "access_token": "tok", "user_id": "u-1"})
        emby_with_user = EmbyServer(config)
        with patch.object(EmbyServer, "_request") as req:
            response = MagicMock()
            # /Users/{userId}/Items/{itemId} returns the item directly,
            # NOT wrapped in {"Items": [...]}.
            response.json.return_value = {"Path": "/per-user.mkv"}
            response.raise_for_status.return_value = None
            req.return_value = response

            result = emby_with_user.resolve_item_to_remote_path("42")
            assert result == "/per-user.mkv"
            # Verify the call hit the per-user endpoint.
            call_args = req.call_args
            assert call_args.args[1] == "/Users/u-1/Items/42", call_args


class TestTriggerRefresh:
    def test_uses_library_media_updated_when_path_known(self, emby):
        with patch.object(EmbyServer, "_request") as req:
            response = MagicMock()
            response.raise_for_status.return_value = None
            req.return_value = response

            emby.trigger_refresh(item_id=None, remote_path="/m/foo.mkv")

            req.assert_called_once_with(
                "POST",
                "/Library/Media/Updated",
                json_body={"Updates": [{"Path": "/m/foo.mkv", "UpdateType": "Modified"}]},
            )

    def test_falls_back_to_item_refresh_when_only_id(self, emby):
        with patch.object(EmbyServer, "_request") as req:
            response = MagicMock()
            response.raise_for_status.return_value = None
            req.return_value = response

            emby.trigger_refresh(item_id="42", remote_path=None)

            req.assert_called_once_with("POST", "/Items/42/Refresh")

    def test_swallows_exceptions_for_path_refresh(self, emby):
        with patch.object(EmbyServer, "_request", side_effect=RuntimeError("boom")):
            # Must not raise.
            emby.trigger_refresh(item_id=None, remote_path="/m/foo.mkv")


class TestParseWebhook:
    def test_library_new_event(self, emby):
        payload = {
            "Event": "library.new",
            "Item": {"Id": "12345", "Type": "Movie"},
            "Server": {"Id": "abc"},
        }
        ev = emby.parse_webhook(payload, headers={})
        assert isinstance(ev, WebhookEvent)
        assert ev.event_type == "library.new"
        assert ev.item_id == "12345"
        assert ev.remote_path is None

    def test_itemadded_event(self, emby):
        payload = {"Event": "ItemAdded", "Item": {"Id": "999"}}
        ev = emby.parse_webhook(payload, headers={})
        assert ev is not None
        assert ev.item_id == "999"

    def test_irrelevant_events_return_none(self, emby):
        for event in ["media.play", "media.stop", "PlaybackStart"]:
            payload = {"Event": event, "Item": {"Id": "1"}}
            assert emby.parse_webhook(payload, headers={}) is None

    def test_accepts_raw_bytes(self, emby):
        body = json.dumps({"Event": "library.new", "Item": {"Id": "7"}}).encode("utf-8")
        ev = emby.parse_webhook(body, headers={})
        assert ev is not None
        assert ev.item_id == "7"

    def test_invalid_json_returns_none(self, emby):
        assert emby.parse_webhook(b"not-json{", headers={}) is None

    def test_missing_item_id_yields_none_item_id(self, emby):
        ev = emby.parse_webhook({"Event": "library.new", "Item": {}}, headers={})
        assert ev is not None
        assert ev.item_id is None


class TestRegistryWiring:
    def test_registry_can_construct_emby_server(self):
        from media_preview_generator.servers import ServerRegistry

        registry = ServerRegistry.from_settings(
            [
                {
                    "id": "emby-1",
                    "type": "emby",
                    "name": "Test Emby",
                    "enabled": True,
                    "url": "http://emby:8096",
                    "auth": {"method": "api_key", "api_key": "k"},
                }
            ],
            legacy_config=None,
        )
        servers = registry.servers()
        assert len(servers) == 1
        assert isinstance(servers[0], EmbyServer)
