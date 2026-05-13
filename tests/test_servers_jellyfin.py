"""Tests for the Jellyfin server client.

Covers the same MediaServer surface as the Emby tests but with Jellyfin's
particularities (Quick Connect-derived auth shape, NotificationType
webhook payload, ``/Items/{id}/Refresh`` instead of
``/Library/Media/Updated``).
"""

from __future__ import annotations

import json
import re
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
        # Patch _request to prove the short-circuit avoided a wasted network call.
        with patch.object(JellyfinServer, "_request") as req:
            result = s.test_connection()

        # Production format: "Jellyfin URL is required".
        assert not result.ok
        req.assert_not_called(), "missing-URL must short-circuit before any HTTP call"
        assert re.search(r"\bURL\b", result.message), (
            f"missing-URL error must mention 'URL' as a word, got {result.message!r}"
        )
        assert "required" in result.message.lower()

    def test_missing_token(self):
        s = JellyfinServer(_jelly_config(auth={}))
        with patch.object(JellyfinServer, "_request") as req:
            result = s.test_connection()

        # Production format: "Jellyfin access token / API key is required".
        assert not result.ok
        req.assert_not_called(), "missing-token must short-circuit before any HTTP call"
        assert re.search(r"\b(token|API key)\b", result.message, re.IGNORECASE), (
            f"missing-token error must mention 'token' or 'API key', got {result.message!r}"
        )
        assert "required" in result.message.lower()

    def test_unauthorized(self, jelly):
        with patch.object(JellyfinServer, "_request") as req:
            err_response = MagicMock(status_code=401)
            err = requests.exceptions.HTTPError(response=err_response)
            response = MagicMock()
            response.raise_for_status.side_effect = err
            req.return_value = response

            result = jelly.test_connection()

        # Production format: "Jellyfin rejected the access token (401)".
        assert not result.ok
        assert req.call_count == 1, "401 path must hit _request once"
        assert re.search(r"\b401\b", result.message), f"expected '401' as a standalone token in {result.message!r}"
        assert "rejected" in result.message.lower(), f"401 must say the token was rejected, got {result.message!r}"


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
        # Non-transport error → quietly degrade to the public API.
        # Audit fix: ``assert got is None`` alone doesn't distinguish
        # "plugin raised → fell through to base class which also missed"
        # from "early-return None before ever hitting the base class".
        # We assert the fallback was ACTUALLY exercised by checking the
        # call sequence: 1st call = plugin (raises), 2nd+3rd = base
        # class's Pass-1 + Pass-2 ``GET /Items`` searchTerm queries.
        #
        # NOTE: ``RuntimeError`` here exercises the generic-Exception
        # branch in jellyfin.py. The Timeout / ConnectionError branch
        # behaves differently — see
        # ``test_skips_base_fallback_when_plugin_times_out`` (task #51).
        empty_resp = MagicMock()
        empty_resp.json.return_value = {"Items": []}
        empty_resp.raise_for_status.return_value = None
        with patch.object(
            JellyfinServer,
            "_request",
            side_effect=[RuntimeError("malformed plugin response"), empty_resp, empty_resp],
        ) as req:
            got = jelly._uncached_resolve_remote_path_to_item_id("/x.mkv")
            assert got is None  # base class also misses; that's fine for this assertion
            # Three calls total: plugin probe + Pass-1 + Pass-2.
            assert req.call_count == 3, (
                f"expected plugin call + base class Pass-1 + Pass-2 = 3 calls, got {req.call_count} "
                f"(call list: {[c.args for c in req.call_args_list]!r})"
            )
            # First call WAS the plugin probe — proves the plugin path
            # ran and raised before fallback kicked in (rather than the
            # SUT skipping the plugin altogether).
            assert req.call_args_list[0].args == ("GET", "/MediaPreviewBridge/ResolvePath")
            # Subsequent calls are the base class's public-API search,
            # not another attempt at the plugin endpoint — proves the
            # exception-swallow routed into the fallback rather than
            # retrying the plugin in a loop.
            assert req.call_args_list[1].args == ("GET", "/Items")
            assert req.call_args_list[2].args == ("GET", "/Items")

    @pytest.mark.parametrize(
        "transport_exc",
        [
            requests.exceptions.Timeout("plugin call timed out after 30s"),
            requests.exceptions.ConnectTimeout("connect timeout"),
            requests.exceptions.ReadTimeout("read timeout"),
            requests.exceptions.ConnectionError("connection refused / broken pipe"),
        ],
        ids=["Timeout", "ConnectTimeout", "ReadTimeout", "ConnectionError"],
    )
    def test_skips_base_fallback_when_plugin_times_out(self, jelly, transport_exc):
        """Task #51 regression pin — when the plugin call raises a
        transport-level exception (Timeout / ConnectionError), the
        Jellyfin server is unreachable or overloaded. The base
        resolver hits the SAME server with the SAME symptoms — a
        second 30s timeout is wasted. Skip it.

        Live evidence: job baf4f9cc on 2026-05-06 08:36-37 fired 3
        webhooks for Jersey Shore Family Vacation S04 episodes.
        Sonarr's import had triggered a Jellyfin scan at 08:34.
        At 08:36 our webhook hit JellyTest mid-scan and EVERY one of
        the 3 files burned exactly 59.4-59.5s on JellyTest before
        moving on to Plex. 30s × 2 timeouts == 60s.

        Pin: when the plugin call raises a transport-level
        exception, only ONE ``_request`` is dispatched (the plugin
        probe). The base resolver MUST NOT run. The slow-backoff
        retry queue picks the file up later when JellyTest is idle.
        """
        with patch.object(JellyfinServer, "_request", side_effect=[transport_exc]) as req:
            got = jelly._uncached_resolve_remote_path_to_item_id("/x.mkv")
            assert got is None
            # Exactly ONE _request — the plugin probe. Base resolver
            # MUST NOT fire (would be req.call_count >= 3).
            assert req.call_count == 1, (
                f"On transport-level plugin failure, base fallback MUST be skipped to avoid "
                f"a second 30s timeout against the same overloaded server. Got "
                f"{req.call_count} requests: {[c.args for c in req.call_args_list]!r}"
            )
            assert req.call_args_list[0].args == ("GET", "/MediaPreviewBridge/ResolvePath")


