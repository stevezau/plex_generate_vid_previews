"""GPU detection for video processing acceleration.

Detects available GPU hardware and returns appropriate configuration
for FFmpeg hardware acceleration. Supports NVIDIA, AMD, Intel, Apple (macOS), and Windows GPUs.
"""

import glob
import json
import os
import platform
import re
import subprocess
import tempfile
from functools import lru_cache
from typing import List, Optional, Tuple

from loguru import logger

from .utils import is_macos, is_windows

# Minimum required FFmpeg version
MIN_FFMPEG_VERSION = (7, 0, 0)  # FFmpeg 7.0.0+ for better hardware acceleration support

# GPU vendor to acceleration method mapping
# This defines which acceleration methods to use for each GPU vendor
GPU_ACCELERATION_MAP = {
    "NVIDIA": {
        "primary": "CUDA",
        "fallback": None,  # VAAPI doesn't work properly with NVIDIA
        "requires_runtime": True,  # Needs nvidia-docker runtime
        "test_encoder": "h264_nvenc",
    },
    "AMD": {
        "primary": "VAAPI",
        "fallback": None,
        "requires_runtime": False,
        "test_encoder": None,  # Use hwaccel test instead
    },
    # VAAPI only. QSV is not implemented: on Linux it runs through the
    # same iHD/oneVPL runtime as VAAPI (no decode-only speed gain) and
    # has documented H.264 instability on Intel Arc.
    "INTEL": {
        "primary": "VAAPI",
        "fallback": None,
        "requires_runtime": False,
        "test_encoder": None,
    },
    "ARM": {
        "primary": "VAAPI",
        "fallback": None,
        "requires_runtime": False,
        "test_encoder": None,
    },
    "VIDEOCORE": {
        "primary": "VAAPI",
        "fallback": None,
        "requires_runtime": False,
        "test_encoder": None,
    },
    "APPLE": {
        "primary": "VIDEOTOOLBOX",
        "fallback": None,
        "requires_runtime": False,
        "test_encoder": None,  # Use hwaccel test instead
    },
    "WINDOWS_GPU": {
        "primary": "D3D11VA",
        "fallback": None,
        "requires_runtime": False,
        "test_encoder": None,  # Use hwaccel test instead
    },
}

# Driver name to GPU vendor mapping
DRIVER_VENDOR_MAP = {
    "nvidia": "NVIDIA",
    "nouveau": "NVIDIA",
    "amdgpu": "AMD",
    "radeon": "AMD",
    "i915": "INTEL",
    "xe": "INTEL",  # New Intel graphics driver
    "panfrost": "ARM",
    "vc4": "VIDEOCORE",
}

# Kernel drivers that correspond to Intel GPUs (worth probing vainfo for
# the user-space VA-API driver identity, since the kernel driver name
# alone is misleading — i915/xe sit underneath iHD).
_INTEL_KERNEL_DRIVERS = frozenset({"i915", "xe"})


