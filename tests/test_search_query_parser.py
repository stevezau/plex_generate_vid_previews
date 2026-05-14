"""Tests for SearchQuery parser + rank scoring.

Pre-fix the Preview Inspector handed each vendor the raw query string
and let it figure out what to do. Three concrete failure modes:

* Plex's ``library.search(title="the.boys.s01e01.1080p.web.h264-rarbg")``
  scored zero hits — no Plex item has that literal title.
* Emby's ``searchTerm`` substring-matched ANY token, returning Wonder
  Boys / Nickel Boys / Jersey Boys when the user typed "the boys".
* Jellyfin returned nothing because ``searchTerm`` permission/scope
  issues weren't surfaced.

The new parser + rank helper sits between the user input and the
vendor-specific search call so every vendor sees a normalised
``(title, season, episode, tokens)`` block AND benefits from the same
relevance ranking pass.

Matrix coverage per .claude/rules/testing.md — every distinct input
shape gets a row:
  * canonical S##E## ("the boys s01e01")
  * release-style filename paste ("the.boys.s01e01.1080p.web.h264-rarbg")
  * alternate NxN notation ("the boys 1x01")
  * movie-only ("wonder boys", "boys state")
  * mixed case + Unicode + edge whitespace
  * empty / whitespace-only input
"""

from __future__ import annotations

import pytest

from media_preview_generator.search import SearchQuery, rank_score
from media_preview_generator.search.rank import filter_and_rank


class TestSearchQueryParse:
    @pytest.mark.parametrize(
        "raw,expected_title,expected_season,expected_episode",
        [
            ("the boys s01e01", "the boys", 1, 1),
            ("The Boys S01E01", "the boys", 1, 1),
            ("the boys S01E01", "the boys", 1, 1),
            ("the.boys.s01e01.1080p.web.h264-rarbg", "the boys", 1, 1),
            # PROPER is release metadata — gets stripped from the right edge.
            ("The.Boys.S02E08.PROPER.1080p.WEB.H264-CAKES", "the boys", 2, 8),
            ("the boys 1x01", "the boys", 1, 1),
            ("the boys 12x345", "the boys", 12, 345),
            ("Deadliest Catch s22e01 Kings of the Frozen North", "deadliest catch kings of the frozen north", 22, 1),
            # Movie-only — no S##E## present.
            ("wonder boys", "wonder boys", None, None),
            ("Wonder Boys (2000)", "wonder boys (2000)", None, None),
            ("Boys State", "boys state", None, None),
        ],
    )
    def test_parse_extracts_title_season_episode(self, raw, expected_title, expected_season, expected_episode):
        q = SearchQuery.parse(raw)
        assert q.title == expected_title, f"input={raw!r}"
        assert q.season == expected_season, f"input={raw!r}"
        assert q.episode == expected_episode, f"input={raw!r}"
        assert q.raw == raw

    def test_empty_input_is_empty_query(self):
        q = SearchQuery.parse("")
        assert q.is_empty
        assert q.tokens == ()
        assert q.season is None and q.episode is None

    def test_whitespace_only_is_empty_query(self):
        q = SearchQuery.parse("   \t\n  ")
        assert q.is_empty

    def test_none_is_empty_query(self):
        q = SearchQuery.parse(None)
        assert q.is_empty

    def test_tokens_lowercased(self):
        q = SearchQuery.parse("The Boys")
        assert q.tokens == ("the", "boys")

    def test_release_junk_only_stripped_from_right_edge(self):
        # "1080p" at the end gets stripped; "boys" never does even
        # though it's a common word.
        q = SearchQuery.parse("Boys State 1080p")
        assert q.title == "boys state"
        assert q.tokens == ("boys", "state")

    def test_release_junk_in_middle_is_kept(self):
        # We only strip junk from the right edge — a real title with
        # release-looking words in the middle (rare but possible) is
        # preserved.
        q = SearchQuery.parse("HDR Story")
        # HDR is at the LEFT edge so it stays. (We're conservative.)
        assert q.title == "hdr story"

    def test_has_episode_property(self):
        assert SearchQuery.parse("the boys s01e01").has_episode is True
        assert SearchQuery.parse("the boys").has_episode is False
        assert SearchQuery.parse("").has_episode is False


