"""Pure helpers for detecting Dolby Vision / HDR content and the FFmpeg
stderr signatures that mean our tonemap filter fell over.

Side-effect-free functions of their inputs (media-metadata fields or
a list of stderr lines). Grouping them here keeps the detection
vocabulary in one place so :func:`generate_images` stays focused on
pipeline orchestration.
"""

from __future__ import annotations

import re


def is_dolby_vision(hdr_format: str | None) -> bool:
    """Detect any Dolby Vision content (any profile).

    Used to identify DV content so the caller can choose the correct
    tone-mapping strategy:

    * DV Profile 5 (no backward-compat HDR10 layer) — requires libplacebo.
    * DV Profile 7/8 (with HDR10 fallback) — the standard zscale/tonemap
      chain reads the HDR10 base layer and works correctly.

    Args:
        hdr_format: Value of ``MediaInfo.video_tracks[0].hdr_format``.

    Returns:
        bool: ``True`` if Dolby Vision metadata is present, ``False`` otherwise.
    """
    if not hdr_format or hdr_format == "None":
        return False

    return "dolby vision" in hdr_format.lower()


def is_dv_no_backward_compat(hdr_format: str | None) -> bool:
    """Detect Dolby Vision content without a backward-compatible HDR base layer.

    DV Profile 5 (and similar) uses IPT-PQ transfer characteristics with
    no HDR10/HLG fallback.  These files **must** be processed via
    libplacebo because there is no HDR10 base layer for zscale/tonemap.

    DV Profile 7/8 (with HDR10 fallback) returns ``False`` here and can
    safely use the standard zscale/tonemap chain on the HDR10 base layer.

    Args:
        hdr_format: Value of ``MediaInfo.video_tracks[0].hdr_format``.

    Returns:
        bool: ``True`` if content is DV without backward compat,
              ``False`` otherwise.
    """
    if not hdr_format or hdr_format == "None":
        return False

    hdr_lower = hdr_format.lower()

    if "dolby vision" not in hdr_lower:
        return False

    # Profiles that use IPT-PQ transfer — no backward-compat base layer.
    # Profile 5 (HEVC): dvhe.05  |  Profile 4 (HEVC, non-backward-compat): dvhe.04
    # AV1 DV Profile 5: dvav.05  |  AV1 DV set/entry: dvav.se
    dv_unsafe_profiles = ["dvhe.05", "dvhe.04", "dvav.05", "dvav.se"]
    if any(tag in hdr_lower for tag in dv_unsafe_profiles):
        return True

    backward_compat_keywords = [
        "hdr10",
        "hlg",
        "pq10",
        "smpte st 2086",
        "smpte st 2094",
        "compatible",
        "compat",
    ]
    return not any(kw in hdr_lower for kw in backward_compat_keywords)


def detect_dolby_vision_rpu_error(stderr_lines: list[str]) -> bool:
    """Detect FFmpeg Dolby Vision RPU parsing failures that can abort processing.

    This is intentionally narrow to avoid false positives. It matches a small
    allow-list of known fatal signatures from upstream FFmpeg/libdovi output.

    Args:
        stderr_lines: List of FFmpeg stderr lines

    Returns:
        bool: True if the Dolby Vision RPU error is detected
    """
    if not stderr_lines:
        return False

    # Known fatal Dolby Vision parsing signatures (extend as new cases are reported).
    # Keep these specific to avoid triggering on benign informational/warning messages.
    fatal_signatures = [
        "multiple dolby vision rpus found in one au",
        # Some FFmpeg builds append additional context after the core message.
        "multiple dolby vision rpus found in one au. skipping previous.",
    ]

    stderr_text = " ".join(stderr_lines).lower()
    return any(sig in stderr_text for sig in fatal_signatures)


def detect_zscale_colorspace_error(stderr_lines: list[str]) -> bool:
    """Detect zscale filter failures caused by unsupported colorspace conversions.

    Dolby Vision Profile 5 (and some other HDR flavours) use IPT-PQ or
    proprietary transfer characteristics that zscale cannot map to linear.
    FFmpeg emits ``code 3074: no path between colorspaces`` and then crashes
    the filter graph, typically producing exit code 187.

    Args:
        stderr_lines: List of FFmpeg stderr lines

    Returns:
        bool: True if a zscale colorspace error is detected
    """
    if not stderr_lines:
        return False

    stderr_text = " ".join(stderr_lines).lower()

    # Exact substring signatures — the most reliable patterns.
    fatal_signatures = [
        "no path between colorspaces",
        "zscale: generic error in an external library",
    ]
    if any(sig in stderr_text for sig in fatal_signatures):
        return True

    # FFmpeg may log the filter name in brackets with an address, e.g.
    # [Parsed_zscale_1 @ 0x55eb] Generic error in an external library
    # [vf#0:0/zscale @ 0x5f3a] Generic error in an external library
    # Match these even when "zscale:" doesn't appear as a bare prefix.
    if re.search(r"parsed_zscale_\d+.*generic error in an external library", stderr_text):
        return True
    if re.search(
        r"zscale\s*@\s*0x[0-9a-f]+\].*generic error in an external library",
        stderr_text,
    ):
        return True

    return False
