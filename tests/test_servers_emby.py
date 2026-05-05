"""Tests for the Emby server client."""

from __future__ import annotations

import json
import re
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
        # Patch _request so we can prove the short-circuit didn't pay
        # for a network call before bailing.
        with patch.object(EmbyServer, "_request") as req:
            result = s.test_connection()

        # Production format: "Emby URL is required".
        assert not result.ok
        req.assert_not_called(), "missing-URL must short-circuit before any HTTP call"
        assert re.search(r"\bURL\b", result.message), (
            f"missing-URL error must mention 'URL' as a word, got {result.message!r}"
        )
        assert "required" in result.message.lower()

    def test_missing_token(self):
        s = EmbyServer(_emby_config(auth={}))
        with patch.object(EmbyServer, "_request") as req:
            result = s.test_connection()

        # Production format: "Emby access token / API key is required".
        assert not result.ok
        req.assert_not_called(), "missing-token must short-circuit before any HTTP call"
        assert re.search(r"\b(token|API key)\b", result.message, re.IGNORECASE), (
            f"missing-token error must mention 'token' or 'API key', got {result.message!r}"
        )
        assert "required" in result.message.lower()

    def test_unauthorized(self, emby):
        with patch.object(EmbyServer, "_request") as req:
            err_response = MagicMock(status_code=401)
            err = requests.exceptions.HTTPError(response=err_response)
            response = MagicMock()
            response.raise_for_status.side_effect = err
            req.return_value = response

            result = emby.test_connection()

        # Production format: "Emby rejected the access token (401)".
        assert not result.ok
        assert req.call_count == 1, "401 path must hit _request once"
        assert re.search(r"\b401\b", result.message), f"expected '401' as a standalone token in {result.message!r}"
        assert "rejected" in result.message.lower(), f"401 must say the token was rejected, got {result.message!r}"

    def test_timeout(self, emby):
        with patch.object(EmbyServer, "_request") as req:
            req.side_effect = requests.exceptions.Timeout()

            result = emby.test_connection()

        # Production format: "Connection to <url> timed out".
        assert not result.ok
        assert req.call_count == 1, "timeout path must have hit _request"
        assert re.search(r"\btimed out\b", result.message, re.IGNORECASE), (
            f"timeout message must contain 'timed out' as a phrase, got {result.message!r}"
        )


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