class TestRankScore:
    def test_exact_title_match_scores_1(self):
        q = SearchQuery.parse("the boys")
        assert rank_score(q, "The Boys") == 1.0

    def test_prefix_match_scores_0_8(self):
        q = SearchQuery.parse("the boys")
        assert rank_score(q, "The Boys' Life") == 0.8

    def test_all_tokens_present_scores_0_5(self):
        q = SearchQuery.parse("the boys")
        assert rank_score(q, "Boys, Behold The") == 0.5

    def test_partial_token_match_scores_0_2(self):
        q = SearchQuery.parse("the boys")
        # Only "boys" matches; "the" isn't there.
        assert rank_score(q, "Wonder Boys") == 0.2

    def test_no_overlap_scores_0(self):
        q = SearchQuery.parse("the boys")
        assert rank_score(q, "Game of Thrones") == 0.0

    def test_episode_context_bonus_for_series(self):
        """When the user typed S##E##, Series candidates get a small
        bump so they beat coincidentally-named Movies."""
        q = SearchQuery.parse("the boys s01e01")
        series_score = rank_score(q, "The Boys", candidate_type="series")
        movie_score = rank_score(q, "The Boys", candidate_type="movie")
        assert series_score > movie_score

    def test_no_episode_context_bonus_for_plain_query(self):
        q = SearchQuery.parse("the boys")
        series_score = rank_score(q, "The Boys", candidate_type="series")
        movie_score = rank_score(q, "The Boys", candidate_type="movie")
        # No bonus because the query carries no S##E## hint.
        assert series_score == movie_score

    def test_empty_query_scores_0(self):
        q = SearchQuery.parse("")
        assert rank_score(q, "Anything") == 0.0


class TestFilterAndRank:
    def test_filter_floor_drops_noise(self):
        """Live regression: 'the boys' against [The Boys, Wonder Boys,
        Nickel Boys, Jersey Boys, Bad Boys, Boys State, Good Boys].
        Pre-fix all 7 came back unranked — the user got 6 wrong hits
        before the right one. With the 0.3 floor, only The Boys
        survives (1.0 score); the rest fall below the threshold.
        """
        q = SearchQuery.parse("the boys")
        candidates = [
            ("The Boys", "series", "carrier-the-boys"),
            ("Wonder Boys", "movie", "carrier-wonder"),
            ("Nickel Boys", "movie", "carrier-nickel"),
            ("Jersey Boys", "movie", "carrier-jersey"),
            ("Bad Boys", "movie", "carrier-bad"),
            ("Boys State", "movie", "carrier-state"),
            ("Good Boys", "movie", "carrier-good"),
        ]
        result = filter_and_rank(q, candidates)
        # The Boys is the only candidate that scores above the 0.3 floor.
        # Wonder/Nickel/Jersey/Bad/Good Boys score 0.2 (only "boys"
        # matches — "the" is missing). Boys State scores 0.2 as well.
        assert result == ["carrier-the-boys"], f"Expected only The Boys above floor; got {result}"

    def test_filter_keeps_all_top_tier_matches(self):
        q = SearchQuery.parse("game of")
        candidates = [
            ("Game of Thrones", "series", "got"),
            ("The Game", "series", "the-game"),  # 0.2 — only "game" matches
            ("Game of Cards", "series", "cards"),
            ("Game", "series", "game"),  # 0.2 — partial
        ]
        result = filter_and_rank(q, candidates)
        # Both "Game of Thrones" and "Game of Cards" prefix-match → 0.8.
        # "The Game" and "Game" only have one token → 0.2 → below floor.
        assert "got" in result
        assert "cards" in result
        assert "the-game" not in result
        assert "game" not in result

    def test_filter_orders_by_score_descending(self):
        q = SearchQuery.parse("the boys")
        candidates = [
            ("Boys, Behold The", "series", "tokens-only"),  # 0.5
            ("The Boys", "series", "exact"),  # 1.0
            ("The Boys' Life", "series", "prefix"),  # 0.8
        ]
        result = filter_and_rank(q, candidates)
        assert result == ["exact", "prefix", "tokens-only"]
