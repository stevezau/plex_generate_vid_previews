"""End-to-end search behaviour tests across all three server types.

The Preview Inspector search was broken on every vendor pre-fix
(2026-05-10):

* Plex returned 0 results because ``library.search()`` was scoped to
  the user's enabled-library config; multi-server installs with
  ``plex_library_ids = []`` got nothing across the entire catalogue.
* Emby returned ``Wonder Boys, Nickel Boys, Jersey Boys, Bad Boys,
  Boys State, Good Boys`` for ``"the boys s01e01"`` because
  ``searchTerm`` is a substring matcher with no relevance ranking.
* Jellyfin returned 0 results — same code path as Emby, but failures
  were silently swallowed.

Each vendor now uses the shared :class:`SearchQuery` parser plus the
:func:`rank_score` filter pass so a 1.0 exact-name match beats a 0.2
substring hit, and the 0.3 relevance floor drops the noise entirely.

Matrix coverage per .claude/rules/testing.md:
  * vendor (Plex / Emby / Jellyfin)
  * query shape (S##E## present / movie-only / nothing matches)
  * concrete regression: "the boys s01e01" against [The Boys, Wonder
    Boys, Nickel Boys, Jersey Boys, Bad Boys, Boys State, Good Boys]
    must return The Boys (or its episode), NOT the Movie noise.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from loguru import logger

from media_preview_generator.servers.base import ServerConfig, ServerType
from media_preview_generator.servers.emby import EmbyServer
from media_preview_generator.servers.jellyfin import JellyfinServer


@pytest.fixture
def info_log_sink():
    """Temporary INFO-level loguru sink — yields captured records list.

    The codebase uses loguru, not stdlib logging — caplog can't see
    loguru sinks unless we add one explicitly. Mirrors the pattern in
    ``test_servers_refresh_logging.py``.
    """
    records: list[dict] = []

    def _sink(message):
        records.append(
            {
                "level": message.record["level"].name,
                "message": message.record["message"],
            }
        )

    sink_id = logger.add(_sink, level="INFO")
    yield records
    logger.remove(sink_id)


def _emby() -> EmbyServer:
    return EmbyServer(
        ServerConfig(
            id="emby-1",
            type=ServerType.EMBY,
            name="EmbyTest",
            enabled=True,
            url="http://emby:8096",
            auth={"method": "api_key", "api_key": "k", "user_id": "u"},
        )
    )


def _jelly() -> JellyfinServer:
    return JellyfinServer(
        ServerConfig(
            id="jelly-1",
            type=ServerType.JELLYFIN,
            name="JellyTest",
            enabled=True,
            url="http://jelly:8096",
            auth={"method": "api_key", "api_key": "k"},
        )
    )


# ---------------------------------------------------------------------------
# Emby / Jellyfin shared search behaviour (both go through _embyish.py)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("server_factory", [_emby, _jelly])
class TestEmbyishSearchBoyRegressionMatrix:
    """The exact regression the user reported against Emby (and the
    silent-fail equivalent on Jellyfin) MUST be fixed for both vendors.
    """

    def test_the_boys_s01e01_returns_only_the_boys_episode(self, server_factory):
        server = server_factory()
        # Pass 1 (Series-first NameStartsWith=The Boys) returns the
        # series; the episode lookup returns S01E01.
        # Pass 2 (searchTerm fallback) returns the noise — but ranking
        # drops everything below the 0.3 floor.
        series_response = [
            {"Id": "series-the-boys", "Type": "Series", "Name": "The Boys"},
        ]
        episodes_response = {
            "Items": [
                {
                    "Id": "ep-s01e01",
                    "Type": "Episode",
                    "Name": "The Name of the Game",
                    "SeriesName": "The Boys",
                    "ParentIndexNumber": 1,
                    "IndexNumber": 1,
                    "Path": "/data/TV/The Boys/Season 01/The Boys S01E01.mkv",
                },
                {
                    "Id": "ep-s01e02",
                    "Type": "Episode",
                    "IndexNumber": 2,
                    "ParentIndexNumber": 1,
                    "Name": "Cherry",
                    "SeriesName": "The Boys",
                    "Path": "/data/TV/The Boys/Season 01/The Boys S01E02.mkv",
                },
            ]
        }
        # The user-reported noise — every "Boys" Movie returned by the
        # bare searchTerm fallback. Pre-fix all 6 came back to the UI;
        # post-fix the ranker drops them below the 0.3 floor.
        noise_response = [
            {
                "Id": "wonder-boys",
                "Type": "Movie",
                "Name": "Wonder Boys",
                "Path": "/data/Movies/Wonder Boys/Wonder Boys.mkv",
            },
            {
                "Id": "nickel-boys",
                "Type": "Movie",
                "Name": "Nickel Boys",
                "Path": "/data/Movies/Nickel Boys/Nickel Boys.mkv",
            },
            {
                "Id": "jersey-boys",
                "Type": "Movie",
                "Name": "Jersey Boys",
                "Path": "/data/Movies/Jersey Boys/Jersey Boys.mkv",
            },
            {"Id": "bad-boys", "Type": "Movie", "Name": "Bad Boys", "Path": "/data/Movies/Bad Boys/Bad Boys.mkv"},
            {
                "Id": "boys-state",
                "Type": "Movie",
                "Name": "Boys State",
                "Path": "/data/Movies/Boys State/Boys State.mkv",
            },
            {"Id": "good-boys", "Type": "Movie", "Name": "Good Boys", "Path": "/data/Movies/Good Boys/Good Boys.mkv"},
        ]

        with patch.object(type(server), "query_items") as qi, patch.object(type(server), "_request") as req:
            # qi handles both the Series-first NameStartsWith pass and
            # the searchTerm fallback. _request handles the per-series
            # /Shows/<id>/Episodes follow-up.
            def query_items_router(params):
                if params.get("NameStartsWith") == "the boys":
                    return series_response
                if params.get("searchTerm") == "the boys":
                    return noise_response
                return []

            qi.side_effect = query_items_router

            ep_resp_obj = MagicMock()
            ep_resp_obj.json.return_value = episodes_response
            req.return_value = ep_resp_obj

            results = server.search_items("the boys s01e01", limit=20)

        ids = [r.id for r in results]
        # The ONLY result must be S01E01 — the noise is filtered out and
        # S01E02 didn't match the episode index.
        assert "ep-s01e01" in ids, f"Expected the S01E01 episode to be returned, got ids={ids}"
        assert "ep-s01e02" not in ids, "S01E02 must not appear when S01E01 was requested"
        for noise_id in (
            "wonder-boys",
            "nickel-boys",
            "jersey-boys",
            "bad-boys",
            "boys-state",
            "good-boys",
        ):
            assert noise_id not in ids, (
                f"Movie '{noise_id}' should be ranked below the 0.3 floor for query 'the boys s01e01'"
            )

    def test_movie_query_returns_movies_through_fallback(self, server_factory):
        """No S##E## hint → skip Series-first; fallback ranks Movies
        and the exact-title-match floats to the top.
        """
        server = server_factory()
        candidates = [
            {"Id": "wonder-boys", "Type": "Movie", "Name": "Wonder Boys", "Path": "/m/Wonder Boys.mkv"},
            {"Id": "boys-state", "Type": "Movie", "Name": "Boys State", "Path": "/m/Boys State.mkv"},
        ]
        with patch.object(type(server), "query_items", return_value=candidates):
            results = server.search_items("wonder boys", limit=10)
        # Wonder Boys is the exact-title match (1.0); Boys State only
        # shares the "boys" token and falls below the 0.3 floor.
        ids = [r.id for r in results]
        assert "wonder-boys" in ids
        assert "boys-state" not in ids

    def test_empty_query_returns_empty_without_calling_server(self, server_factory):
        server = server_factory()
        with patch.object(type(server), "query_items") as qi, patch.object(type(server), "_request") as req:
            results = server.search_items("", limit=20)
        assert results == []
        qi.assert_not_called()
        req.assert_not_called()

    def test_no_results_logs_at_info(self, server_factory, info_log_sink):
        """When BOTH passes return nothing, log INFO so the user can
        grep for it without enabling debug. Pre-fix the search failed
        silently — the user typed something and just saw an empty list.
        """
        server = server_factory()
        with patch.object(type(server), "query_items", return_value=[]):
            results = server.search_items("totally unknown title", limit=10)
        assert results == []
        assert any("no results" in r["message"].lower() for r in info_log_sink), (
            "Zero-result searches MUST log INFO so users can grep for them. "
            f"Got records: {[r['message'] for r in info_log_sink]}"
        )


@pytest.mark.parametrize("server_factory", [_emby, _jelly])
class TestEmbyishSearchPreviewInspectorBugFix:
    """Direct regression coverage for the 2026-05-12 Preview Inspector
    bug report: 'searching The Neighbourhood S01E08 returned 3 show
    folders + S01E01..S01E08' on Emby, 'returned 3 show folders, no
    episodes' on Jellyfin.

    The matrix is asymmetric across vendors only because of Jellyfin's
    UserId requirement on /Shows/{id}/Episodes — the title/episode
    filter logic is shared and must hold for both clients.
    """

    def _series_response(self):
        return [{"Id": "series-neighbourhood", "Type": "Series", "Name": "The Neighbourhood"}]

    def _episodes_response_s01e01_to_e08(self):
        return {
            "Items": [
                {
                    "Id": f"ep-s01e{ep:02d}",
                    "Type": "Episode",
                    "Name": f"Episode {ep}",
                    "SeriesName": "The Neighbourhood",
                    "ParentIndexNumber": 1,
                    "IndexNumber": ep,
                    "Path": f"/data/TV/Neighbourhood/S01/E{ep:02d}.mkv",
                }
                for ep in range(1, 9)
            ]
        }

    def test_episode_query_returns_only_the_requested_episode(self, server_factory):
        """The exact user-reported bug — S01E08 query must surface only S01E08.

        Pre-fix: Pass 1 returned S01E08, then Pass 2's searchTerm
        fallback added S01E01..S01E08 + the Series row, leaving the
        UI with 9 wrong rows above the right one.
        """
        server = server_factory()

        # Pass 2's noise — a Series row WITH a Path (the show folder)
        # plus every episode (mimics what searchTerm returns when
        # IncludeItemTypes=Series,Movie,Episode).
        pass2_payload = [
            {
                "Id": "series-neighbourhood",
                "Type": "Series",
                "Name": "The Neighbourhood",
                "Path": "/data/TV/Neighbourhood",
            }
        ] + self._episodes_response_s01e01_to_e08()["Items"]

        with (
            patch.object(type(server), "query_items") as qi,
            patch.object(type(server), "_request") as req,
        ):

            def router(params):
                if params.get("NameStartsWith") == "the neighbourhood":
                    return self._series_response()
                if params.get("searchTerm") == "the neighbourhood":
                    return pass2_payload
                return []

            qi.side_effect = router

            ep_resp = MagicMock()
            ep_resp.json.return_value = self._episodes_response_s01e01_to_e08()
            req.return_value = ep_resp

            results = server.search_items("The Neighbourhood S01E08", limit=20)

        ids = [r.id for r in results]
        assert "ep-s01e08" in ids, f"S01E08 must be returned, got {ids}"
        for unwanted in [f"ep-s01e{n:02d}" for n in range(1, 8)]:
            assert unwanted not in ids, f"Unwanted episode {unwanted} leaked through Pass 2 (got {ids})"
        assert "series-neighbourhood" not in ids, (
            f"Series row must be dropped — show folders aren't loadable as previews (got {ids})"
        )

    def test_show_name_query_expands_into_all_episodes(self, server_factory):
        """Plain 'The Neighbourhood' (no S##E##) returns every episode.

        User explicitly chose this behaviour over 'show row + hint'
        when shaping the plan: 'when I search for a movie or show it
        will search the server, return all entries, check if each entry
        has a bif'.
        """
        server = server_factory()

        with (
            patch.object(type(server), "query_items") as qi,
            patch.object(type(server), "_request") as req,
        ):

            def router(params):
                if params.get("NameStartsWith") == "the neighbourhood":
                    return self._series_response()
                # Pass 2 isn't strictly needed when Pass 1 already
                # returned episodes, but the mock has to handle it.
                return []

            qi.side_effect = router

            ep_resp = MagicMock()
            ep_resp.json.return_value = self._episodes_response_s01e01_to_e08()
            req.return_value = ep_resp

            results = server.search_items("The Neighbourhood", limit=20)

        ids = [r.id for r in results]
        assert ids == [f"ep-s01e{n:02d}" for n in range(1, 9)], (
            f"Show-name expansion must return every episode, got {ids}"
        )
        # The /Shows/{id}/Episodes call must NOT have included Season —
        # Pass 1 only adds the Season filter when has_episode is True.
        call = req.call_args
        params = call.kwargs.get("params") or (call.args[2] if len(call.args) > 2 else {})
        assert "Season" not in params, f"Show-name query must not filter by Season, got params={params}"

    def test_pass2_series_row_expands_into_episodes(self, server_factory):
        """When Pass 1's NameStartsWith misses (e.g. Jellyfin's quirk on
        'The'-prefixed titles) but Pass 2's searchTerm finds the Series,
        Pass 2 must EXPAND the Series row into its episodes via
        /Shows/{id}/Episodes — not drop it as a non-loadable folder.

        Pre-2026-05-12 (initial fix attempt): Pass 2 dropped Series
        rows entirely. On Jellyfin that meant 0 results because
        Pass 1's NameStartsWith doesn't match titles starting with
        stop-words like 'The'. Now Pass 2's expansion mirrors Pass 1.
        """
        server = server_factory()
        # Pass 1 misses (Jellyfin NameStartsWith quirk).
        # Pass 2's searchTerm returns the Series.
        # /Shows/{id}/Episodes returns the actual episodes.
        pass2_payload = [
            {
                "Id": "series-the-boys",
                "Type": "Series",
                "Name": "The Boys",
                "Path": "/data/TV/The Boys",
            },
        ]
        episodes_response = {
            "Items": [
                {
                    "Id": f"ep-s01e{n:02d}",
                    "Type": "Episode",
                    "Name": f"Episode {n}",
                    "SeriesName": "The Boys",
                    "ParentIndexNumber": 1,
                    "IndexNumber": n,
                    "Path": f"/data/TV/The Boys/S01/E{n:02d}.mkv",
                }
                for n in range(1, 4)
            ]
        }
        with (
            patch.object(type(server), "query_items") as qi,
            patch.object(type(server), "_request") as req,
        ):

            def router(params):
                if params.get("NameStartsWith") == "the boys":
                    return []
                if params.get("searchTerm") == "the boys":
                    return pass2_payload
                return []

            qi.side_effect = router
            ep_resp = MagicMock()
            ep_resp.json.return_value = episodes_response
            req.return_value = ep_resp

            results = server.search_items("the boys", limit=10)

        ids = [r.id for r in results]
        assert "series-the-boys" not in ids, (
            f"Series row must NOT appear directly — only its expanded episodes (got {ids})"
        )
        assert ids == ["ep-s01e01", "ep-s01e02", "ep-s01e03"], (
            f"Pass 2 must expand the Series via /Shows/{{id}}/Episodes when Pass 1 missed (got {ids})"
        )


class TestEmbyishShowsEpisodesUserIdMatrix:
    """Both vendors get UserId-parity coverage on /Shows/{id}/Episodes.

    Jellyfin-specific quirk: /Shows/{id}/Episodes returns 400 (or an
    empty list) without UserId on any non-public catalogue — that's
    the bug the user reported on 2026-05-12. Emby is permissive and
    accepts the param either way, but we cover both vendors per the
    .claude/rules/testing.md 'cover the matrix' rule:

      (vendor=EMBY|JELLYFIN) × (auth.user_id present|absent) = 4 cells

    The shared `self._user_id()` helper means a future change that
    flips the conditional regresses both vendors at once; without
    Emby coverage that regression would only surface against
    Jellyfin in production.

    Pin the SUT-controlled kwargs per .claude/rules/testing.md
    'assert the kwargs the SUT controls'. Without these assertions the
    fix is invisible — the test passes whether or not UserId is sent.
    """

    def test_jellyfin_threads_user_id_to_episodes_endpoint(self):
        """auth carries user_id → /Shows/{id}/Episodes params include UserId."""
        server = JellyfinServer(
            ServerConfig(
                id="jelly-1",
                type=ServerType.JELLYFIN,
                name="JellyTest",
                enabled=True,
                url="http://jelly:8096",
                auth={"method": "password", "access_token": "tok", "user_id": "user-abc"},
            )
        )
        with (
            patch.object(JellyfinServer, "query_items") as qi,
            patch.object(JellyfinServer, "_request") as req,
        ):
            qi.return_value = [{"Id": "series-x", "Type": "Series", "Name": "Show"}]
            ep_resp = MagicMock()
            ep_resp.json.return_value = {"Items": []}
            req.return_value = ep_resp

            server.search_items("Show S01E01", limit=10)

        # Find the /Shows/.../Episodes call (Pass 1 may not be the
        # only _request call if the SUT later re-uses it).
        episode_calls = [
            c for c in req.call_args_list if any("/Shows/" in str(a) and "/Episodes" in str(a) for a in c.args)
        ]
        assert episode_calls, "Pass 1 must call /Shows/{id}/Episodes"
        params = episode_calls[0].kwargs.get("params") or {}
        assert params.get("UserId") == "user-abc", (
            f"Jellyfin /Shows/{{id}}/Episodes MUST include UserId from auth — got params={params}"
        )

    def test_jellyfin_omits_user_id_when_auth_has_none(self):
        """API-key-only auth (no captured user_id) → no UserId param.

        Sending UserId=None or UserId="" is worse than omitting it
        entirely — Jellyfin treats the empty string as a missing-but-
        present param and may 400. Symmetric to the /Items/{id}
        adapter's behaviour.
        """
        server = JellyfinServer(
            ServerConfig(
                id="jelly-2",
                type=ServerType.JELLYFIN,
                name="JellyTest",
                enabled=True,
                url="http://jelly:8096",
                auth={"method": "api_key", "api_key": "k"},  # no user_id
            )
        )
        with (
            patch.object(JellyfinServer, "query_items") as qi,
            patch.object(JellyfinServer, "_request") as req,
        ):
            qi.return_value = [{"Id": "series-x", "Type": "Series", "Name": "Show"}]
            ep_resp = MagicMock()
            ep_resp.json.return_value = {"Items": []}
            req.return_value = ep_resp

            server.search_items("Show S01E01", limit=10)

        episode_calls = [
            c for c in req.call_args_list if any("/Shows/" in str(a) and "/Episodes" in str(a) for a in c.args)
        ]
        assert episode_calls, "Pass 1 must call /Shows/{id}/Episodes"
        params = episode_calls[0].kwargs.get("params") or {}
        assert "UserId" not in params, (
            f"UserId must be omitted entirely (not set to None/'') when auth has no user_id — got {params}"
        )

    def test_emby_threads_user_id_to_episodes_endpoint(self):
        """Emby half of the matrix. Emby tolerates either presence or
        absence of UserId — but the SUT-controlled branch is the same
        ``self._user_id() → ep_params["UserId"]`` line, so flipping
        that conditional would regress Jellyfin silently if only the
        Jellyfin row covered it.
        """
        server = EmbyServer(
            ServerConfig(
                id="emby-1",
                type=ServerType.EMBY,
                name="EmbyTest",
                enabled=True,
                url="http://emby:8096",
                auth={"method": "api_key", "api_key": "k", "user_id": "emby-user"},
            )
        )
        with (
            patch.object(EmbyServer, "query_items") as qi,
            patch.object(EmbyServer, "_request") as req,
        ):
            qi.return_value = [{"Id": "series-x", "Type": "Series", "Name": "Show"}]
            ep_resp = MagicMock()
            ep_resp.json.return_value = {"Items": []}
            req.return_value = ep_resp

            server.search_items("Show S01E01", limit=10)

        episode_calls = [
            c for c in req.call_args_list if any("/Shows/" in str(a) and "/Episodes" in str(a) for a in c.args)
        ]
        assert episode_calls, "Pass 1 must call /Shows/{id}/Episodes"
        params = episode_calls[0].kwargs.get("params") or {}
        assert params.get("UserId") == "emby-user", (
            f"Emby /Shows/{{id}}/Episodes MUST include UserId from auth (parity with Jellyfin) — got {params}"
        )

    def test_emby_omits_user_id_when_auth_has_none(self):
        """Symmetric: Emby API-key-only auth (no captured user_id)
        must omit UserId entirely, not set it to None/''. Otherwise a
        future Emby version that becomes strict about UserId would
        regress for our paste-in-API-key users.
        """
        server = EmbyServer(
            ServerConfig(
                id="emby-2",
                type=ServerType.EMBY,
                name="EmbyTest",
                enabled=True,
                url="http://emby:8096",
                auth={"method": "api_key", "api_key": "k"},  # no user_id
            )
        )
        with (
            patch.object(EmbyServer, "query_items") as qi,
            patch.object(EmbyServer, "_request") as req,
        ):
            qi.return_value = [{"Id": "series-x", "Type": "Series", "Name": "Show"}]
            ep_resp = MagicMock()
            ep_resp.json.return_value = {"Items": []}
            req.return_value = ep_resp

            server.search_items("Show S01E01", limit=10)

        episode_calls = [
            c for c in req.call_args_list if any("/Shows/" in str(a) and "/Episodes" in str(a) for a in c.args)
        ]
        assert episode_calls, "Pass 1 must call /Shows/{id}/Episodes"
        params = episode_calls[0].kwargs.get("params") or {}
        assert "UserId" not in params, f"UserId must be omitted entirely when auth has no user_id — got {params}"


class TestEmbyishSearchSeriesPassMissesGracefully:
    def test_series_first_miss_falls_through_to_searchterm(self):
        """Series-first NameStartsWith might miss (the user typed a
        partial title) — fallback searchTerm pass picks it up.

        For an S##E## query, Pass 2 must still match the requested
        season+episode (the new filter post-2026-05-12 fix). Pre-fix
        this test fixture omitted ParentIndexNumber/IndexNumber and
        the buggy code returned the row regardless — that was the
        Emby bug shipping in disguise.
        """
        server = _emby()
        # Pass 1 finds nothing (NameStartsWith is strict).
        # Pass 2 finds the series via fuzzy searchTerm.
        with patch.object(EmbyServer, "query_items") as qi:

            def router(params):
                if params.get("NameStartsWith") == "boys":
                    return []
                if params.get("searchTerm") == "boys":
                    return [
                        {
                            "Id": "ep-1",
                            "Type": "Episode",
                            "Name": "The Boys",
                            "ParentIndexNumber": 1,
                            "IndexNumber": 1,
                            "Path": "/m/The Boys/The Boys.mkv",
                        }
                    ]
                return []

            qi.side_effect = router
            results = server.search_items("boys s01e01", limit=10)
        # The ranker handles the result — exact-name "The Boys" beats
        # the partial query token "boys".
        assert any(r.id == "ep-1" for r in results)


# ---------------------------------------------------------------------------
# Plex search behaviour
# ---------------------------------------------------------------------------


class TestPlexSearchBoyRegression:
    """Plex's pre-fix path was different — it returned 0 results
    because ``library.search`` was scoped to enabled libraries. The
    fix routes through ``plex.search`` (cross-library hub search) and
    runs the same shared ranker.
    """

    def _plex_server(self):
        from media_preview_generator.servers.plex import PlexServer

        srv = PlexServer(
            ServerConfig(
                id="plex-1",
                type=ServerType.PLEX,
                name="PlexTest",
                enabled=True,
                url="http://plex:32400",
                auth={"token": "t"},
                libraries=[],
            )
        )
        return srv

    def test_the_boys_s01e01_returns_episode_via_searchHubs(self):
        server = self._plex_server()

        # plexapi stubs — searchHubs returns mixed Show/Movie items.
        the_boys_show = MagicMock()
        the_boys_show.title = "The Boys"
        the_boys_show.METADATA_TYPE = "show"
        the_boys_show.type = "show"
        # The episode the show.episode() lookup returns.
        s01e01 = MagicMock()
        s01e01.METADATA_TYPE = "episode"
        s01e01.type = "episode"
        s01e01.title = "The Name of the Game"
        s01e01.parentIndex = 1
        s01e01.index = 1
        s01e01.grandparentTitle = "The Boys"
        s01e01.ratingKey = 999
        the_boys_show.episode = MagicMock(return_value=s01e01)

        wonder_boys = MagicMock()
        wonder_boys.title = "Wonder Boys"
        wonder_boys.METADATA_TYPE = "movie"
        wonder_boys.type = "movie"
        wonder_boys.ratingKey = 100

        plex_mock = MagicMock()
        plex_mock.search.return_value = [the_boys_show, wonder_boys]

        with (
            patch.object(type(server), "_connect", return_value=plex_mock),
            patch(
                "media_preview_generator.plex_client._extract_item_locations",
                return_value=["/data/TV/The Boys/Season 01/The Boys S01E01.mkv"],
            ),
        ):
            results = server.search_items("the boys s01e01", limit=20)

        # The Boys S01E01 must be the top result.
        assert results, "Expected at least one result"
        assert results[0].title.startswith("The Boys") or "Name of the Game" in results[0].title
        assert results[0].remote_path.endswith("S01E01.mkv")
        # Wonder Boys must NOT be in the result list — it's a Movie
        # candidate when the query carries S##E##, so the ranker
        # multiplies its score by 0.5 (0.2 token-only score → 0.1, well
        # below the 0.3 floor).
        ids = [(r.id, r.title) for r in results]
        for _id, title in ids:
            assert title != "Wonder Boys", (
                f"Wonder Boys should be filtered out by the rank pass for an S01E01 query — got {ids!r}"
            )

    def test_empty_query_returns_empty_without_connecting(self):
        server = self._plex_server()
        with patch.object(type(server), "_connect") as conn:
            results = server.search_items("", limit=10)
        assert results == []
        conn.assert_not_called()

    def test_show_name_query_expands_into_all_episodes(self):
        """Plain show-name search (no S##E##) drills into every episode.

        Pre-fix the show row had no MediaPart so ``_extract_item_locations``
        returned ``[]`` and the row was silently dropped — plain
        ``"the boys"`` returned zero results on Plex even when the show
        existed. Now show hits expand into their episodes so the user
        can browse the whole series.
        """
        server = self._plex_server()

        the_boys_show = MagicMock()
        the_boys_show.title = "The Boys"
        the_boys_show.METADATA_TYPE = "show"
        the_boys_show.type = "show"

        episodes_stub: list[MagicMock] = []
        for season, ep in [(1, 1), (1, 2), (2, 1)]:
            ep_mock = MagicMock()
            ep_mock.METADATA_TYPE = "episode"
            ep_mock.type = "episode"
            ep_mock.title = f"Episode S{season:02d}E{ep:02d}"
            ep_mock.parentIndex = season
            ep_mock.index = ep
            ep_mock.grandparentTitle = "The Boys"
            ep_mock.ratingKey = season * 100 + ep
            episodes_stub.append(ep_mock)
        the_boys_show.episodes = MagicMock(return_value=episodes_stub)

        plex_mock = MagicMock()
        plex_mock.search.return_value = [the_boys_show]

        with (
            patch.object(type(server), "_connect", return_value=plex_mock),
            patch(
                "media_preview_generator.plex_client._extract_item_locations",
                side_effect=lambda obj: (
                    [f"/data/TV/The Boys/S{obj.parentIndex:02d}/ep{obj.index}.mkv"]
                    if getattr(obj, "METADATA_TYPE", "") == "episode"
                    else []
                ),
            ),
        ):
            results = server.search_items("the boys", limit=20)

        assert len(results) == 3, f"Expected 3 episodes from show expansion, got {len(results)}"
        # m.episode (the single-episode drill) MUST NOT have been called
        # — show-name queries use m.episodes (plural).
        the_boys_show.episode.assert_not_called()
        the_boys_show.episodes.assert_called_once()
        ratingkeys = {r.id for r in results}
        assert ratingkeys == {"101", "102", "201"}

    def test_show_expansion_respects_limit(self):
        """Show with 50 episodes capped at limit=10."""
        server = self._plex_server()

        show = MagicMock()
        show.title = "Long Running Show"
        show.METADATA_TYPE = "show"
        show.type = "show"

        episodes_stub = []
        for ep in range(50):
            ep_mock = MagicMock()
            ep_mock.METADATA_TYPE = "episode"
            ep_mock.type = "episode"
            ep_mock.title = f"Episode {ep}"
            ep_mock.parentIndex = 1
            ep_mock.index = ep + 1
            ep_mock.grandparentTitle = "Long Running Show"
            ep_mock.ratingKey = ep + 1
            episodes_stub.append(ep_mock)
        show.episodes = MagicMock(return_value=episodes_stub)

        plex_mock = MagicMock()
        plex_mock.search.return_value = [show]

        with (
            patch.object(type(server), "_connect", return_value=plex_mock),
            patch(
                "media_preview_generator.plex_client._extract_item_locations",
                return_value=["/data/TV/x.mkv"],
            ),
        ):
            results = server.search_items("long running show", limit=10)

        assert len(results) == 10, f"Show expansion must respect limit=10, got {len(results)}"
