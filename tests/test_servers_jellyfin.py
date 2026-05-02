"""Tests for the Jellyfin server client.

Covers the same MediaServer surface as the Emby tests but with Jellyfin's
particularities (Quick Connect-derived auth shape, NotificationType
webhook payload, ``/Items/{id}/Refresh`` instead of
``/Library/Media/Updated``).
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
import requests

from media_preview_generator.servers import (
    ConnectionResult,
    JellyfinServer,
    Library,
    MediaItem,
    ServerConfig,
    ServerType,
    WebhookEvent,
)

_DEFAULT_AUTH = {"method": "quick_connect", "access_token": "tok", "user_id": "u"}
_SENTINEL = object()


def _jelly_config(
    *,
    server_id: str = "jelly-1",
    name: str = "Test Jellyfin",
    auth=_SENTINEL,
    libraries: list[Library] | None = None,
    url: str = "http://jellyfin:8096",
) -> ServerConfig:
    if auth is _SENTINEL:
        auth = dict(_DEFAULT_AUTH)
    return ServerConfig(
        id=server_id,
        type=ServerType.JELLYFIN,
        name=name,
        enabled=True,
        url=url,
        auth=auth,
        libraries=libraries or [],
    )


@pytest.fixture
def jelly():
    return JellyfinServer(_jelly_config())


class TestConstruction:
    def test_implements_media_server(self, jelly):
        from media_preview_generator.servers import MediaServer

        assert isinstance(jelly, MediaServer)

    def test_type_is_jellyfin(self, jelly):
        assert jelly.type is ServerType.JELLYFIN


class TestTokenExtraction:
    def test_quick_connect_token(self):
        s = JellyfinServer(_jelly_config(auth={"method": "quick_connect", "access_token": "qc"}))
        assert s._token() == "qc"

    def test_password_flow_token(self):
        s = JellyfinServer(_jelly_config(auth={"method": "password", "access_token": "pw", "user_id": "u"}))
        assert s._token() == "pw"

    def test_api_key(self):
        s = JellyfinServer(_jelly_config(auth={"method": "api_key", "api_key": "k"}))
        assert s._token() == "k"

    def test_no_auth_returns_empty_string(self):
        s = JellyfinServer(_jelly_config(auth={}))
        assert s._token() == ""


class TestTestConnection:
    def test_success(self, jelly):
        with patch.object(JellyfinServer, "_request") as req:
            response = MagicMock()
            response.json.return_value = {
                "Id": "jf-abc",
                "ServerName": "Family Jellyfin",
                "Version": "10.10.0",
            }
            response.raise_for_status.return_value = None
            req.return_value = response

            result = jelly.test_connection()

        assert isinstance(result, ConnectionResult)
        assert result.ok is True
        assert result.server_id == "jf-abc"
        assert result.server_name == "Family Jellyfin"
        assert result.version == "10.10.0"

    def test_missing_url(self):
        s = JellyfinServer(_jelly_config(url=""))
        result = s.test_connection()
        assert not result.ok

    def test_missing_token(self):
        s = JellyfinServer(_jelly_config(auth={}))
        result = s.test_connection()
        assert not result.ok

    def test_unauthorized(self, jelly):
        with patch.object(JellyfinServer, "_request") as req:
            err_response = MagicMock(status_code=401)
            err = requests.exceptions.HTTPError(response=err_response)
            response = MagicMock()
            response.raise_for_status.side_effect = err
            req.return_value = response

            result = jelly.test_connection()

        assert not result.ok
        assert "401" in result.message


class TestListLibraries:
    def test_maps_virtual_folders(self, jelly):
        with patch.object(JellyfinServer, "_request") as req:
            response = MagicMock()
            response.json.return_value = [
                {
                    "Name": "Movies",
                    "ItemId": "1",
                    "Locations": ["/jf-media/Movies"],
                    "CollectionType": "movies",
                },
                {
                    "Name": "TV Shows",
                    "ItemId": "2",
                    "Locations": ["/jf-media/TV"],
                    "CollectionType": "tvshows",
                },
            ]
            response.raise_for_status.return_value = None
            req.return_value = response

            libs = jelly.list_libraries()

        assert [lib.name for lib in libs] == ["Movies", "TV Shows"]
        assert libs[0].kind == "movies"

    def test_preserves_existing_enabled_toggles(self):
        jelly = JellyfinServer(
            _jelly_config(
                libraries=[
                    Library(id="1", name="Movies", remote_paths=("/m",), enabled=False),
                ]
            )
        )

        with patch.object(JellyfinServer, "_request") as req:
            response = MagicMock()
            response.json.return_value = [
                {"Name": "Movies", "ItemId": "1", "Locations": ["/jf-media/Movies"]},
                {"Name": "TV Shows", "ItemId": "2", "Locations": ["/jf-media/TV"]},
            ]
            response.raise_for_status.return_value = None
            req.return_value = response

            libs = jelly.list_libraries()

        by_id = {lib.id: lib for lib in libs}
        assert by_id["1"].enabled is False
        assert by_id["2"].enabled is True

    def test_empty_on_failure(self, jelly):
        with patch.object(JellyfinServer, "_request", side_effect=RuntimeError("boom")):
            assert jelly.list_libraries() == []


class TestListItems:
    def test_yields_movies_and_episodes(self, jelly):
        with patch.object(JellyfinServer, "_request") as req:
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

            items = list(jelly.list_items("lib-1"))

        assert len(items) == 2
        assert all(isinstance(i, MediaItem) for i in items)
        assert any("S01E01" in i.title for i in items)


class TestResolveItemToRemotePath:
    def test_prefers_media_sources_path(self, jelly):
        """Verifies BOTH the parsed result AND the URL Jellyfin was queried at.

        Without the URL assertion, a regression that called the wrong endpoint
        (e.g. doubling the prefix to ``/Users/u/Items//Users/u/Items/42``, or
        flipping to the bare ``/Items/{id}`` shape that 400s on Jellyfin)
        would still pass — the mock returns the canned payload regardless.
        Mirrors the assertion pattern in
        ``test_servers_emby.py::test_per_user_endpoint_when_user_id_present``.
        """
        with patch.object(JellyfinServer, "_request") as req:
            response = MagicMock()
            response.json.return_value = {
                "Path": "/top.mkv",
                "MediaSources": [{"Path": "/media-source.mkv"}],
            }
            response.raise_for_status.return_value = None
            req.return_value = response

            assert jelly.resolve_item_to_remote_path("42") == "/media-source.mkv"

            # Default auth fixture has user_id="u" so the per-user endpoint must be used.
            call_args = req.call_args
            assert call_args.args[0] == "GET", call_args
            assert call_args.args[1] == "/Users/u/Items/42", (
                f"Jellyfin item lookup hit the wrong URL: {call_args.args[1]!r}. "
                "Expected /Users/u/Items/42 — bare /Items/{id} returns 400 on Jellyfin."
            )

    def test_falls_back_to_plural_items_endpoint_when_no_user_id(self):
        """Without user_id (api_key auth), the universal /Items?Ids= endpoint is used."""
        config = _jelly_config(auth={"method": "api_key", "api_key": "k"})
        jelly_no_user = JellyfinServer(config)
        with patch.object(JellyfinServer, "_request") as req:
            response = MagicMock()
            response.json.return_value = {"Items": [{"Path": "/found.mkv"}]}
            response.raise_for_status.return_value = None
            req.return_value = response

            assert jelly_no_user.resolve_item_to_remote_path("99") == "/found.mkv"

            call_args = req.call_args
            assert call_args.args[0] == "GET"
            assert call_args.args[1] == "/Items", (
                f"Jellyfin item lookup hit the wrong URL: {call_args.args[1]!r}. "
                "Expected /Items (plural) when user_id is unknown."
            )
            # And the params carry the id — otherwise we'd get the whole library.
            assert call_args.kwargs.get("params", {}).get("Ids") == "99"

    def test_returns_none_on_failure(self, jelly):
        with patch.object(JellyfinServer, "_request", side_effect=RuntimeError("404")):
            assert jelly.resolve_item_to_remote_path("42") is None


class TestTriggerRefresh:
    def test_uses_per_item_refresh_when_id_known(self, jelly):
        with patch.object(JellyfinServer, "_request") as req:
            response = MagicMock()
            response.raise_for_status.return_value = None
            req.return_value = response

            jelly.trigger_refresh(item_id="42", remote_path=None)

            req.assert_called_once_with("POST", "/Items/42/Refresh")

    def test_falls_back_to_library_refresh_without_id(self, jelly):
        with patch.object(JellyfinServer, "_request") as req:
            response = MagicMock()
            response.raise_for_status.return_value = None
            req.return_value = response

            jelly.trigger_refresh(item_id=None, remote_path="/some/path.mkv")

            req.assert_called_once_with("POST", "/Library/Refresh")

    def test_falls_back_to_library_refresh_when_per_item_fails(self, jelly):
        # If the per-item refresh raises (e.g. item not yet indexed), we
        # still nudge the full library scan as a best-effort fallback.
        responses = [RuntimeError("404"), MagicMock(raise_for_status=MagicMock(return_value=None))]

        def side_effect(*args, **kwargs):
            value = responses.pop(0)
            if isinstance(value, Exception):
                raise value
            return value

        with patch.object(JellyfinServer, "_request", side_effect=side_effect) as req:
            jelly.trigger_refresh(item_id="42", remote_path=None)

            assert req.call_count == 2
            assert req.call_args_list[0].args == ("POST", "/Items/42/Refresh")
            assert req.call_args_list[1].args == ("POST", "/Library/Refresh")


class TestParseWebhook:
    def test_itemadded_event(self, jelly):
        payload = {
            "NotificationType": "ItemAdded",
            "ItemId": "42",
            "ItemType": "Movie",
            "ServerId": "abc",
        }
        ev = jelly.parse_webhook(payload, headers={})
        assert isinstance(ev, WebhookEvent)
        assert ev.item_id == "42"

    def test_library_new_emby_template(self, jelly):
        payload = {"Event": "library.new", "ItemId": "99"}
        ev = jelly.parse_webhook(payload, headers={})
        assert ev is not None
        assert ev.item_id == "99"

    def test_irrelevant_events_return_none(self, jelly):
        for event in ["PlaybackStart", "PlaybackStop", "UserCreated"]:
            payload = {"NotificationType": event, "ItemId": "1"}
            assert jelly.parse_webhook(payload, headers={}) is None

    def test_accepts_raw_bytes(self, jelly):
        body = json.dumps({"NotificationType": "ItemAdded", "ItemId": "7"}).encode("utf-8")
        ev = jelly.parse_webhook(body, headers={})
        assert ev is not None
        assert ev.item_id == "7"

    def test_invalid_json_returns_none(self, jelly):
        assert jelly.parse_webhook(b"not-json{", headers={}) is None


class TestTrickplayExtractionStatus:
    """Surface the EnableTrickplayImageExtraction misconfiguration to the UI."""

    def test_returns_per_library_flags(self, jelly):
        with patch.object(JellyfinServer, "_request") as req:
            req.return_value = MagicMock(
                status_code=200,
                json=MagicMock(
                    return_value=[
                        {
                            "Name": "Movies",
                            "ItemId": "1",
                            "Locations": ["/jf-media/Movies"],
                            "LibraryOptions": {
                                "EnableTrickplayImageExtraction": True,
                                "ExtractTrickplayImagesDuringLibraryScan": True,
                            },
                        },
                        {
                            "Name": "TV",
                            "ItemId": "2",
                            "Locations": ["/jf-media/TV"],
                            "LibraryOptions": {
                                "EnableTrickplayImageExtraction": False,
                                "ExtractTrickplayImagesDuringLibraryScan": False,
                            },
                        },
                    ]
                ),
                raise_for_status=MagicMock(),
            )
            result = jelly.check_trickplay_extraction_status()

        assert result == [
            {
                "id": "1",
                "name": "Movies",
                "locations": ["/jf-media/Movies"],
                "extraction_enabled": True,
                "scan_extraction_enabled": True,
            },
            {
                "id": "2",
                "name": "TV",
                "locations": ["/jf-media/TV"],
                "extraction_enabled": False,
                "scan_extraction_enabled": False,
            },
        ]

    def test_empty_on_request_failure(self, jelly):
        with patch.object(JellyfinServer, "_request", side_effect=RuntimeError("boom")):
            assert jelly.check_trickplay_extraction_status() == []

    def test_handles_missing_library_options(self, jelly):
        """Older Jellyfin versions might not return ``LibraryOptions``; fall back to False."""
        with patch.object(JellyfinServer, "_request") as req:
            req.return_value = MagicMock(
                status_code=200,
                json=MagicMock(return_value=[{"Name": "Movies", "ItemId": "1", "Locations": []}]),
                raise_for_status=MagicMock(),
            )
            result = jelly.check_trickplay_extraction_status()
        assert result[0]["extraction_enabled"] is False
        assert result[0]["scan_extraction_enabled"] is False


class TestEnableTrickplayExtraction:
    """One-click fix that flips the flag on selected libraries."""

    def test_updates_each_library(self, jelly):
        get_response = MagicMock(
            status_code=200,
            json=MagicMock(
                return_value=[
                    {
                        "Name": "Movies",
                        "ItemId": "1",
                        "LibraryOptions": {"EnableTrickplayImageExtraction": False},
                    },
                    {
                        "Name": "TV",
                        "ItemId": "2",
                        "LibraryOptions": {"EnableTrickplayImageExtraction": False},
                    },
                ]
            ),
            raise_for_status=MagicMock(),
        )
        post_response = MagicMock(status_code=204, raise_for_status=MagicMock())

        captured: list[dict] = []

        def fake_request(method, path, *, params=None, json_body=None):
            if method == "GET" and path == "/Library/VirtualFolders":
                return get_response
            if method == "POST" and path == "/Library/VirtualFolders/LibraryOptions":
                captured.append(json_body)
                return post_response
            raise AssertionError(f"unexpected request {method} {path}")

        with patch.object(JellyfinServer, "_request", side_effect=fake_request):
            results = jelly.enable_trickplay_extraction()

        assert results == {"1": "ok", "2": "ok"}
        # Verify the POST body kept the existing options dict and flipped the flag.
        assert all(c["LibraryOptions"]["EnableTrickplayImageExtraction"] is True for c in captured)
        assert all(c["LibraryOptions"]["ExtractTrickplayImagesDuringLibraryScan"] is True for c in captured)
        assert {c["Id"] for c in captured} == {"1", "2"}

    def test_filters_to_requested_library_ids(self, jelly):
        get_response = MagicMock(
            status_code=200,
            json=MagicMock(
                return_value=[
                    {"Name": "Movies", "ItemId": "1", "LibraryOptions": {}},
                    {"Name": "TV", "ItemId": "2", "LibraryOptions": {}},
                ]
            ),
            raise_for_status=MagicMock(),
        )
        post_response = MagicMock(status_code=204, raise_for_status=MagicMock())

        captured: list[dict] = []

        def fake_request(method, path, *, params=None, json_body=None):
            if method == "GET":
                return get_response
            captured.append(json_body)
            return post_response

        with patch.object(JellyfinServer, "_request", side_effect=fake_request):
            results = jelly.enable_trickplay_extraction(library_ids=["1"])

        assert list(results.keys()) == ["1"]
        assert len(captured) == 1
        assert captured[0]["Id"] == "1"

    def test_per_library_failure_reported(self, jelly):
        get_response = MagicMock(
            status_code=200,
            json=MagicMock(
                return_value=[
                    {"Name": "Movies", "ItemId": "1", "LibraryOptions": {}},
                    {"Name": "Broken", "ItemId": "2", "LibraryOptions": {}},
                ]
            ),
            raise_for_status=MagicMock(),
        )
        ok_post = MagicMock(status_code=204, raise_for_status=MagicMock())

        def fake_request(method, path, *, params=None, json_body=None):
            if method == "GET":
                return get_response
            if json_body and json_body.get("Id") == "2":
                raise requests.HTTPError("403")
            return ok_post

        with patch.object(JellyfinServer, "_request", side_effect=fake_request):
            results = jelly.enable_trickplay_extraction()

        assert results["1"] == "ok"
        assert results["2"].startswith("error:")


class TestRegistryWiring:
    def test_registry_can_construct_jellyfin_server(self):
        from media_preview_generator.servers import ServerRegistry

        registry = ServerRegistry.from_settings(
            [
                {
                    "id": "jelly-1",
                    "type": "jellyfin",
                    "name": "Test Jellyfin",
                    "enabled": True,
                    "url": "http://jellyfin:8096",
                    "auth": {"method": "api_key", "api_key": "k"},
                }
            ],
            legacy_config=None,
        )
        servers = registry.servers()
        assert len(servers) == 1
        assert isinstance(servers[0], JellyfinServer)
