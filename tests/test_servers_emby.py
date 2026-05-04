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
        """Patch the Session.request the client uses and return the call list.

        The client now goes through ``self._get_session().request(...)`` for
        HTTP keep-alive across the dozens of round-trips a single scan makes,
        so the patch target is the per-client Session, not the module-level
        ``requests.request``.
        """
        captured: list[dict] = []

        def fake_request(method, url, **kwargs):
            captured.append({"method": method, "url": url, **kwargs})
            response = MagicMock()
            response.json.return_value = {"Items": []}
            response.raise_for_status.return_value = None
            return response

        # Force the lazy session init now so we have a concrete object to
        # patch — _get_session() is otherwise called inside _request and
        # we'd be patching after the call.
        session = emby._get_session()
        return captured, patch.object(session, "request", side_effect=fake_request)

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

    def test_session_is_reused_across_calls(self, emby):
        """All requests share one Session so HTTP keep-alive amortises the
        TCP+TLS handshake across the dozens of /Items round-trips a single
        scan makes. A regression that reverted to per-call ``requests.request``
        would silently re-handshake every call and add seconds of dead wall
        time to a 500-item scan."""
        first = emby._get_session()
        second = emby._get_session()
        assert first is second, "Session must be reused across calls"


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


class TestResolveRemotePathToItemIdViaExactPath:
    """Emby override that uses the native ``GET /Items?Path=<exact>``
    filter (single indexed-column lookup, ~1 ms) before falling back
    to the public ``searchTerm`` API. Confirmed working on Emby per
    https://emby.media/community/index.php?/topic/70680-search-item-by-file-path/
    — Jellyfin does not support this param (it was lost in the .NET
    Core rewrite of ItemsController), which is why only EmbyServer
    overrides this method, not Jellyfin's.
    """

    def test_uses_exact_path_filter_when_item_found(self, emby):
        path_resp = MagicMock()
        path_resp.json.return_value = {"Items": [{"Id": "abc-123", "Path": "/m/movie.mkv"}]}
        path_resp.raise_for_status.return_value = None
        with patch.object(EmbyServer, "_request", return_value=path_resp) as req:
            got = emby._uncached_resolve_remote_path_to_item_id("/m/movie.mkv")
            assert got == "abc-123"
            # Single network call — the exact-Path query, not the legacy
            # two-pass searchTerm path.
            assert req.call_count == 1
            args, kwargs = req.call_args
            assert args == ("GET", "/Items")
            assert kwargs["params"]["Path"] == "/m/movie.mkv"
            # Don't accidentally hit a slow full-enumeration cap here.
            assert kwargs["params"].get("Limit") == 1

    def test_falls_back_to_search_when_exact_path_returns_empty(self, emby):
        empty_path_resp = MagicMock()
        empty_path_resp.json.return_value = {"Items": []}
        empty_path_resp.raise_for_status.return_value = None
        # Base class's Pass-1 + Pass-2 also miss for this test.
        empty_search_resp = MagicMock()
        empty_search_resp.json.return_value = {"Items": []}
        empty_search_resp.raise_for_status.return_value = None
        with patch.object(
            EmbyServer,
            "_request",
            side_effect=[empty_path_resp, empty_search_resp, empty_search_resp],
        ) as req:
            got = emby._uncached_resolve_remote_path_to_item_id("/missing.mkv")
            assert got is None
            assert req.call_count >= 2
            # First call was the exact-Path fast path.
            assert req.call_args_list[0].kwargs["params"]["Path"] == "/missing.mkv"


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
        """Path-refresh must call _request AND swallow exceptions.

        Originally only asserted "didn't raise" — a regression that
        early-returned before calling _request would have passed silently
        and the user would never know their refresh hadn't fired.
        Audit fix: assert the call WAS attempted.
        """
        with patch.object(EmbyServer, "_request", side_effect=RuntimeError("boom")) as req:
            emby.trigger_refresh(item_id=None, remote_path="/m/foo.mkv")
            assert req.call_count >= 1, (
                "trigger_refresh must call _request even when it raises — "
                "otherwise a regression that early-returns would silently no-op"
            )


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

    def test_library_new_event_captures_path_when_present(self, emby):
        """Audit fix — Emby's library.new payload includes ``Item.Path``.
        Capturing it lets the dispatcher skip the reverse lookup.
        """
        payload = {
            "Event": "library.new",
            "Item": {"Id": "12345", "Type": "Movie", "Path": "/media/movies/Foo.mkv"},
            "Server": {"Id": "abc"},
        }
        ev = emby.parse_webhook(payload, headers={})
        assert ev is not None
        assert ev.item_id == "12345"
        assert ev.remote_path == "/media/movies/Foo.mkv", (
            "Emby's Item.Path was silently dropped — dispatcher will pay "
            "for an extra reverse-lookup roundtrip on every webhook"
        )

    def test_itemadded_event(self, emby):
        payload = {"Event": "ItemAdded", "Item": {"Id": "999"}}
        ev = emby.parse_webhook(payload, headers={})
        assert ev is not None
        assert ev.event_type == "ItemAdded", "event_type must be captured for downstream filtering"
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


