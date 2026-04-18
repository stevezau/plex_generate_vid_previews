"""Tier-by-tier retry-cascade predicates for FFmpeg thumbnail jobs.

:func:`generate_images` in :mod:`..media_processing` runs up to four
FFmpeg invocations in sequence when the first attempt produces zero
thumbnails.  This module pulls the *pure* decision logic — "given the
outcome of the last FFmpeg pass, should we retry at the next tier, and
with what reason label?" — out of that huge orchestrator function so it
can be unit-tested on synthetic stderr streams without needing FFmpeg
on the test box.

The tiers (from first to last):

1. ``SKIP_FRAME``         — retry without ``-skip_frame nokey`` when the
                            keyframe-only pass produced nothing.
2. ``SW_LIBPLACEBO``      — fall back from hardware DV5 paths
                            (Intel OpenCL or VAAPI→Vulkan) to software
                            decode + libplacebo when the hardware
                            pipeline fails.
3. ``DV_SAFE_FILTER``     — swap the zscale/tonemap/libplacebo chain
                            for a plain fps+scale chain on
                            HDR/DV-specific errors.
4. ``CPU_FALLBACK``       — raise :class:`CodecNotSupportedError` so
                            the GPU worker can retry the whole item on
                            CPU in-place.

The orchestration loop itself still lives in :func:`generate_images`
because it mutates a dozen local variables (rc, seconds, speed,
stderr_lines, image_count, plus several flags) and early-returns on
cancellation — extracting it into a pure function would require
threading a large ``RetryState`` dataclass through every call site
and gain little readability over the current in-line form.
"""

from __future__ import annotations

from enum import Enum
from typing import List, Optional, Tuple

from .hdr_detection import (
    detect_dolby_vision_rpu_error,
    detect_zscale_colorspace_error,
)


class RetryTier(Enum):
    """Which FFmpeg-retry tier fired for a given item.

    Exposed primarily for logging / telemetry so future callers can
    aggregate retry reasons per job without reparsing log strings.
    """

    NONE = "none"
    SKIP_FRAME = "skip_frame"
    SW_LIBPLACEBO = "sw_libplacebo"
    DV_SAFE_FILTER = "dv_safe_filter"
    CPU_FALLBACK = "cpu_fallback"


# ---------------------------------------------------------------------------
# Tier 3 — DV-safe filter retry
# ---------------------------------------------------------------------------


def classify_dv_safe_retry_reason(
    stderr_lines_all: List[str],
    *,
    use_libplacebo: bool,
) -> Optional[str]:
    """Decide whether to retry with the DV-safe fps+scale filter chain.

    Fires on any of the three error shapes that are specific to the
    colour-aware filter stages (zscale/tonemap, libplacebo):

    * Dolby Vision RPU parsing error (detected via stderr signature).
    * zscale colorspace-conversion error (same).
    * Catch-all ``libplacebo`` failure when libplacebo was active and
      neither of the above matched — this covers Vulkan init failures,
      driver issues, etc., where swapping to the simple fps+scale
      chain lets us still produce usable thumbnails.

    Args:
        stderr_lines_all: Combined stderr from every attempt so far.
        use_libplacebo: Whether the previous pass used libplacebo.

    Returns:
        A human-readable reason string when the DV-safe retry should
        fire, or ``None`` to skip this tier.
    """
    is_dv_rpu = detect_dolby_vision_rpu_error(stderr_lines_all)
    is_zscale = detect_zscale_colorspace_error(stderr_lines_all)
    is_libplacebo_fail = use_libplacebo and not is_dv_rpu and not is_zscale
    if is_dv_rpu:
        return "Dolby Vision RPU parsing error"
    if is_zscale:
        return "zscale colorspace conversion error"
    if is_libplacebo_fail:
        return "libplacebo tone mapping error"
    return None


# ---------------------------------------------------------------------------
# Tier 4 — CPU fallback evaluation
# ---------------------------------------------------------------------------


def classify_cpu_fallback_reason(
    returncode: int,
    stderr_lines: List[str],
    stderr_lines_all: List[str],
    *,
    detect_codec_error,
    detect_hwaccel_runtime_error,
    is_signal_killed,
) -> Tuple[bool, Optional[str]]:
    """Decide whether a GPU-context failure should fall back to CPU.

    Ordered check across three distinct failure shapes, all of which
    empirically succeed on CPU:

    1. Codec/decoder not supported by the hardware decoder.
    2. Hardware-accelerator runtime error (CUDA / VAAPI / QSV surface
       errors, driver-level issues).
    3. FFmpeg killed by a signal (segfault, OOM, driver crash).

    The detection predicates are injected (rather than imported) to
    keep this module dependency-free on the private ``_detect_*``
    helpers in :mod:`..media_processing` — callers pass them in.

    Args:
        returncode: FFmpeg exit code.
        stderr_lines: stderr from the *last* FFmpeg invocation only —
            used for codec-error pattern matching.
        stderr_lines_all: combined stderr across every attempt — used
            for hwaccel-runtime matching since some errors only surface
            on the primary pass and are not re-emitted on retry.
        detect_codec_error: predicate ``(rc, lines) -> bool``.
        detect_hwaccel_runtime_error: predicate ``(lines) -> bool``.
        is_signal_killed: predicate ``(rc) -> bool``.

    Returns:
        ``(should_fallback, reason)`` where ``reason`` is ``None`` when
        ``should_fallback`` is ``False``.
    """
    if detect_codec_error(returncode, stderr_lines):
        return True, "codec error"
    if detect_hwaccel_runtime_error(stderr_lines_all):
        return True, "hardware accelerator runtime error"
    if is_signal_killed(returncode):
        signal_num = returncode - 128 if returncode > 128 else returncode
        return True, f"signal kill (signal {signal_num})"
    return False, None


__all__ = [
    "RetryTier",
    "classify_dv_safe_retry_reason",
    "classify_cpu_fallback_reason",
]
