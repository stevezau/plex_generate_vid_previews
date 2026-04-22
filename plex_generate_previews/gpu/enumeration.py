"""Platform-specific GPU enumeration helpers.

The information-gathering layer for GPU detection: probes sysfs / DRM /
lspci / nvidia-smi / system_profiler to answer questions like "which
render nodes exist?", "what vendor owns this driver?", "what does
lspci call this PCI device?". The orchestration layer
(:func:`..gpu_detection.detect_all_gpus` and its per-OS detectors)
composes these helpers into the public GPU list.
"""

from __future__ import annotations

import os
import platform
import re
import subprocess

from loguru import logger

from .vaapi_probe import _format_driver_label

# Driver name → GPU vendor mapping.  Linux kernel drivers appear in
# ``/sys/class/drm/<node>/device/driver`` (symlink target); we map them
# to the vendor buckets that the rest of the codebase uses
# ("NVIDIA", "AMD", "INTEL", "ARM", "VIDEOCORE").
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
            with open(version_file, encoding="utf-8", errors="replace") as f:
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


def _get_gpu_devices() -> list[tuple[str, str, str]]:
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
                    render_device_path = os.path.realpath(os.path.join(drm_dir, render_entry, "device"))
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
                logger.debug(f"Skipping {entry}: {render_device} not present in /dev/dri")
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


def _scan_dev_dri_render_devices() -> list[str]:
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
            if entry.startswith("renderD") and os.access(os.path.join(dev_dri, entry), os.R_OK)
        )
    except OSError as e:
        logger.debug(f"Error scanning {dev_dri}: {e}")
        return []


def _enumerate_nvidia_gpus_via_smi() -> list[dict[str, str]]:
    """Enumerate NVIDIA GPUs via nvidia-smi with per-GPU metadata.

    Runs ``nvidia-smi --query-gpu=index,name,uuid --format=csv,noheader``
    and parses the CSV output.  This is the primary NVIDIA enumeration
    source for all environments (bare metal, nvidia-container-runtime,
    WSL2) because NVIDIA CUDA operates through /dev/nvidia* and does not
    require /dev/dri render nodes.

    Returns:
        list[dict[str, str]]: One entry per GPU with keys ``index``,
        ``name``, ``uuid``.  Returns an empty list when nvidia-smi is
        unavailable, times out, or reports no GPUs.

    """
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=index,name,uuid", "--format=csv,noheader"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except FileNotFoundError:
        logger.debug("nvidia-smi command not found (NVIDIA driver may not be installed)")
        return []
    except subprocess.TimeoutExpired:
        logger.debug("nvidia-smi command timed out after 5 seconds")
        return []
    except Exception as e:
        logger.debug(f"Error running nvidia-smi: {e}")
        return []

    if result.returncode != 0:
        logger.debug(f"nvidia-smi failed with return code: {result.returncode}")
        if result.stderr:
            logger.debug(f"nvidia-smi stderr: {result.stderr.strip()}")
        return []

    stdout = (result.stdout or "").strip()
    if not stdout:
        logger.debug("nvidia-smi returned no GPU information")
        return []

    gpus: list[dict[str, str]] = []
    for line in stdout.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 2 or not parts[0]:
            continue
        gpus.append(
            {
                "index": parts[0],
                "name": parts[1],
                "uuid": parts[2] if len(parts) >= 3 else "",
            }
        )

    if gpus:
        logger.debug(f"nvidia-smi detected {len(gpus)} NVIDIA GPU(s): {[g['name'] for g in gpus]}")
    return gpus


def _detect_nvidia_via_nvidia_smi() -> str:
    """Detect NVIDIA GPU using nvidia-smi (presence check only).

    Thin wrapper around :func:`_enumerate_nvidia_gpus_via_smi` preserved
    for callers that only need a vendor string (``"NVIDIA"`` /
    ``"UNKNOWN"``), such as the lspci fallback and vendor-annotation
    paths.  New code that needs per-GPU metadata should call the
    enumeration helper directly.

    Returns:
        str: 'NVIDIA' if at least one NVIDIA GPU is detected, 'UNKNOWN'
        otherwise.

    """
    return "NVIDIA" if _enumerate_nvidia_gpus_via_smi() else "UNKNOWN"


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
        vga_lines = [line for line in result.stdout.split("\n") if "VGA" in line or "Display" in line]
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

        logger.debug("lspci found VGA/Display devices but did not identify known GPU vendor")
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
            logger.debug("WSL2 detected and lspci failed, attempting nvidia-smi detection")
            logger.warning("⚠️  WSL2 environment detected - GPU vendor detection via lspci is unreliable")
            logger.warning("⚠️  NVIDIA GPUs are unofficially supported in WSL2 (via CUDA passthrough)")
            logger.warning("⚠️  Other GPU vendors (AMD, Intel) are NOT supported in WSL2")
            logger.warning("⚠️  For non-NVIDIA GPUs in WSL2, please use CPU-only processing (disable GPUs in Settings)")
            nvidia_vendor = _detect_nvidia_via_nvidia_smi()
            if nvidia_vendor == "NVIDIA":
                vendor = "NVIDIA"
                logger.debug("Successfully detected NVIDIA GPU via nvidia-smi in WSL2")

    return vendor


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
                if "VGA" in line and (gpu_type == "AMD" and "AMD" in line or gpu_type == "INTEL" and "Intel" in line):
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


def _get_pci_address_from_drm_device(gpu_device: str) -> str | None:
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


def _get_lspci_device_name_for_pci_address(pci_address: str) -> str | None:
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
        result = subprocess.run(["lspci", "-s", pci_address], capture_output=True, text=True, timeout=5)
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
                gpu_names = [line.strip() for line in result.stdout.strip().split("\n") if line.strip()]
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


def _log_system_info() -> None:
    """Log system information for debugging GPU detection issues."""
    # Late imports to avoid circular dependency at module-import time.
    from .ffmpeg_capabilities import _check_ffmpeg_version, _get_ffmpeg_hwaccels

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


__all__ = [
    "DRIVER_VENDOR_MAP",
    "_detect_gpu_type_from_lspci",
    "_detect_nvidia_via_nvidia_smi",
    "_get_apple_gpu_name",
    "_get_gpu_devices",
    "_get_gpu_vendor_from_driver",
    "_get_lspci_device_name_for_pci_address",
    "_get_pci_address_from_drm_device",
    "_is_wsl2",
    "_log_system_info",
    "_parse_lspci_gpu_name",
    "_scan_dev_dri_render_devices",
    "get_gpu_name",
]