class TestEmbySettingsHealth:
    """Mirror of TestSettingsHealth in test_servers_jellyfin — Emby uses the
    same VirtualFolders endpoint with a slightly different flag set."""

    def test_no_issues_when_recommended(self, emby):
        # ExtractTrickplayImagesDuringLibraryScan is Emby 4.8+; if the
        # field isn't present the audit must NOT flag it (older Emby
        # uses ExtractChapterImagesDuringLibraryScan instead).
        good = {
            "ExtractChapterImagesDuringLibraryScan": False,
            "EnableRealtimeMonitor": True,
        }
        with patch.object(EmbyServer, "_request") as req:
            req.return_value = MagicMock(
                json=MagicMock(return_value=[{"Name": "Movies", "ItemId": "1", "LibraryOptions": good}]),
                raise_for_status=MagicMock(),
            )
            assert emby.check_settings_health() == []

    def test_reports_misset_flags_per_library(self, emby):
        # Emby 4.8+ shape: trickplay flag present and wrongly on; both
        # other flags also wrong → 3 issues for the single library.
        bad = {
            "ExtractTrickplayImagesDuringLibraryScan": True,
            "ExtractChapterImagesDuringLibraryScan": True,
            "EnableRealtimeMonitor": False,
        }
        with patch.object(EmbyServer, "_request") as req:
            req.return_value = MagicMock(
                json=MagicMock(return_value=[{"Name": "Movies", "ItemId": "1", "LibraryOptions": bad}]),
                raise_for_status=MagicMock(),
            )
            issues = emby.check_settings_health()
        flags = {i.flag for i in issues}
        assert flags == {
            "ExtractTrickplayImagesDuringLibraryScan",
            "ExtractChapterImagesDuringLibraryScan",
            "EnableRealtimeMonitor",
        }
        assert all(i.library_id == "1" and i.fixable for i in issues)

    def test_skips_trickplay_flag_on_older_emby(self, emby):
        # Older Emby's LibraryOptions doesn't include
        # ExtractTrickplayImagesDuringLibraryScan at all. The audit
        # must NOT surface a "false != False" issue from a missing key.
        legacy_bad = {
            "ExtractChapterImagesDuringLibraryScan": True,  # only this is wrong
            "EnableRealtimeMonitor": True,
        }
        with patch.object(EmbyServer, "_request") as req:
            req.return_value = MagicMock(
                json=MagicMock(return_value=[{"Name": "Movies", "ItemId": "1", "LibraryOptions": legacy_bad}]),
                raise_for_status=MagicMock(),
            )
            issues = emby.check_settings_health()
        flags = {i.flag for i in issues}
        assert flags == {"ExtractChapterImagesDuringLibraryScan"}


class TestEmbyApplyRecommended:
    def test_writes_only_misset_flags(self, emby):
        bad = {
            "ExtractTrickplayImagesDuringLibraryScan": True,
            "ExtractChapterImagesDuringLibraryScan": False,  # already correct
            "EnableRealtimeMonitor": False,
        }
        get_resp = MagicMock(
            json=MagicMock(return_value=[{"Name": "Movies", "ItemId": "1", "LibraryOptions": bad}]),
            raise_for_status=MagicMock(),
        )
        post_resp = MagicMock(raise_for_status=MagicMock())

        def fake(method, url, **kwargs):
            return get_resp if method == "GET" else post_resp

        with patch.object(EmbyServer, "_request", side_effect=fake) as req:
            results = emby.apply_recommended_settings()

        assert set(results.keys()) == {
            "1:ExtractTrickplayImagesDuringLibraryScan",
            "1:EnableRealtimeMonitor",
        }
        assert all(v == "ok" for v in results.values())
        sent_options = next(c for c in req.call_args_list if c.args[0] == "POST").kwargs["json_body"]["LibraryOptions"]
        assert sent_options["ExtractTrickplayImagesDuringLibraryScan"] is False
        assert sent_options["EnableRealtimeMonitor"] is True
        # Already-correct field stays at its original value.
        assert sent_options["ExtractChapterImagesDuringLibraryScan"] is False


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
