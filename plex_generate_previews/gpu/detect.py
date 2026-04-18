"""GPU detection orchestrator.

Composes the information-gathering helpers in this subpackage
(:mod:`.enumeration`, :mod:`.ffmpeg_capabilities`, :mod:`.vaapi_probe`,
:mod:`.vulkan_probe`) into the public GPU list returned to worker
configuration and the web UI. :func:`detect_all_gpus` is the top-level
entry point; it fans out to per-OS detectors.
"""

import os
import platform
import subprocess

from loguru import logger

from ..utils import is_macos, is_windows
from .enumeration import (
    _detect_gpu_type_from_lspci,
    _detect_nvidia_via_nvidia_smi,
    _get_gpu_devices,
    _get_gpu_vendor_from_driver,
    _is_wsl2,
    _log_system_info,
    _scan_dev_dri_render_devices,
    get_gpu_name,
)
from .ffmpeg_capabilities import (
    _get_ffmpeg_hwaccels,
    _is_hwaccel_available,
)
from .vaapi_probe import _format_driver_label

# MIN_FFMPEG_VERSION is re-exported from gpu/ffmpeg_capabilities.py above.

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

# DRIVER_VENDOR_MAP moved to plex_generate_previews/gpu/enumeration.py;
# re-exported from this module's top-of-file imports for backwards compat.

# VA-API driver probing moved to plex_generate_previews/gpu/vaapi_probe.py;
# re-exported from this module's top-of-file imports.


# FFmpeg capability probing moved to plex_generate_previews/gpu/ffmpeg_capabilities.py;
# re-exported from this module's top-of-file imports.


# Platform-specific GPU enumeration moved to
# plex_generate_previews/gpu/enumeration.py; re-exported from this
# module's top-of-file imports.


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
            logger.debug(f"  Device permissions: {perms} (owner={owner_uid}, group={group_gid})")
            logger.debug(f"  Current user: {os.getuid()}, groups: {os.getgroups()}")
        except Exception as e:
            logger.debug(f"✗ Device exists but is not accessible: {device_path}")
            logger.debug(f"  Current user: {os.getuid()}, groups: {os.getgroups()}")
            logger.debug(f"  Could not get device stats: {e}")
        return False, "permission_denied"

    logger.debug(f"✓ Device is accessible: {device_path}")
    return True, "accessible"


