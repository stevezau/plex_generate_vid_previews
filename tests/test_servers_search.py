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


class TestEmbyishSearchSeriesPassMissesGracefully:
    def test_series_first_miss_falls_through_to_searchterm(self):
        """Series-first NameStartsWith might miss (the user typed a
        partial title) — fallback searchTerm pass picks it up.
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
