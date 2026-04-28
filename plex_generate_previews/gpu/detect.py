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
    _enumerate_nvidia_gpus_via_smi,
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
        logger.debug("✗ Device does not exist: {}", device_path)
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
            logger.debug("✗ Device exists but is {}: {}", detail, device_path)
            logger.debug("  Device permissions: {} (owner={}, group={})", perms, owner_uid, group_gid)
            logger.debug("  Current user: {}, groups: {}", os.getuid(), os.getgroups())
        except Exception as e:
            logger.debug("✗ Device exists but is not accessible: {}", device_path)
            logger.debug("  Current user: {}, groups: {}", os.getuid(), os.getgroups())
            logger.debug("  Could not get device stats: {}", e)
        return False, "permission_denied"

    logger.debug("✓ Device is accessible: {}", device_path)
    return True, "accessible"


def _test_hwaccel_functionality(
    hwaccel: str,
    device_path: str | None = None,
    cuda_device_index: str | None = None,
) -> bool:
    """Test if hardware acceleration actually works by running a simple FFmpeg command.

    Args:
        hwaccel: Hardware acceleration type to test
        device_path: Optional device path for VAAPI
        cuda_device_index: Optional CUDA device index (e.g. "0", "1")
            passed to FFmpeg as ``-hwaccel_device`` so each GPU on a
            multi-card host can be tested independently.

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

                        if device_gid not in user_groups:
                            logger.warning(
                                "VAAPI hardware decoding unavailable on {}: permission denied. "
                                "Device is owned by group {}, but the container is running as groups {}. "
                                "CPU fallback will be used for any GPU job that needed this device. "
                                "Fix: make sure your docker run / compose passes --device /dev/dri:/dev/dri "
                                "(the whole directory, not a single sub-device); the container auto-detects "
                                "the render group at startup. On the host, check `ls -l {}` to see the owning group.",
                                device_path,
                                device_gid,
                                user_groups,
                                device_path,
                            )
                        else:
                            logger.warning(
                                "VAAPI hardware decoding unavailable on {}: permission denied even though the "
                                "container is in the device's group ({}). This usually means host-side permissions "
                                "on the device node are restricted. CPU fallback will be used for jobs that needed "
                                "this GPU. Fix: on the host, run `ls -l {}` and confirm the render group has rw "
                                "access (typical: `crw-rw---- root render`). Adjust host udev rules if needed.",
                                device_path,
                                device_gid,
                                device_path,
                            )
                    except Exception:
                        logger.warning(
                            "VAAPI hardware decoding unavailable on {}: permission denied (could not inspect "
                            "device ownership to give a more specific hint). CPU fallback will be used for jobs "
                            "that needed this GPU. Fix: make sure your docker run / compose passes "
                            "--device /dev/dri:/dev/dri (the whole directory, not a single sub-device), "
                            "and that the device is readable on the host (run `ls -l {}`).",
                            device_path,
                            device_path,
                        )
                # If device doesn't exist, just skip silently (expected for wrong GPU type)
                return False
        # Probe video — the hwaccel-functionality test decodes a real H.264
        # sample so "ffmpeg lists the hwaccel" isn't confused with "hwaccel
        # actually works on this host." Bundled alongside this module.
        test_video = None
        gpu_dir = os.path.dirname(__file__)
        possible_paths = [
            os.path.join(gpu_dir, "assets", "test_video.mp4"),
            os.path.join(os.getcwd(), "plex_generate_previews", "gpu", "assets", "test_video.mp4"),
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
            if cuda_device_index:
                cmd += ["-hwaccel_device", cuda_device_index]
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

        logger.debug("Testing {} functionality: {}", hwaccel, " ".join(cmd))
        result = subprocess.run(cmd, capture_output=True, timeout=20)

        # FFmpeg returns 0 for success, 141 for SIGPIPE (which is OK for our test)
        if result.returncode in [0, 141]:
            logger.debug("✓ {} functionality test passed", hwaccel)
            return True
        else:
            logger.debug("✗ {} functionality test failed (exit code: {})", hwaccel, result.returncode)

            if result.stderr:
                stderr_text = result.stderr.decode("utf-8", "ignore").strip()
                stderr_lower = stderr_text.lower()

                # VAAPI failures directly affect user-visible GPU status — log at WARNING
                if hwaccel == "vaapi":
                    logger.warning(
                        "FFmpeg VAAPI hardware-decode test failed on {} (exit code {}). "
                        "This GPU will be marked failed; CPU fallback still works for affected jobs. "
                        "FFmpeg's last 15 lines of output follow — paste them into a GitHub issue if "
                        "you can't tell what driver is missing:",
                        device_path,
                        result.returncode,
                    )
                    for line in stderr_text.splitlines()[-15:]:
                        if line.strip():
                            logger.warning("  {}", line.rstrip())

                    if "permission denied" in stderr_lower:
                        logger.warning(
                            "FFmpeg reported 'permission denied' for the VAAPI device. "
                            "Fix: (1) the container auto-detects the GPU device group at startup — check the "
                            "container's earlier startup logs for lines about 'adding' and 'permissions'; "
                            "(2) verify your docker run / compose passes --device /dev/dri:/dev/dri "
                            "(the whole directory, not a single sub-device)."
                        )
                else:
                    logger.debug("FFmpeg {} stderr: {}", hwaccel, stderr_text[-500:])

                if hwaccel == "cuda":
                    if "/dev/null" in stderr_lower and "operation not permitted" in stderr_lower:
                        logger.debug("Note: /dev/null errors usually indicate missing NVIDIA Container Toolkit")
                        logger.debug("      Run with --gpus all or configure nvidia-docker runtime")

            return False

    except subprocess.TimeoutExpired as e:
        logger.warning(
            "{} hardware-decode test timed out after 20s on {}. "
            "This usually means the driver is hanging during init (common with broken VA-API drivers, "
            "stuck NVIDIA contexts, or an over-loaded host). "
            "This GPU will be marked failed; CPU fallback still works for affected jobs.",
            hwaccel,
            device_path or "default device",
        )
        if e.stderr:
            stderr_text = e.stderr.decode("utf-8", "ignore").strip()
            if stderr_text:
                logger.warning("FFmpeg output captured before the hang (last 15 lines follow):")
                for line in stderr_text.splitlines()[-15:]:
                    if line.strip():
                        logger.warning("  {}", line.rstrip())
        if hwaccel == "vaapi" and device_path:
            logger.warning(
                "To diagnose the hang, run this inside the container: "
                "vainfo --display drm --device {} . "
                "It will report whether the VA-API driver is loaded and which codecs it advertises.",
                device_path,
            )
        return False
    except Exception as e:
        logger.debug("✗ {} functionality test failed with exception: {}", hwaccel, e)
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
        logger.debug("{} not available in FFmpeg", acceleration)
        return False

    # Get test configuration from GPU_ACCELERATION_MAP
    if vendor not in GPU_ACCELERATION_MAP:
        logger.debug("Unknown vendor {}, cannot test", vendor)
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
        logger.debug("Unknown acceleration method: {}", acceleration)
        return False

    # Return test result
    if test_passed:
        logger.debug("✓ {} {} hardware acceleration test passed", vendor, acceleration)
    else:
        logger.debug("✗ {} {} functionality test failed", vendor, acceleration)
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

    # NVIDIA CUDA is driven through /dev/nvidia* and does not require
    # /dev/dri render nodes — nvidia-container-runtime deliberately does
    # not expose DRM nodes to containers.  Enumerate NVIDIA GPUs via
    # nvidia-smi first so multi-GPU hosts (issue #221) register one
    # entry per physical card, each tested with -hwaccel_device <index>.
    # This covers bare metal, nvidia-container-runtime, and WSL2
    # uniformly; the DRM loop below handles AMD/Intel only.
    if _is_hwaccel_available("cuda"):
        smi_gpus = _enumerate_nvidia_gpus_via_smi()
        if smi_gpus:
            logger.debug("=== Testing {} NVIDIA GPU(s) via nvidia-smi ===", len(smi_gpus))
            if _is_wsl2():
                logger.warning(
                    "WSL2 detected. NVIDIA GPU support inside WSL2 is unofficial. "
                    "If detection or decoding misbehaves, prefer running the container directly on a Linux "
                    "or Windows host. Intel/AMD GPUs are NOT supported under WSL2 — disable them in Settings → GPU."
                )
            for g in smi_gpus:
                idx = g["index"]
                device = f"cuda:{idx}"
                logger.info("  Checking NVIDIA GPU {} ({})...", idx, g["name"])
                logger.info("    Testing CUDA acceleration...")
                if _test_hwaccel_functionality("cuda", cuda_device_index=idx):
                    gpu_info = {
                        "name": g["name"],
                        "acceleration": "CUDA",
                        "device_path": device,
                        "render_device": None,
                        "card": f"nvidia-{idx}",
                        "driver": "nvidia",
                        "uuid": g.get("uuid", ""),
                        "status": "ok",
                    }
                    detected_gpus.append(("NVIDIA", device, gpu_info))
                    detected_vendors.add("NVIDIA")
                    logger.info("  ✅ NVIDIA CUDA working: GPU {} ({})", idx, g["name"])
                else:
                    # Silent skip (matches legacy WSL2/container fallback
                    # behaviour) — the warning below tells the user what
                    # to check without cluttering the UI with a failed
                    # row that they can't act on individually.
                    logger.warning(
                        "NVIDIA GPU {} ({}): CUDA hardware-decode test failed. "
                        "This GPU will not be used — CPU fallback still works for jobs targeted at it. "
                        "Fix: (1) set NVIDIA_DRIVER_CAPABILITIES=all on the container (the 'graphics' "
                        "capability is also required for Dolby Vision Profile 5 thumbnails); "
                        "(2) confirm /dev/nvidia* devices are exposed: docker run --gpus all ... "
                        "or use the NVIDIA Container Toolkit runtime; "
                        "(3) check: nvidia-smi (inside the container) — if it fails, the toolkit isn't wired up.",
                        idx,
                        g["name"],
                    )

    # Enumerate physical GPUs from /dev/dri
    logger.debug("=== Enumerating Physical GPUs ===")
    physical_gpus = _get_gpu_devices()  # Returns: [(card_name, render_device, driver)]

    if not physical_gpus:
        logger.debug("No physical GPUs found in /dev/dri")

        # Container fallback: /sys/class/drm unavailable but /dev/dri render
        # devices may be mounted directly (e.g. TrueNAS Scale, Kubernetes).
        # Only meaningful for VAAPI (AMD/Intel) — NVIDIA was handled above.
        if not [g for g in detected_gpus if g[2].get("status") == "ok"]:
            render_devices = _scan_dev_dri_render_devices()
            if render_devices and _is_hwaccel_available("vaapi"):
                logger.info("  /sys/class/drm unavailable — probing /dev/dri render devices directly")
                vendor = _detect_gpu_type_from_lspci()
                if vendor == "UNKNOWN":
                    logger.debug("  lspci could not identify GPU vendor")

                for device_path in render_devices:
                    logger.info("    Testing VAAPI on {}...", device_path)
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
                        logger.info("  ✅ VAAPI working on {}: {}", device_path, gpu_name)
                    else:
                        logger.debug("  ✗ VAAPI test failed on {}", device_path)
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
        logger.debug("Found {} physical GPU(s) in /dev/dri", len(physical_gpus))
        for card_name, render_device, driver in physical_gpus:
            label = _format_driver_label(render_device, driver)
            logger.debug("  {}: {} ({})", card_name, render_device, label)

    # For each physical GPU, test appropriate acceleration methods
    logger.debug("=== Testing GPU Acceleration Methods ===")
    for card_name, render_device, driver in physical_gpus:
        vendor = _get_gpu_vendor_from_driver(driver)

        # NVIDIA cards are enumerated authoritatively via nvidia-smi at
        # the top of this function.  Skip them in the DRM loop so a
        # two-GPU host doesn't double-register or collapse both cards
        # onto a single "cuda" device path (issue #221).
        if (driver == "nvidia" or vendor == "NVIDIA") and "NVIDIA" in detected_vendors:
            logger.debug("Skipping {}: NVIDIA already registered via nvidia-smi", card_name)
            continue

        # Handle UNKNOWN vendor with special fallback logic for CUDA
        if vendor == "UNKNOWN":
            # Check if CUDA acceleration is available (useful for WSL2 NVIDIA GPUs)
            if _is_hwaccel_available("cuda"):
                logger.debug("Unknown vendor for {}, but CUDA is available - attempting NVIDIA detection", card_name)
                nvidia_vendor = _detect_nvidia_via_nvidia_smi()
                if nvidia_vendor == "NVIDIA":
                    logger.info("  Detected NVIDIA GPU via nvidia-smi for {} (vendor was unknown)", card_name)
                    vendor = "NVIDIA"
                    if _is_wsl2():
                        logger.warning(
                            "WSL2 detected and the GPU vendor couldn't be identified via the usual channels — "
                            "treating as NVIDIA because nvidia-smi succeeded. NVIDIA support in WSL2 is unofficial. "
                            "If GPU jobs misbehave, run the container on Linux or Windows directly. "
                            "Intel/AMD GPUs are NOT supported under WSL2 — disable them in Settings → GPU."
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
                        logger.info("  Checking {} (UNKNOWN vendor, attempting CUDA)...", card_name)
                        logger.info("    Testing CUDA acceleration...")
                        if _test_hwaccel_functionality("cuda"):
                            # CUDA works! Treat as NVIDIA even though we couldn't confirm
                            vendor = "NVIDIA"
                            logger.warning(
                                "CUDA hardware-decode works on this GPU but the vendor couldn't be confirmed — "
                                "treating it as NVIDIA. This is the unofficial WSL2 NVIDIA pathway. "
                                "Things may still work, but if GPU jobs misbehave, run the container on Linux "
                                "or Windows directly. Intel/AMD GPUs are NOT supported under WSL2 — disable them "
                                "in Settings → GPU."
                            )
                            # Fall through to normal NVIDIA processing
                        else:
                            logger.debug("Skipping {} - CUDA acceleration test failed", card_name)
                            continue
                    else:
                        # Not WSL2, skip unknown vendor
                        logger.debug("Skipping {} - unknown vendor and nvidia-smi did not confirm NVIDIA", card_name)
                        continue
            else:
                # No CUDA available and vendor is unknown, skip
                logger.debug("Skipping {} - unknown vendor '{}' and CUDA not available", card_name, vendor)
                continue

        # After UNKNOWN→NVIDIA promotion, skip if nvidia-smi already
        # registered this GPU at the top of the function (issue #221).
        if vendor == "NVIDIA" and "NVIDIA" in detected_vendors:
            logger.debug("Skipping {}: NVIDIA already registered via nvidia-smi", card_name)
            continue

        if vendor not in GPU_ACCELERATION_MAP:
            logger.debug("Skipping {} - vendor '{}' not in GPU acceleration map", card_name, vendor)
            continue

        logger.debug("Testing {} ({} - {})...", card_name, vendor, driver)
        logger.info("  Checking {} ({})...", card_name, vendor)

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
        logger.debug("  Testing primary method: {}", primary_method)
        logger.info("    Testing {} acceleration...", primary_method)
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
            logger.info("  ✅ {}: {} {} working", card_name, vendor, primary_method)
            continue  # Primary worked, skip fallback

        # Primary failed, log appropriate message
        if accel_config.get("requires_runtime"):
            if primary_method == "CUDA":
                logger.warning(
                    "{}: {} CUDA hardware-decode test failed. "
                    "This usually means the NVIDIA Container Toolkit runtime isn't configured. "
                    "This GPU is unusable until fixed; CPU fallback still works for affected jobs. "
                    "Install/configure the toolkit: "
                    "https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html",
                    card_name,
                    vendor,
                )
            else:
                logger.warning(
                    "{}: {} {} hardware-decode test failed and the required runtime isn't configured. "
                    "This GPU is unusable; CPU fallback still works for affected jobs.",
                    card_name,
                    vendor,
                    primary_method,
                )
        else:
            logger.debug("  ✗ {}: {} test failed", card_name, primary_method)

        # Test fallback method if available
        if fallback_method:
            logger.debug("  Testing fallback method: {}", fallback_method)
            logger.info("    Testing fallback {} acceleration...", fallback_method)
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
                logger.info("  ✅ {}: {} {} working (fallback)", card_name, vendor, fallback_method)
            else:
                logger.warning(
                    "{}: every hardware-acceleration method tested ({} primary, {} fallback) failed. "
                    "This GPU will be skipped; CPU fallback still works for jobs that targeted it. "
                    "See Settings → GPU for the per-device error detail.",
                    card_name,
                    primary_method,
                    fallback_method,
                )
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
            logger.warning(
                "{}: {} {} test failed and there's no fallback acceleration method for this vendor. "
                "This GPU will be skipped; CPU fallback still works for jobs that targeted it. "
                "See Settings → GPU for the per-device error detail.",
                card_name,
                vendor,
                primary_method,
            )
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
    logger.debug("Windows platform detected; FFmpeg hwaccels: {}", hwaccels)

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
            logger.info("  ✅ Windows NVIDIA CUDA working: {}", gpu_name)
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
        logger.warning(
            "Apple VideoToolbox hardware-decode test failed on this Mac. "
            "All preview generation will fall back to CPU, which is slow on large libraries. "
            "Fix: confirm `ffmpeg -hwaccels` lists 'videotoolbox' on this machine, "
            "and that you're not running ffmpeg under Rosetta. See Settings → GPU for details."
        )

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

    logger.debug("=== Multi-GPU Detection Complete: Found {} working GPU(s) ===", len(detected_gpus))
    return detected_gpus
