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


class TestResolveRemotePathToItemIdViaPlugin:
    """Jellyfin override that prefers the Media Preview Bridge plugin's
    ``/MediaPreviewBridge/ResolvePath`` (single indexed-column lookup,
    ~1 ms) over the public ``/Items?searchTerm=…`` API (full-text
    title index that drops 4K/HDR/DV tokens, plus ~30 s Pass-2 walk
    when Pass 1 misses).
    """

    def test_uses_plugin_resolve_path_when_installed(self, jelly):
        plugin_resp = MagicMock(status_code=200)
        plugin_resp.json.return_value = {"itemId": "abc-123", "name": "Inception", "type": "Movie"}
        with patch.object(JellyfinServer, "_request", return_value=plugin_resp) as req:
            got = jelly._uncached_resolve_remote_path_to_item_id("/data/movies/inception.mkv")
            assert got == "abc-123"
            # Exactly ONE network call — the plugin endpoint, not the
            # legacy two-pass searchTerm path.
            assert req.call_count == 1
            args, kwargs = req.call_args
            assert args == ("GET", "/MediaPreviewBridge/ResolvePath")
            assert kwargs["params"]["path"] == "/data/movies/inception.mkv"

    def test_falls_back_to_public_api_when_plugin_returns_404(self, jelly):
        # 404 = either plugin not installed or no item at this path.
        # Either way: fall through to the public API on the base class
        # so the lookup still completes (the base class's library-prefix
        # short-circuit handles the "no item" case in microseconds).
        plugin_resp = MagicMock(status_code=404)
        # Empty Pass-1 search response — base class's fallback path.
        empty_resp = MagicMock()
        empty_resp.json.return_value = {"Items": []}
        empty_resp.raise_for_status.return_value = None
        with patch.object(JellyfinServer, "_request", side_effect=[plugin_resp, empty_resp, empty_resp]) as req:
            got = jelly._uncached_resolve_remote_path_to_item_id("/nope.mkv")
            assert got is None
            # First call was the plugin probe, then the base class kicked
            # in (Pass 1 + Pass 2).
            assert req.call_count >= 2
            assert req.call_args_list[0].args == ("GET", "/MediaPreviewBridge/ResolvePath")

    def test_falls_back_when_plugin_request_raises(self, jelly):
        # Network/transport error → quietly degrade to the public API.
        empty_resp = MagicMock()
        empty_resp.json.return_value = {"Items": []}
        empty_resp.raise_for_status.return_value = None
        with patch.object(
            JellyfinServer,
            "_request",
            side_effect=[RuntimeError("connection refused"), empty_resp, empty_resp],
        ):
            got = jelly._uncached_resolve_remote_path_to_item_id("/x.mkv")
            assert got is None  # base class also misses; that's fine for this assertion