class TestResolveOnePathCacheSemantics:
    """``_resolve_one_path`` caches positive results but MUST NOT cache
    negatives.

    Live regression — chain ``62e32c35`` (Jonestown movie,
    2026-05-11 22:30 → 22:38): a ``PUBLISHED_PENDING_REGISTRATION``
    chain was armed because Jellyfin hadn't finished scanning the
    new MKV by the time the originating dispatch resolved its
    item id. The job-level retry path
    (``web/routes/job_runner.py:_spawn_retry_job``) re-runs the
    dispatch against the still-pending paths, sharing the parent's
    :class:`JellyfinServer` instance via the live ``ServerRegistry``
    — and therefore the same ``_reverse_lookup_cache``. With
    negative caching at 300 s TTL,
    retries #1 (T+60 s) and #2 (T+180 s) both hit the cached
    ``None`` and returned in 0.0 s without re-querying — even
    though Jellyfin had completed its scan within the first
    minute. The chain only recovered at attempt #3 (T+480 s) when
    the TTL expired naturally.

    The pin: a ``None`` result from
    :meth:`_uncached_resolve_remote_path_to_item_id` must NEVER
    leave a cache entry behind, so the very next
    :meth:`_resolve_one_path` call re-runs the lookup. Positive
    results retain their TTL cache (the perf win for full-library
    scans is real and unaffected).
    """

    def test_negative_result_is_not_cached(self, jelly):
        path = "/data_16tb2/Movies/Foo (2024)/Foo (2024).mkv"
        with patch.object(JellyfinServer, "_uncached_resolve_remote_path_to_item_id") as uncached:
            uncached.return_value = None

            assert jelly._resolve_one_path(path) is None
            assert jelly._resolve_one_path(path) is None

            # If the cache had absorbed the first ``None``, the second
            # call would short-circuit and ``_uncached`` would only run
            # once. Two calls proves the lookup re-runs.
            assert uncached.call_count == 2, (
                f"Negative result was cached — retry chains would short-circuit on stale ``None`` "
                f"for the full TTL (see TestResolveOnePathCacheSemantics docstring). Expected "
                f"two ``_uncached`` invocations on back-to-back miss-then-miss, got "
                f"{uncached.call_count}."
            )
            # Pin the forwarded kwarg the SUT controls — a future
            # refactor that normalised or stripped the path before
            # forwarding would leave call_count untouched but break
            # the cache key contract.
            assert uncached.call_args_list[0].args == (path,)
            assert uncached.call_args_list[1].args == (path,)

    def test_retry_chain_pattern_negative_then_positive_returns_positive(self, jelly):
        """Simulates the exact retry-chain scenario: the originating
        dispatch's lookup misses (server still scanning), then a
        subsequent attempt on the SAME server instance finds the
        item (server has finished its scan). Pre-fix this returned
        ``None`` from cache; post-fix it must return the freshly
        resolved id.
        """
        path = "/data_16tb2/Movies/Jonestown (2006)/Jonestown (2006).mkv"
        with patch.object(JellyfinServer, "_uncached_resolve_remote_path_to_item_id") as uncached:
            uncached.side_effect = [None, "ecaae1ad830f417baa6c521237e86a64"]

            first = jelly._resolve_one_path(path)
            second = jelly._resolve_one_path(path)

            assert first is None
            assert second == "ecaae1ad830f417baa6c521237e86a64", (
                "Retry attempt re-queried a server that has now indexed the file but the "
                "wrapper returned a stale cached ``None``. The negative result from the "
                "originating dispatch must not survive in the cache."
            )
            assert uncached.call_count == 2
            # Both invocations must forward the exact ``server_view_path``;
            # a regression that transformed the path between calls would
            # silently change the cache key.
            assert uncached.call_args_list[0].args == (path,)
            assert uncached.call_args_list[1].args == (path,)

    def test_positive_result_is_cached_within_ttl(self, jelly):
        """Perf-preservation pin: positive caching must still work so
        full-library scans don't pay the per-path lookup cost twice
        for the same path within the TTL window. Without this, the
        fix would regress the 200K-item-Jellyfin full-scan path the
        original caching policy was written for.
        """
        path = "/data_16tb2/Movies/Foo (2024)/Foo (2024).mkv"
        with patch.object(JellyfinServer, "_uncached_resolve_remote_path_to_item_id") as uncached:
            uncached.return_value = "item-42"

            assert jelly._resolve_one_path(path) == "item-42"
            assert jelly._resolve_one_path(path) == "item-42"

            assert uncached.call_count == 1, (
                f"Positive result was NOT cached — full-library scans would re-pay the lookup "
                f"cost on every duplicate path within the TTL window. Expected one ``_uncached`` "
                f"invocation on back-to-back hits, got {uncached.call_count}."
            )


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

    def test_plugin_registration_uses_adapter_width_and_interval_not_hardcoded_defaults(self):
        """Audit L1 — plugin's ``Trickplay`` endpoint MUST be called
        with the same ``width`` and ``intervalMs`` the trickplay
        adapter wrote tiles for. The plugin's controller checks that
        ``<basename>.trickplay/<width> - 10x10/`` exists on disk
        before registering the trickplay row; mismatched width =
        404 = silent registration miss = trickplay only appears at
        the next 3 AM scheduled scan instead of immediately.

        Pre-fix: the params dict hardcoded ``width=320`` and
        ``intervalMs=10000`` regardless of what
        ``server_config.output`` configured. A user who set
        ``output.width=480`` got 100% silent failure on the plugin
        bridge — same shape as the Plex ``type=`` bug.
        """
        custom_jelly = JellyfinServer(
            _jelly_config(),
        )
        # Inject non-default output settings on the wrapped config so
        # the plugin call has to read them, not fall back to defaults.
        custom_jelly._config.output = {"adapter": "jellyfin_trickplay", "width": 480, "frame_interval": 5}

        plugin_resp = MagicMock(status_code=204, text="")
        refresh_resp = MagicMock()
        refresh_resp.raise_for_status.return_value = None

        with patch.object(JellyfinServer, "_request", side_effect=[plugin_resp, refresh_resp]) as req:
            custom_jelly.trigger_refresh(item_id="42", remote_path=None)

        # The plugin call's params must reflect the adapter's actual
        # configured values (width=480, interval=5s → 5000 ms), NOT
        # the hardcoded defaults that pre-fix landed in production.
        plugin_call = req.call_args_list[0]
        params = plugin_call.kwargs.get("params") or {}
        assert params.get("width") == 480, (
            f"Plugin width must mirror server_config.output.width (480); got {params!r}. "
            "A regression here re-introduces the silent 404 → next-3-AM-scan registration delay."
        )
        assert params.get("intervalMs") == 5000, (
            f"Plugin intervalMs must equal frame_interval * 1000 (5*1000=5000); got {params!r}."
        )

    def test_plugin_registration_falls_back_to_safe_defaults_when_output_missing(self):
        """Belt-and-braces — when ``server_config.output`` is empty
        (older configs, mid-migration), the plugin call still uses
        sensible defaults (width=320, intervalMs=10000) rather than
        raising. The 320/10000 case is the dominant default shape in
        production today, so a user who hasn't touched their settings
        keeps working unchanged.
        """
        bare_jelly = JellyfinServer(_jelly_config())
        bare_jelly._config.output = {}  # no width / frame_interval

        plugin_resp = MagicMock(status_code=204, text="")
        refresh_resp = MagicMock()
        refresh_resp.raise_for_status.return_value = None

        with patch.object(JellyfinServer, "_request", side_effect=[plugin_resp, refresh_resp]) as req:
            bare_jelly.trigger_refresh(item_id="42", remote_path=None)

        params = req.call_args_list[0].kwargs.get("params") or {}
        assert params.get("width") == 320
        assert params.get("intervalMs") == 10000

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

    def test_no_path_or_id_is_a_noop(self, jelly):
        # When neither item_id nor remote_path is supplied there's
        # nothing to refresh — the base wrapper short-circuits and no
        # API call is made. (Old behaviour was to fall back to a full
        # /Library/Refresh; that was an expensive guess fired by no
        # production caller, removed when path-mapping centralisation
        # moved the candidate-walk into the base class.)
        with patch.object(JellyfinServer, "_request") as req:
            jelly.trigger_refresh(item_id=None, remote_path=None)
            req.assert_not_called()

    def test_full_refresh_is_rate_limited_per_server(self, jelly):
        # /Library/Refresh is heavyweight (full library scan, no path
        # filter). The fallback-to-full path fires when the per-path
        # nudge errors — a burst of nudges with the same failure mode
        # must NOT trigger one /Library/Refresh per call or Jellyfin
        # pins for minutes. The cooldown lives on the server instance
        # so concurrent calls collapse to a single scan.
        path_exc = RuntimeError("path nudge failed")
        full_resp = MagicMock(raise_for_status=MagicMock(return_value=None))

        def side_effect(method, endpoint, *args, **kwargs):
            if endpoint == "/Library/Media/Updated":
                raise path_exc
            return full_resp

        with patch.object(JellyfinServer, "_request", side_effect=side_effect) as req:
            jelly.trigger_refresh(item_id=None, remote_path="/some/path.mkv")
            jelly.trigger_refresh(item_id=None, remote_path="/some/path.mkv")
            jelly.trigger_refresh(item_id=None, remote_path="/some/path.mkv")

            # Each call attempts the path-based nudge; only the first
            # is allowed to escalate to /Library/Refresh.
            full_refresh_calls = [c for c in req.call_args_list if c.args == ("POST", "/Library/Refresh")]
            assert len(full_refresh_calls) == 1, f"Expected one full refresh, got {len(full_refresh_calls)}"

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

    def test_no_issues_when_all_recommended_with_plugin(self, jelly):
        """Mode A (plugin installed): scan-extraction OFF is recommended."""
        jelly._media_preview_bridge_installed = True
        all_good = {
            "EnableTrickplayImageExtraction": True,
            "SaveTrickplayWithMedia": True,
            "ExtractTrickplayImagesDuringLibraryScan": False,  # off — plugin handles activation
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

    def test_no_issues_when_all_recommended_without_plugin(self, jelly):
        """Mode B (no plugin): scan-extraction ON is recommended — it's what
        triggers TrickplayProvider to adopt our existing tiles on scan."""
        jelly._media_preview_bridge_installed = False
        all_good = {
            "EnableTrickplayImageExtraction": True,
            "SaveTrickplayWithMedia": True,
            "ExtractTrickplayImagesDuringLibraryScan": True,  # ON — needed for adoption
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

    def test_scan_extraction_recommendation_flips_with_plugin_state(self, jelly):
        """Pin the plugin-aware flag: same library options render as OK or
        as an issue depending solely on whether the plugin is installed."""
        options = {
            "EnableTrickplayImageExtraction": True,
            "SaveTrickplayWithMedia": True,
            "ExtractTrickplayImagesDuringLibraryScan": False,  # Mode A: fine, Mode B: wrong
            "EnableRealtimeMonitor": True,
        }

        def _one_lib_response(*a, **kw):
            return MagicMock(
                status_code=200,
                json=MagicMock(return_value=[{"Name": "Movies", "ItemId": "1", "LibraryOptions": options}]),
                raise_for_status=MagicMock(),
            )

        # Plugin installed → flag=False is the recommendation → no issue.
        jelly._media_preview_bridge_installed = True
        with patch.object(JellyfinServer, "_request", side_effect=_one_lib_response):
            issues_with_plugin = jelly.check_settings_health()
        assert issues_with_plugin == []

        # Plugin absent → flag=False is wrong (Mode B needs True for
        # scan-nudge adoption) → issue raised.
        jelly._media_preview_bridge_installed = False
        with patch.object(JellyfinServer, "_request", side_effect=_one_lib_response):
            issues_without_plugin = jelly.check_settings_health()
        assert any(
            i.flag == "ExtractTrickplayImagesDuringLibraryScan" and i.recommended is True for i in issues_without_plugin
        )

    def test_reports_each_misset_flag_per_library(self, jelly):
        # Pin plugin-installed path so scan-extraction=False is the
        # expected recommendation. Without this, the per-library issue
        # count depends on plugin state (covered separately above).
        jelly._media_preview_bridge_installed = True
        bad_critical = {
            "EnableTrickplayImageExtraction": False,  # critical
            "SaveTrickplayWithMedia": False,  # critical
            "ExtractTrickplayImagesDuringLibraryScan": True,  # recommended (with plugin: want False)
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
        #
        # Pin plugin-installed → scan-extraction=False is the
        # recommendation, keeping this test insensitive to plugin state
        # (covered separately in TestSettingsHealth).
        jelly._media_preview_bridge_installed = True
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
        # Pin plugin-installed so scan-ext=False matches the recommendation.
        jelly._media_preview_bridge_installed = True
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


class TestUninstallPlugin:
    """uninstall_plugin: DELETE /Plugins/{guid} + restart Jellyfin.

    Uses /Plugins/{guid} NOT /Packages/{guid} — the latter returns 405
    (regression pinned here: a user's live Jellyfin returned 405 when
    the older /Packages path shipped; /Plugins is the Jellyfin-correct
    assembly-GUID uninstall endpoint).

    404 on the DELETE is treated as success (already-gone is the
    desired end state). Restart failure leaves ``ok=True`` because
    the plugin was removed — it just won't unload visibly until
    next manual restart.
    """

    def test_happy_path_deletes_then_restarts(self, jelly):
        responses: dict[tuple[str, str], MagicMock] = {
            ("DELETE", f"/Plugins/{JellyfinServer.PLUGIN_GUID}"): MagicMock(
                status_code=204, raise_for_status=MagicMock()
            ),
            ("POST", "/System/Restart"): MagicMock(status_code=204, raise_for_status=MagicMock()),
        }

        def fake_request(method, url, **kwargs):
            return responses[(method, url)]

        with patch.object(JellyfinServer, "_request", side_effect=fake_request) as req:
            result = jelly.uninstall_plugin()

        assert result["ok"] is True
        steps = {s["step"] for s in result["steps"]}
        assert steps == {"uninstall_package", "restart"}
        # Both endpoints were hit, in order.
        assert req.call_args_list[0].args == ("DELETE", f"/Plugins/{JellyfinServer.PLUGIN_GUID}")
        assert req.call_args_list[1].args == ("POST", "/System/Restart")

    def test_treats_404_as_success(self, jelly):
        """Uninstalling a plugin that's already gone is the desired end state —
        no error, restart still fires, ok=True. If this regresses, users
        will see "uninstall failed" on a redundant click."""
        responses: dict[tuple[str, str], MagicMock] = {
            ("DELETE", f"/Plugins/{JellyfinServer.PLUGIN_GUID}"): MagicMock(
                status_code=404, raise_for_status=MagicMock()
            ),
            ("POST", "/System/Restart"): MagicMock(status_code=204, raise_for_status=MagicMock()),
        }

        def fake_request(method, url, **kwargs):
            return responses[(method, url)]

        with patch.object(JellyfinServer, "_request", side_effect=fake_request):
            result = jelly.uninstall_plugin()

        assert result["ok"] is True
        uninstall_step = next(s for s in result["steps"] if s["step"] == "uninstall_package")
        assert uninstall_step["ok"] is True
        assert "already" in uninstall_step["detail"].lower()

    def test_restart_failure_still_reports_package_removed(self, jelly):
        """Plugin removed but the restart call errored — treat as success
        (ok=True) because the package IS gone, it just won't unload
        visibly until the user restarts Jellyfin themselves. This matches
        install_plugin's symmetrical behaviour."""

        def fake_request(method, url, **kwargs):
            if method == "DELETE":
                return MagicMock(status_code=204, raise_for_status=MagicMock())
            if method == "POST":
                raise RuntimeError("restart endpoint unavailable")
            raise AssertionError(f"unexpected {method} {url}")

        with patch.object(JellyfinServer, "_request", side_effect=fake_request):
            result = jelly.uninstall_plugin()

        assert result["ok"] is True
        assert "restart failed" in result["error"]

    def test_uses_plugins_endpoint_not_packages(self, jelly):
        """Regression pin: the first URL segment MUST be /Plugins, not /Packages.

        A user's live Jellyfin returned HTTP 405 Method Not Allowed when
        /Packages/{guid} was used — Jellyfin's /Packages endpoint is the
        install-catalogue API and doesn't accept DELETE. The correct
        uninstall endpoint keys off the plugin's assembly GUID at
        /Plugins/{guid}. This test exists specifically so that bug
        never ships again."""

        captured_urls: list[str] = []

        def fake_request(method, url, **kwargs):
            captured_urls.append(f"{method} {url}")
            if method == "DELETE":
                return MagicMock(status_code=204, raise_for_status=MagicMock())
            if method == "POST":
                return MagicMock(status_code=204, raise_for_status=MagicMock())
            raise AssertionError(f"unexpected {method} {url}")

        with patch.object(JellyfinServer, "_request", side_effect=fake_request):
            jelly.uninstall_plugin()

        # Exact list equality — protects against: wrong URL path
        # (/Packages), wrong method (POST), trailing slashes, accidental
        # query-string suffixes, or reordering where the restart happens
        # before the delete. Any one of these would have returned 405 or
        # worse on the live Jellyfin that produced the bug report.
        assert captured_urls == [
            f"DELETE /Plugins/{JellyfinServer.PLUGIN_GUID}",
            "POST /System/Restart",
        ], (
            "uninstall_plugin MUST target /Plugins/{guid} in that exact order — "
            "using /Packages/{guid} returned HTTP 405 on the live Jellyfin that "
            f"surfaced this bug. Got call sequence: {captured_urls!r}"
        )


class TestApplyFlagValues:
    """apply_flag_values sets flags to explicit values across libraries.

    Symmetric with apply_recommended_settings but accepts explicit
    booleans so disable-toggles on the Previews readiness card work.
    """

    def test_flips_flag_away_from_recommended(self, jelly):
        """Users need to DISABLE flags too — not just flip them to recommended.
        apply_flag_values must accept value=False and forward that."""
        current_options = {
            "EnableTrickplayImageExtraction": True,
            "SaveTrickplayWithMedia": True,
            "ExtractTrickplayImagesDuringLibraryScan": False,
            "EnableRealtimeMonitor": True,
        }
        get_response = MagicMock(
            status_code=200,
            json=MagicMock(return_value=[{"Name": "Movies", "ItemId": "m", "LibraryOptions": current_options}]),
            raise_for_status=MagicMock(),
        )
        post_response = MagicMock(status_code=204, raise_for_status=MagicMock())

        def fake_request(method, url, **kwargs):
            return get_response if method == "GET" else post_response

        targets = [{"flag": "EnableRealtimeMonitor", "value": False, "library_ids": None}]
        with patch.object(JellyfinServer, "_request", side_effect=fake_request) as req:
            results = jelly.apply_flag_values(targets)

        assert results == {"m:EnableRealtimeMonitor": "ok"}
        post_call = next(c for c in req.call_args_list if c.args[0] == "POST")
        sent_options = post_call.kwargs["json_body"]["LibraryOptions"]
        # Explicitly flipped to False — this is the whole point of apply_flag_values.
        assert sent_options["EnableRealtimeMonitor"] is False
        # Other flags preserved (wholesale replace contract).
        assert sent_options["EnableTrickplayImageExtraction"] is True

    def test_scopes_to_specific_library(self, jelly):
        """library_ids=[id] restricts the update to exactly that library."""
        opts = {"EnableRealtimeMonitor": True}
        get_response = MagicMock(
            status_code=200,
            json=MagicMock(
                return_value=[
                    {"Name": "Movies", "ItemId": "m", "LibraryOptions": opts},
                    {"Name": "TV", "ItemId": "t", "LibraryOptions": opts},
                ]
            ),
            raise_for_status=MagicMock(),
        )
        post_response = MagicMock(status_code=204, raise_for_status=MagicMock())

        def fake_request(method, url, **kwargs):
            return get_response if method == "GET" else post_response

        targets = [{"flag": "EnableRealtimeMonitor", "value": False, "library_ids": ["m"]}]
        with patch.object(JellyfinServer, "_request", side_effect=fake_request) as req:
            results = jelly.apply_flag_values(targets)

        # Only Movies got touched.
        assert results == {"m:EnableRealtimeMonitor": "ok"}
        post_calls = [c for c in req.call_args_list if c.args[0] == "POST"]
        assert len(post_calls) == 1
        assert post_calls[0].kwargs["json_body"]["Id"] == "m"

    def test_no_ops_when_flag_already_at_requested_value(self, jelly):
        """Same-value short-circuit — avoids spurious POSTs."""
        opts = {"EnableRealtimeMonitor": False}
        get_response = MagicMock(
            status_code=200,
            json=MagicMock(return_value=[{"Name": "Movies", "ItemId": "m", "LibraryOptions": opts}]),
            raise_for_status=MagicMock(),
        )

        def fake_request(method, url, **kwargs):
            if method == "GET":
                return get_response
            raise AssertionError("POST should NOT be called when state matches")

        with patch.object(JellyfinServer, "_request", side_effect=fake_request):
            targets = [{"flag": "EnableRealtimeMonitor", "value": False, "library_ids": None}]
            results = jelly.apply_flag_values(targets)
        assert results == {}

    def test_empty_targets_returns_empty(self, jelly):
        assert jelly.apply_flag_values([]) == {}


class TestPreviewsReadinessJellyfin:
    """Unified previews_readiness envelope — shape, overall_ok derivation,
    and per-check ``actions`` metadata (esp. destructive ``confirm``)."""

    def _wire_healthy(self, jelly):
        """Wire a fully healthy Jellyfin response — plugin installed,
        all flags correct, TrickplayOptions matches adapter geometry."""
        jelly._media_preview_bridge_installed = True
        good_options = {
            "EnableTrickplayImageExtraction": True,
            "SaveTrickplayWithMedia": True,
            "ExtractTrickplayImagesDuringLibraryScan": False,
            "EnableRealtimeMonitor": True,
        }
        good_trickplay = {
            "TileWidth": 10,
            "TileHeight": 10,
            "Interval": 10000,
            "WidthResolutions": [320],
        }

        def fake_request(method, url, **kwargs):
            if url == "/MediaPreviewBridge/Ping":
                return MagicMock(
                    status_code=200,
                    json=MagicMock(return_value={"ok": True, "version": "10.11.0.2"}),
                )
            if url == "/System/Info":
                return MagicMock(
                    status_code=200,
                    json=MagicMock(return_value={"Version": "10.11.8"}),
                    raise_for_status=MagicMock(),
                )
            if url == "/System/Configuration":
                return MagicMock(
                    status_code=200,
                    json=MagicMock(return_value={"TrickplayOptions": good_trickplay}),
                    raise_for_status=MagicMock(),
                )
            if url == "/Library/VirtualFolders":
                return MagicMock(
                    status_code=200,
                    json=MagicMock(return_value=[{"Name": "Movies", "ItemId": "m", "LibraryOptions": good_options}]),
                    raise_for_status=MagicMock(),
                )
            if url == "/ScheduledTasks":
                # Healthy Jellyfin for a Mode-A user (plugin installed):
                # daily scheduled trickplay task is DISABLED → optimal,
                # no duplicate work. Matches the recommended-state row
                # the readiness card emits.
                return MagicMock(
                    status_code=200,
                    json=MagicMock(
                        return_value=[
                            {
                                "Name": "Generate Trickplay Images",
                                "Key": "RefreshTrickplayImages",
                                "Id": "sched-trickplay-id",
                                "Triggers": [],
                                "State": "Idle",
                                "Description": "Creates trickplay previews for videos.",
                            }
                        ]
                    ),
                    raise_for_status=MagicMock(),
                )
            raise AssertionError(f"unexpected {method} {url}")

        return fake_request

    def test_unified_envelope_shape(self, jelly):
        with patch.object(JellyfinServer, "_request", side_effect=self._wire_healthy(jelly)):
            payload = jelly.previews_readiness()

        assert payload["vendor"] == "jellyfin"
        assert payload["overall_ok"] is True
        section_ids = [s["id"] for s in payload["sections"]]
        # Jellyfin emits: connection, version, plugin, library_settings,
        # server_options, vendor_extraction.
        assert "connection" in section_ids
        assert "version" in section_ids
        assert "plugin" in section_ids
        assert "library_settings" in section_ids
        assert "server_options" in section_ids
        assert "vendor_extraction" in section_ids

        # Every check has the canonical keys the frontend walks.
        for section in payload["sections"]:
            assert "id" in section
            assert "title" in section
            assert "docs_anchor" in section
            assert "ok" in section
            assert "checks" in section
            for check in section["checks"]:
                assert "id" in check
                assert "label" in check
                assert "tooltip" in check
                assert "ok" in check
                assert "severity" in check
                assert "actions" in check

    def test_destructive_flags_carry_confirm_payload(self, jelly):
        """EnableTrickplayImageExtraction=false deletes published tiles.
        The disable action MUST carry a type-confirm blob with the exact
        phrase 'disable trickplay'. Removing the confirm from the code
        would let a click-through footgun into production."""
        jelly._media_preview_bridge_installed = True
        # Library with EnableTrickplayImageExtraction currently TRUE
        # (so the disable toggle renders).
        bad_options = {
            "EnableTrickplayImageExtraction": True,
            "SaveTrickplayWithMedia": True,
            "ExtractTrickplayImagesDuringLibraryScan": False,
            "EnableRealtimeMonitor": True,
        }
        good_trickplay = {
            "TileWidth": 10,
            "TileHeight": 10,
            "Interval": 10000,
            "WidthResolutions": [320],
        }

        def fake_request(method, url, **kwargs):
            if url == "/MediaPreviewBridge/Ping":
                return MagicMock(
                    status_code=200,
                    json=MagicMock(return_value={"ok": True, "version": "10.11.0.2"}),
                )
            if url == "/System/Info":
                return MagicMock(
                    status_code=200,
                    json=MagicMock(return_value={"Version": "10.11.8"}),
                    raise_for_status=MagicMock(),
                )
            if url == "/System/Configuration":
                return MagicMock(
                    status_code=200,
                    json=MagicMock(return_value={"TrickplayOptions": good_trickplay}),
                    raise_for_status=MagicMock(),
                )
            if url == "/Library/VirtualFolders":
                return MagicMock(
                    status_code=200,
                    json=MagicMock(return_value=[{"Name": "Movies", "ItemId": "m", "LibraryOptions": bad_options}]),
                    raise_for_status=MagicMock(),
                )
            raise AssertionError(f"unexpected {method} {url}")

        with patch.object(JellyfinServer, "_request", side_effect=fake_request):
            payload = jelly.previews_readiness()

        library_section = next(s for s in payload["sections"] if s["id"] == "library_settings")
        # Find the EnableTrickplayImageExtraction row.
        enable_tp_row = next(
            c for c in library_section["checks"] if c["meta"].get("flag") == "EnableTrickplayImageExtraction"
        )
        disable_action = enable_tp_row["actions"]["disable"]
        confirm = disable_action["confirm"]
        assert confirm is not None, (
            "disabling EnableTrickplayImageExtraction is DATA-DESTRUCTIVE "
            "(Jellyfin deletes published tiles) — MUST carry a confirm payload"
        )
        assert confirm["kind"] == "type"
        assert confirm["phrase"] == "disable trickplay"
        # Body must cite the deletion so users understand the risk.
        assert "DELETE" in confirm["body"] or "delete" in confirm["body"].lower()

    def test_plugin_uninstall_carries_confirm_payload(self, jelly):
        """The 'Uninstall plugin' toggle needs a confirm — not type-to-confirm
        (not data-destructive), but a button confirm so an accidental
        click doesn't restart Jellyfin unexpectedly."""
        with patch.object(JellyfinServer, "_request", side_effect=self._wire_healthy(jelly)):
            payload = jelly.previews_readiness()

        plugin_section = next(s for s in payload["sections"] if s["id"] == "plugin")
        row = plugin_section["checks"][0]
        disable = row["actions"]["disable"]
        assert disable["action"] == "uninstall_plugin"
        assert disable["confirm"] is not None
        assert disable["confirm"]["kind"] == "button"

    def test_plugin_absent_with_mode_a_library_is_critical(self, jelly):
        """Plugin 404 + any library with ExtractTrickplayImagesDuringLibraryScan=false
        is a hard failure — without the plugin, nothing adopts our tiles and
        scrubbing previews never render. The row MUST be red X (ok=False,
        severity=critical), not the prior hardcoded green tick."""
        jelly._media_preview_bridge_installed = False
        mode_a_options = {
            "EnableTrickplayImageExtraction": True,
            "SaveTrickplayWithMedia": True,
            "ExtractTrickplayImagesDuringLibraryScan": False,
            "EnableRealtimeMonitor": True,
        }
        good_trickplay = {"TileWidth": 10, "TileHeight": 10, "Interval": 10000, "WidthResolutions": [320]}

        def fake_request(method, url, **kwargs):
            if url == "/MediaPreviewBridge/Ping":
                return MagicMock(status_code=404, json=MagicMock(return_value={}))
            if url == "/System/Info":
                return MagicMock(
                    status_code=200,
                    json=MagicMock(return_value={"Version": "10.11.8"}),
                    raise_for_status=MagicMock(),
                )
            if url == "/System/Configuration":
                return MagicMock(
                    status_code=200,
                    json=MagicMock(return_value={"TrickplayOptions": good_trickplay}),
                    raise_for_status=MagicMock(),
                )
            if url == "/Library/VirtualFolders":
                return MagicMock(
                    status_code=200,
                    json=MagicMock(return_value=[{"Name": "Movies", "ItemId": "m", "LibraryOptions": mode_a_options}]),
                    raise_for_status=MagicMock(),
                )
            raise AssertionError(f"unexpected {method} {url}")

        with patch.object(JellyfinServer, "_request", side_effect=fake_request):
            payload = jelly.previews_readiness()

        plugin_section = next(s for s in payload["sections"] if s["id"] == "plugin")
        assert plugin_section["ok"] is False, (
            "plugin-absent + Mode A library must fail — the on-disk tiles sit invisible forever. "
            "Prior regression hardcoded ok=True and hid the real cause of missing previews."
        )
        assert plugin_section["severity"] == "critical"
        row = plugin_section["checks"][0]
        assert row["ok"] is False
        assert row["severity"] == "critical"
        assert "Movies" in row["reason"]
        assert row["meta"]["plugin_required"] is True
        assert "Movies" in row["meta"]["mode_a_libraries"]
        # overall_ok must fall through — plugin is a real block now.
        assert payload["overall_ok"] is False

    def test_plugin_absent_with_all_mode_b_libraries_is_ok(self, jelly):
        """Plugin 404 is fine when every library has scan-extraction enabled
        (Mode B — Jellyfin adopts tiles on its own next scan). The row
        stays a green tick with info severity."""
        jelly._media_preview_bridge_installed = False
        mode_b_options = {
            "EnableTrickplayImageExtraction": True,
            "SaveTrickplayWithMedia": True,
            "ExtractTrickplayImagesDuringLibraryScan": True,
            "EnableRealtimeMonitor": True,
        }
        good_trickplay = {"TileWidth": 10, "TileHeight": 10, "Interval": 10000, "WidthResolutions": [320]}

        def fake_request(method, url, **kwargs):
            if url == "/MediaPreviewBridge/Ping":
                return MagicMock(status_code=404, json=MagicMock(return_value={}))
            if url == "/System/Info":
                return MagicMock(
                    status_code=200,
                    json=MagicMock(return_value={"Version": "10.11.8"}),
                    raise_for_status=MagicMock(),
                )
            if url == "/System/Configuration":
                return MagicMock(
                    status_code=200,
                    json=MagicMock(return_value={"TrickplayOptions": good_trickplay}),
                    raise_for_status=MagicMock(),
                )
            if url == "/Library/VirtualFolders":
                return MagicMock(
                    status_code=200,
                    json=MagicMock(return_value=[{"Name": "Movies", "ItemId": "m", "LibraryOptions": mode_b_options}]),
                    raise_for_status=MagicMock(),
                )
            raise AssertionError(f"unexpected {method} {url}")

        with patch.object(JellyfinServer, "_request", side_effect=fake_request):
            payload = jelly.previews_readiness()

        plugin_section = next(s for s in payload["sections"] if s["id"] == "plugin")
        assert plugin_section["ok"] is True
        assert plugin_section["severity"] == "info"
        row = plugin_section["checks"][0]
        assert row["ok"] is True
        assert row["meta"]["plugin_required"] is False

    def test_plugin_absent_with_no_libraries_is_ok(self, jelly):
        """Matrix coverage: plugin absent + ``/Library/VirtualFolders``
        returns an empty list (fresh install, or admin hasn't created
        libraries yet). No libraries means no Mode A dependency, so the
        plugin is truly optional and the row stays green.

        Regression guard for someone inverting the plugin-required
        condition (``not mode_a_library_names`` instead of
        ``bool(mode_a_library_names)``) — that would flip the empty-libs
        user to a red critical row with no test to catch it."""
        jelly._media_preview_bridge_installed = False
        good_trickplay = {"TileWidth": 10, "TileHeight": 10, "Interval": 10000, "WidthResolutions": [320]}

        def fake_request(method, url, **kwargs):
            if url == "/MediaPreviewBridge/Ping":
                return MagicMock(status_code=404, json=MagicMock(return_value={}))
            if url == "/System/Info":
                return MagicMock(
                    status_code=200,
                    json=MagicMock(return_value={"Version": "10.11.8"}),
                    raise_for_status=MagicMock(),
                )
            if url == "/System/Configuration":
                return MagicMock(
                    status_code=200,
                    json=MagicMock(return_value={"TrickplayOptions": good_trickplay}),
                    raise_for_status=MagicMock(),
                )
            if url == "/Library/VirtualFolders":
                return MagicMock(
                    status_code=200,
                    json=MagicMock(return_value=[]),
                    raise_for_status=MagicMock(),
                )
            raise AssertionError(f"unexpected {method} {url}")

        with patch.object(JellyfinServer, "_request", side_effect=fake_request):
            payload = jelly.previews_readiness()

        plugin_section = next(s for s in payload["sections"] if s["id"] == "plugin")
        assert plugin_section["ok"] is True
        assert plugin_section["severity"] == "info"
        row = plugin_section["checks"][0]
        assert row["meta"]["plugin_required"] is False
        assert row["meta"]["mode_a_libraries"] == []

    def test_plugin_installed_regression_guard(self, jelly):
        """With plugin installed, the row stays green regardless of library mode.
        Regression guard against the Mode A/B logic accidentally flipping green→red
        when the plugin probe is actually healthy."""
        with patch.object(JellyfinServer, "_request", side_effect=self._wire_healthy(jelly)):
            payload = jelly.previews_readiness()

        plugin_section = next(s for s in payload["sections"] if s["id"] == "plugin")
        assert plugin_section["ok"] is True
        assert plugin_section["severity"] == "info"
        assert plugin_section["checks"][0]["ok"] is True

    def test_vendor_extraction_probe_failure_is_not_green(self, jelly):
        """When get_vendor_extraction_status raises, the section MUST report
        ok=False (severity=info — not critical, since a missed probe doesn't
        break playback, but the UI must stop lying about state it can't read).
        Prior regression hardcoded ok=True and rendered a green tick."""
        with (
            patch.object(JellyfinServer, "_request", side_effect=self._wire_healthy(jelly)),
            patch.object(
                JellyfinServer,
                "get_vendor_extraction_status",
                side_effect=RuntimeError("boom"),
            ),
        ):
            payload = jelly.previews_readiness()

        vendor = next(s for s in payload["sections"] if s["id"] == "vendor_extraction")
        assert vendor["ok"] is False
        assert vendor["severity"] == "info"
        row = vendor["checks"][0]
        assert row["ok"] is False
        assert row["severity"] == "info"
        assert "boom" in (row["reason"] or "")
        assert row["current"] == "unknown (probe failed)"

    def test_legacy_trickplay_readiness_alias_still_works(self, jelly):
        """External tools that pin /trickplay-readiness must keep working.
        The alias must still return the legacy shape (plugin.mode,
        trickplay_options, library_settings.issues, etc.)."""
        with patch.object(JellyfinServer, "_request", side_effect=self._wire_healthy(jelly)):
            payload = jelly.trickplay_readiness()

        # Legacy shape: top-level version/plugin/library_settings/trickplay_options.
        assert "version" in payload
        assert "plugin" in payload
        assert payload["plugin"]["mode"] == "plugin_instant"
        assert "library_settings" in payload
        assert "trickplay_options" in payload
        assert payload["overall_ok"] is True


class TestScheduledTrickplayTaskReadiness:
    """Matrix coverage for the new scheduled-trickplay readiness check.

    Two branching variables; four cells. Bug shape #8 ("cover the matrix,
    not one cell") demands every cell that produces different downstream
    behaviour gets a row. The recommendation flips on plugin state:
      * Plugin installed (Mode A): daily task is duplicate CPU →
        recommend disable.
      * Plugin NOT installed (Mode B): daily task is the ONLY
        registration path → flag disable-with-no-triggers as critical.

    Each test pins the load-bearing kwargs the SUT controls — the
    severity, current/recommended copy, and the action shape. A test
    that only checked ``section in payload`` would miss a regression
    that flipped Mode A's recommendation to "keep enabled" (the entire
    point of the check).
    """

    def _wire(self, jelly, *, plugin_installed: bool, triggers_count: int, state: str = "Idle"):
        """Build a fake `_request` side-effect for one matrix cell.

        Stubs only the URLs the readiness path hits — plugin-ping,
        version, config, libraries, scheduled-tasks. Other readiness
        sub-probes (library options, server-wide trickplay geometry)
        are wired to healthy responses so the test isolates the
        scheduled-task row.
        """
        good_lib_options = {
            "EnableTrickplayImageExtraction": True,
            "SaveTrickplayWithMedia": True,
            "ExtractTrickplayImagesDuringLibraryScan": False,
            "EnableRealtimeMonitor": True,
        }
        good_trickplay = {"TileWidth": 10, "TileHeight": 10, "Interval": 10000, "WidthResolutions": [320]}
        triggers = [{"Type": "DailyTrigger", "TimeOfDayTicks": 108_000_000_000}] * triggers_count

        def fake_request(method, url, **kwargs):
            if url == "/MediaPreviewBridge/Ping":
                if not plugin_installed:
                    return MagicMock(status_code=404, json=MagicMock(return_value={}))
                return MagicMock(status_code=200, json=MagicMock(return_value={"ok": True, "version": "10.11.0.2"}))
            if url == "/System/Info":
                return MagicMock(
                    status_code=200,
                    json=MagicMock(return_value={"Version": "10.11.8"}),
                    raise_for_status=MagicMock(),
                )
            if url == "/System/Configuration":
                return MagicMock(
                    status_code=200,
                    json=MagicMock(return_value={"TrickplayOptions": good_trickplay}),
                    raise_for_status=MagicMock(),
                )
            if url == "/Library/VirtualFolders":
                return MagicMock(
                    status_code=200,
                    json=MagicMock(
                        return_value=[{"Name": "Movies", "ItemId": "m", "LibraryOptions": good_lib_options}]
                    ),
                    raise_for_status=MagicMock(),
                )
            if url == "/ScheduledTasks":
                return MagicMock(
                    status_code=200,
                    json=MagicMock(
                        return_value=[
                            {
                                "Name": "Generate Trickplay Images",
                                "Key": "RefreshTrickplayImages",
                                "Id": "sched-trickplay-id",
                                "Triggers": triggers,
                                "State": state,
                                "Description": "Creates trickplay previews for videos.",
                            }
                        ]
                    ),
                    raise_for_status=MagicMock(),
                )
            raise AssertionError(f"unexpected {method} {url}")

        return fake_request

    def test_mode_a_with_triggers_recommends_disable(self, jelly):
        """Plugin installed + task armed → Recommended severity, disable button present."""
        with patch.object(
            JellyfinServer, "_request", side_effect=self._wire(jelly, plugin_installed=True, triggers_count=1)
        ):
            payload = jelly.previews_readiness()

        sched = next(s for s in payload["sections"] if s["id"] == "scheduled_trickplay")
        assert sched["severity"] == "recommended", sched
        assert sched["ok"] is False
        check = sched["checks"][0]
        # The disable action MUST exist and carry the right body — a
        # bug-blind ``"disable" in actions`` would pass if a future
        # refactor renamed the args key. Pin the args shape.
        assert check["actions"]["disable"]["action"] == "set_scheduled_trickplay"
        assert check["actions"]["disable"]["args"] == {"enabled": False}
        # Mode A row's recommended copy must mention the plugin so users
        # understand why disabling is safe for them specifically.
        assert "Bridge plugin" in (check["recommended"] or "")
        # The explicit fix_action hint MUST point at "disable" so the
        # JS direction-picker picks the right action key. The truthy
        # string ``recommended`` would otherwise trigger the boolean
        # fallback at the JS side and pick "enable" — silently doing
        # the OPPOSITE of the row's recommendation. Pin the contract
        # so a regression that drops the hint fails this test.
        assert check["fix_action"] == "disable", (
            f"Mode A + armed row must declare fix_action='disable'; got {check.get('fix_action')!r}"
        )

    def test_mode_a_without_triggers_is_all_good(self, jelly):
        """Plugin installed + task disabled → info severity, ok=True."""
        with patch.object(
            JellyfinServer, "_request", side_effect=self._wire(jelly, plugin_installed=True, triggers_count=0)
        ):
            payload = jelly.previews_readiness()

        sched = next(s for s in payload["sections"] if s["id"] == "scheduled_trickplay")
        assert sched["severity"] == "info"
        assert sched["ok"] is True
        check = sched["checks"][0]
        # No disable button (already disabled) but enable available
        # for users who plan to uninstall the plugin.
        assert "disable" not in check["actions"]
        assert "enable" in check["actions"]

    def test_mode_b_with_triggers_is_informational(self, jelly):
        """No plugin + task armed → info severity, NO actions (don't
        offer disable because it would break registration)."""
        with patch.object(
            JellyfinServer, "_request", side_effect=self._wire(jelly, plugin_installed=False, triggers_count=1)
        ):
            payload = jelly.previews_readiness()

        sched = next(s for s in payload["sections"] if s["id"] == "scheduled_trickplay")
        check = sched["checks"][0]
        assert check["severity"] == "info"
        assert check["ok"] is True
        # CRITICAL: no disable action when plugin is missing — disabling
        # the task in Mode B silently breaks trickplay registration.
        # If this assertion ever fires, the UI has a footgun.
        assert "disable" not in check["actions"], (
            "Mode B (no plugin) MUST NOT offer a disable action — the task is "
            "the only registration path. UI would let users silently break "
            "their trickplay setup."
        )

    def test_mode_b_without_triggers_is_critical(self, jelly):
        """No plugin + task disabled → critical: trickplay broken silently."""
        with patch.object(
            JellyfinServer, "_request", side_effect=self._wire(jelly, plugin_installed=False, triggers_count=0)
        ):
            payload = jelly.previews_readiness()

        sched = next(s for s in payload["sections"] if s["id"] == "scheduled_trickplay")
        check = sched["checks"][0]
        assert check["severity"] == "critical", check
        assert check["ok"] is False
        # The enable action must be present so the user can fix this.
        assert check["actions"]["enable"]["args"] == {"enabled": True}
        # Explicit fix_action hint = re-enable (Mode B + no triggers).
        assert check["fix_action"] == "enable", (
            f"Mode B + disabled row must declare fix_action='enable'; got {check.get('fix_action')!r}"
        )
        assert payload["overall_ok"] is False, (
            "Critical-severity scheduled-task row must trip overall_ok so the badge surfaces the breakage."
        )


class TestRegistryWiring:
    def test_registry_can_construct_jellyfin_server(self):
        """Audit fix — was instantiation-only smoke. Now also asserts
        configured fields survive the registry round-trip."""
        from media_preview_generator.servers import ServerRegistry, ServerType

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
        srv = servers[0]
        assert isinstance(srv, JellyfinServer)
        assert srv.type is ServerType.JELLYFIN
        assert srv.id == "jelly-1"
        cfg = registry.get_config("jelly-1")
        assert cfg is not None
        assert cfg.url == "http://jellyfin:8096"
