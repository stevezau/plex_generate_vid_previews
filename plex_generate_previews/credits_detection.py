"""Credits detection using FFmpeg blackdetect and silencedetect filters.

Analyzes the last portion of a video file to identify where credits begin
by combining black frame detection and silence detection.  The earliest
qualifying combined region is reported as the credits start point.

No external dependencies beyond FFmpeg are required.
"""

import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass

from loguru import logger

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class BlackFrame:
    """A detected black frame region."""

    start: float  # seconds
    end: float  # seconds
    duration: float  # seconds


@dataclass
class SilenceRegion:
    """A detected silence region."""

    start: float  # seconds
    end: float  # seconds
    duration: float  # seconds


@dataclass
class CreditsSegment:
    """A detected credits segment."""

    start_ms: int  # milliseconds
    end_ms: int  # milliseconds
    confidence: float  # 0.0 to 1.0
    method: str  # "black+silence", "black_only", "silence_only"


@dataclass
class CreditsDetectionConfig:
    """Tunable parameters for credits detection."""

    enabled: bool = False
    scan_last_pct: float = 25.0  # Scan last N% of video
    black_min_duration: float = 0.5  # Min black frame duration (seconds)
    black_pix_threshold: float = 0.10  # Pixel threshold for black detection
    silence_noise_threshold: str = "-40dB"  # Noise floor for silence
    silence_min_duration: float = 3.0  # Min silence duration (seconds)
    min_credits_duration: float = 15.0  # Min credits length to accept (seconds)
    max_credits_start_pct: float = 75.0  # Credits must start after this % of video


# ---------------------------------------------------------------------------
# FFmpeg filter runners
# ---------------------------------------------------------------------------

_BLACK_RE = re.compile(
    r"black_start:(\d+(?:\.\d+)?)\s+"
    r"black_end:(\d+(?:\.\d+)?)\s+"
    r"black_duration:(\d+(?:\.\d+)?)"
)

_SILENCE_START_RE = re.compile(r"silence_start:\s*(\d+(?:\.\d+)?)")
_SILENCE_END_RE = re.compile(r"silence_end:\s*(\d+(?:\.\d+)?)\s*\|\s*silence_duration:\s*(\d+(?:\.\d+)?)")


def _run_blackdetect(
    media_file: str,
    seek_to: float,
    ffmpeg_path: str = "ffmpeg",
    black_min_duration: float = 0.5,
    pix_threshold: float = 0.10,
    cancel_check: Callable | None = None,
) -> list[BlackFrame]:
    """Run FFmpeg blackdetect filter and parse output.

    Args:
        media_file: Path to the video file.
        seek_to: Start scanning from this timestamp (seconds).
        ffmpeg_path: Path to the ffmpeg binary.
        black_min_duration: Minimum black frame duration in seconds.
        pix_threshold: Pixel brightness threshold (0.0–1.0).
        cancel_check: Optional callable for cancellation.

    Returns:
        List of detected black frame regions.

    """
    cmd = [
        ffmpeg_path,
        "-ss",
        str(seek_to),
        "-i",
        media_file,
        "-vf",
        f"blackdetect=d={black_min_duration}:pix_th={pix_threshold}",
        "-an",
        "-f",
        "null",
        "-",
    ]

    frames: list[BlackFrame] = []
    try:
        proc = subprocess.Popen(
            cmd,
            stderr=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            text=True,
        )
        for line in proc.stderr:
            if cancel_check and cancel_check():
                proc.kill()
                break
            match = _BLACK_RE.search(line)
            if match:
                # Timestamps are relative to the seek point; add offset
                start = float(match.group(1)) + seek_to
                end = float(match.group(2)) + seek_to
                duration = float(match.group(3))
                frames.append(BlackFrame(start=start, end=end, duration=duration))
        proc.wait()
    except FileNotFoundError:
        logger.warning(f"ffmpeg not found at {ffmpeg_path}")
    except Exception as exc:
        logger.warning(f"blackdetect failed: {exc}")

    return frames


