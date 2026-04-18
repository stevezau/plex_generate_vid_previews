"""Tests for intro detection using chromaprint fingerprinting."""

import random
from unittest.mock import MagicMock, patch

from plex_generate_previews.intro_detection import (
    IntroFingerprintStore,
    MatchRegion,
    _compare_fingerprints,
    _find_best_common_segment,
    _popcount,
    _run_fpcalc,
    check_fpcalc_available,
    find_common_intro,
    fingerprint_episode,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_fingerprint(length: int, seed: int = 42) -> list[int]:
    """Generate a deterministic random fingerprint."""
    rng = random.Random(seed)
    return [rng.randint(0, 0xFFFFFFFF) for _ in range(length)]


def _make_matching_pair(
    total_length: int = 500,
    intro_length: int = 100,
    intro_offset_a: int = 10,
    intro_offset_b: int = 20,
    seed: int = 42,
) -> tuple[list[int], list[int]]:
    """Create two fingerprints with a shared intro segment.

    The intro segment is identical in both fingerprints but at
    different offsets.  The rest is random noise.
    """
    rng = random.Random(seed)
    # Shared intro segment
    intro = [rng.randint(0, 0xFFFFFFFF) for _ in range(intro_length)]

    # Build fp1: noise + intro at offset_a + noise
    fp1 = [rng.randint(0, 0xFFFFFFFF) for _ in range(total_length)]
    fp1[intro_offset_a : intro_offset_a + intro_length] = intro

    # Build fp2: noise + intro at offset_b + noise (different seed for noise)
    rng2 = random.Random(seed + 999)
    fp2 = [rng2.randint(0, 0xFFFFFFFF) for _ in range(total_length)]
    fp2[intro_offset_b : intro_offset_b + intro_length] = intro

    return fp1, fp2


# ---------------------------------------------------------------------------
# popcount
# ---------------------------------------------------------------------------


class TestPopcount:
    def test_zero(self):
        assert _popcount(0) == 0

    def test_all_ones(self):
        assert _popcount(0xFFFFFFFF) == 32

    def test_specific_value(self):
        assert _popcount(0b10101010) == 4


# ---------------------------------------------------------------------------
# fpcalc parsing
# ---------------------------------------------------------------------------


class TestRunFpcalc:
    def test_parses_fingerprint(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "DURATION=120\nFINGERPRINT=100,200,300,400\n"
        mock_result.stderr = ""

        with patch(
            "plex_generate_previews.intro_detection.subprocess.run",
            return_value=mock_result,
        ):
            fp = _run_fpcalc("video.mp4", length_sec=120)

        assert fp == [100, 200, 300, 400]

    def test_returns_none_on_error(self):
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "error: cannot open file"

        with patch(
            "plex_generate_previews.intro_detection.subprocess.run",
            return_value=mock_result,
        ):
            fp = _run_fpcalc("video.mp4", length_sec=120)

        assert fp is None

    def test_returns_none_when_not_installed(self):
        with patch(
            "plex_generate_previews.intro_detection.subprocess.run",
            side_effect=FileNotFoundError,
        ):
            fp = _run_fpcalc("video.mp4", length_sec=120)

        assert fp is None


# ---------------------------------------------------------------------------
# fingerprint_episode
# ---------------------------------------------------------------------------


class TestFingerprintEpisode:
    def test_respects_cancellation(self):
        result = fingerprint_episode("video.mp4", cancel_check=lambda: True)
        assert result is None


# ---------------------------------------------------------------------------
# check_fpcalc_available
# ---------------------------------------------------------------------------


class TestCheckFpcalcAvailable:
    def test_available(self):
        with patch(
            "plex_generate_previews.intro_detection.shutil.which",
            return_value="/usr/bin/fpcalc",
        ):
            assert check_fpcalc_available() is True

    def test_not_available(self):
        with patch(
            "plex_generate_previews.intro_detection.shutil.which",
            return_value=None,
        ):
            assert check_fpcalc_available() is False


# ---------------------------------------------------------------------------
# _compare_fingerprints
# ---------------------------------------------------------------------------


class TestCompareFingerprints:
    def test_identical_fingerprints_match(self):
        fp = _make_fingerprint(200)
        regions = _compare_fingerprints(fp, fp, min_run_length=50)
        assert len(regions) > 0
        # Should find a long match at offset 0
        best = max(regions, key=lambda r: r.length)
        assert best.length >= 100

    def test_no_match_for_random_fingerprints(self):
        fp1 = _make_fingerprint(200, seed=1)
        fp2 = _make_fingerprint(200, seed=2)
        regions = _compare_fingerprints(fp1, fp2, min_run_length=50)
        assert len(regions) == 0

    def test_finds_shared_intro_segment(self):
        fp1, fp2 = _make_matching_pair(
            total_length=500,
            intro_length=120,
            intro_offset_a=10,
            intro_offset_b=20,
        )
        regions = _compare_fingerprints(fp1, fp2, min_run_length=50)
        assert len(regions) > 0
        best = max(regions, key=lambda r: r.length)
        assert best.length >= 100
        assert best.score > 0.9


# ---------------------------------------------------------------------------
# _find_best_common_segment
# ---------------------------------------------------------------------------


class TestFindBestCommonSegment:
    def test_selects_best_by_score_and_duration(self):
        matches = [
            MatchRegion(offset_a=10, offset_b=20, length=200, score=0.95),
            MatchRegion(offset_a=50, offset_b=60, length=100, score=0.80),
        ]
        result = _find_best_common_segment(matches, min_duration_sec=5.0)
        assert result is not None
        assert result.confidence == 0.95

    def test_rejects_too_short(self):
        matches = [
            MatchRegion(offset_a=10, offset_b=20, length=10, score=0.95),
        ]
        result = _find_best_common_segment(matches, min_duration_sec=15.0)
        assert result is None

    def test_rejects_too_long(self):
        matches = [
            MatchRegion(offset_a=0, offset_b=0, length=10000, score=0.95),
        ]
        result = _find_best_common_segment(matches, min_duration_sec=15.0, max_duration_sec=120.0)
        assert result is None

    def test_returns_none_for_empty(self):
        assert _find_best_common_segment([]) is None


# ---------------------------------------------------------------------------
# IntroFingerprintStore
# ---------------------------------------------------------------------------


class TestIntroFingerprintStore:
    def test_add_and_retrieve(self):
        store = IntroFingerprintStore()
        store.add("Breaking Bad", 1, 100, [1, 2, 3])
        store.add("Breaking Bad", 1, 101, [4, 5, 6])
        store.add("Breaking Bad", 2, 200, [7, 8, 9])

        seasons = store.get_seasons()
        # Season 2 only has 1 episode, so it's excluded
        assert len(seasons) == 1
        show, season, episodes = seasons[0]
        assert show == "Breaking Bad"
        assert season == 1
        assert len(episodes) == 2

    def test_requires_minimum_two_episodes(self):
        store = IntroFingerprintStore()
        store.add("Show", 1, 100, [1, 2, 3])
        assert store.get_seasons() == []

    def test_bool_and_len(self):
        store = IntroFingerprintStore()
        assert not store
        assert len(store) == 0

        store.add("Show", 1, 100, [1, 2, 3])
        assert store
        assert len(store) == 1

    def test_thread_safety(self):
        """Concurrent adds don't corrupt the store."""
        import threading

        store = IntroFingerprintStore()
        errors = []

        def add_many(show_idx):
            try:
                for ep in range(50):
                    store.add(f"Show{show_idx}", 1, ep, [ep] * 10)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=add_many, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        assert len(store) == 250  # 5 shows * 50 episodes


# ---------------------------------------------------------------------------
# find_common_intro
# ---------------------------------------------------------------------------


class TestFindCommonIntro:
    def test_finds_common_intro_in_matching_episodes(self):
        fp1, fp2 = _make_matching_pair(
            total_length=500,
            intro_length=150,  # ~18.5 seconds
            intro_offset_a=10,
            intro_offset_b=15,
        )
        result = find_common_intro(
            [(1, fp1), (2, fp2)],
            min_duration_sec=5.0,
            max_duration_sec=120.0,
        )
        assert result is not None
        assert result.confidence > 0.5
        assert result.start_ms >= 0
        assert result.end_ms > result.start_ms

    def test_returns_none_for_single_episode(self):
        fp = _make_fingerprint(200)
        result = find_common_intro([(1, fp)])
        assert result is None

    def test_returns_none_for_unrelated_episodes(self):
        fp1 = _make_fingerprint(200, seed=1)
        fp2 = _make_fingerprint(200, seed=2)
        result = find_common_intro(
            [(1, fp1), (2, fp2)],
            min_duration_sec=5.0,
        )
        assert result is None