class TestTriggerRefresh:
    def test_calls_plugin_bridge_then_per_item_refresh_when_id_known(self, jelly):
        # Two requests fire when an item_id is supplied: the Media Preview
        # Bridge plugin endpoint (HTTP 204 = trickplay registered, no
        # ffmpeg, no flag flip), then the standard /Items/{id}/Refresh.
        plugin_resp = MagicMock(status_code=204, text="")
        refresh_resp = MagicMock()
        refresh_resp.raise_for_status.return_value = None

        with patch.object(JellyfinServer, "_request", side_effect=[plugin_resp, refresh_resp]) as req:
            jelly.trigger_refresh(item_id="42", remote_path=None)

            assert req.call_count == 2
            assert req.call_args_list[0].args == ("POST", "/MediaPreviewBridge/Trickplay/42")
            assert req.call_args_list[1].args == ("POST", "/Items/42/Refresh")

    def test_continues_to_per_item_refresh_when_plugin_not_installed(self, jelly):
        # Plugin returns 404 (not installed) — log and continue to the
        # standard refresh so the call chain still fires.
        plugin_resp = MagicMock(status_code=404, text="not found")
        refresh_resp = MagicMock()
        refresh_resp.raise_for_status.return_value = None

        with patch.object(JellyfinServer, "_request", side_effect=[plugin_resp, refresh_resp]) as req:
            jelly.trigger_refresh(item_id="42", remote_path=None)

            assert req.call_count == 2
            assert req.call_args_list[1].args == ("POST", "/Items/42/Refresh")

    def test_path_based_nudge_when_no_item_id(self, jelly):
        # With a remote_path but no item_id we use Jellyfin's
        # path-based scan-nudge (/Library/Media/Updated, same shape
        # as Emby's). Per-file, no global library scan.
        with patch.object(JellyfinServer, "_request") as req:
            response = MagicMock()
            response.raise_for_status.return_value = None
            req.return_value = response

            jelly.trigger_refresh(item_id=None, remote_path="/some/path.mkv")

            req.assert_called_once_with(
                "POST",
                "/Library/Media/Updated",
                json_body={"Updates": [{"Path": "/some/path.mkv", "UpdateType": "Created"}]},
            )

    def test_falls_back_to_full_refresh_when_no_path_and_no_id(self, jelly):
        # Neither item_id nor remote_path given — last-resort full
        # /Library/Refresh fires (rate-limited).
        with patch.object(JellyfinServer, "_request") as req:
            response = MagicMock()
            response.raise_for_status.return_value = None
            req.return_value = response

            jelly.trigger_refresh(item_id=None, remote_path=None)

            req.assert_called_once_with("POST", "/Library/Refresh")

    def test_full_refresh_is_rate_limited_per_server(self, jelly):
        # /Library/Refresh is heavyweight (full library scan, no path
        # filter). A burst of nudges with neither item_id nor
        # remote_path — the only branch that reaches the full refresh —
        # must NOT trigger one /Library/Refresh per call or Jellyfin
        # pins for minutes. The cooldown lives on the server instance
        # so concurrent calls for the same server collapse to a single
        # scan.
        with patch.object(JellyfinServer, "_request") as req:
            response = MagicMock()
            response.raise_for_status.return_value = None
            req.return_value = response

            jelly.trigger_refresh(item_id=None, remote_path=None)
            jelly.trigger_refresh(item_id=None, remote_path=None)
            jelly.trigger_refresh(item_id=None, remote_path=None)

            # Only the first nudge fires the API call; the next two
            # land inside the cooldown window and short-circuit.
            req.assert_called_once_with("POST", "/Library/Refresh")

    def test_path_nudge_failure_falls_back_to_full_refresh(self, jelly):
        # Path-based nudge raises (e.g. older Jellyfin without the
        # endpoint, or Jellyfin returns 5xx) — fall back to the
        # rate-limited full refresh so the SKIPPED_NOT_IN_LIBRARY
        # retry path still gets *some* scan triggered.
        path_exc = RuntimeError("path nudge failed")
        full_resp = MagicMock(raise_for_status=MagicMock(return_value=None))
        responses = [path_exc, full_resp]

        def side_effect(*args, **kwargs):
            value = responses.pop(0)
            if isinstance(value, Exception):
                raise value
            return value

        with patch.object(JellyfinServer, "_request", side_effect=side_effect) as req:
            jelly.trigger_refresh(item_id=None, remote_path="/some/path.mkv")

            assert req.call_count == 2
            assert req.call_args_list[0].args == (
                "POST",
                "/Library/Media/Updated",
            )
            assert req.call_args_list[1].args == ("POST", "/Library/Refresh")

    def test_falls_back_to_library_refresh_when_per_item_fails(self, jelly):
        # Plugin call + per-item refresh both raise → full library
        # refresh fires as last-resort best-effort.
        plugin_exc = RuntimeError("plugin 404")
        refresh_exc = RuntimeError("refresh 404")
        library_resp = MagicMock(raise_for_status=MagicMock(return_value=None))
        responses = [plugin_exc, refresh_exc, library_resp]

        def side_effect(*args, **kwargs):
            value = responses.pop(0)
            if isinstance(value, Exception):
                raise value
            return value

        with patch.object(JellyfinServer, "_request", side_effect=side_effect) as req:
            jelly.trigger_refresh(item_id="42", remote_path=None)

            assert req.call_count == 3
            assert req.call_args_list[0].args == ("POST", "/MediaPreviewBridge/Trickplay/42")
            assert req.call_args_list[1].args == ("POST", "/Items/42/Refresh")
            assert req.call_args_list[2].args == ("POST", "/Library/Refresh")


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
        assert ev.event_type == "ItemAdded", "event_type must be captured for downstream filtering"
        assert ev.item_id == "42"

    def test_itemadded_event_captures_path_when_template_provides_it(self, jelly):
        """Audit fix — Jellyfin's "Generic Destination" webhook template
        includes ``{{ItemPath}}``. Capturing it lets the dispatcher skip
        the per-item reverse lookup. A regression that drops this field
        forces every Jellyfin webhook to pay an extra HTTP roundtrip.
        """
        payload = {
            "NotificationType": "ItemAdded",
            "ItemId": "42",
            "ItemType": "Movie",
            "ItemPath": "/media/movies/Foo.mkv",
            "ServerId": "abc",
        }
        ev = jelly.parse_webhook(payload, headers={})
        assert ev is not None
        assert ev.item_id == "42"
        assert ev.remote_path == "/media/movies/Foo.mkv", (
            "Jellyfin's ItemPath template field was silently dropped — "
            "dispatcher will pay for an extra reverse-lookup roundtrip"
        )

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