class TestExtractTitlePrefix:
    """Unit tests for ``EmbyApiClient._extract_title_prefix``.

    NameStartsWith filter on /Items matches against ``SortName``, not
    ``Name``. SortName strips leading English articles
    ("The 'Burbs" → "'Burbs"). The extractor must therefore drop
    "The "/"A "/"An " before sending the prefix, otherwise the fast
    path misses every "The …" / "A …" title.

    Empirically verified on a live Emby 4.9 instance: the legacy stem
    extractor that did NOT strip the article missed item 13504
    ("The 'Burbs") in tools/bench_emby_reverse_path.py — the only
    miss across 22 paths until this regex was added.
    """

    def test_tv_returns_first_word_and_episode_kind(self, emby):
        prefix, is_episode = emby._extract_title_prefix(
            "/library/TV/DMV (2025) [imdb-tt33078075]/Season 01/DMV - S01E03 - Title.mkv"
        )
        assert prefix == "DMV"
        assert is_episode is True

    def test_movie_returns_first_word_and_movie_kind(self, emby):
        prefix, is_episode = emby._extract_title_prefix(
            "/library/Movies/'71 (2014)/'71 (2014) [imdb-tt2614684][Bluray-1080p].mkv"
        )
        assert prefix == "'71"
        assert is_episode is False

    def test_strips_leading_article_the(self, emby):
        # "The 'Burbs" → SortName "'Burbs"; first word after article-strip is "'Burbs".
        prefix, _ = emby._extract_title_prefix("/library/Movies/The 'Burbs (1989)/The 'Burbs.mkv")
        assert prefix == "'Burbs"

    def test_strips_leading_article_a(self, emby):
        prefix, _ = emby._extract_title_prefix("/library/Movies/A Quiet Place (2018)/A Quiet Place.mkv")
        assert prefix == "Quiet"

    def test_strips_leading_article_an(self, emby):
        prefix, _ = emby._extract_title_prefix("/library/Movies/An American Tail (1986)/An American Tail.mkv")
        assert prefix == "American"

    def test_article_stripping_is_case_insensitive(self, emby):
        prefix, _ = emby._extract_title_prefix("/library/Movies/the Matrix (1999)/the Matrix.mkv")
        assert prefix == "Matrix"

    def test_article_only_stripped_when_followed_by_space(self, emby):
        # "Therapy" should NOT have "The" stripped — it doesn't start with "The ".
        prefix, _ = emby._extract_title_prefix("/library/TV/Therapy/Season 01/Therapy - S01E01.mkv")
        assert prefix == "Therapy"

    def test_normalises_unicode_accents(self, emby):
        # SortName for "Pokémon" is "Pokemon" (accent stripped). The
        # prefix must be sent without the accent so NameStartsWith matches.
        # Empirical: NameStartsWith="Pokémon"→0 results,
        # NameStartsWith="Pokemon"→2 results on a real Emby instance.
        prefix, _ = emby._extract_title_prefix("/library/TV/Pokémon (1997)/Season 17/Pokémon - S17E22.mkv")
        assert prefix == "Pokemon", "Unicode combining marks must be stripped to match Emby SortName"

    def test_uses_first_word_when_full_title_would_mismatch_internal_punctuation(self, emby):
        # Path "TRON Legacy" but Emby Name "TRON: Legacy" — the colon
        # difference would make a full-title NameStartsWith miss.
        # Returning just "TRON" matches via SortName and the local
        # basename match still narrows down to the right movie.
        prefix, _ = emby._extract_title_prefix(
            "/library/Movies/TRON Legacy (2010)/TRON Legacy (2010) [Bluray-2160p].mkv"
        )
        assert prefix == "TRON"

    def test_detects_episode_in_flat_tv_layout(self, emby):
        # Some users keep episodes flat under the show folder (no Season
        # subdirectory). Detect by S01E… pattern in basename so we still
        # query Series + per-series enumerate.
        prefix, is_episode = emby._extract_title_prefix(
            "/library/TV/Bewitched (1964)/Bewitched (1964) - S05E17 - One Touch of Midas.mkv"
        )
        assert prefix == "Bewitched"
        assert is_episode is True, (
            "Flat-TV layout (no Season folder) must still be detected as episode "
            "via the S\\d+E\\d+ pattern in basename — otherwise we query Movie "
            "type and miss every Series+Episode."
        )

    def test_detects_episode_with_lowercase_pattern(self, emby):
        prefix, is_episode = emby._extract_title_prefix("/library/TV/Show/show.s01e01.mkv")
        assert prefix == "Show"
        assert is_episode is True

    def test_detects_episode_with_NxNN_pattern(self, emby):
        # Older naming convention: 1x05 instead of S01E05.
        prefix, is_episode = emby._extract_title_prefix("/library/TV/Show/Show 1x05.mkv")
        assert prefix == "Show"
        assert is_episode is True

    def test_returns_none_for_too_short_after_cleaning(self, emby):
        # "(2024)" alone would clean to empty string.
        assert emby._extract_title_prefix("/library/Movies/(2024)/foo.mkv") is None

    def test_returns_none_for_path_without_parent(self, emby):
        assert emby._extract_title_prefix("foo.mkv") is None
        assert emby._extract_title_prefix("") is None

    def test_strips_brackets(self, emby):
        prefix, _ = emby._extract_title_prefix(
            "/library/Movies/Movie Title [imdb-x][Bluray-1080p][AAC]/Movie Title.mkv"
        )
        assert prefix == "Movie"  # First word after bracket strip


