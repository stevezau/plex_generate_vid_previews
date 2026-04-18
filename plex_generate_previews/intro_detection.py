"""Intro detection using chromaprint audio fingerprinting.

Generates audio fingerprints for TV episodes using ``fpcalc`` (from
``libchromaprint-tools``), then compares fingerprints across episodes
in a season to find the recurring intro segment.

Two-phase design:
- **Phase 1** (parallel, per-episode): ``fingerprint_episode()`` generates
  a fingerprint for the first N minutes and stores it in an
  ``IntroFingerprintStore``.
- **Phase 2** (sequential, post-dispatch): ``find_common_intro()``
  compares fingerprints within each season to identify the common intro.
"""

import shutil
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass

from loguru import logger

# Chromaprint samples at ~8000 fingerprint integers per minute of audio
# at the default sample rate.  Each integer covers ~0.1238 seconds.
_FPCALC_ITEM_DURATION_SEC = 0.1238


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class MatchRegion:
    """A region of matching audio between two fingerprints."""

    offset_a: int  # index in fingerprint A
    offset_b: int  # index in fingerprint B
    length: int  # number of matching items
    score: float  # average match quality (0.0–1.0)

    @property
    def duration_sec(self) -> float:
        return self.length * _FPCALC_ITEM_DURATION_SEC

    @property
    def start_sec_a(self) -> float:
        return self.offset_a * _FPCALC_ITEM_DURATION_SEC

    @property
    def start_sec_b(self) -> float:
        return self.offset_b * _FPCALC_ITEM_DURATION_SEC


@dataclass
class IntroSegment:
    """A detected intro segment."""

    start_ms: int
    end_ms: int
    confidence: float  # 0.0 to 1.0


# ---------------------------------------------------------------------------
# Fingerprint store (thread-safe, used during parallel pass 1)
# ---------------------------------------------------------------------------