def _run_silencedetect(
    media_file: str,
    seek_to: float,
    ffmpeg_path: str = "ffmpeg",
    noise_threshold: str = "-40dB",
    silence_duration: float = 3.0,
    cancel_check: Callable | None = None,
) -> list[SilenceRegion]:
    """Run FFmpeg silencedetect filter and parse output.

    Args:
        media_file: Path to the video file.
        seek_to: Start scanning from this timestamp (seconds).
        ffmpeg_path: Path to the ffmpeg binary.
        noise_threshold: Noise floor (e.g. ``"-40dB"``).
        silence_duration: Minimum silence duration in seconds.
        cancel_check: Optional callable for cancellation.

    Returns:
        List of detected silence regions.

    """
    cmd = [
        ffmpeg_path,
        "-ss",
        str(seek_to),
        "-i",
        media_file,
        "-af",
        f"silencedetect=n={noise_threshold}:d={silence_duration}",
        "-vn",
        "-f",
        "null",
        "-",
    ]

    regions: list[SilenceRegion] = []
    pending_start: float | None = None
    try:
        proc = subprocess.Popen(
            cmd,
            stderr=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            text=True,
        )
        for line in proc.stderr:
            if cancel_check and cancel_check():
                proc.kill()
                break

            start_match = _SILENCE_START_RE.search(line)
            if start_match:
                pending_start = float(start_match.group(1)) + seek_to

            end_match = _SILENCE_END_RE.search(line)
            if end_match and pending_start is not None:
                end = float(end_match.group(1)) + seek_to
                duration = float(end_match.group(2))
                regions.append(SilenceRegion(start=pending_start, end=end, duration=duration))
                pending_start = None
        proc.wait()
    except FileNotFoundError:
        logger.warning(f"ffmpeg not found at {ffmpeg_path}")
    except Exception as exc:
        logger.warning(f"silencedetect failed: {exc}")

    return regions


# ---------------------------------------------------------------------------
# Combination logic
# ---------------------------------------------------------------------------


def _regions_overlap_or_adjacent(
    a_start: float,
    a_end: float,
    b_start: float,
    b_end: float,
    tolerance: float = 2.0,
) -> bool:
    """Check if two time regions overlap or are within *tolerance* seconds."""
    return a_start <= b_end + tolerance and b_start <= a_end + tolerance


def _find_black_cluster(
    black_frames: list[BlackFrame],
    min_start_time: float,
    min_frames: int = 2,
    cluster_window: float = 60.0,
) -> float | None:
    """Find the earliest cluster of multiple black frames.

    A single black frame can be a scene transition; credits typically
    produce several black frames within a short window.

    Args:
        black_frames: Detected black frame regions (sorted by start).
        min_start_time: Ignore frames before this timestamp.
        min_frames: Minimum black frames in a cluster.
        cluster_window: Max seconds between first and last frame in cluster.

    Returns:
        Start time of the first frame in the cluster, or None.

    """
    eligible = [bf for bf in black_frames if bf.start >= min_start_time]
    for i, anchor in enumerate(eligible):
        cluster = [anchor]
        for later in eligible[i + 1 :]:
            if later.start - anchor.start <= cluster_window:
                cluster.append(later)
            else:
                break
        if len(cluster) >= min_frames:
            return cluster[0].start
    return None