@lru_cache(maxsize=None)
def _probe_vaapi_driver(render_device: str) -> Optional[str]:
    """Return the user-space VA-API driver version string for a render node.

    Runs ``vainfo --display drm --device <render_device>`` and extracts
    the ``Driver version:`` line. Returns None on any failure (missing
    binary, timeout, parse failure) so callers can fall back to a
    legacy log format.

    Cached for the lifetime of the process: the underlying VA-API
    driver does not change at runtime, and three log sites probe the
    same device during startup.

    Args:
        render_device: Path to a DRM render node (e.g. ``/dev/dri/renderD128``).

    Returns:
        Optional[str]: The raw driver version string (e.g.
        ``"Intel iHD driver for Intel(R) Gen Graphics - 25.3.4"``) on
        success, or None if the probe could not determine a driver.

    """
    try:
        result = subprocess.run(
            ["vainfo", "--display", "drm", "--device", render_device],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    marker = "Driver version:"
    for line in result.stdout.splitlines():
        idx = line.find(marker)
        if idx == -1:
            continue
        value = line[idx + len(marker) :].strip()
        return value or None
    return None


def _format_driver_label(render_device: str, kernel_driver: str) -> str:
    """Build the parenthesised driver label for a GPU log line.

    For Intel GPUs the label shows both the kernel DRM driver (``i915``
    or ``xe``) and the user-space VA-API driver from ``vainfo``. For
    everything else, or when ``vainfo`` is unavailable, the label falls
    back to the legacy ``driver: <kernel_driver>`` format.

    Args:
        render_device: Render node path (e.g. ``/dev/dri/renderD128``).
        kernel_driver: Kernel driver name read from
            ``/sys/class/drm/cardX/device/driver``.

    Returns:
        str: Label without enclosing parens, suitable for inclusion in
        debug log lines, e.g. ``"kernel driver: i915, va-api driver:
        Intel iHD driver for Intel(R) Gen Graphics - 25.3.4"`` or
        ``"driver: i915"``.

    """
    if kernel_driver in _INTEL_KERNEL_DRIVERS:
        vaapi_driver = _probe_vaapi_driver(render_device)
        if vaapi_driver:
            return f"kernel driver: {kernel_driver}, va-api driver: {vaapi_driver}"
    return f"driver: {kernel_driver}"


def _get_ffmpeg_version() -> Optional[Tuple[int, int, int]]:
    """Get FFmpeg version as a tuple of integers.

    Returns:
        Optional[Tuple[int, int, int]]: Version tuple (major, minor, patch) or None if failed

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
            logger.debug(f"Failed to get FFmpeg version: {result.stderr}")
            return None

        # Extract version from first line: "ffmpeg version 7.1.1-1ubuntu1.2 Copyright..."
        version_line = result.stdout.split("\n")[0] if result.stdout else ""
        logger.debug(f"FFmpeg version string: '{version_line}'")

        # Special-case date-based git builds (e.g., "ffmpeg version 2025-10-12-git-...")
        # These are not semantic versions; treat as "unknown version" so we don't
        # incorrectly parse the year as the major version.
        if re.search(r"ffmpeg version \d{4}-\d{2}-\d{2}-", version_line):
            logger.debug(
                "Detected date-based FFmpeg git build; skipping semantic version parsing"
            )
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
                logger.debug(f"FFmpeg version detected: {major}.{minor}.{patch}")
                return (major, minor, patch)

        logger.debug(f"Could not parse FFmpeg version from: '{version_line}'")
        return None

    except Exception as e:
        logger.debug(f"Error getting FFmpeg version: {e}")
        return None


def _check_ffmpeg_version() -> bool:
    """Check if FFmpeg version meets minimum requirements.

    Returns:
        bool: True if version is sufficient, False otherwise

    """
    version = _get_ffmpeg_version()
    if version is None:
        logger.warning("Could not determine FFmpeg version - proceeding with caution")
        return True  # Don't fail if we can't determine version

    if version >= MIN_FFMPEG_VERSION:
        logger.debug(
            f"✓ FFmpeg version {version[0]}.{version[1]}.{version[2]} meets minimum requirement {MIN_FFMPEG_VERSION[0]}.{MIN_FFMPEG_VERSION[1]}.{MIN_FFMPEG_VERSION[2]}"
        )
        return True
    else:
        logger.warning(
            f"⚠ FFmpeg version {version[0]}.{version[1]}.{version[2]} is below minimum requirement {MIN_FFMPEG_VERSION[0]}.{MIN_FFMPEG_VERSION[1]}.{MIN_FFMPEG_VERSION[2]}"
        )
        logger.warning(
            "Hardware acceleration may not work properly. Please upgrade FFmpeg."
        )
        return False


def _get_ffmpeg_hwaccels() -> List[str]:
    """Get list of available FFmpeg hardware accelerators.

    Returns:
        List[str]: Available hardware accelerators

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
            logger.debug(f"Failed to get FFmpeg hardware accelerators: {result.stderr}")
            return []

        hwaccels = []
        for line in result.stdout.split("\n"):
            line = line.strip()
            if line and not line.startswith("Hardware acceleration methods:"):
                hwaccels.append(line)

        return hwaccels
    except Exception as e:
        logger.debug(f"Error getting FFmpeg hardware accelerators: {e}")
        return []


def _is_hwaccel_available(hwaccel: str) -> bool:
    """Check if a specific hardware acceleration is available.

    Args:
        hwaccel: Hardware acceleration type to check

    Returns:
        bool: True if available, False otherwise

    """
    available_hwaccels = _get_ffmpeg_hwaccels()
    is_available = hwaccel in available_hwaccels

    if is_available:
        logger.debug(f"✓ {hwaccel} hardware acceleration is available")
    else:
        logger.debug(f"✗ {hwaccel} hardware acceleration is not available")

    return is_available


def _check_device_access(device_path: str) -> tuple[bool, str]:
    """Check if a device is accessible (exists with read+write permission).

    VAAPI opens render devices with O_RDWR, so both read and write access are
    required for hardware acceleration to work.

    Args:
        device_path: Path to device to check

    Returns:
        tuple[bool, str]: (is_accessible, reason) where reason is:
            'accessible' - device exists and is read+write accessible
            'not_found' - device does not exist
            'permission_denied' - device exists but lacks required permissions

    """
    if not os.path.exists(device_path):
        logger.debug(f"✗ Device does not exist: {device_path}")
        return False, "not_found"

    if not os.access(device_path, os.R_OK | os.W_OK):
        try:
            stat_info = os.stat(device_path)
            import stat as stat_module

            mode = stat_info.st_mode
            owner_uid = stat_info.st_uid
            group_gid = stat_info.st_gid
            perms = stat_module.filemode(mode)

            has_read = os.access(device_path, os.R_OK)
            detail = "read-only" if has_read else "not readable"
            logger.debug(f"✗ Device exists but is {detail}: {device_path}")
            logger.debug(
                f"  Device permissions: {perms} (owner={owner_uid}, group={group_gid})"
            )
            logger.debug(f"  Current user: {os.getuid()}, groups: {os.getgroups()}")
        except Exception as e:
            logger.debug(f"✗ Device exists but is not accessible: {device_path}")
            logger.debug(f"  Current user: {os.getuid()}, groups: {os.getgroups()}")
            logger.debug(f"  Could not get device stats: {e}")
        return False, "permission_denied"

    logger.debug(f"✓ Device is accessible: {device_path}")
    return True, "accessible"


def _test_hwaccel_functionality(
    hwaccel: str, device_path: Optional[str] = None
) -> bool:
    """Test if hardware acceleration actually works by running a simple FFmpeg command.

    Args:
        hwaccel: Hardware acceleration type to test
        device_path: Optional device path for VAAPI

    Returns:
        bool: True if hardware acceleration works, False otherwise

    """
    try:
        # For VAAPI, check device accessibility first
        if hwaccel == "vaapi" and device_path:
            accessible, reason = _check_device_access(device_path)
            if not accessible:
                # Only show permission warnings if the device exists but is not accessible
                if reason == "permission_denied":
                    # Get device group for specific recommendation
                    try:
                        stat_info = os.stat(device_path)
                        device_gid = stat_info.st_gid
                        user_groups = os.getgroups()

                        logger.warning(
                            f"⚠ VAAPI device {device_path} is not accessible (permission denied)"
                        )
                        logger.warning(
                            f"⚠ Device group: {device_gid}, your groups: {user_groups}"
                        )

                        if device_gid not in user_groups:
                            logger.warning(
                                "⚠ The container should auto-detect GPU device groups at startup"
                            )
                            logger.warning(
                                "⚠ Verify you are passing --device /dev/dri:/dev/dri (not a single device)"
                            )
                        else:
                            logger.warning(
                                f"⚠ You are in group {device_gid}, but device is still not accessible"
                            )
                            logger.warning(
                                f"⚠ Check host device permissions: ls -l {device_path}"
                            )
                    except Exception:
                        logger.warning(
                            f"⚠ VAAPI device {device_path} is not accessible (permission denied)"
                        )
                        logger.warning(
                            "⚠ Verify --device /dev/dri:/dev/dri is passed and the device is readable on the host"
                        )
                # If device doesn't exist, just skip silently (expected for wrong GPU type)
                return False
        # Get test video fixture - all GPU tests use real H.264 video for accurate testing
        test_video = None
        possible_paths = [
            os.path.join(os.path.dirname(__file__), "fixtures", "test_video.mp4"),
            os.path.join(
                os.getcwd(), "plex_generate_previews", "fixtures", "test_video.mp4"
            ),
        ]

        for path in possible_paths:
            if os.path.exists(path):
                test_video = path
                break

        if not test_video:
            logger.debug("Test video fixture not found, cannot test GPU acceleration")
            return False

        # Build FFmpeg command - test hardware decode -> scale -> JPEG encode
        cmd = ["ffmpeg"]

        # Add hardware acceleration flags (before -i)
        if hwaccel == "cuda":
            cmd += ["-hwaccel", "cuda"]
        elif hwaccel == "vaapi" and device_path:
            cmd += ["-hwaccel", "vaapi", "-vaapi_device", device_path]
        elif hwaccel == "d3d11va":
            cmd += ["-hwaccel", "d3d11va"]
        elif hwaccel == "videotoolbox":
            cmd += ["-hwaccel", "videotoolbox"]
        else:
            cmd += ["-hwaccel", hwaccel]

        # Choose OS-appropriate null sink
        null_sink = "NUL" if is_windows() else "/dev/null"

        # Filters: D3D11VA requires downloading frames from GPU memory
        if hwaccel == "d3d11va":
            # Ensure frames are brought back to system memory before software filters
            # Also set explicit output format for d3d11
            if "-hwaccel_output_format" not in cmd:
                cmd += ["-hwaccel_output_format", "d3d11"]
            video_filter = "hwdownload,format=nv12,select=eq(n\\,0),scale=320:240"
        else:
            video_filter = "select=eq(n\\,0),scale=320:240"

        # Add input file and JPEG extraction
        cmd += [
            "-i",
            test_video,
            "-vf",
            video_filter,
            "-frames:v",
            "1",
            "-f",
            "image2",
            "-c:v",
            "mjpeg",
            "-q:v",
            "2",
            "-y",
            null_sink,
        ]

        logger.debug(f"Testing {hwaccel} functionality: {' '.join(cmd)}")
        result = subprocess.run(cmd, capture_output=True, timeout=20)

        # FFmpeg returns 0 for success, 141 for SIGPIPE (which is OK for our test)
        if result.returncode in [0, 141]:
            logger.debug(f"✓ {hwaccel} functionality test passed")
            return True
        else:
            logger.debug(
                f"✗ {hwaccel} functionality test failed (exit code: {result.returncode})"
            )

            if result.stderr:
                stderr_text = result.stderr.decode("utf-8", "ignore").strip()
                stderr_lower = stderr_text.lower()

                # VAAPI failures directly affect user-visible GPU status — log at WARNING
                if hwaccel == "vaapi":
                    logger.warning(
                        f"⚠ FFmpeg VAAPI test failed on {device_path} "
                        f"(exit code {result.returncode}):"
                    )
                    for line in stderr_text.splitlines()[-15:]:
                        if line.strip():
                            logger.warning(f"  {line.rstrip()}")

                    if "permission denied" in stderr_lower:
                        logger.warning(
                            "⚠ The container should auto-detect GPU device groups at startup"
                        )
                        logger.warning(
                            "⚠ Verify --device /dev/dri:/dev/dri is passed (not a single device)"
                        )
                else:
                    logger.debug(f"FFmpeg {hwaccel} stderr: {stderr_text[-500:]}")

                if hwaccel == "cuda":
                    if (
                        "/dev/null" in stderr_lower
                        and "operation not permitted" in stderr_lower
                    ):
                        logger.debug(
                            "Note: /dev/null errors usually indicate missing NVIDIA Container Toolkit"
                        )
                        logger.debug(
                            "      Run with --gpus all or configure nvidia-docker runtime"
                        )

            return False

    except subprocess.TimeoutExpired as e:
        logger.warning(
            f"⚠ {hwaccel} test timed out on {device_path or 'default device'} (>20s)"
        )
        if e.stderr:
            stderr_text = e.stderr.decode("utf-8", "ignore").strip()
            if stderr_text:
                logger.warning("⚠ FFmpeg output before hang:")
                for line in stderr_text.splitlines()[-15:]:
                    if line.strip():
                        logger.warning(f"  {line.rstrip()}")
        if hwaccel == "vaapi" and device_path:
            logger.warning(
                f"⚠ Debug: run 'vainfo --display drm --device {device_path}' "
                f"inside the container to check driver support"
            )
        return False
    except Exception as e:
        logger.debug(f"✗ {hwaccel} functionality test failed with exception: {e}")
        return False


_VULKAN_DEVICE_CACHE: Optional[str] = None
_VULKAN_DEVICE_PROBED: bool = False
_VULKAN_ENV_OVERRIDES: dict = {}
_VULKAN_DEBUG_BUFFER: str = ""

# Candidate paths the Vulkan loader searches for the NVIDIA ICD JSON. The
# nvidia-container-toolkit mounts it at /etc/vulkan/icd.d/, but older
# loaders and some distributions only look under /usr/share/vulkan/icd.d/
# (see nvidia-container-toolkit issue #1392). The retry strategy below
# tries each path in order.
_NVIDIA_ICD_JSON_PATHS = (
    "/etc/vulkan/icd.d/nvidia_icd.json",
    "/usr/share/vulkan/icd.d/nvidia_icd.json",
)

# Candidate paths for the GLVND NVIDIA EGL vendor config. nvidia-container-
# toolkit injects this at ``/usr/share/glvnd/egl_vendor.d/10_nvidia.json``
# when the ``graphics`` driver capability is declared; it tells the GLVND
# libEGL dispatcher which vendor library (``libEGL_nvidia.so.0``) to use.
#
# WHY THIS MATTERS for the DV5 Vulkan path:
# NVIDIA's libGLX_nvidia.so.0 is both a GLX backend AND the Vulkan ICD.
# During Vulkan ICD initialisation, its constructor dlopens ``libEGL.so.1``
# for an internal EGL capability probe. On the linuxserver/ffmpeg base
# image, libEGL.so.1 is GLVND's dispatcher (not NVIDIA's own EGL), so the
# probe goes through GLVND's vendor selection. If GLVND has no vendor hint,
# it picks whichever vendor file is first on disk — which on this image is
# Mesa's, not NVIDIA's. The EGL probe then returns a degraded context,
# NVIDIA's ICD silently marks itself unusable, and
# ``vk_icdGetInstanceProcAddr(NULL, "vkCreateInstance")`` returns NULL.
# Result: ``VK_ERROR_INCOMPATIBLE_DRIVER`` and a llvmpipe fallback.
#
# Setting ``__EGL_VENDOR_LIBRARY_FILENAMES=<path-to-10_nvidia.json>`` tells
# GLVND to use the NVIDIA vendor directly, the EGL probe succeeds, and
# libGLX_nvidia's Vulkan ICD wakes up. This is the Strategy-2 fix.
_NVIDIA_EGL_VENDOR_JSON_PATHS = (
    "/usr/share/glvnd/egl_vendor.d/10_nvidia.json",
    "/etc/glvnd/egl_vendor.d/10_nvidia.json",
)

# Cap on the size of the VK_LOADER_DEBUG=all capture buffer. One run of
# the diagnostic probe is typically 5–15 KB of loader trace; 20 KB is a
# comfortable upper bound that still fits in a GitHub issue comment.
_VULKAN_DEBUG_BUFFER_CAP = 20_000


def _is_software_vulkan_device(device: Optional[str]) -> bool:
    """Return True if ``device`` is a software rasterizer (llvmpipe/lavapipe)."""
    if not device:
        return False
    d = device.lower()
    return "llvmpipe" in d or "software" in d or "lavapipe" in d


def _find_nvidia_icd_json() -> Optional[str]:
    """Return the path to ``nvidia_icd.json`` if present at a standard location.

    Checks both the nvidia-container-toolkit mount path
    (``/etc/vulkan/icd.d/``) and the loader's legacy search path
    (``/usr/share/vulkan/icd.d/``). Returns the first match or None.
    """
    for path in _NVIDIA_ICD_JSON_PATHS:
        if os.path.exists(path):
            return path
    return None


def _find_nvidia_egl_vendor_json() -> Optional[str]:
    """Return the path to the GLVND NVIDIA EGL vendor JSON if present.

    Checks both the nvidia-container-toolkit mount path
    (``/usr/share/glvnd/egl_vendor.d/``) and the per-host override
    (``/etc/glvnd/egl_vendor.d/``). Returns the first match or None.
    """
    for path in _NVIDIA_EGL_VENDOR_JSON_PATHS:
        if os.path.exists(path):
            return path
    return None


# Glob patterns for ``libEGL_nvidia.so*``. nvidia-container-toolkit mounts
# this library when the ``graphics`` driver capability is declared, and
# Strategy 2c (below) needs to know whether it's present before trying to
# route GLVND's libEGL lookup at it. If it isn't mounted, synthesising a
# ``10_nvidia.json`` that points at ``libEGL_nvidia.so.0`` is a dead end.
_LIBEGL_NVIDIA_GLOBS = (
    "/usr/lib/x86_64-linux-gnu/libEGL_nvidia.so*",
    "/usr/lib/aarch64-linux-gnu/libEGL_nvidia.so*",
    "/usr/lib/libEGL_nvidia.so*",
    "/usr/lib64/libEGL_nvidia.so*",
)


def _find_libegl_nvidia() -> Optional[str]:
    """Return the first path to ``libEGL_nvidia.so*`` if the library is present.

    Searches the standard Debian multiarch paths plus the classic
    ``/usr/lib`` / ``/usr/lib64`` fallbacks that nvidia-container-toolkit
    uses when mounting the ``graphics`` capability.  Returns the first
    match or None.  Used by :func:`_probe_vulkan_device` Strategy 2c to
    decide whether synthesising a GLVND vendor JSON is useful: if the
    library is absent, GLVND would route to a file that doesn't exist
    and the fix would no-op.
    """
    for pattern in _LIBEGL_NVIDIA_GLOBS:
        matches = glob.glob(pattern)
        if matches:
            return matches[0]
    return None


def _run_vulkan_probe(
    env_overrides: Optional[dict] = None,
) -> tuple[Optional[str], str]:
    """Run a single Vulkan init probe and return ``(device, full_stderr)``.

    Runs a trivial FFmpeg command with ``-init_hw_device vulkan=vk`` at
    ``-loglevel debug`` and parses the ``Device N selected:`` line emitted
    by FFmpeg's Vulkan hwcontext. Returns the parsed device name (or None
    on miss/failure) and the full stderr for optional downstream use
    (e.g. a ``VK_LOADER_DEBUG=all`` diagnostic capture).

    Args:
        env_overrides: Optional env vars to merge into the subprocess
            environment. Used by the Layer-3 retry strategy to force
            ``VK_DRIVER_FILES`` and/or enable ``VK_LOADER_DEBUG=all``.
    """
    if not _is_hwaccel_available("vulkan"):
        # DEBUG only: get_vulkan_device_info() will log a single
        # user-facing INFO line summarising the final outcome. Logging
        # here would fire once per retry strategy (up to 4 times) and
        # clutter the startup log on FFmpeg builds without Vulkan.
        logger.debug(
            "Vulkan probe: FFmpeg was built without Vulkan hwaccel support; "
            "libplacebo DV Profile 5 tone mapping will run in software."
        )
        return None, ""
    cmd = [
        "ffmpeg",
        "-loglevel",
        "debug",
        "-init_hw_device",
        "vulkan=vk",
        "-f",
        "lavfi",
        "-i",
        "nullsrc",
        "-frames:v",
        "1",
        "-f",
        "null",
        "-",
    ]
    env = None
    if env_overrides:
        env = os.environ.copy()
        env.update(env_overrides)
    logger.debug(
        f"Vulkan probe: running {' '.join(cmd)}"
        + (f" with env overrides {env_overrides}" if env_overrides else "")
    )
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        logger.warning(
            f"Vulkan probe failed (subprocess error: {exc}); "
            "falling back to 'no Vulkan device' for DV5 diagnosis."
        )
        return None, str(exc)
    except Exception as exc:
        logger.warning(
            f"Vulkan probe raised unexpected exception: {exc}; "
            "falling back to 'no Vulkan device' for DV5 diagnosis."
        )
        return None, str(exc)
    stderr = result.stderr or ""
    for line in stderr.splitlines():
        # Matches e.g. "[Vulkan @ 0x...] Device 0 selected: Intel(R) Graphics (RPL-S) (integrated) (0xa780)"
        # or         "[Vulkan @ 0x...] Device 0 selected: llvmpipe (LLVM 18.1.3, 256 bits) (software) (0x0)"
        if "Device" in line and "selected:" in line:
            return line.split("selected:", 1)[1].strip(), stderr
    return None, stderr


def _probe_vulkan_device() -> Optional[str]:
    """Return the Vulkan device libplacebo will use, running up to three strategies.

    **Strategy 1** — default probe with inherited environment. Lets the
    Vulkan loader pick whichever ICD it wants. This is the happy path
    for correctly-configured hosts.

    **Strategy 2** — if Strategy 1 returned a software rasterizer
    (``llvmpipe``/``lavapipe``) or nothing AND the GLVND NVIDIA EGL
    vendor JSON is present, retry the probe with
    ``__EGL_VENDOR_LIBRARY_FILENAMES`` pointing at it. See the comment
    on :data:`_NVIDIA_EGL_VENDOR_JSON_PATHS` above for the full
    mechanism; the short version is that NVIDIA's
    ``libGLX_nvidia.so.0`` runs a ``dlopen("libEGL.so.1")`` probe
    during Vulkan ICD init, GLVND picks the Mesa vendor by default on
    the linuxserver/ffmpeg base image, and that causes NVIDIA's
    ``vk_icdGetInstanceProcAddr`` to return NULL for
    ``vkCreateInstance``. The env var forces GLVND to pick the NVIDIA
    vendor, and the ICD wakes up. Verified empirically on an NVIDIA
    TITAN RTX + driver 590.48.01 + linuxserver/ffmpeg 8.0.1-cli-ls56.

    **Strategy 2b** — if the EGL vendor JSON is not present but the
    NVIDIA ICD JSON is (or if Strategy 2 ran but did not fix things),
    fall back to forcing ``VK_DRIVER_FILES`` at the NVIDIA ICD. This
    is the older heuristic; kept as a secondary because some
    nvidia-container-toolkit releases inject the ICD JSON but not the
    GLVND vendor config (see
    `nvidia-container-toolkit#1559 <https://github.com/NVIDIA/nvidia-container-toolkit/issues/1559>`_).
    If the forced probe succeeds, the env override is stashed in
    ``_VULKAN_ENV_OVERRIDES`` so :func:`get_vulkan_env_overrides` can
    feed it into the real FFmpeg invocation on the libplacebo path.

    **Strategy 3** — if 1 and both 2 branches failed, run one final
    probe with ``VK_LOADER_DEBUG=all`` (plus whichever env vars are
    available) and capture the full stderr into
    ``_VULKAN_DEBUG_BUFFER`` so users can copy-paste it into a GitHub
    issue via ``GET /api/system/vulkan/debug``. The strategy does NOT
    attempt to return a device from the diagnostic probe — it just
    captures the trace for human diagnosis.

    Returns:
        The Vulkan device description the libplacebo path will actually
        use (Strategy 1 or Strategy 2 success), the software rasterizer
        from Strategy 1 if nothing else worked, or ``None`` if Vulkan is
        completely unavailable.
    """
    global _VULKAN_ENV_OVERRIDES, _VULKAN_DEBUG_BUFFER

    # Strategy 1: default probe.
    device, _ = _run_vulkan_probe()
    if device and not _is_software_vulkan_device(device):
        logger.debug(
            f"Vulkan probe (strategy 1): FFmpeg selected hardware device: {device}"
        )
        return device

    if device:
        logger.debug(
            f"Vulkan probe (strategy 1): got software device {device!r}; "
            "will attempt NVIDIA-specific retries"
        )
    else:
        logger.debug(
            "Vulkan probe (strategy 1): no 'Device N selected:' line; "
            "will attempt NVIDIA-specific retries"
        )

    nvidia_egl_vendor = _find_nvidia_egl_vendor_json()
    nvidia_icd = _find_nvidia_icd_json()

    # Strategy 2: point GLVND at NVIDIA's EGL vendor via
    # __EGL_VENDOR_LIBRARY_FILENAMES. This is the verified fix for the
    # linuxserver/ffmpeg + NVIDIA case — see the doc comment on
    # _NVIDIA_EGL_VENDOR_JSON_PATHS above.
    if nvidia_egl_vendor:
        logger.debug(
            f"Vulkan probe (strategy 2): forcing "
            f"__EGL_VENDOR_LIBRARY_FILENAMES={nvidia_egl_vendor}"
        )
        retry_env = {"__EGL_VENDOR_LIBRARY_FILENAMES": nvidia_egl_vendor}
        retry_device, _ = _run_vulkan_probe(retry_env)
        if retry_device and not _is_software_vulkan_device(retry_device):
            logger.debug(
                f"Vulkan probe (strategy 2): success with {retry_device!r} "
                f"via __EGL_VENDOR_LIBRARY_FILENAMES={nvidia_egl_vendor}"
            )
            _VULKAN_ENV_OVERRIDES = dict(retry_env)
            return retry_device
        logger.debug(
            f"Vulkan probe (strategy 2): forcing "
            f"__EGL_VENDOR_LIBRARY_FILENAMES={nvidia_egl_vendor} "
            f"still returned {retry_device!r}; trying Strategy 2b."
        )
    else:
        logger.debug(
            "Vulkan probe: no NVIDIA GLVND EGL vendor JSON found at "
            f"{_NVIDIA_EGL_VENDOR_JSON_PATHS}; skipping Strategy 2."
        )

    # Strategy 2c: synthesise a GLVND NVIDIA EGL vendor JSON into a
    # temp file when one doesn't exist on disk AND ``libEGL_nvidia.so``
    # is present in the container.  This is the fix for users whose
    # ``nvidia-container-toolkit`` mounts the NVIDIA libraries (ICD,
    # libEGL_nvidia, libGLX_nvidia, the glvkspirv SPIR-V compiler, ...)
    # but does NOT mount the single tiny ``10_nvidia.json`` GLVND vendor
    # config that tells the libEGL dispatcher which vendor library to
    # hand out.  Without that file, GLVND picks whichever vendor config
    # is first on disk — which is Mesa's on the linuxserver/ffmpeg image
    # — the libGLX_nvidia init-time EGL probe gets a Mesa context, and
    # NVIDIA's Vulkan ICD quietly marks itself unusable.
    #
    # NVIDIA's own "minimal Docker Vulkan offscreen setup" guidance on
    # forums.developer.nvidia.com (thread id 242883) confirms that the
    # GLVND vendor JSON is required and that it is a three-line file:
    #
    #     {"file_format_version":"1.0.0",
    #      "ICD":{"library_path":"libEGL_nvidia.so.0"}}
    #
    # We write that verbatim to ``{tempdir}/plex_previews_nvidia_egl_
    # vendor.json`` and set ``__EGL_VENDOR_LIBRARY_FILENAMES`` at it.
    # The library_path stays bare so the dynamic loader resolves it via
    # the standard search path (exactly what NVIDIA's own Dockerfile
    # does).  Gated on ``libEGL_nvidia.so*`` actually being present in
    # the container so we don't fabricate a pointer to a file that
    # doesn't exist.
    if nvidia_egl_vendor is None:
        libegl_nvidia = _find_libegl_nvidia()
        if libegl_nvidia:
            synth_vendor_path = os.path.join(
                tempfile.gettempdir(), "plex_previews_nvidia_egl_vendor.json"
            )
            synth_payload = {
                "file_format_version": "1.0.0",
                "ICD": {"library_path": "libEGL_nvidia.so.0"},
            }
            try:
                with open(synth_vendor_path, "w", encoding="utf-8") as fh:
                    json.dump(synth_payload, fh)
                logger.debug(
                    f"Vulkan probe (strategy 2c): synthesised GLVND NVIDIA "
                    f"EGL vendor JSON at {synth_vendor_path} "
                    f"(libEGL_nvidia.so found at {libegl_nvidia}); retrying probe"
                )
                retry_env = {"__EGL_VENDOR_LIBRARY_FILENAMES": synth_vendor_path}
                retry_device, _ = _run_vulkan_probe(retry_env)
                if retry_device and not _is_software_vulkan_device(retry_device):
                    logger.info(
                        f"Vulkan probe (strategy 2c): success with "
                        f"{retry_device!r} via synthesised GLVND vendor JSON "
                        f"at {synth_vendor_path}"
                    )
                    _VULKAN_ENV_OVERRIDES = dict(retry_env)
                    return retry_device
                logger.debug(
                    f"Vulkan probe (strategy 2c): synthesised vendor JSON "
                    f"probe still returned {retry_device!r}; trying Strategy 2b."
                )
            except OSError as exc:
                logger.debug(
                    f"Vulkan probe (strategy 2c): could not write "
                    f"{synth_vendor_path}: {exc}; trying Strategy 2b."
                )
        else:
            logger.debug(
                "Vulkan probe: no libEGL_nvidia.so* found in standard "
                f"library paths ({_LIBEGL_NVIDIA_GLOBS}); skipping Strategy 2c."
            )

    # Strategy 2b: older heuristic — force VK_DRIVER_FILES at the NVIDIA
    # ICD. Kept for the case where the ICD JSON is injected but the EGL
    # vendor config is not (nvidia-container-toolkit#1559 / partial CDI
    # manifests), and for general belt-and-suspenders coverage.
    if nvidia_icd:
        logger.debug(
            f"Vulkan probe (strategy 2b): forcing VK_DRIVER_FILES={nvidia_icd}"
        )
        # If Strategy 2 ran and found an EGL vendor, carry it through
        # the 2b retry as well so the two fixes stack.
        retry_env = {"VK_DRIVER_FILES": nvidia_icd}
        if nvidia_egl_vendor:
            retry_env["__EGL_VENDOR_LIBRARY_FILENAMES"] = nvidia_egl_vendor
        retry_device, _ = _run_vulkan_probe(retry_env)
        if retry_device and not _is_software_vulkan_device(retry_device):
            logger.debug(
                f"Vulkan probe (strategy 2b): success with {retry_device!r} "
                f"via {retry_env}"
            )
            _VULKAN_ENV_OVERRIDES = dict(retry_env)
            return retry_device
        logger.debug(
            f"Vulkan probe (strategy 2b): forcing VK_DRIVER_FILES={nvidia_icd} "
            f"still returned {retry_device!r}; running diagnostic capture."
        )
    else:
        logger.debug(
            "Vulkan probe: no NVIDIA ICD JSON found at "
            f"{_NVIDIA_ICD_JSON_PATHS}; skipping Strategy 2b."
        )

    # Strategy 3: VK_LOADER_DEBUG=all capture for issue reports.
    diag_overrides: dict = {"VK_LOADER_DEBUG": "all"}
    if nvidia_egl_vendor:
        diag_overrides["__EGL_VENDOR_LIBRARY_FILENAMES"] = nvidia_egl_vendor
    if nvidia_icd:
        diag_overrides["VK_DRIVER_FILES"] = nvidia_icd
    _, diag_stderr = _run_vulkan_probe(diag_overrides)
    _VULKAN_DEBUG_BUFFER = (diag_stderr or "")[-_VULKAN_DEBUG_BUFFER_CAP:]
    # One-line WARNING at Strategy-3 exit is fine because it only runs
    # once per probe (first call to get_vulkan_device_info), and only
    # when everything else has already failed — i.e. the user DOES have
    # a real problem worth seeing in the main log.
    logger.warning(
        f"Vulkan probe: all strategies exhausted. Captured "
        f"{len(_VULKAN_DEBUG_BUFFER)} bytes of VK_LOADER_DEBUG=all output "
        "for issue reports (GET /api/system/vulkan/debug)."
    )
    if _VULKAN_DEBUG_BUFFER:
        # Surface the last few informative lines to the main log at
        # DEBUG level so a user reading `docker logs --tail` in the
        # normal INFO flow isn't overwhelmed, but issue reporters with
        # LOG_LEVEL=DEBUG get the immediate hint without hitting the
        # dashboard debug endpoint.
        for line in _VULKAN_DEBUG_BUFFER.splitlines()[-15:]:
            logger.debug(f"  ffmpeg/vulkan-loader stderr: {line}")

    # Return whatever Strategy 1 found so `get_vulkan_device_info` can
    # correctly classify it as software (or None) and render the banner.
    return device


def get_vulkan_device_info() -> dict:
    """Return cached Vulkan device info for libplacebo diagnostics.

    The underlying probe (including Strategy-2 retry and Strategy-3
    diagnostic capture) is cached across calls at module level because
    it runs subprocesses and its result does not change during the
    app's lifetime — the container's Vulkan environment is fixed at
    startup.

    Returns:
        dict: Contains ``device`` (Vulkan device description string, or
            ``None`` if Vulkan is unavailable) and ``is_software`` (True
            when the selected device is a software rasteriser like
            ``llvmpipe``/``lavapipe``, which triggers the DV5 green
            overlay bug in libplacebo). Callers assemble the
            user-facing warning message themselves.
    """
    global _VULKAN_DEVICE_CACHE, _VULKAN_DEVICE_PROBED
    if not _VULKAN_DEVICE_PROBED:
        logger.debug("Vulkan device info: running first-time probe")
        _VULKAN_DEVICE_CACHE = _probe_vulkan_device()
        _VULKAN_DEVICE_PROBED = True

        # First-time probe finished — log the outcome exactly once.
        # Every subsequent call returns the cached dict silently. The
        # three branches below are intentionally mutually exclusive:
        #   - INFO on success (single line, user-friendly)
        #   - WARNING on software fallback (action needed)
        #   - INFO on no-Vulkan (informational, harmless)
        probe_device = _VULKAN_DEVICE_CACHE
        if probe_device is None:
            logger.info(
                "Vulkan not available in this container; Dolby Vision "
                "Profile 5 thumbnails will render in software. Non-DV5 "
                "content is unaffected."
            )
        elif _is_software_vulkan_device(probe_device):
            logger.warning(
                f"Vulkan probe selected a software rasterizer "
                f"({probe_device}); Dolby Vision Profile 5 thumbnails "
                "will show a green overlay. Open the dashboard or "
                "GET /api/system/vulkan/debug for GPU-specific "
                "remediation steps and a full diagnostic bundle."
            )
        else:
            via = ""
            if _VULKAN_ENV_OVERRIDES:
                override_keys = ", ".join(sorted(_VULKAN_ENV_OVERRIDES))
                via = f" (via {override_keys} override)"
            logger.debug(
                f"Vulkan ready for Dolby Vision Profile 5 tone-mapping: "
                f"{probe_device}{via}"
            )

    device = _VULKAN_DEVICE_CACHE
    if device is None:
        return {"device": None, "is_software": False}
    return {
        "device": device,
        "is_software": _is_software_vulkan_device(device),
    }


def get_vulkan_env_overrides() -> dict:
    """Return env vars to inject into FFmpeg subprocess calls on the libplacebo path.

    Populated by the Strategy-2 (or Strategy-2b) retry in
    :func:`_probe_vulkan_device` when the default Vulkan ICD search
    did not yield a hardware device. Returns an empty dict when no
    overrides are needed (happy path: the loader finds the right ICD
    on its own).

    **Side effect by design:** if the probe has not yet run (e.g. the
    worker thread calling from :func:`media_processing._run_ffmpeg`
    on the libplacebo DV Profile 5 path beats the first
    ``/api/system/vulkan`` poll), this function triggers the probe
    synchronously via :func:`get_vulkan_device_info` before returning.
    Without this auto-trigger, any job that starts before an HTTP
    endpoint is hit would get an empty override dict and would fall
    back to software Vulkan even when the retry would have fixed it.
    """
    if not _VULKAN_DEVICE_PROBED:
        get_vulkan_device_info()
    return dict(_VULKAN_ENV_OVERRIDES)


def get_vulkan_debug_buffer() -> str:
    """Return the captured ``VK_LOADER_DEBUG=all`` stderr from the last probe.

    Populated by Strategy 3 in :func:`_probe_vulkan_device` when both
    the default probe and the ``VK_DRIVER_FILES`` retry failed. Empty
    string when no diagnostic capture was needed. Consumed by the
    ``GET /api/system/vulkan/debug`` endpoint and the "Copy diagnostic
    bundle" button on the dashboard/settings warning banner.

    Same auto-trigger behaviour as :func:`get_vulkan_env_overrides`:
    if the probe has not yet run, it runs synchronously first so the
    caller never sees an empty buffer just because no HTTP endpoint
    has been hit yet.
    """
    if not _VULKAN_DEVICE_PROBED:
        get_vulkan_device_info()
    return _VULKAN_DEBUG_BUFFER


def _reset_vulkan_device_cache() -> None:
    """Testing hook: clear the cached Vulkan probe result and diagnostic state.

    Only intended for unit tests that need to rerun the probe with a
    different mock. Clears all four module-level globals so a new
    probe strategy run starts fresh.
    """
    global _VULKAN_DEVICE_CACHE, _VULKAN_DEVICE_PROBED
    global _VULKAN_ENV_OVERRIDES, _VULKAN_DEBUG_BUFFER
    _VULKAN_DEVICE_CACHE = None
    _VULKAN_DEVICE_PROBED = False
    _VULKAN_ENV_OVERRIDES = {}
    _VULKAN_DEBUG_BUFFER = ""


def _is_wsl2() -> bool:
    """Detect if running on Windows Subsystem for Linux 2 (WSL2).

    WSL2 has limited hardware access due to virtualization, which can affect
    GPU detection via lspci. This function helps identify WSL2 environments
    for special handling.

    Returns:
        bool: True if running on WSL2, False otherwise

    """
    if platform.system() != "Linux":
        return False

    try:
        # Check /proc/version for WSL2 indicators
        # WSL2 typically contains "microsoft" and "WSL" in /proc/version
        version_file = "/proc/version"
        if os.path.exists(version_file):
            with open(version_file, "r", encoding="utf-8", errors="replace") as f:
                version_text = f.read().lower()
                if "microsoft" in version_text and "wsl" in version_text:
                    logger.debug("Detected WSL2 environment")
                    return True
    except Exception as e:
        logger.debug(f"Error checking for WSL2: {e}")

    return False


def _get_apple_gpu_name() -> str:
    """Get Apple GPU name from system_profiler.

    Returns:
        str: GPU name or fallback description

    """
    try:
        result = subprocess.run(
            ["system_profiler", "SPDisplaysDataType"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            # Parse output for GPU name
            # Look for "Chipset Model:" line
            for line in result.stdout.split("\n"):
                if "Chipset Model:" in line:
                    gpu_name = line.split(":", 1)[1].strip()
                    logger.debug(f"Detected Apple GPU: {gpu_name}")
                    return gpu_name
    except Exception as e:
        logger.debug(f"Error getting Apple GPU name: {e}")

    # Fallback - check for Apple Silicon using platform
    machine = platform.machine()
    if machine == "arm64":
        return "Apple Silicon GPU"

    return "Apple GPU"


def _get_gpu_devices() -> List[Tuple[str, str, str]]:
    """Get all GPU devices with their render devices and driver information.

    Returns:
        List[Tuple[str, str, str]]: List of (card_name, render_device, driver) tuples

    """
    devices = []
    drm_dir = "/sys/class/drm"

    # Skip Linux DRM scan entirely on non-Linux platforms to avoid misleading logs
    if platform.system() != "Linux":
        return devices

    if not os.path.exists(drm_dir):
        logger.debug(f"DRM directory {drm_dir} does not exist")
        return devices

    try:
        entries = os.listdir(drm_dir)
        logger.debug(f"Scanning DRM devices: {entries}")

        for entry in entries:
            if not entry.startswith("card") or "-" in entry:
                continue  # Skip card1-HDMI-A-1, card0-DP-2, etc.

            # Validate card number format
            try:
                int(entry[4:])  # card0 -> 0, card1 -> 1
            except ValueError:
                continue

            # Get render device for this card by checking device symlinks
            # Cannot assume renderD128 -> card0, must check actual device paths
            card_device_path = os.path.realpath(os.path.join(drm_dir, entry, "device"))
            render_device = None

            for render_entry in entries:
                if not render_entry.startswith("renderD"):
                    continue
                try:
                    render_device_path = os.path.realpath(
                        os.path.join(drm_dir, render_entry, "device")
                    )
                    if card_device_path == render_device_path:
                        render_device = f"/dev/dri/{render_entry}"
                        break
                except OSError:
                    continue

            if not render_device:
                logger.debug(f"No render device found for {entry}")
                continue

            # Skip GPUs visible in sysfs but not mounted into the container
            if not os.path.exists(render_device):
                logger.debug(
                    f"Skipping {entry}: {render_device} not present in /dev/dri"
                )
                continue

            # Get driver information
            driver_path = os.path.join(drm_dir, entry, "device", "driver")
            driver = "unknown"
            if os.path.islink(driver_path):
                driver = os.path.basename(os.readlink(driver_path))

            devices.append((entry, render_device, driver))
            label = _format_driver_label(render_device, driver)
            logger.debug(f"Found GPU: {entry} -> {render_device} ({label})")

    except Exception as e:
        logger.debug(f"Error scanning GPU devices: {e}")

    return devices


def _scan_dev_dri_render_devices() -> List[str]:
    """Scan /dev/dri directly for render device nodes.

    Fallback for container environments (e.g. TrueNAS Scale, Kubernetes) where
    /sys/class/drm is unavailable but GPU devices are mounted via --device.

    Returns:
        List[str]: Sorted list of render device paths (e.g. ['/dev/dri/renderD128'])

    """
    dev_dri = "/dev/dri"
    if not os.path.isdir(dev_dri):
        return []
    try:
        return sorted(
            os.path.join(dev_dri, entry)
            for entry in os.listdir(dev_dri)
            if entry.startswith("renderD")
            and os.access(os.path.join(dev_dri, entry), os.R_OK)
        )
    except OSError as e:
        logger.debug(f"Error scanning {dev_dri}: {e}")
        return []


def _get_gpu_vendor_from_driver(driver_name: str) -> str:
    """Map driver name to GPU vendor using DRIVER_VENDOR_MAP.

    Args:
        driver_name: Linux driver name (e.g., 'i915', 'nvidia', 'amdgpu')

    Returns:
        str: GPU vendor ('NVIDIA', 'AMD', 'INTEL', 'ARM', 'VIDEOCORE', or 'UNKNOWN')

    """
    vendor = DRIVER_VENDOR_MAP.get(driver_name, "UNKNOWN")

    if vendor == "UNKNOWN":
        logger.debug(f"Unknown driver '{driver_name}', attempting lspci detection")
        vendor = _detect_gpu_type_from_lspci()

        # If lspci failed and we're in WSL2, try nvidia-smi as fallback
        # This helps detect NVIDIA GPUs in WSL2 where lspci doesn't work
        if vendor == "UNKNOWN" and _is_wsl2():
            logger.debug(
                "WSL2 detected and lspci failed, attempting nvidia-smi detection"
            )
            logger.warning(
                "⚠️  WSL2 environment detected - GPU vendor detection via lspci is unreliable"
            )
            logger.warning(
                "⚠️  NVIDIA GPUs are unofficially supported in WSL2 (via CUDA passthrough)"
            )
            logger.warning(
                "⚠️  Other GPU vendors (AMD, Intel) are NOT supported in WSL2"
            )
            logger.warning(
                "⚠️  For non-NVIDIA GPUs in WSL2, please use CPU-only processing (disable GPUs in Settings)"
            )
            nvidia_vendor = _detect_nvidia_via_nvidia_smi()
            if nvidia_vendor == "NVIDIA":
                vendor = "NVIDIA"
                logger.debug("Successfully detected NVIDIA GPU via nvidia-smi in WSL2")

    return vendor


def _detect_nvidia_via_nvidia_smi() -> str:
    """Detect NVIDIA GPU using nvidia-smi as fallback when driver detection fails.

    This is useful in WSL2 environments where lspci cannot detect GPU vendors
    due to hardware virtualization, but nvidia-smi works correctly via the
    Windows NVIDIA driver passthrough.

    Returns:
        str: 'NVIDIA' if NVIDIA GPU is detected, 'UNKNOWN' otherwise

    """
    try:
        # Check if nvidia-smi is available and can query GPU information
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True,
            text=True,
            timeout=5,
        )

        if result.returncode == 0 and result.stdout.strip():
            # nvidia-smi successfully returned GPU names
            gpu_names = [
                line.strip()
                for line in result.stdout.strip().split("\n")
                if line.strip()
            ]
            if gpu_names:
                logger.debug(
                    f"nvidia-smi detected {len(gpu_names)} NVIDIA GPU(s): {gpu_names}"
                )
                return "NVIDIA"

        # nvidia-smi failed or returned no GPUs
        if result.returncode != 0:
            logger.debug(f"nvidia-smi failed with return code: {result.returncode}")
            if result.stderr:
                logger.debug(f"nvidia-smi stderr: {result.stderr.strip()}")
        else:
            logger.debug("nvidia-smi returned no GPU information")

        return "UNKNOWN"
    except FileNotFoundError:
        # nvidia-smi not installed
        logger.debug(
            "nvidia-smi command not found (NVIDIA driver may not be installed)"
        )
        return "UNKNOWN"
    except subprocess.TimeoutExpired:
        logger.debug("nvidia-smi command timed out after 5 seconds")
        return "UNKNOWN"
    except Exception as e:
        logger.debug(f"Error running nvidia-smi: {e}")
        return "UNKNOWN"


def _detect_gpu_type_from_lspci() -> str:
    """Detect GPU type using lspci as fallback when driver detection fails.

    This is a non-critical optional enhancement. If lspci is not available
    or fails for any reason, it safely returns 'UNKNOWN' without logging errors.

    Returns:
        str: GPU type ('AMD', 'INTEL', 'NVIDIA', 'ARM', or 'UNKNOWN')

    """
    try:
        result = subprocess.run(["lspci"], capture_output=True, text=True, timeout=5)
        if result.returncode != 0:
            logger.debug(f"lspci command failed with return code: {result.returncode}")
            if result.stderr:
                logger.debug(f"lspci stderr: {result.stderr.strip()}")
            if result.stdout:
                logger.debug(f"lspci stdout (partial): {result.stdout[:200]}")
            return "UNKNOWN"

        # Check if we have any output
        if not result.stdout.strip():
            logger.debug("lspci returned empty output")
            return "UNKNOWN"

        # Count VGA/Display lines for debugging
        vga_lines = [
            line
            for line in result.stdout.split("\n")
            if "VGA" in line or "Display" in line
        ]
        if not vga_lines:
            logger.debug("lspci did not find any VGA or Display devices in output")
            logger.debug(f"lspci output (first 500 chars): {result.stdout[:500]}")
            return "UNKNOWN"

        logger.debug(f"lspci found {len(vga_lines)} VGA/Display device(s)")
        for line in vga_lines:
            logger.debug(f"lspci VGA/Display line: {line.strip()}")
            line_lower = line.lower()
            if "amd" in line_lower or "radeon" in line_lower:
                logger.debug("lspci detected AMD GPU")
                return "AMD"
            elif "intel" in line_lower:
                logger.debug("lspci detected Intel GPU")
                return "INTEL"
            elif "nvidia" in line_lower or "geforce" in line_lower:
                logger.debug("lspci detected NVIDIA GPU")
                return "NVIDIA"
            elif "mali" in line_lower or "arm" in line_lower:
                logger.debug("lspci detected ARM GPU")
                return "ARM"

        logger.debug(
            "lspci found VGA/Display devices but did not identify known GPU vendor"
        )
        logger.debug(f"VGA/Display lines: {[line.strip() for line in vga_lines]}")
        return "UNKNOWN"
    except FileNotFoundError:
        # lspci not installed - this is expected in many environments
        logger.debug("lspci command not found (not installed)")
        return "UNKNOWN"
    except subprocess.TimeoutExpired:
        logger.debug("lspci command timed out after 5 seconds")
        return "UNKNOWN"
    except Exception as e:
        logger.debug(f"Error running lspci: {e}")
        return "UNKNOWN"


def _log_system_info() -> None:
    """Log system information for debugging GPU detection issues."""
    logger.debug("=== System Information ===")
    logger.debug(f"Platform: {platform.platform()}")
    logger.debug(f"Python version: {platform.python_version()}")
    logger.debug(f"FFmpeg path: {os.environ.get('FFMPEG_PATH', 'ffmpeg')}")

    # Check FFmpeg version
    _check_ffmpeg_version()

    # Log available hardware accelerators
    hwaccels = _get_ffmpeg_hwaccels()
    if hwaccels:
        logger.debug(f"Available FFmpeg hardware accelerators: {hwaccels}")

    # Log GPU device mapping (standard Linux devices)
    gpu_devices = _get_gpu_devices()
    if gpu_devices:
        logger.debug("GPU device mapping:")
        for card_name, render_device, driver in gpu_devices:
            label = _format_driver_label(render_device, driver)
            logger.debug(f"  {card_name} -> {render_device} ({label})")

    logger.debug("=== End System Information ===")


def _parse_lspci_gpu_name(gpu_type: str) -> str:
    """Parse GPU name from lspci output to get a user-friendly GPU model name.

    This is a non-critical optional enhancement. If lspci is not available,
    it silently falls back to a generic name like "INTEL GPU" or "AMD GPU".
    GPU detection and functionality are not affected.

    Args:
        gpu_type: Type of GPU ('AMD', 'INTEL')

    Returns:
        str: GPU name or fallback description (never fails, always returns a string)

    """
    try:
        result = subprocess.run(["lspci"], capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            for line in result.stdout.split("\n"):
                if "VGA" in line and (
                    gpu_type == "AMD"
                    and "AMD" in line
                    or gpu_type == "INTEL"
                    and "Intel" in line
                ):
                    parts = line.split(":")
                    if len(parts) > 2:
                        return parts[2].strip()
    except FileNotFoundError:
        # lspci not installed - this is fine, just use generic name
        pass
    except Exception as e:
        # Other errors (timeout, etc) - log for debugging
        logger.debug(f"Error running lspci for {gpu_type}: {e}")

    return f"{gpu_type} GPU"


def _get_pci_address_from_drm_device(gpu_device: str) -> Optional[str]:
    """Resolve a Linux DRM device (e.g., /dev/dri/renderD128) to its PCI address.

    This uses sysfs and works even when multiple GPUs share the same driver/API.

    Args:
        gpu_device: Path to a DRM node under /dev/dri (e.g., '/dev/dri/renderD128')

    Returns:
        Optional[str]: PCI address like '0000:06:00.0' if resolvable; otherwise None.

    """
    if not gpu_device or not gpu_device.startswith("/dev/dri/"):
        return None

    node = os.path.basename(gpu_device)
    sysfs_device_path = os.path.join("/sys/class/drm", node, "device")
    if not os.path.exists(sysfs_device_path):
        return None

    try:
        real_path = os.path.realpath(sysfs_device_path)
    except OSError:
        return None

    # The realpath commonly ends with the PCI address for PCI devices, e.g.:
    # /sys/devices/pci0000:00/0000:00:02.0
    pci_addr_re = re.compile(r"^\d{4}:\d{2}:\d{2}\.\d$")
    for part in reversed(real_path.split(os.sep)):
        if pci_addr_re.match(part):
            return part

    return None


def _get_lspci_device_name_for_pci_address(pci_address: str) -> Optional[str]:
    """Get a user-friendly GPU device name for a specific PCI address via lspci.

    Args:
        pci_address: PCI address like '0000:06:00.0'

    Returns:
        Optional[str]: Parsed device name, or None if lspci is unavailable or parsing fails.

    """
    if not pci_address:
        return None

    try:
        # Use -s to query exactly one device. This prevents incorrect names when multiple GPUs exist.
        result = subprocess.run(
            ["lspci", "-s", pci_address], capture_output=True, text=True, timeout=5
        )
        if result.returncode != 0 or not result.stdout.strip():
            return None

        # Typical line: "06:00.0 VGA compatible controller: Intel Corporation DG2 [Arc A380] (rev 05)"
        line = result.stdout.splitlines()[0].strip()
        parts = line.split(":", 2)
        if len(parts) == 3:
            return parts[2].strip()
        return None
    except FileNotFoundError:
        # lspci not installed - fine, caller will fall back.
        return None
    except Exception as e:
        logger.debug(f"Error running lspci for PCI address {pci_address}: {e}")
        return None


def get_gpu_name(gpu_type: str, gpu_device: str) -> str:
    """Extract GPU model name from system.

    Args:
        gpu_type: Type of GPU ('NVIDIA', 'AMD', 'INTEL', 'APPLE')
        gpu_device: GPU device path or info string

    Returns:
        str: GPU model name or fallback description

    """
    try:
        if gpu_type == "NVIDIA":
            # Use nvidia-smi to get GPU name
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader,nounits"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                gpu_names = [
                    line.strip()
                    for line in result.stdout.strip().split("\n")
                    if line.strip()
                ]
                if gpu_names:
                    return gpu_names[0]  # Return first GPU name
            return "NVIDIA GPU"

        elif gpu_type == "APPLE":
            return _get_apple_gpu_name()

        elif gpu_type == "WINDOWS_GPU":
            return "Windows GPU"

        elif gpu_type in ("AMD", "INTEL") and gpu_device.startswith("/dev/dri/"):
            # Prefer per-device PCI lookup to avoid duplicate names when multiple GPUs exist.
            pci_address = _get_pci_address_from_drm_device(gpu_device)
            if pci_address:
                per_device_name = _get_lspci_device_name_for_pci_address(pci_address)
                if per_device_name:
                    return per_device_name  # Don't add (VAAPI) here; format_gpu_info will add it

            # Fallback to generic lspci scan (may be ambiguous on multi-GPU systems).
            gpu_name = _parse_lspci_gpu_name(gpu_type)
            return gpu_name  # Don't add (VAAPI) here; format_gpu_info will add it

    except Exception as e:
        logger.debug(f"Error getting GPU name for {gpu_type}: {e}")

    # Fallback - return only the GPU type name without acceleration method
    return f"{gpu_type} GPU"


def format_gpu_info(
    gpu_type: str, gpu_device: str, gpu_name: str, acceleration: str = None
) -> str:
    """Format GPU information for display.

    Args:
        gpu_type: Type of GPU
        gpu_device: GPU device path or info
        gpu_name: GPU model name
        acceleration: Acceleration method (CUDA, VAAPI, D3D11VA)

    Returns:
        str: Formatted GPU description

    """
    # Use acceleration field if provided (more accurate than guessing from GPU type)
    if acceleration:
        if acceleration == "VAAPI" and gpu_device.startswith("/dev/dri/"):
            return f"{gpu_name} (VAAPI - {gpu_device})"
        else:
            return f"{gpu_name} ({acceleration})"

    # Fallback to old logic for backward compatibility
    if gpu_type == "NVIDIA":
        return f"{gpu_name} (CUDA)"
    elif gpu_type == "WINDOWS_GPU":
        return f"{gpu_name} (D3D11VA - Universal Windows GPU)"
    elif gpu_type == "APPLE":
        return f"{gpu_name} (VideoToolbox)"
    elif gpu_type in ("AMD", "INTEL", "ARM", "VIDEOCORE") and gpu_device.startswith(
        "/dev/dri/"
    ):
        return f"{gpu_name} (VAAPI - {gpu_device})"
    elif gpu_type == "UNKNOWN":
        return f"{gpu_name} (Unknown GPU)"
    else:
        return f"{gpu_name} ({gpu_type})"


def _test_acceleration_method(
    vendor: str, acceleration: str, device_path: Optional[str] = None
) -> bool:
    """Test if a specific acceleration method works for a GPU vendor.

    Args:
        vendor: GPU vendor ('NVIDIA', 'AMD', 'INTEL', etc.)
        acceleration: Acceleration method ('CUDA', 'VAAPI', 'D3D11VA', etc.)
        device_path: Device path for VAAPI (e.g., '/dev/dri/renderD128'), or 'cuda' for NVIDIA

    Returns:
        bool: True if acceleration method works

    """
    accel_lower = acceleration.lower()

    # Check if the acceleration method is available in FFmpeg
    if not _is_hwaccel_available(accel_lower):
        logger.debug(f"{acceleration} not available in FFmpeg")
        return False

    # Get test configuration from GPU_ACCELERATION_MAP
    if vendor not in GPU_ACCELERATION_MAP:
        logger.debug(f"Unknown vendor {vendor}, cannot test")
        return False

    # Test the acceleration functionality
    if acceleration == "CUDA":
        test_passed = _test_hwaccel_functionality("cuda")
    elif acceleration == "VAAPI":
        if not device_path:
            logger.debug("VAAPI requires device_path")
            return False
        test_passed = _test_hwaccel_functionality("vaapi", device_path)
    elif acceleration == "D3D11VA":
        test_passed = _test_hwaccel_functionality("d3d11va")
    elif acceleration == "VIDEOTOOLBOX":
        test_passed = _test_hwaccel_functionality("videotoolbox")
    else:
        logger.debug(f"Unknown acceleration method: {acceleration}")
        return False

    # Return test result
    if test_passed:
        logger.debug(f"✓ {vendor} {acceleration} hardware acceleration test passed")
    else:
        logger.debug(f"✗ {vendor} {acceleration} functionality test failed")
    return test_passed


def _build_gpu_error_detail(
    acceleration: str,
    device_path: Optional[str],
    render_device: str,
    accel_config: dict,
) -> tuple[str, str]:
    """Build a user-facing error/detail pair for a failed GPU.

    Args:
        acceleration: Primary acceleration method that was attempted.
        device_path: Device path used for the test (may be 'cuda' or a /dev/dri path).
        render_device: The /dev/dri render device for this GPU.
        accel_config: Entry from GPU_ACCELERATION_MAP for this vendor.

    Returns:
        (error, error_detail): Short headline and longer actionable fix.

    """
    # VAAPI: check whether the failure is a permission issue
    if acceleration == "VAAPI" and render_device:
        check_path = (
            device_path
            if device_path and device_path.startswith("/dev/")
            else render_device
        )
        accessible, reason = _check_device_access(check_path)
        if not accessible and reason == "permission_denied":
            try:
                device_gid = os.stat(check_path).st_gid
                user_groups = os.getgroups()
                if device_gid not in user_groups:
                    return (
                        f"VAAPI device {check_path} permission denied",
                        f"Device group is {device_gid} but container runs as groups {user_groups}. "
                        f"The container auto-detects GPU device groups at startup. "
                        f"Verify --device /dev/dri:/dev/dri is passed (not a single sub-device). "
                        f"Check container startup logs for 'adding' and 'permissions' messages.",
                    )
                return (
                    f"VAAPI device {check_path} permission denied",
                    f"You are in group {device_gid} but the device is still not accessible. "
                    f"Check host device permissions: ls -l {check_path}",
                )
            except Exception:
                return (
                    f"VAAPI device {check_path} permission denied",
                    "Verify --device /dev/dri:/dev/dri is passed and the device is readable on the host.",
                )
        if not accessible and reason == "not_found":
            return (
                f"Device {check_path} not found",
                "The render device does not exist. Ensure --device /dev/dri:/dev/dri is passed to docker.",
            )
        if accessible:
            return (
                f"VAAPI test failed on {check_path} (device accessible)",
                "FFmpeg could not use this device for hardware decoding. "
                f"Run 'vainfo --display drm --device {check_path}' inside the "
                "container to check driver support. Check container logs for "
                "FFmpeg error output.",
            )

    # CUDA: likely missing nvidia-docker runtime
    if acceleration == "CUDA" and accel_config.get("requires_runtime"):
        return (
            "CUDA acceleration test failed",
            "This usually means the NVIDIA Container Toolkit runtime is not configured. "
            "See: https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html",
        )

    return (
        f"{acceleration} acceleration test failed",
        "The FFmpeg hardware acceleration test did not succeed. Check container logs for details.",
    )


def _detect_linux_gpus() -> List[Tuple[str, str, dict]]:
    """Detect Linux GPUs from /dev/dri devices.

    Returns:
        List[Tuple[str, str, dict]]: List of (gpu_type, gpu_device, gpu_info_dict)

    """
    detected_gpus = []
    detected_vendors = set()  # Track which vendors we've already detected

    # Enumerate physical GPUs from /dev/dri
    logger.debug("=== Enumerating Physical GPUs ===")
    physical_gpus = _get_gpu_devices()  # Returns: [(card_name, render_device, driver)]

    if not physical_gpus:
        logger.debug("No physical GPUs found in /dev/dri")

        # WSL2 with newer kernels (6.6+) may have no DRM devices in /dev/dri
        # because CONFIG_DRM_VGEM is a loadable module instead of built-in.
        # CUDA still works via /dev/dxg paravirtualization, so detect it directly.
        if _is_wsl2() and _is_hwaccel_available("cuda"):
            logger.info(
                "  WSL2 detected with no DRM devices - attempting CUDA detection"
            )
            if _detect_nvidia_via_nvidia_smi() == "NVIDIA":
                if _test_hwaccel_functionality("cuda"):
                    gpu_name = get_gpu_name("NVIDIA", "cuda")
                    gpu_info = {
                        "name": gpu_name,
                        "acceleration": "CUDA",
                        "device_path": "cuda",
                        "render_device": None,
                        "card": "wsl2",
                        "driver": "nvidia",
                        "status": "ok",
                    }
                    detected_gpus.append(("NVIDIA", "cuda", gpu_info))
                    logger.warning(
                        "  WSL2 NVIDIA GPU support is unofficial and may have limitations"
                    )
                    logger.info(f"  WSL2 NVIDIA CUDA working: {gpu_name}")
                    return detected_gpus
                else:
                    logger.debug("  WSL2 CUDA functionality test failed")
            else:
                logger.debug("  WSL2 nvidia-smi did not detect NVIDIA GPU")

        # Linux container fallback: nvidia-container-runtime exposes NVIDIA
        # GPUs via /dev/nvidia* but does NOT mount /dev/dri/renderD* nodes,
        # so DRM enumeration finds nothing even though CUDA is usable. If
        # nvidia-smi confirms an NVIDIA GPU and CUDA hwaccel is compiled
        # into FFmpeg, probe CUDA directly — it doesn't need a DRM node.
        if (
            not detected_gpus
            and not _is_wsl2()
            and _is_hwaccel_available("cuda")
            and _detect_nvidia_via_nvidia_smi() == "NVIDIA"
        ):
            logger.info(
                "  NVIDIA GPU detected via nvidia-smi with no DRM render nodes — testing CUDA directly"
            )
            if _test_hwaccel_functionality("cuda"):
                gpu_name = get_gpu_name("NVIDIA", "cuda")
                gpu_info = {
                    "name": gpu_name,
                    "acceleration": "CUDA",
                    "device_path": "cuda",
                    "render_device": None,
                    "card": "nvidia-container",
                    "driver": "nvidia",
                    "status": "ok",
                }
                detected_gpus.append(("NVIDIA", "cuda", gpu_info))
                logger.info(f"  ✅ NVIDIA CUDA working: {gpu_name}")
            else:
                logger.debug(
                    "  CUDA functionality test failed — container may be missing "
                    "driver capabilities (set NVIDIA_DRIVER_CAPABILITIES=all; the "
                    "'graphics' capability is also required for Dolby Vision "
                    "Profile 5 thumbnails) or /dev/nvidia* devices"
                )

        # Container fallback: /sys/class/drm unavailable but /dev/dri render
        # devices may be mounted directly (e.g. TrueNAS Scale, Kubernetes).
        if not detected_gpus:
            render_devices = _scan_dev_dri_render_devices()
            if render_devices and _is_hwaccel_available("vaapi"):
                logger.info(
                    "  /sys/class/drm unavailable — probing /dev/dri render devices directly"
                )
                vendor = _detect_gpu_type_from_lspci()
                if vendor == "UNKNOWN":
                    logger.debug("  lspci could not identify GPU vendor")

                for device_path in render_devices:
                    logger.info(f"    Testing VAAPI on {device_path}...")
                    if _test_hwaccel_functionality("vaapi", device_path):
                        if vendor in GPU_ACCELERATION_MAP:
                            gpu_name = get_gpu_name(vendor, device_path)
                        else:
                            gpu_name = "GPU"
                        gpu_info = {
                            "name": gpu_name,
                            "acceleration": "VAAPI",
                            "device_path": device_path,
                            "render_device": device_path,
                            "card": f"dri-{os.path.basename(device_path)}",
                            "driver": "unknown",
                            "status": "ok",
                        }
                        detected_gpus.append((vendor, device_path, gpu_info))
                        logger.info(f"  ✅ VAAPI working on {device_path}: {gpu_name}")
                    else:
                        logger.debug(f"  ✗ VAAPI test failed on {device_path}")
                        vaapi_cfg = GPU_ACCELERATION_MAP.get(vendor, {})
                        error, error_detail = _build_gpu_error_detail(
                            "VAAPI", device_path, device_path, vaapi_cfg
                        )
                        gpu_info = {
                            "name": f"GPU ({os.path.basename(device_path)})",
                            "acceleration": "VAAPI",
                            "device_path": device_path,
                            "render_device": device_path,
                            "card": f"dri-{os.path.basename(device_path)}",
                            "driver": "unknown",
                            "status": "failed",
                            "error": error,
                            "error_detail": error_detail,
                        }
                        detected_gpus.append((vendor, device_path, gpu_info))
    else:
        logger.debug(f"Found {len(physical_gpus)} physical GPU(s) in /dev/dri")
        for card_name, render_device, driver in physical_gpus:
            label = _format_driver_label(render_device, driver)
            logger.debug(f"  {card_name}: {render_device} ({label})")

    # For each physical GPU, test appropriate acceleration methods
    logger.debug("=== Testing GPU Acceleration Methods ===")
    for card_name, render_device, driver in physical_gpus:
        vendor = _get_gpu_vendor_from_driver(driver)

        # Handle UNKNOWN vendor with special fallback logic for CUDA
        if vendor == "UNKNOWN":
            # Check if CUDA acceleration is available (useful for WSL2 NVIDIA GPUs)
            if _is_hwaccel_available("cuda"):
                logger.debug(
                    f"Unknown vendor for {card_name}, but CUDA is available - attempting NVIDIA detection"
                )
                nvidia_vendor = _detect_nvidia_via_nvidia_smi()
                if nvidia_vendor == "NVIDIA":
                    logger.info(
                        f"  Detected NVIDIA GPU via nvidia-smi for {card_name} (vendor was unknown)"
                    )
                    vendor = "NVIDIA"
                    if _is_wsl2():
                        logger.warning(
                            "  ⚠️  WSL2 environment detected - NVIDIA GPU support is unofficial and may have limitations"
                        )
                else:
                    # Even if nvidia-smi didn't confirm, try CUDA anyway if available
                    # This allows unofficial WSL2 support where detection may be unreliable
                    logger.debug(
                        "nvidia-smi did not confirm NVIDIA, but will attempt CUDA acceleration anyway"
                    )
                    if _is_wsl2():
                        logger.debug(
                            "WSL2 detected - allowing CUDA acceleration attempt with unknown vendor (unofficial support)"
                        )
                        # Test CUDA directly - if it works, treat as NVIDIA
                        logger.info(
                            f"  Checking {card_name} (UNKNOWN vendor, attempting CUDA)..."
                        )
                        logger.info("    Testing CUDA acceleration...")
                        if _test_hwaccel_functionality("cuda"):
                            # CUDA works! Treat as NVIDIA even though we couldn't confirm
                            vendor = "NVIDIA"
                            logger.warning(
                                "  ⚠️  CUDA acceleration works but vendor detection failed - treating as NVIDIA"
                            )
                            logger.warning(
                                "  ⚠️  WSL2 environment detected - NVIDIA GPU support is unofficial and may have limitations"
                            )
                            # Fall through to normal NVIDIA processing
                        else:
                            logger.debug(
                                f"Skipping {card_name} - CUDA acceleration test failed"
                            )
                            continue
                    else:
                        # Not WSL2, skip unknown vendor
                        logger.debug(
                            f"Skipping {card_name} - unknown vendor and nvidia-smi did not confirm NVIDIA"
                        )
                        continue
            else:
                # No CUDA available and vendor is unknown, skip
                logger.debug(
                    f"Skipping {card_name} - unknown vendor '{vendor}' and CUDA not available"
                )
                continue

        if vendor not in GPU_ACCELERATION_MAP:
            logger.debug(
                f"Skipping {card_name} - vendor '{vendor}' not in GPU acceleration map"
            )
            continue

        logger.debug(f"Testing {card_name} ({vendor} - {driver})...")
        logger.info(f"  Checking {card_name} ({vendor})...")

        accel_config = GPU_ACCELERATION_MAP[vendor]
        primary_method = accel_config["primary"]
        fallback_method = accel_config["fallback"]

        # Determine device path for testing
        if primary_method == "CUDA":
            device_path = "cuda"
        elif primary_method == "VAAPI":
            device_path = render_device
        else:
            device_path = None

        # Test primary acceleration method
        logger.debug(f"  Testing primary method: {primary_method}")
        logger.info(f"    Testing {primary_method} acceleration...")
        if _test_acceleration_method(vendor, primary_method, device_path):
            gpu_name = get_gpu_name(vendor, device_path)
            gpu_info = {
                "name": gpu_name,
                "acceleration": primary_method,
                "device_path": device_path,
                "render_device": render_device,
                "card": card_name,
                "driver": driver,
                "status": "ok",
            }
            detected_gpus.append((vendor, device_path, gpu_info))
            detected_vendors.add(vendor)
            logger.info(f"  ✅ {card_name}: {vendor} {primary_method} working")
            continue  # Primary worked, skip fallback

        # Primary failed, log appropriate message
        if accel_config.get("requires_runtime"):
            logger.warning(f"  ⚠️  {card_name}: {vendor} {primary_method} test failed")
            logger.warning(
                "  ⚠️  This usually means the required runtime is not configured"
            )
            if primary_method == "CUDA":
                logger.warning(
                    "  ⚠️  See: https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html"
                )
        else:
            logger.debug(f"  ✗ {card_name}: {primary_method} test failed")

        # Test fallback method if available
        if fallback_method:
            logger.debug(f"  Testing fallback method: {fallback_method}")
            logger.info(f"    Testing fallback {fallback_method} acceleration...")
            fallback_device_path = (
                render_device if fallback_method == "VAAPI" else device_path
            )

            if _test_acceleration_method(vendor, fallback_method, fallback_device_path):
                gpu_name = get_gpu_name(vendor, fallback_device_path)
                gpu_info = {
                    "name": gpu_name,
                    "acceleration": fallback_method,
                    "device_path": fallback_device_path,
                    "render_device": render_device,
                    "card": card_name,
                    "driver": driver,
                    "status": "ok",
                }
                detected_gpus.append((vendor, fallback_device_path, gpu_info))
                detected_vendors.add(vendor)
                logger.info(
                    f"  ✅ {card_name}: {vendor} {fallback_method} working (fallback)"
                )
            else:
                logger.warning(f"  ❌ {card_name}: All acceleration methods failed")
                error, error_detail = _build_gpu_error_detail(
                    primary_method, device_path, render_device, accel_config
                )
                gpu_name = get_gpu_name(vendor, device_path or render_device)
                gpu_info = {
                    "name": gpu_name or f"{vendor} GPU ({card_name})",
                    "acceleration": primary_method,
                    "device_path": device_path,
                    "render_device": render_device,
                    "card": card_name,
                    "driver": driver,
                    "status": "failed",
                    "error": error,
                    "error_detail": error_detail,
                }
                detected_gpus.append((vendor, device_path or render_device, gpu_info))
        else:
            logger.warning(f"  ❌ {card_name}: No fallback available, GPU unusable")
            error, error_detail = _build_gpu_error_detail(
                primary_method, device_path, render_device, accel_config
            )
            gpu_name = get_gpu_name(vendor, device_path or render_device)
            gpu_info = {
                "name": gpu_name or f"{vendor} GPU ({card_name})",
                "acceleration": primary_method,
                "device_path": device_path,
                "render_device": render_device,
                "card": card_name,
                "driver": driver,
                "status": "failed",
                "error": error,
                "error_detail": error_detail,
            }
            detected_gpus.append((vendor, device_path or render_device, gpu_info))

    return detected_gpus


def _detect_windows_gpus() -> List[Tuple[str, str, dict]]:
    """Detect Windows GPUs using CUDA (NVIDIA) or D3D11VA fallback.

    Returns:
        List[Tuple[str, str, dict]]: List of (gpu_type, gpu_device, gpu_info_dict)

    """
    detected_gpus = []

    hwaccels = _get_ffmpeg_hwaccels()
    logger.debug(f"Windows platform detected; FFmpeg hwaccels: {hwaccels}")

    # Try NVIDIA CUDA first
    if "cuda" in hwaccels and _detect_nvidia_via_nvidia_smi() == "NVIDIA":
        logger.info("  Checking Windows NVIDIA CUDA GPU...")
        logger.info("    Testing CUDA acceleration...")
        if _test_hwaccel_functionality("cuda"):
            gpu_name = get_gpu_name("NVIDIA", "cuda")
            gpu_info = {
                "name": gpu_name,
                "acceleration": "CUDA",
                "device_path": "cuda",
            }
            detected_gpus.append(("NVIDIA", "cuda", gpu_info))
            logger.info(f"  ✅ Windows NVIDIA CUDA working: {gpu_name}")
            return detected_gpus
        else:
            logger.debug("  CUDA functionality test failed, falling back to D3D11VA")

    # Fall back to D3D11VA
    if "d3d11va" in hwaccels:
        logger.info("  Checking Windows D3D11VA GPU...")
        logger.info("    Testing D3D11VA acceleration...")
        if _test_acceleration_method("WINDOWS_GPU", "D3D11VA", "d3d11va"):
            gpu_name = "Windows GPU"
            gpu_info = {
                "name": gpu_name,
                "acceleration": "D3D11VA",
                "device_path": "d3d11va",
            }
            detected_gpus.append(("WINDOWS_GPU", "d3d11va", gpu_info))
            logger.info("  ✅ Windows D3D11VA working")
    else:
        logger.debug("d3d11va not reported by FFmpeg; skipping Windows D3D11VA probe")

    return detected_gpus


def _detect_macos_gpus() -> List[Tuple[str, str, dict]]:
    """Detect macOS GPUs using VideoToolbox.

    Returns:
        List[Tuple[str, str, dict]]: List of (gpu_type, gpu_device, gpu_info_dict)

    """
    detected_gpus = []

    # Check for macOS VideoToolbox (doesn't use /dev/dri)
    logger.debug("Detected macOS platform, testing VideoToolbox acceleration...")
    logger.info("  Checking Apple GPU...")
    logger.info("    Testing VideoToolbox acceleration...")
    if _test_acceleration_method("APPLE", "VIDEOTOOLBOX", "videotoolbox"):
        gpu_name = get_gpu_name("APPLE", "videotoolbox")
        gpu_info = {
            "name": gpu_name,
            "acceleration": "VIDEOTOOLBOX",
            "device_path": "videotoolbox",
        }
        detected_gpus.append(("APPLE", "videotoolbox", gpu_info))
        logger.info("  ✅ Apple VideoToolbox working")
    else:
        logger.warning("  ❌ Apple VideoToolbox test failed")

    return detected_gpus


def detect_all_gpus() -> List[Tuple[str, str, dict]]:
    """Detect all available GPU hardware using FFmpeg capability detection.

    Checks FFmpeg's available hardware acceleration capabilities and returns
    all working GPUs instead of just the first one.

    Returns:
        List[Tuple[str, str, dict]]: List of (gpu_type, gpu_device, gpu_info_dict)
            - gpu_type: 'NVIDIA', 'AMD', 'INTEL', 'APPLE', 'WINDOWS_GPU'
            - gpu_device: Device path or info string
            - gpu_info_dict: Dictionary with GPU details (name, vram, etc.)

    """
    logger.debug("=== Starting Multi-GPU Detection ===")
    _log_system_info()

    detected_gpus = []

    # Detect GPUs based on platform
    if platform.system() == "Linux":
        detected_gpus.extend(_detect_linux_gpus())
    elif is_windows():
        detected_gpus.extend(_detect_windows_gpus())
    elif is_macos():
        detected_gpus.extend(_detect_macos_gpus())

    logger.debug(
        f"=== Multi-GPU Detection Complete: Found {len(detected_gpus)} working GPU(s) ==="
    )
    return detected_gpus