class IntroFingerprintStore:
    """Thread-safe store for episode fingerprints grouped by season.

    Collects fingerprints during the parallel processing pass and
    provides grouped access for the comparison pass.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # Key: (show_title, season_number) → [(rating_key, fingerprint)]
        self._data: dict[tuple[str, int], list[tuple[int, list[int]]]] = {}

    def add(
        self,
        show_title: str,
        season_number: int,
        rating_key: int,
        fingerprint: list[int],
    ) -> None:
        """Add a fingerprint for an episode."""
        key = (show_title, season_number)
        with self._lock:
            if key not in self._data:
                self._data[key] = []
            self._data[key].append((rating_key, fingerprint))

    def get_seasons(self) -> list[tuple[str, int, list[tuple[int, list[int]]]]]:
        """Return all seasons with 2+ fingerprinted episodes.

        Returns:
            List of (show_title, season_number, [(rating_key, fingerprint)])
            tuples, sorted by show title then season number.

        """
        with self._lock:
            results = []
            for (show, season), episodes in sorted(self._data.items()):
                if len(episodes) >= 2:
                    results.append((show, season, list(episodes)))
            return results

    def __bool__(self) -> bool:
        with self._lock:
            return bool(self._data)

    def __len__(self) -> int:
        with self._lock:
            return sum(len(eps) for eps in self._data.values())


# ---------------------------------------------------------------------------
# fpcalc runner
# ---------------------------------------------------------------------------


def check_fpcalc_available() -> bool:
    """Check whether ``fpcalc`` is installed and reachable."""
    return shutil.which("fpcalc") is not None


def _run_fpcalc(
    media_file: str,
    length_sec: float,
) -> list[int] | None:
    """Run ``fpcalc -raw`` and parse the integer fingerprint array.

    Args:
        media_file: Path to the audio/video file.
        length_sec: Duration to analyze in seconds.

    Returns:
        List of 32-bit fingerprint integers, or None on failure.

    """
    cmd = [
        "fpcalc",
        "-raw",
        "-length",
        str(int(length_sec)),
        media_file,
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            logger.debug(f"fpcalc returned {result.returncode}: {result.stderr.strip()}")
            return None

        for line in result.stdout.splitlines():
            if line.startswith("FINGERPRINT="):
                raw = line[len("FINGERPRINT=") :]
                return [int(x) for x in raw.split(",") if x.strip()]

        logger.debug("fpcalc output did not contain FINGERPRINT line")
        return None
    except FileNotFoundError:
        logger.warning("fpcalc not found — install libchromaprint-tools")
        return None
    except subprocess.TimeoutExpired:
        logger.warning(f"fpcalc timed out on {media_file}")
        return None
    except Exception as exc:
        logger.warning(f"fpcalc failed on {media_file}: {exc}")
        return None


def fingerprint_episode(
    media_file: str,
    duration_limit_sec: float = 600.0,
    cancel_check: Callable | None = None,
) -> list[int] | None:
    """Generate a chromaprint fingerprint for the first N seconds.

    Args:
        media_file: Path to the episode file.
        duration_limit_sec: How many seconds to analyze (default 10 min).
        cancel_check: Optional callable for cancellation.

    Returns:
        List of fingerprint integers, or None on failure.

    """
    if cancel_check and cancel_check():
        return None

    return _run_fpcalc(media_file, duration_limit_sec)


# ---------------------------------------------------------------------------
# Fingerprint comparison
# ---------------------------------------------------------------------------


def _popcount(x: int) -> int:
    """Count the number of set bits in a 32-bit integer."""
    return bin(x & 0xFFFFFFFF).count("1")


def _compare_fingerprints(
    fp1: list[int],
    fp2: list[int],
    max_offset: int = 200,
    match_threshold: int = 8,
    min_run_length: int = 50,
) -> list[MatchRegion]:
    """Compare two fingerprints using sliding window Hamming distance.

    Slides fp2 over fp1 at various offsets and finds runs of matching
    items (where Hamming distance ≤ threshold).

    Complexity: O(max_offset * overlap_length) per pair — pure Python.
    Acceptable for v1; can be optimized with numpy or Cython if needed
    for very large seasons.

    Args:
        fp1: First fingerprint array.
        fp2: Second fingerprint array.
        max_offset: Maximum offset to try in each direction.
        match_threshold: Max Hamming distance to consider a match (0–32).
        min_run_length: Minimum consecutive matches to report.

    Returns:
        List of MatchRegion objects.

    """
    regions: list[MatchRegion] = []

    for offset in range(-max_offset, max_offset + 1):
        # Determine the overlapping range
        if offset >= 0:
            start_a, start_b = offset, 0
        else:
            start_a, start_b = 0, -offset

        overlap_len = min(len(fp1) - start_a, len(fp2) - start_b)
        if overlap_len < min_run_length:
            continue

        # Count consecutive matches
        run_start = None
        run_scores: list[float] = []

        for i in range(overlap_len):
            hamming = _popcount(fp1[start_a + i] ^ fp2[start_b + i])
            if hamming <= match_threshold:
                if run_start is None:
                    run_start = i
                    run_scores = []
                run_scores.append(1.0 - hamming / 32.0)
            else:
                if run_start is not None and len(run_scores) >= min_run_length:
                    regions.append(
                        MatchRegion(
                            offset_a=start_a + run_start,
                            offset_b=start_b + run_start,
                            length=len(run_scores),
                            score=sum(run_scores) / len(run_scores),
                        )
                    )
                run_start = None
                run_scores = []

        # Final run at end of overlap
        if run_start is not None and len(run_scores) >= min_run_length:
            regions.append(
                MatchRegion(
                    offset_a=start_a + run_start,
                    offset_b=start_b + run_start,
                    length=len(run_scores),
                    score=sum(run_scores) / len(run_scores),
                )
            )

    return regions


def _find_best_common_segment(
    all_matches: list[MatchRegion],
    min_duration_sec: float = 15.0,
    max_duration_sec: float = 120.0,
) -> IntroSegment | None:
    """Find the best intro segment from pairwise match results.

    Picks the longest high-scoring match that falls within the
    duration constraints.

    Args:
        all_matches: Match regions from pairwise comparisons.
        min_duration_sec: Minimum intro length.
        max_duration_sec: Maximum intro length.

    Returns:
        IntroSegment or None.

    """
    # Filter by duration and sort by score * length (quality)
    candidates = []
    for m in all_matches:
        duration = m.duration_sec
        if min_duration_sec <= duration <= max_duration_sec:
            candidates.append(m)

    if not candidates:
        return None

    # Pick the best candidate by score * duration
    best = max(candidates, key=lambda m: m.score * m.duration_sec)
    start_ms = int(best.start_sec_a * 1000)
    end_ms = int((best.start_sec_a + best.duration_sec) * 1000)

    return IntroSegment(
        start_ms=start_ms,
        end_ms=end_ms,
        confidence=best.score,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def find_common_intro(
    fingerprints: list[tuple[int, list[int]]],
    min_duration_sec: float = 15.0,
    max_duration_sec: float = 120.0,
) -> IntroSegment | None:
    """Compare fingerprints across episodes to find the common intro.

    Performs pairwise comparison of all fingerprints and identifies
    the recurring audio segment.

    Args:
        fingerprints: List of ``(rating_key, fingerprint)`` tuples.
        min_duration_sec: Minimum intro length in seconds.
        max_duration_sec: Maximum intro length in seconds.

    Returns:
        IntroSegment if a common intro is found, None otherwise.

    """
    if len(fingerprints) < 2:
        return None

    # Multi-reference comparison: compare several pairs instead of just
    # episode[0] vs all others.  This avoids false negatives when the
    # first episode is a pilot/special with a different intro.
    # For N episodes, compare pairs: (0,1), (0,2), (1,2), ... up to a
    # reasonable limit, then vote on the best segment.
    n = len(fingerprints)
    pairs = []
    # Compare first 3 episodes pairwise (up to 3 pairs)
    for i in range(min(n, 3)):
        for j in range(i + 1, min(n, 3)):
            pairs.append((i, j))
    # Also compare episode 0 vs later episodes for broader coverage
    for j in range(3, min(n, 6)):
        pairs.append((0, j))

    all_matches: list[MatchRegion] = []
    for i, j in pairs:
        _, fp_i = fingerprints[i]
        _, fp_j = fingerprints[j]
        matches = _compare_fingerprints(fp_i, fp_j)
        all_matches.extend(matches)

    if not all_matches:
        logger.info("No matching audio segments found across episodes")
        return None

    result = _find_best_common_segment(all_matches, min_duration_sec, max_duration_sec)
    if result:
        logger.info(f"Found common intro: {result.start_ms}ms–{result.end_ms}ms (confidence: {result.confidence:.0%})")
    return result