def _combine_detections(
    black_frames: list[BlackFrame],
    silence_regions: list[SilenceRegion],
    total_duration_sec: float,
    min_credits_duration_sec: float = 15.0,
    max_credits_start_pct: float = 75.0,
) -> CreditsSegment | None:
    """Combine black frame and silence detections to identify credits.

    Strategy (ordered by confidence):
    1. Black+silence overlap — a black frame cluster coincides with silence.
    2. Black cluster only — multiple black frames within 60s (not a single
       scene-transition frame, which caused false positives).
    3. Silence only — long silence near the end (lowest confidence).

    Args:
        black_frames: Detected black frame regions.
        silence_regions: Detected silence regions.
        total_duration_sec: Total video duration in seconds.
        min_credits_duration_sec: Minimum credits length in seconds.
        max_credits_start_pct: Credits must start after this % of video.

    Returns:
        CreditsSegment or None.

    """
    min_start_time = total_duration_sec * (max_credits_start_pct / 100.0)
    end_ms = int(total_duration_sec * 1000)
    sorted_blacks = sorted(black_frames, key=lambda f: f.start)

    # Strategy 1: Black frame cluster + silence overlap (highest confidence)
    cluster_start = _find_black_cluster(sorted_blacks, min_start_time)
    if cluster_start is not None:
        for sr in silence_regions:
            if _regions_overlap_or_adjacent(cluster_start, cluster_start + 60.0, sr.start, sr.end):
                credits_start = min(cluster_start, sr.start)
                credits_duration = total_duration_sec - credits_start
                if credits_duration >= min_credits_duration_sec:
                    return CreditsSegment(
                        start_ms=int(credits_start * 1000),
                        end_ms=end_ms,
                        confidence=0.9,
                        method="black+silence",
                    )

    # Strategy 2: Single black + silence overlap (good confidence)
    for bf in sorted_blacks:
        if bf.start < min_start_time:
            continue
        for sr in silence_regions:
            if _regions_overlap_or_adjacent(bf.start, bf.end, sr.start, sr.end):
                credits_start = min(bf.start, sr.start)
                credits_duration = total_duration_sec - credits_start
                if credits_duration >= min_credits_duration_sec:
                    return CreditsSegment(
                        start_ms=int(credits_start * 1000),
                        end_ms=end_ms,
                        confidence=0.8,
                        method="black+silence",
                    )

    # Strategy 3: Black cluster only — requires 2+ frames to avoid
    # false positives from single scene-transition black frames.
    if cluster_start is not None:
        credits_duration = total_duration_sec - cluster_start
        if credits_duration >= min_credits_duration_sec:
            return CreditsSegment(
                start_ms=int(cluster_start * 1000),
                end_ms=end_ms,
                confidence=0.6,
                method="black_cluster",
            )

    # Strategy 4: Silence only (low confidence — below the 0.5 write
    # threshold, so only written if the user lowers the threshold)
    for sr in sorted(silence_regions, key=lambda r: r.start):
        if sr.start < min_start_time:
            continue
        credits_duration = total_duration_sec - sr.start
        if credits_duration >= min_credits_duration_sec:
            return CreditsSegment(
                start_ms=int(sr.start * 1000),
                end_ms=end_ms,
                confidence=0.4,
                method="silence_only",
            )

    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def detect_credits(
    media_file: str,
    total_duration_sec: float,
    ffmpeg_path: str = "ffmpeg",
    config: CreditsDetectionConfig | None = None,
    cancel_check: Callable | None = None,
) -> CreditsSegment | None:
    """Detect the credits start point in a media file.

    Runs FFmpeg blackdetect and silencedetect filters on the last
    portion of the video and combines results.

    Args:
        media_file: Path to the video file.
        total_duration_sec: Total video duration in seconds.
        ffmpeg_path: Path to the ffmpeg binary.
        config: Detection parameters (uses defaults if None).
        cancel_check: Optional callable for cancellation.

    Returns:
        CreditsSegment if credits were detected, None otherwise.

    """
    if config is None:
        config = CreditsDetectionConfig()

    if total_duration_sec < config.min_credits_duration:
        logger.debug(
            f"Video too short for credits detection ({total_duration_sec:.0f}s < {config.min_credits_duration}s)"
        )
        return None

    # Scan the last N% of the video
    seek_to = total_duration_sec * (1.0 - config.scan_last_pct / 100.0)

    logger.info(
        f"Credits scan: scanning from {seek_to:.0f}s (last {config.scan_last_pct}% of {total_duration_sec:.0f}s)"
    )

    black_frames = _run_blackdetect(
        media_file,
        seek_to,
        ffmpeg_path,
        black_min_duration=config.black_min_duration,
        pix_threshold=config.black_pix_threshold,
        cancel_check=cancel_check,
    )

    if cancel_check and cancel_check():
        return None

    silence_regions = _run_silencedetect(
        media_file,
        seek_to,
        ffmpeg_path,
        noise_threshold=config.silence_noise_threshold,
        silence_duration=config.silence_min_duration,
        cancel_check=cancel_check,
    )

    if cancel_check and cancel_check():
        return None

    logger.debug(f"Credits detection: found {len(black_frames)} black frames, {len(silence_regions)} silence regions")

    return _combine_detections(
        black_frames,
        silence_regions,
        total_duration_sec,
        min_credits_duration_sec=config.min_credits_duration,
        max_credits_start_pct=config.max_credits_start_pct,
    )