class TestSettingsHealth:
    """check_settings_health surfaces every mis-set preview-relevant flag."""

    def test_no_issues_when_all_recommended(self, jelly):
        all_good = {
            "EnableTrickplayImageExtraction": True,
            "SaveTrickplayWithMedia": True,
            "ExtractTrickplayImagesDuringLibraryScan": False,
            "EnableRealtimeMonitor": True,
        }
        with patch.object(JellyfinServer, "_request") as req:
            req.return_value = MagicMock(
                status_code=200,
                json=MagicMock(return_value=[{"Name": "Movies", "ItemId": "1", "LibraryOptions": all_good}]),
                raise_for_status=MagicMock(),
            )
            issues = jelly.check_settings_health()
        assert issues == []

    def test_reports_each_misset_flag_per_library(self, jelly):
        # Movies has Realtime off (recommended); TV has both critical
        # flags wrong AND scan extraction on. We expect one issue per
        # mis-set flag on each library — the apply step decides whether
        # to flip them, the audit just *reports*.
        bad_critical = {
            "EnableTrickplayImageExtraction": False,  # critical
            "SaveTrickplayWithMedia": False,  # critical
            "ExtractTrickplayImagesDuringLibraryScan": True,  # recommended
            "EnableRealtimeMonitor": False,  # recommended
        }
        movies_good_except_realtime = {
            "EnableTrickplayImageExtraction": True,
            "SaveTrickplayWithMedia": True,
            "ExtractTrickplayImagesDuringLibraryScan": False,
            "EnableRealtimeMonitor": False,  # only this one wrong
        }
        with patch.object(JellyfinServer, "_request") as req:
            req.return_value = MagicMock(
                status_code=200,
                json=MagicMock(
                    return_value=[
                        {"Name": "Movies", "ItemId": "m", "LibraryOptions": movies_good_except_realtime},
                        {"Name": "TV", "ItemId": "t", "LibraryOptions": bad_critical},
                    ]
                ),
                raise_for_status=MagicMock(),
            )
            issues = jelly.check_settings_health()

        # Movies: 1 issue (Realtime). TV: 4 issues (all of them).
        movies_issues = [i for i in issues if i.library_id == "m"]
        tv_issues = [i for i in issues if i.library_id == "t"]
        assert len(movies_issues) == 1
        assert movies_issues[0].flag == "EnableRealtimeMonitor"
        assert movies_issues[0].severity == "recommended"
        assert len(tv_issues) == 4

        critical_flags = {i.flag for i in tv_issues if i.severity == "critical"}
        assert critical_flags == {"EnableTrickplayImageExtraction", "SaveTrickplayWithMedia"}

    def test_empty_on_request_failure(self, jelly):
        with patch.object(JellyfinServer, "_request", side_effect=RuntimeError("offline")):
            assert jelly.check_settings_health() == []