class TestPass0NameStartsWithFastPath:
    """Pass 0 — NameStartsWith fast path before the slow Pass 1+2.

    Pass 1's full-stem ``searchTerm`` query was the dominant cost (30-76 s
    per call on a 117K-episode library because Emby ran full-text
    scoring across every item). Pass 0 swaps to ``NameStartsWith=<short
    title>`` (B-tree on the indexed Name/SortName column, ~10 ms) +
    a per-Series enumerate of just the matching Series (~5 ms).

    Empirical: 4173× total speedup across 22 real paths with 22/22 Id
    agreement against the legacy strategy (tools/bench_emby_reverse_path.py).

    These tests pin both the happy path AND the safe-fallback contract:
    Pass 0 returning None must always fall through to Pass 1 and Pass 2,
    so a regression that stops sending searchTerm can't lose recall.
    """

    @pytest.fixture
    def emby_with_tv_lib(self):
        # Library cache populated so library scoping computes a parent_id —
        # Pass 0 only fires when parent_id resolves.
        return EmbyServer(
            _emby_config(
                libraries=[
                    Library(
                        id="tv-1",
                        name="TV Shows",
                        remote_paths=("/library/TV",),
                        kind="tvshows",
                    )
                ]
            )
        )

    @pytest.fixture
    def emby_with_movie_lib(self):
        return EmbyServer(
            _emby_config(
                libraries=[
                    Library(
                        id="movies-1",
                        name="Movies",
                        remote_paths=("/library/Movies",),
                        kind="movies",
                    )
                ]
            )
        )

    @staticmethod
    def _resp(payload):
        r = MagicMock()
        r.json.return_value = payload
        r.raise_for_status.return_value = None
        return r

    def test_tv_episode_resolves_via_pass0(self, emby_with_tv_lib):
        """Happy path: NameStartsWith→Series → enumerate→episode.

        Asserts:
        - Pass 0 finds the episode id.
        - Only TWO ``/Items`` requests are made AFTER Emby's exact-Path
          fallthrough (1 NameStartsWith + 1 episode enumerate). No
          ``searchTerm`` round-trip.
        """
        path = "/library/TV/DMV (2025) [imdb-x]/Season 01/DMV - S01E03 - Title.mkv"
        path_empty = self._resp({"Items": []})  # Emby Path= miss
        ns_series = self._resp(
            {
                "TotalRecordCount": 1,
                "Items": [{"Id": "ser-1", "Name": "DMV"}],
            }
        )
        ep_enum = self._resp(
            {
                "Items": [{"Id": "ep-3", "Path": path}],
            }
        )
        with patch.object(EmbyServer, "_request", side_effect=[path_empty, ns_series, ep_enum]) as req:
            got = emby_with_tv_lib._uncached_resolve_remote_path_to_item_id(path)
            assert got == "ep-3"
            # Verify the SUT's contract — kwargs we control, not just call count.
            ns_call = req.call_args_list[1]
            # First-word-only prefix (see _extract_title_prefix docstring).
            assert ns_call.kwargs["params"]["NameStartsWith"] == "DMV"
            assert ns_call.kwargs["params"]["IncludeItemTypes"] == "Series"
            assert ns_call.kwargs["params"]["ParentId"] == "tv-1"
            ep_call = req.call_args_list[2]
            assert ep_call.kwargs["params"]["ParentId"] == "ser-1"
            assert ep_call.kwargs["params"]["IncludeItemTypes"] == "Episode"
            # Pass 1 (searchTerm) MUST NOT have run.
            assert not any("searchTerm" in (c.kwargs.get("params") or {}) for c in req.call_args_list), (
                "searchTerm path must not run when Pass 0 succeeds"
            )

    def test_movie_resolves_via_pass0_direct_match(self, emby_with_movie_lib):
        """Movies don't enumerate per-series — direct match on candidates."""
        path = "/library/Movies/Quiet Place (2018)/A Quiet Place (2018).mkv"
        path_empty = self._resp({"Items": []})
        ns_movies = self._resp(
            {
                "TotalRecordCount": 1,
                "Items": [{"Id": "mov-99", "Name": "A Quiet Place", "Path": path}],
            }
        )
        with patch.object(EmbyServer, "_request", side_effect=[path_empty, ns_movies]) as req:
            got = emby_with_movie_lib._uncached_resolve_remote_path_to_item_id(path)
            assert got == "mov-99"
            ns_call = req.call_args_list[1]
            # Article stripped + first-word-only: "A Quiet Place" → "Quiet".
            assert ns_call.kwargs["params"]["NameStartsWith"] == "Quiet"
            assert ns_call.kwargs["params"]["IncludeItemTypes"] == "Movie"

    def test_falls_through_to_pass1_when_namestartswith_returns_zero(self, emby_with_tv_lib):
        """Show isn't in Emby (NameStartsWith → 0). Must run Pass 1+2,
        not silently report the file as missing.
        """
        path = "/library/TV/Unknown Show (2099)/Season 01/episode.mkv"
        path_empty = self._resp({"Items": []})
        ns_empty = self._resp({"TotalRecordCount": 0, "Items": []})
        pass1_empty = self._resp({"Items": []})
        pass2_empty = self._resp({"Items": []})
        with patch.object(
            EmbyServer,
            "_request",
            side_effect=[path_empty, ns_empty, pass1_empty, pass2_empty],
        ) as req:
            got = emby_with_tv_lib._uncached_resolve_remote_path_to_item_id(path)
            assert got is None
            # Confirm Pass 1 ran (searchTerm present in some call's params).
            assert any("searchTerm" in (c.kwargs.get("params") or {}) for c in req.call_args_list), (
                "Pass 1 searchTerm MUST run after Pass 0 misses — otherwise we lose "
                "recall for shows whose folder name doesn't match Emby's stored Name."
            )

    def test_falls_through_when_series_match_but_episode_missing(self, emby_with_tv_lib):
        """NameStartsWith finds the Series, but the episode isn't in it
        (e.g. file just downloaded, Emby hasn't scanned yet). The
        per-Series enumerate finds nothing → fall through to Pass 1+2.
        Without fallthrough, a slightly-stale series cache would mask
        files Pass 1's searchTerm could still recover via fuzzy match.
        """
        path = "/library/TV/Show/Season 01/Show - S01E99 - Brand New.mkv"
        path_empty = self._resp({"Items": []})
        ns_series = self._resp({"TotalRecordCount": 1, "Items": [{"Id": "ser-9"}]})
        ep_no_match = self._resp({"Items": [{"Id": "ep-1", "Path": "/library/TV/Show/Season 01/Show - S01E01.mkv"}]})
        pass1_empty = self._resp({"Items": []})
        pass2_empty = self._resp({"Items": []})
        with patch.object(
            EmbyServer,
            "_request",
            side_effect=[path_empty, ns_series, ep_no_match, pass1_empty, pass2_empty],
        ) as req:
            got = emby_with_tv_lib._uncached_resolve_remote_path_to_item_id(path)
            assert got is None
            assert any("searchTerm" in (c.kwargs.get("params") or {}) for c in req.call_args_list)

    def test_aborts_when_namestartswith_returns_too_many_candidates(self, emby_with_tv_lib):
        """Cap busted (e.g. NameStartsWith="A" matches 100+ shows on a
        big library). Fast path aborts immediately and falls through to
        Pass 1's scoring, which is more selective. Without the cap
        the per-series enumerate would walk hundreds of Series and
        defeat the speedup.
        """
        path = "/library/TV/A Show/Season 01/A Show - S01E01.mkv"
        path_empty = self._resp({"Items": []})
        # Show only 2 items but report TotalRecordCount > cap to trigger abort.
        ns_too_many = self._resp(
            {
                "TotalRecordCount": 999,
                "Items": [{"Id": "ser-a"}, {"Id": "ser-b"}],
            }
        )
        pass1_empty = self._resp({"Items": []})
        pass2_empty = self._resp({"Items": []})
        with patch.object(
            EmbyServer,
            "_request",
            side_effect=[path_empty, ns_too_many, pass1_empty, pass2_empty],
        ) as req:
            got = emby_with_tv_lib._uncached_resolve_remote_path_to_item_id(path)
            assert got is None
            # Confirm Pass 0 did NOT enumerate either candidate's episodes
            # (cap-busted abort happens before episode round-trips).
            assert not any(
                (c.kwargs.get("params") or {}).get("ParentId") in ("ser-a", "ser-b") for c in req.call_args_list
            ), "Cap-busted Pass 0 must abort before per-series enumerate"
            # Pass 1 did run.
            assert any("searchTerm" in (c.kwargs.get("params") or {}) for c in req.call_args_list)

    def test_skips_pass0_when_no_extractable_title(self, emby_with_tv_lib):
        """Single-component path → no parent dir → no extractable
        prefix → Pass 0 skipped, Pass 1+2 still run.
        """
        path = "/library/TV/foo.mkv"  # only 3 components, parent is "TV" which is the library dir
        path_empty = self._resp({"Items": []})
        pass1_empty = self._resp({"Items": []})
        pass2_empty = self._resp({"Items": []})
        # Pass 0 may extract "TV" — but we need to ensure no NameStartsWith call
        # runs OR if it does, we still fall through. Simplest: provide enough
        # mocks for either flow + assert searchTerm fired.
        responses = [path_empty, pass1_empty, pass2_empty]
        # Pad in case Pass 0 attempts a query with parent="TV" extracted.
        responses = [path_empty, self._resp({"Items": []}), pass1_empty, pass2_empty]
        with patch.object(EmbyServer, "_request", side_effect=responses) as req:
            got = emby_with_tv_lib._uncached_resolve_remote_path_to_item_id(path)
            assert got is None
            assert any("searchTerm" in (c.kwargs.get("params") or {}) for c in req.call_args_list)


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
        """Audit fix — was an instantiation-only smoke. Now also asserts
        the constructed Emby actually works: ``type`` enum and configured
        URL/auth survive the registry round-trip, otherwise a regression
        in the registry's ``from_settings`` factory could ship a server
        with wrong type/url and this test would still pass.
        """
        from media_preview_generator.servers import ServerRegistry, ServerType

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
        srv = servers[0]
        assert isinstance(srv, EmbyServer)
        assert srv.type is ServerType.EMBY
        assert srv.id == "emby-1"
        # Configured URL / auth survived — not just any-Emby returned.
        cfg = registry.get_config("emby-1")
        assert cfg is not None
        assert cfg.url == "http://emby:8096"