def _test_hwaccel_functionality(hwaccel: str, device_path: str | None = None) -> bool:
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

                        logger.warning(f"⚠ VAAPI device {device_path} is not accessible (permission denied)")
                        logger.warning(f"⚠ Device group: {device_gid}, your groups: {user_groups}")

                        if device_gid not in user_groups:
                            logger.warning("⚠ The container should auto-detect GPU device groups at startup")
                            logger.warning("⚠ Verify you are passing --device /dev/dri:/dev/dri (not a single device)")
                        else:
                            logger.warning(f"⚠ You are in group {device_gid}, but device is still not accessible")
                            logger.warning(f"⚠ Check host device permissions: ls -l {device_path}")
                    except Exception:
                        logger.warning(f"⚠ VAAPI device {device_path} is not accessible (permission denied)")
                        logger.warning(
                            "⚠ Verify --device /dev/dri:/dev/dri is passed and the device is readable on the host"
                        )
                # If device doesn't exist, just skip silently (expected for wrong GPU type)
                return False
        # Get test video fixture - all GPU tests use real H.264 video for accurate testing
        test_video = None
        possible_paths = [
            os.path.join(os.path.dirname(__file__), "fixtures", "test_video.mp4"),
            os.path.join(os.getcwd(), "plex_generate_previews", "fixtures", "test_video.mp4"),
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
            logger.debug(f"✗ {hwaccel} functionality test failed (exit code: {result.returncode})")

            if result.stderr:
                stderr_text = result.stderr.decode("utf-8", "ignore").strip()
                stderr_lower = stderr_text.lower()

                # VAAPI failures directly affect user-visible GPU status — log at WARNING
                if hwaccel == "vaapi":
                    logger.warning(f"⚠ FFmpeg VAAPI test failed on {device_path} (exit code {result.returncode}):")
                    for line in stderr_text.splitlines()[-15:]:
                        if line.strip():
                            logger.warning(f"  {line.rstrip()}")

                    if "permission denied" in stderr_lower:
                        logger.warning("⚠ The container should auto-detect GPU device groups at startup")
                        logger.warning("⚠ Verify --device /dev/dri:/dev/dri is passed (not a single device)")
                else:
                    logger.debug(f"FFmpeg {hwaccel} stderr: {stderr_text[-500:]}")

                if hwaccel == "cuda":
                    if "/dev/null" in stderr_lower and "operation not permitted" in stderr_lower:
                        logger.debug("Note: /dev/null errors usually indicate missing NVIDIA Container Toolkit")
                        logger.debug("      Run with --gpus all or configure nvidia-docker runtime")

            return False

    except subprocess.TimeoutExpired as e:
        logger.warning(f"⚠ {hwaccel} test timed out on {device_path or 'default device'} (>20s)")
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


def format_gpu_info(gpu_type: str, gpu_device: str, gpu_name: str, acceleration: str = None) -> str:
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
    elif gpu_type in ("AMD", "INTEL", "ARM", "VIDEOCORE") and gpu_device.startswith("/dev/dri/"):
        return f"{gpu_name} (VAAPI - {gpu_device})"
    elif gpu_type == "UNKNOWN":
        return f"{gpu_name} (Unknown GPU)"
    else:
        return f"{gpu_name} ({gpu_type})"


def _test_acceleration_method(vendor: str, acceleration: str, device_path: str | None = None) -> bool:
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
    device_path: str | None,
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
        check_path = device_path if device_path and device_path.startswith("/dev/") else render_device
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
                logger.debug(
                    "VAAPI diagnostic: could not inspect {} gid/groups; falling back to generic permission-denied message.",
                    check_path,
                    exc_info=True,
                )
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


def _detect_linux_gpus() -> list[tuple[str, str, dict]]:
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
            logger.info("  WSL2 detected with no DRM devices - attempting CUDA detection")
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
                    logger.warning("  WSL2 NVIDIA GPU support is unofficial and may have limitations")
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
            logger.info("  NVIDIA GPU detected via nvidia-smi with no DRM render nodes — testing CUDA directly")
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
                logger.info("  /sys/class/drm unavailable — probing /dev/dri render devices directly")
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
                        error, error_detail = _build_gpu_error_detail("VAAPI", device_path, device_path, vaapi_cfg)
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
                logger.debug(f"Unknown vendor for {card_name}, but CUDA is available - attempting NVIDIA detection")
                nvidia_vendor = _detect_nvidia_via_nvidia_smi()
                if nvidia_vendor == "NVIDIA":
                    logger.info(f"  Detected NVIDIA GPU via nvidia-smi for {card_name} (vendor was unknown)")
                    vendor = "NVIDIA"
                    if _is_wsl2():
                        logger.warning(
                            "  ⚠️  WSL2 environment detected - NVIDIA GPU support is unofficial and may have limitations"
                        )
                else:
                    # Even if nvidia-smi didn't confirm, try CUDA anyway if available
                    # This allows unofficial WSL2 support where detection may be unreliable
                    logger.debug("nvidia-smi did not confirm NVIDIA, but will attempt CUDA acceleration anyway")
                    if _is_wsl2():
                        logger.debug(
                            "WSL2 detected - allowing CUDA acceleration attempt with unknown vendor (unofficial support)"
                        )
                        # Test CUDA directly - if it works, treat as NVIDIA
                        logger.info(f"  Checking {card_name} (UNKNOWN vendor, attempting CUDA)...")
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
                            logger.debug(f"Skipping {card_name} - CUDA acceleration test failed")
                            continue
                    else:
                        # Not WSL2, skip unknown vendor
                        logger.debug(f"Skipping {card_name} - unknown vendor and nvidia-smi did not confirm NVIDIA")
                        continue
            else:
                # No CUDA available and vendor is unknown, skip
                logger.debug(f"Skipping {card_name} - unknown vendor '{vendor}' and CUDA not available")
                continue

        if vendor not in GPU_ACCELERATION_MAP:
            logger.debug(f"Skipping {card_name} - vendor '{vendor}' not in GPU acceleration map")
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
            logger.warning("  ⚠️  This usually means the required runtime is not configured")
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
            fallback_device_path = render_device if fallback_method == "VAAPI" else device_path

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
                logger.info(f"  ✅ {card_name}: {vendor} {fallback_method} working (fallback)")
            else:
                logger.warning(f"  ❌ {card_name}: All acceleration methods failed")
                error, error_detail = _build_gpu_error_detail(primary_method, device_path, render_device, accel_config)
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
            error, error_detail = _build_gpu_error_detail(primary_method, device_path, render_device, accel_config)
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


def _detect_windows_gpus() -> list[tuple[str, str, dict]]:
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


def _detect_macos_gpus() -> list[tuple[str, str, dict]]:
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


def detect_all_gpus() -> list[tuple[str, str, dict]]:
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

    logger.debug(f"=== Multi-GPU Detection Complete: Found {len(detected_gpus)} working GPU(s) ===")
    return detected_gpus
