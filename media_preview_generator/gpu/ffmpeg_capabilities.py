"""FFmpeg version and hardware-acceleration capability probes.

Small, stateless helpers that shell out to ``ffmpeg -version`` and
``ffmpeg -hwaccels`` and cache/parse the output.  Separated from the
GPU enumeration and Vulkan probing code because every other module
needs these facts and they have no dependency on device state.
"""

from __future__ import annotations

import re
import subprocess

from loguru import logger

# Minimum required FFmpeg version.  Everything below this is likely to
# be missing hwaccel features we rely on (libplacebo apply_dolbyvision,
# the tonemap_opencl Jellyfin patch, etc.).
MIN_FFMPEG_VERSION = (7, 0, 0)


def _get_ffmpeg_version() -> tuple[int, int, int] | None:
    """Get FFmpeg version as a tuple of integers.

    Returns:
        Optional[Tuple[int, int, int]]: Version tuple (major, minor, patch)
        or None if the output couldn't be parsed (unusual git build,
        missing ffmpeg, etc.).
    """
    try:
        result = subprocess.run(
            ["ffmpeg", "-version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
        if result.returncode != 0:
            logger.debug("Failed to get FFmpeg version: {}", result.stderr)
            return None

        # Extract version from first line: "ffmpeg version 7.1.1-1ubuntu1.2 Copyright..."
        version_line = result.stdout.split("\n")[0] if result.stdout else ""
        logger.debug("FFmpeg version string: '{}'", version_line)

        # Special-case date-based git builds (e.g., "ffmpeg version 2025-10-12-git-...")
        # These are not semantic versions; treat as "unknown version" so we don't
        # incorrectly parse the year as the major version.
        if re.search(r"ffmpeg version \d{4}-\d{2}-\d{2}-", version_line):
            logger.debug("Detected date-based FFmpeg git build; skipping semantic version parsing")
            return None

        # Try multiple patterns to handle different FFmpeg version formats
        # Patterns ordered from most specific to least specific
        patterns = [
            (
                r"ffmpeg version (\d+)\.(\d+)\.(\d+)",
                3,
            ),  # Standard: ffmpeg version 7.1.1
            (r"version (\d+)\.(\d+)\.(\d+)", 3),  # Alternate: version 7.1.1
            (
                r"ffmpeg[^\d]*(\d+)\.(\d+)\.(\d+)",
                3,
            ),  # Flexible: any text between ffmpeg and version
            (r"ffmpeg version (\d+)\.(\d+)", 2),  # Two-part version: ffmpeg version 8.0
            (r"version (\d+)\.(\d+)", 2),  # Alternate: version 8.0
            (
                r"ffmpeg[^\d]*(\d+)\.(\d+)",
                2,
            ),  # Flexible: any text between ffmpeg and version
            (r"ffmpeg version (\d+)", 1),  # Single version: ffmpeg version 8
            (r"version (\d+)", 1),  # Alternate: version 8
        ]

        for pattern, num_groups in patterns:
            version_match = re.search(pattern, version_line)
            if version_match:
                groups = version_match.groups()
                # Pad with zeros if fewer than 3 components
                major = int(groups[0])
                minor = int(groups[1]) if num_groups >= 2 else 0
                patch = int(groups[2]) if num_groups >= 3 else 0
                logger.debug("FFmpeg version detected: {}.{}.{}", major, minor, patch)
                return (major, minor, patch)

        logger.debug("Could not parse FFmpeg version from: '{}'", version_line)
        return None

    except Exception:
        logger.warning(
            "FFmpeg version detection raised an unexpected error. "
            "Version-gated GPU features will fall back to the lowest-supported assumptions; "
            "if a feature you expect doesn't appear, this is the place to look.",
            exc_info=True,
        )
        return None


def _check_ffmpeg_version() -> bool:
    """Check if FFmpeg version meets minimum requirements.

    Returns:
        bool: True if version is sufficient (or unknown), False if
        below the minimum supported version.
    """
    version = _get_ffmpeg_version()
    if version is None:
        logger.warning(
            "Could not detect the installed FFmpeg version. Carrying on, but if preview "
            "generation fails later, run `ffmpeg -version` from a terminal inside the same "
            "container/host to confirm FFmpeg is installed and on the PATH."
        )
        return True  # Don't fail if we can't determine version

    if version >= MIN_FFMPEG_VERSION:
        logger.debug(
            "✓ FFmpeg version {}.{}.{} meets minimum requirement {}.{}.{}",
            version[0],
            version[1],
            version[2],
            MIN_FFMPEG_VERSION[0],
            MIN_FFMPEG_VERSION[1],
            MIN_FFMPEG_VERSION[2],
        )
        return True
    logger.warning(
        "Installed FFmpeg is {}.{}.{} but this app needs at least {}.{}.{}. "
        "Older FFmpeg builds are missing hardware-acceleration features (CUDA / VAAPI / QSV / VideoToolbox) "
        "that BIF generation relies on for speed. CPU fallback may still work but will be slow. "
        "Fix: rebuild the Docker image (it ships a recent FFmpeg) or upgrade FFmpeg on your host.",
        version[0],
        version[1],
        version[2],
        MIN_FFMPEG_VERSION[0],
        MIN_FFMPEG_VERSION[1],
        MIN_FFMPEG_VERSION[2],
    )
    return False


def _get_ffmpeg_hwaccels() -> list[str]:
    """Get list of available FFmpeg hardware accelerators.

    Returns:
        List[str]: Hardware-accel names reported by ``ffmpeg -hwaccels``
        (e.g. ``["cuda", "vaapi", "vulkan"]``).  Empty on any error.
    """
    try:
        result = subprocess.run(
            ["ffmpeg", "-hwaccels"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
        if result.returncode != 0:
            logger.debug("Failed to get FFmpeg hardware accelerators: {}", result.stderr)
            return []

        hwaccels = []
        for line in result.stdout.split("\n"):
            line = line.strip()
            if line and not line.startswith("Hardware acceleration methods:"):
                hwaccels.append(line)

        return hwaccels
    except Exception as e:
        logger.debug("Error getting FFmpeg hardware accelerators: {}", e)
        return []


def _is_hwaccel_available(hwaccel: str) -> bool:
    """Check if a specific hardware acceleration is available.

    Args:
        hwaccel: Hardware acceleration type to check (e.g. ``"cuda"``,
            ``"vaapi"``, ``"vulkan"``).

    Returns:
        bool: True if available in this FFmpeg build, False otherwise.
    """
    available_hwaccels = _get_ffmpeg_hwaccels()
    is_available = hwaccel in available_hwaccels

    if is_available:
        logger.debug("✓ {} hardware acceleration is available", hwaccel)
    else:
        logger.debug("✗ {} hardware acceleration is not available", hwaccel)

    return is_available