class TestApplyRecommendedSettings:
    """Apply step writes only the flags actually needing change."""

    def test_writes_only_misset_flags_back(self, jelly):
        # The audit endpoint will surface a single issue (Realtime off)
        # — the apply path must flip THAT flag (and only that flag) on
        # the library. Other flags retain their current values in the
        # POST'd LibraryOptions because Jellyfin's update endpoint is a
        # wholesale replace, not a diff (D38 caveat).
        bad_realtime = {
            "EnableTrickplayImageExtraction": True,
            "SaveTrickplayWithMedia": True,
            "ExtractTrickplayImagesDuringLibraryScan": False,
            "EnableRealtimeMonitor": False,
            "SomeUnrelatedFlag": "preserve_me",
        }
        get_response = MagicMock(
            status_code=200,
            json=MagicMock(return_value=[{"Name": "Movies", "ItemId": "1", "LibraryOptions": bad_realtime}]),
            raise_for_status=MagicMock(),
        )
        post_response = MagicMock(status_code=204, raise_for_status=MagicMock())

        def fake_request(method, url, **kwargs):
            if method == "GET":
                return get_response
            if method == "POST":
                return post_response
            raise AssertionError(f"unexpected {method} {url}")

        with patch.object(JellyfinServer, "_request", side_effect=fake_request) as req:
            results = jelly.apply_recommended_settings()

        # Only EnableRealtimeMonitor was wrong; no other rows in results.
        assert results == {"1:EnableRealtimeMonitor": "ok"}

        # And the POSTed body kept SomeUnrelatedFlag intact (D38: replace,
        # not diff) and only flipped EnableRealtimeMonitor.
        post_call = next(c for c in req.call_args_list if c.args[0] == "POST")
        sent_options = post_call.kwargs["json_body"]["LibraryOptions"]
        assert sent_options["EnableRealtimeMonitor"] is True
        assert sent_options["SomeUnrelatedFlag"] == "preserve_me"
        # Audit fix — also assert the other CRITICAL flags weren't quietly
        # rewritten. Without this, a regression where apply silently
        # flipped EnableTrickplayImageExtraction (or any other unrelated
        # critical flag) would have passed because results checks only
        # the changed-flag dict.
        assert sent_options["EnableTrickplayImageExtraction"] is True, (
            "apply silently mutated EnableTrickplayImageExtraction — regression in the apply-only-misset logic"
        )
        assert sent_options["SaveTrickplayWithMedia"] is True
        assert sent_options["ExtractTrickplayImagesDuringLibraryScan"] is False

    def test_skips_libraries_already_correct(self, jelly):
        # Both libraries fine — apply makes zero POSTs and returns {}.
        all_good = {
            "EnableTrickplayImageExtraction": True,
            "SaveTrickplayWithMedia": True,
            "ExtractTrickplayImagesDuringLibraryScan": False,
            "EnableRealtimeMonitor": True,
        }
        with patch.object(JellyfinServer, "_request") as req:
            req.return_value = MagicMock(
                status_code=200,
                json=MagicMock(
                    return_value=[
                        {"Name": "Movies", "ItemId": "1", "LibraryOptions": all_good},
                        {"Name": "TV", "ItemId": "2", "LibraryOptions": all_good},
                    ]
                ),
                raise_for_status=MagicMock(),
            )
            results = jelly.apply_recommended_settings()

        assert results == {}
        # Only the GET fired, no POST attempts.
        post_calls = [c for c in req.call_args_list if c.args[0] == "POST"]
        assert post_calls == []

    def test_flag_filter_restricts_target(self, jelly):
        # Caller asks for only EnableRealtimeMonitor — even though
        # other flags are also wrong, leave them alone.
        all_wrong = {
            "EnableTrickplayImageExtraction": False,  # would be touched if not filtered
            "SaveTrickplayWithMedia": False,
            "ExtractTrickplayImagesDuringLibraryScan": True,
            "EnableRealtimeMonitor": False,
        }
        get_response = MagicMock(
            status_code=200,
            json=MagicMock(return_value=[{"Name": "Movies", "ItemId": "1", "LibraryOptions": all_wrong}]),
            raise_for_status=MagicMock(),
        )
        post_response = MagicMock(status_code=204, raise_for_status=MagicMock())

        def fake_request(method, url, **kwargs):
            return get_response if method == "GET" else post_response

        with patch.object(JellyfinServer, "_request", side_effect=fake_request) as req:
            results = jelly.apply_recommended_settings(flags=["EnableRealtimeMonitor"])

        assert results == {"1:EnableRealtimeMonitor": "ok"}
        post_call = next(c for c in req.call_args_list if c.args[0] == "POST")
        sent_options = post_call.kwargs["json_body"]["LibraryOptions"]
        # Only the filtered flag flipped; the others left at False/True.
        assert sent_options["EnableRealtimeMonitor"] is True
        assert sent_options["EnableTrickplayImageExtraction"] is False
        assert sent_options["SaveTrickplayWithMedia"] is False
        assert sent_options["ExtractTrickplayImagesDuringLibraryScan"] is True


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
