"""
GPU detection for video processing acceleration.

Detects available GPU hardware and returns appropriate configuration
for FFmpeg hardware acceleration. Supports NVIDIA, AMD, Intel, Apple (macOS), and Windows GPUs.
"""

import os
import subprocess
import platform
import re
from typing import Tuple, Optional, List, Dict, Any
from loguru import logger

from .utils import is_windows, is_macos

# Minimum required FFmpeg version
MIN_FFMPEG_VERSION = (7, 0, 0)  # FFmpeg 7.0.0+ for better hardware acceleration support

# GPU vendor to acceleration method mapping
# This defines which acceleration methods to use for each GPU vendor
GPU_ACCELERATION_MAP = {
    'NVIDIA': {
        'primary': 'CUDA',
        'fallback': None,  # VAAPI doesn't work properly with NVIDIA
        'requires_runtime': True,  # Needs nvidia-docker runtime
        'test_encoder': 'h264_nvenc'
    },
    'AMD': {
        'primary': 'VAAPI',
        'fallback': None,
        'requires_runtime': False,
        'test_encoder': None  # Use hwaccel test instead
    },
    'INTEL': {
        'primary': 'VAAPI',
        'fallback': None,
        'requires_runtime': False,
        'test_encoder': None
    },
    'ARM': {
        'primary': 'VAAPI',
        'fallback': None,
        'requires_runtime': False,
        'test_encoder': None
    },
    'VIDEOCORE': {
        'primary': 'VAAPI',
        'fallback': None,
        'requires_runtime': False,
        'test_encoder': None
    },
    'APPLE': {
        'primary': 'VIDEOTOOLBOX',
        'fallback': None,
        'requires_runtime': False,
        'test_encoder': None  # Use hwaccel test instead
    },
    'WINDOWS_GPU': {
        'primary': 'D3D11VA',
        'fallback': None,
        'requires_runtime': False,
        'test_encoder': None  # Use hwaccel test instead
    }
}

# Driver name to GPU vendor mapping
DRIVER_VENDOR_MAP = {
    'nvidia': 'NVIDIA',
    'nouveau': 'NVIDIA',
    'amdgpu': 'AMD',
    'radeon': 'AMD',
    'i915': 'INTEL',
    'xe': 'INTEL',  # New Intel graphics driver
    'panfrost': 'ARM',
    'vc4': 'VIDEOCORE'
}


def _get_ffmpeg_version() -> Optional[Tuple[int, int, int]]:
    """
    Get FFmpeg version as a tuple of integers.
    
    Returns:
        Optional[Tuple[int, int, int]]: Version tuple (major, minor, patch) or None if failed
    """
    try:
        result = subprocess.run(['ffmpeg', '-version'], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=5)
        if result.returncode != 0:
            logger.debug(f"Failed to get FFmpeg version: {result.stderr}")
            return None
        
        # Extract version from first line: "ffmpeg version 7.1.1-1ubuntu1.2 Copyright..."
        version_line = result.stdout.split('\n')[0] if result.stdout else ""
        logger.debug(f"FFmpeg version string: '{version_line}'")
        
        # Special-case date-based git builds (e.g., "ffmpeg version 2025-10-12-git-...")
        # These are not semantic versions; treat as "unknown version" so we don't
        # incorrectly parse the year as the major version.
        if re.search(r"ffmpeg version \d{4}-\d{2}-\d{2}-", version_line):
            logger.debug("Detected date-based FFmpeg git build; skipping semantic version parsing")
            return None
        
        # Try multiple patterns to handle different FFmpeg version formats
        # Patterns ordered from most specific to least specific
        patterns = [
            (r'ffmpeg version (\d+)\.(\d+)\.(\d+)', 3),  # Standard: ffmpeg version 7.1.1
            (r'version (\d+)\.(\d+)\.(\d+)', 3),          # Alternate: version 7.1.1
            (r'ffmpeg[^\d]*(\d+)\.(\d+)\.(\d+)', 3),     # Flexible: any text between ffmpeg and version
            (r'ffmpeg version (\d+)\.(\d+)', 2),          # Two-part version: ffmpeg version 8.0
            (r'version (\d+)\.(\d+)', 2),                 # Alternate: version 8.0
            (r'ffmpeg[^\d]*(\d+)\.(\d+)', 2),            # Flexible: any text between ffmpeg and version
            (r'ffmpeg version (\d+)', 1),                 # Single version: ffmpeg version 8
            (r'version (\d+)', 1),                        # Alternate: version 8
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
    """
    Check if FFmpeg version meets minimum requirements.
    
    Returns:
        bool: True if version is sufficient, False otherwise
    """
    version = _get_ffmpeg_version()
    if version is None:
        logger.warning("Could not determine FFmpeg version - proceeding with caution")
        return True  # Don't fail if we can't determine version
    
    if version >= MIN_FFMPEG_VERSION:
        logger.debug(f"✓ FFmpeg version {version[0]}.{version[1]}.{version[2]} meets minimum requirement {MIN_FFMPEG_VERSION[0]}.{MIN_FFMPEG_VERSION[1]}.{MIN_FFMPEG_VERSION[2]}")
        return True
    else:
        logger.warning(f"⚠ FFmpeg version {version[0]}.{version[1]}.{version[2]} is below minimum requirement {MIN_FFMPEG_VERSION[0]}.{MIN_FFMPEG_VERSION[1]}.{MIN_FFMPEG_VERSION[2]}")
        logger.warning("Hardware acceleration may not work properly. Please upgrade FFmpeg.")
        return False


def _get_ffmpeg_hwaccels() -> List[str]:
    """
    Get list of available FFmpeg hardware accelerators.
    
    Returns:
        List[str]: Available hardware accelerators
    """
    try:
        result = subprocess.run(['ffmpeg', '-hwaccels'], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=5)
        if result.returncode != 0:
            logger.debug(f"Failed to get FFmpeg hardware accelerators: {result.stderr}")
            return []
        
        hwaccels = []
        for line in result.stdout.split('\n'):
            line = line.strip()
            if line and not line.startswith('Hardware acceleration methods:'):
                hwaccels.append(line)
        
        return hwaccels
    except Exception as e:
        logger.debug(f"Error getting FFmpeg hardware accelerators: {e}")
        return []


def _is_hwaccel_available(hwaccel: str) -> bool:
    """
    Check if a specific hardware acceleration is available.
    
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
    """
    Check if a device is accessible (exists and readable).
    
    Args:
        device_path: Path to device to check
        
    Returns:
        tuple[bool, str]: (is_accessible, reason) where reason is:
            'accessible' - device exists and is readable
            'not_found' - device does not exist
            'permission_denied' - device exists but is not readable
    """
    if not os.path.exists(device_path):
        logger.debug(f"✗ Device does not exist: {device_path}")
        return False, 'not_found'
    
    if not os.access(device_path, os.R_OK):
        # Get device file stats for better diagnostics
        try:
            stat_info = os.stat(device_path)
            import stat as stat_module
            mode = stat_info.st_mode
            owner_uid = stat_info.st_uid
            group_gid = stat_info.st_gid
            perms = stat_module.filemode(mode)
            
            logger.debug(f"✗ Device exists but is not readable: {device_path}")
            logger.debug(f"  Device permissions: {perms} (owner={owner_uid}, group={group_gid})")
            logger.debug(f"  Current user: {os.getuid()}, groups: {os.getgroups()}")
        except Exception as e:
            logger.debug(f"✗ Device exists but is not readable: {device_path}")
            logger.debug(f"  Current user: {os.getuid()}, groups: {os.getgroups()}")
            logger.debug(f"  Could not get device stats: {e}")
        return False, 'permission_denied'
    
    logger.debug(f"✓ Device is accessible: {device_path}")
    return True, 'accessible'


def _test_hwaccel_functionality(hwaccel: str, device_path: Optional[str] = None) -> bool:
    """
    Test if hardware acceleration actually works by running a simple FFmpeg command.
    
    Args:
        hwaccel: Hardware acceleration type to test
        device_path: Optional device path for VAAPI
        
    Returns:
        bool: True if hardware acceleration works, False otherwise
    """
    try:
        # For VAAPI, check device accessibility first
        if hwaccel == 'vaapi' and device_path:
            accessible, reason = _check_device_access(device_path)
            if not accessible:
                # Only show permission warnings if the device exists but is not accessible
                if reason == 'permission_denied':
                    # Get device group for specific recommendation
                    try:
                        stat_info = os.stat(device_path)
                        device_gid = stat_info.st_gid
                        user_groups = os.getgroups()
                        
                        logger.warning(f"⚠ VAAPI device {device_path} is not accessible (permission denied)")
                        logger.warning(f"⚠ Device group: {device_gid}, your groups: {user_groups}")
                        
                        if device_gid not in user_groups:
                            current_uid = os.getuid()
                            logger.warning(f"⚠ Solution: Set PGID to {device_gid} to access this device")
                            logger.warning(f"⚠ Example: docker run -e PUID={current_uid} -e PGID={device_gid} --device /dev/dri:/dev/dri ...")
                        else:
                            logger.warning(f"⚠ You are in group {device_gid}, but device is still not accessible")
                            logger.warning(f"⚠ Check host device permissions: ls -l {device_path}")
                    except Exception:
                        logger.warning(f"⚠ VAAPI device {device_path} is not accessible (permission denied)")
                        logger.warning(f"⚠ Solution: Add your user to the 'render' or 'video' group, or set PGID to match the device group")
                        logger.warning(f"⚠ Example: docker run -e PGID=<device_group_id> --device /dev/dri:/dev/dri ...")
                # If device doesn't exist, just skip silently (expected for wrong GPU type)
                return False
        # Get test video fixture - all GPU tests use real H.264 video for accurate testing
        test_video = None
        possible_paths = [
            os.path.join(os.path.dirname(__file__), 'fixtures', 'test_video.mp4'),
            os.path.join(os.getcwd(), 'plex_generate_previews', 'fixtures', 'test_video.mp4'),
        ]
        
        for path in possible_paths:
            if os.path.exists(path):
                test_video = path
                break
        
        if not test_video:
            logger.debug("Test video fixture not found, cannot test GPU acceleration")
            return False
        
        # Build FFmpeg command - test hardware decode -> scale -> JPEG encode
        cmd = ['ffmpeg']
        
        # Add hardware acceleration flags (before -i)
        if hwaccel == 'cuda':
            cmd += ['-hwaccel', 'cuda']
        elif hwaccel == 'vaapi' and device_path:
            cmd += ['-hwaccel', 'vaapi', '-vaapi_device', device_path]
        elif hwaccel == 'd3d11va':
            cmd += ['-hwaccel', 'd3d11va']
        elif hwaccel == 'videotoolbox':
            cmd += ['-hwaccel', 'videotoolbox']
        else:
            cmd += ['-hwaccel', hwaccel]
        
        # Choose OS-appropriate null sink
        null_sink = 'NUL' if is_windows() else '/dev/null'
        
        # Filters: D3D11VA requires downloading frames from GPU memory
        if hwaccel == 'd3d11va':
            # Ensure frames are brought back to system memory before software filters
            # Also set explicit output format for d3d11
            if '-hwaccel_output_format' not in cmd:
                cmd += ['-hwaccel_output_format', 'd3d11']
            video_filter = 'hwdownload,format=nv12,select=eq(n\\,0),scale=320:240'
        else:
            video_filter = 'select=eq(n\\,0),scale=320:240'
        
        # Add input file and JPEG extraction
        cmd += [
            '-i', test_video,
            '-vf', video_filter,
            '-frames:v', '1',
            '-f', 'image2',
            '-c:v', 'mjpeg',
            '-q:v', '2',
            '-y',
            null_sink
        ]
        
        logger.debug(f"Testing {hwaccel} functionality: {' '.join(cmd)}")
        result = subprocess.run(cmd, capture_output=True, timeout=10)
        
        # FFmpeg returns 0 for success, 141 for SIGPIPE (which is OK for our test)
        if result.returncode in [0, 141]:
            logger.debug(f"✓ {hwaccel} functionality test passed")
            return True
        else:
            logger.debug(f"✗ {hwaccel} functionality test failed (exit code: {result.returncode})")
            
            # Show the actual FFmpeg error
            if result.stderr:
                stderr_text = result.stderr.decode('utf-8', 'ignore')
                stderr_lines = stderr_text.split('\n')[-3:]
                logger.debug(f"Error output: {' '.join(line.strip() for line in stderr_lines if line.strip())}")
                
                # Add helpful context for common errors
                stderr_lower = stderr_text.lower()
                if hwaccel == 'cuda':
                    if '/dev/null' in stderr_lower and 'operation not permitted' in stderr_lower:
                        logger.debug(f"Note: /dev/null errors usually indicate missing NVIDIA Container Toolkit")
                        logger.debug(f"      Run with --gpus all or configure nvidia-docker runtime")
                elif hwaccel == 'vaapi':
                    if 'permission denied' in stderr_lower:
                        logger.warning(f"⚠ VAAPI device permission denied")
                        logger.warning(f"⚠ Add user to 'render' or 'video' group, or adjust PGID")
            
            return False
            
    except subprocess.TimeoutExpired:
        logger.debug(f"✗ {hwaccel} functionality test timed out")
        return False
    except Exception as e:
        logger.debug(f"✗ {hwaccel} functionality test failed with exception: {e}")
        return False




def _is_wsl2() -> bool:
    """
    Detect if running on Windows Subsystem for Linux 2 (WSL2).
    
    WSL2 has limited hardware access due to virtualization, which can affect
    GPU detection via lspci. This function helps identify WSL2 environments
    for special handling.
    
    Returns:
        bool: True if running on WSL2, False otherwise
    """
    if platform.system() != 'Linux':
        return False
    
    try:
        # Check /proc/version for WSL2 indicators
        # WSL2 typically contains "microsoft" and "WSL" in /proc/version
        version_file = '/proc/version'
        if os.path.exists(version_file):
            with open(version_file, 'r', encoding='utf-8', errors='replace') as f:
                version_text = f.read().lower()
                if 'microsoft' in version_text and 'wsl' in version_text:
                    logger.debug("Detected WSL2 environment")
                    return True
    except Exception as e:
        logger.debug(f"Error checking for WSL2: {e}")
    
    return False


def _get_apple_gpu_name() -> str:
    """
    Get Apple GPU name from system_profiler.
    
    Returns:
        str: GPU name or fallback description
    """
    try:
        result = subprocess.run(
            ['system_profiler', 'SPDisplaysDataType'], 
            capture_output=True, 
            text=True, 
            timeout=5
        )
        if result.returncode == 0:
            # Parse output for GPU name
            # Look for "Chipset Model:" line
            for line in result.stdout.split('\n'):
                if 'Chipset Model:' in line:
                    gpu_name = line.split(':', 1)[1].strip()
                    logger.debug(f"Detected Apple GPU: {gpu_name}")
                    return gpu_name
    except Exception as e:
        logger.debug(f"Error getting Apple GPU name: {e}")
    
    # Fallback - check for Apple Silicon using platform
    machine = platform.machine()
    if machine == 'arm64':
        return "Apple Silicon GPU"
    
    return "Apple GPU"


def _get_gpu_devices() -> List[Tuple[str, str, str]]:
    """
    Get all GPU devices with their render devices and driver information.
    
    Returns:
        List[Tuple[str, str, str]]: List of (card_name, render_device, driver) tuples
    """
    devices = []
    drm_dir = "/sys/class/drm"
    
    # Skip Linux DRM scan entirely on non-Linux platforms to avoid misleading logs
    if platform.system() != 'Linux':
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
            
            # Extract card number
            try:
                card_num = int(entry[4:])  # card0 -> 0, card1 -> 1
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
            
            # Get driver information
            driver_path = os.path.join(drm_dir, entry, "device", "driver")
            driver = "unknown"
            if os.path.islink(driver_path):
                driver = os.path.basename(os.readlink(driver_path))
            
            devices.append((entry, render_device, driver))
            logger.debug(f"Found GPU: {entry} -> {render_device} (driver: {driver})")
    
    except Exception as e:
        logger.debug(f"Error scanning GPU devices: {e}")
    
    return devices




def _get_gpu_vendor_from_driver(driver_name: str) -> str:
    """
    Map driver name to GPU vendor using DRIVER_VENDOR_MAP.
    
    Args:
        driver_name: Linux driver name (e.g., 'i915', 'nvidia', 'amdgpu')
        
    Returns:
        str: GPU vendor ('NVIDIA', 'AMD', 'INTEL', 'ARM', 'VIDEOCORE', or 'UNKNOWN')
    """
    vendor = DRIVER_VENDOR_MAP.get(driver_name, 'UNKNOWN')
    
    if vendor == 'UNKNOWN':
        logger.debug(f"Unknown driver '{driver_name}', attempting lspci detection")
        vendor = _detect_gpu_type_from_lspci()
        
        # If lspci failed and we're in WSL2, try nvidia-smi as fallback
        # This helps detect NVIDIA GPUs in WSL2 where lspci doesn't work
        if vendor == 'UNKNOWN' and _is_wsl2():
            logger.debug("WSL2 detected and lspci failed, attempting nvidia-smi detection")
            logger.warning("⚠️  WSL2 environment detected - GPU vendor detection via lspci is unreliable")
            logger.warning("⚠️  NVIDIA GPUs are unofficially supported in WSL2 (via CUDA passthrough)")
            logger.warning("⚠️  Other GPU vendors (AMD, Intel) are NOT supported in WSL2")
            logger.warning("⚠️  For non-NVIDIA GPUs in WSL2, please use CPU-only processing (set GPU_THREADS=0)")
            nvidia_vendor = _detect_nvidia_via_nvidia_smi()
            if nvidia_vendor == 'NVIDIA':
                vendor = 'NVIDIA'
                logger.debug("Successfully detected NVIDIA GPU via nvidia-smi in WSL2")
    
    return vendor


def _detect_nvidia_via_nvidia_smi() -> str:
    """
    Detect NVIDIA GPU using nvidia-smi as fallback when driver detection fails.
    
    This is useful in WSL2 environments where lspci cannot detect GPU vendors
    due to hardware virtualization, but nvidia-smi works correctly via the
    Windows NVIDIA driver passthrough.
    
    Returns:
        str: 'NVIDIA' if NVIDIA GPU is detected, 'UNKNOWN' otherwise
    """
    try:
        # Check if nvidia-smi is available and can query GPU information
        result = subprocess.run(
            ['nvidia-smi', '--query-gpu=name', '--format=csv,noheader'],
            capture_output=True,
            text=True,
            timeout=5
        )
        
        if result.returncode == 0 and result.stdout.strip():
            # nvidia-smi successfully returned GPU names
            gpu_names = [line.strip() for line in result.stdout.strip().split('\n') if line.strip()]
            if gpu_names:
                logger.debug(f"nvidia-smi detected {len(gpu_names)} NVIDIA GPU(s): {gpu_names}")
                return 'NVIDIA'
        
        # nvidia-smi failed or returned no GPUs
        if result.returncode != 0:
            logger.debug(f"nvidia-smi failed with return code: {result.returncode}")
            if result.stderr:
                logger.debug(f"nvidia-smi stderr: {result.stderr.strip()}")
        else:
            logger.debug("nvidia-smi returned no GPU information")
        
        return 'UNKNOWN'
    except FileNotFoundError:
        # nvidia-smi not installed
        logger.debug("nvidia-smi command not found (NVIDIA driver may not be installed)")
        return 'UNKNOWN'
    except subprocess.TimeoutExpired:
        logger.debug("nvidia-smi command timed out after 5 seconds")
        return 'UNKNOWN'
    except Exception as e:
        logger.debug(f"Error running nvidia-smi: {e}")
        return 'UNKNOWN'


def _detect_gpu_type_from_lspci() -> str:
    """
    Detect GPU type using lspci as fallback when driver detection fails.
    
    This is a non-critical optional enhancement. If lspci is not available
    or fails for any reason, it safely returns 'UNKNOWN' without logging errors.
    
    Returns:
        str: GPU type ('AMD', 'INTEL', 'NVIDIA', 'ARM', or 'UNKNOWN')
    """
    try:
        result = subprocess.run(['lspci'], capture_output=True, text=True, timeout=5)
        if result.returncode != 0:
            logger.debug(f"lspci command failed with return code: {result.returncode}")
            if result.stderr:
                logger.debug(f"lspci stderr: {result.stderr.strip()}")
            if result.stdout:
                logger.debug(f"lspci stdout (partial): {result.stdout[:200]}")
            return 'UNKNOWN'
        
        # Check if we have any output
        if not result.stdout.strip():
            logger.debug("lspci returned empty output")
            return 'UNKNOWN'
        
        # Count VGA/Display lines for debugging
        vga_lines = [line for line in result.stdout.split('\n') if 'VGA' in line or 'Display' in line]
        if not vga_lines:
            logger.debug("lspci did not find any VGA or Display devices in output")
            logger.debug(f"lspci output (first 500 chars): {result.stdout[:500]}")
            return 'UNKNOWN'
        
        logger.debug(f"lspci found {len(vga_lines)} VGA/Display device(s)")
        for line in vga_lines:
            logger.debug(f"lspci VGA/Display line: {line.strip()}")
            line_lower = line.lower()
            if 'amd' in line_lower or 'radeon' in line_lower:
                logger.debug("lspci detected AMD GPU")
                return 'AMD'
            elif 'intel' in line_lower:
                logger.debug("lspci detected Intel GPU")
                return 'INTEL'
            elif 'nvidia' in line_lower or 'geforce' in line_lower:
                logger.debug("lspci detected NVIDIA GPU")
                return 'NVIDIA'
            elif 'mali' in line_lower or 'arm' in line_lower:
                logger.debug("lspci detected ARM GPU")
                return 'ARM'
        
        logger.debug("lspci found VGA/Display devices but did not identify known GPU vendor")
        logger.debug(f"VGA/Display lines: {[line.strip() for line in vga_lines]}")
        return 'UNKNOWN'
    except FileNotFoundError:
        # lspci not installed - this is expected in many environments
        logger.debug("lspci command not found (not installed)")
        return 'UNKNOWN'
    except subprocess.TimeoutExpired:
        logger.debug("lspci command timed out after 5 seconds")
        return 'UNKNOWN'
    except Exception as e:
        logger.debug(f"Error running lspci: {e}")
        return 'UNKNOWN'


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
            logger.debug(f"  {card_name} -> {render_device} (driver: {driver})")
    
    logger.debug("=== End System Information ===")


def _parse_lspci_gpu_name(gpu_type: str) -> str:
    """
    Parse GPU name from lspci output to get a user-friendly GPU model name.
    
    This is a non-critical optional enhancement. If lspci is not available,
    it silently falls back to a generic name like "INTEL GPU" or "AMD GPU".
    GPU detection and functionality are not affected.
    
    Args:
        gpu_type: Type of GPU ('AMD', 'INTEL')
        
    Returns:
        str: GPU name or fallback description (never fails, always returns a string)
    """
    try:
        result = subprocess.run(['lspci'], capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            for line in result.stdout.split('\n'):
                if 'VGA' in line and (gpu_type == 'AMD' and 'AMD' in line or gpu_type == 'INTEL' and 'Intel' in line):
                    parts = line.split(':')
                    if len(parts) > 2:
                        return parts[2].strip()
    except FileNotFoundError:
        # lspci not installed - this is fine, just use generic name
        pass
    except Exception as e:
        # Other errors (timeout, etc) - log for debugging
        logger.debug(f"Error running lspci for {gpu_type}: {e}")
    
    return f"{gpu_type} GPU"


def get_gpu_name(gpu_type: str, gpu_device: str) -> str:
    """
    Extract GPU model name from system.
    
    Args:
        gpu_type: Type of GPU ('NVIDIA', 'AMD', 'INTEL', 'APPLE')
        gpu_device: GPU device path or info string
        
    Returns:
        str: GPU model name or fallback description
    """
    try:
        if gpu_type == 'NVIDIA':
            # Use nvidia-smi to get GPU name
            result = subprocess.run(['nvidia-smi', '--query-gpu=name', '--format=csv,noheader,nounits'], 
                                  capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                gpu_names = [line.strip() for line in result.stdout.strip().split('\n') if line.strip()]
                if gpu_names:
                    return gpu_names[0]  # Return first GPU name
            return "NVIDIA GPU"
            
        elif gpu_type == 'APPLE':
            return _get_apple_gpu_name()
            
        elif gpu_type == 'WINDOWS_GPU':
            return "Windows GPU"
            
        elif gpu_type in ('AMD', 'INTEL') and gpu_device.startswith('/dev/dri/'):
            # Try to get GPU info from lspci
            gpu_name = _parse_lspci_gpu_name(gpu_type)
            return gpu_name  # Don't add (VAAPI) here, format_gpu_info will add it
            
    except Exception as e:
        logger.debug(f"Error getting GPU name for {gpu_type}: {e}")
    
    # Fallback - return only the GPU type name without acceleration method
    return f"{gpu_type} GPU"


def format_gpu_info(gpu_type: str, gpu_device: str, gpu_name: str, acceleration: str = None) -> str:
    """
    Format GPU information for display.
    
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
        if acceleration == 'VAAPI' and gpu_device.startswith('/dev/dri/'):
            return f"{gpu_name} (VAAPI - {gpu_device})"
        else:
            return f"{gpu_name} ({acceleration})"
    
    # Fallback to old logic for backward compatibility
    if gpu_type == 'NVIDIA':
        return f"{gpu_name} (CUDA)"
    elif gpu_type == 'WINDOWS_GPU':
        return f"{gpu_name} (D3D11VA - Universal Windows GPU)"
    elif gpu_type == 'APPLE':
        return f"{gpu_name} (VideoToolbox)"
    elif gpu_type in ('AMD', 'INTEL', 'ARM', 'VIDEOCORE') and gpu_device.startswith('/dev/dri/'):
        return f"{gpu_name} (VAAPI - {gpu_device})"
    elif gpu_type == 'UNKNOWN':
        return f"{gpu_name} (Unknown GPU)"
    else:
        return f"{gpu_name} ({gpu_type})"


def _test_acceleration_method(vendor: str, acceleration: str, device_path: Optional[str] = None) -> bool:
    """
    Test if a specific acceleration method works for a GPU vendor.
    
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
    
    config = GPU_ACCELERATION_MAP[vendor]
    
    # Test the acceleration functionality
    if acceleration == 'CUDA':
        test_passed = _test_hwaccel_functionality('cuda')
    elif acceleration == 'VAAPI':
        if not device_path:
            logger.debug("VAAPI requires device_path")
            return False
        test_passed = _test_hwaccel_functionality('vaapi', device_path)
    elif acceleration == 'D3D11VA':
        test_passed = _test_hwaccel_functionality('d3d11va')
    elif acceleration == 'VIDEOTOOLBOX':
        test_passed = _test_hwaccel_functionality('videotoolbox')
    else:
        logger.debug(f"Unknown acceleration method: {acceleration}")
        return False
    
    # Return test result
    if test_passed:
        logger.debug(f"✓ {vendor} {acceleration} hardware acceleration test passed")
    else:
        logger.debug(f"✗ {vendor} {acceleration} functionality test failed")
    return test_passed


def _detect_linux_gpus() -> List[Tuple[str, str, dict]]:
    """
    Detect Linux GPUs from /dev/dri devices.
    
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
    else:
        logger.debug(f"Found {len(physical_gpus)} physical GPU(s) in /dev/dri")
        for card_name, render_device, driver in physical_gpus:
            logger.debug(f"  {card_name}: {render_device} (driver: {driver})")
    
    # For each physical GPU, test appropriate acceleration methods
    logger.debug("=== Testing GPU Acceleration Methods ===")
    for card_name, render_device, driver in physical_gpus:
        vendor = _get_gpu_vendor_from_driver(driver)
        
        # Handle UNKNOWN vendor with special fallback logic for CUDA
        if vendor == 'UNKNOWN':
            # Check if CUDA acceleration is available (useful for WSL2 NVIDIA GPUs)
            if _is_hwaccel_available('cuda'):
                logger.debug(f"Unknown vendor for {card_name}, but CUDA is available - attempting NVIDIA detection")
                nvidia_vendor = _detect_nvidia_via_nvidia_smi()
                if nvidia_vendor == 'NVIDIA':
                    logger.info(f"  Detected NVIDIA GPU via nvidia-smi for {card_name} (vendor was unknown)")
                    vendor = 'NVIDIA'
                    if _is_wsl2():
                        logger.warning(f"  ⚠️  WSL2 environment detected - NVIDIA GPU support is unofficial and may have limitations")
                else:
                    # Even if nvidia-smi didn't confirm, try CUDA anyway if available
                    # This allows unofficial WSL2 support where detection may be unreliable
                    logger.debug(f"nvidia-smi did not confirm NVIDIA, but will attempt CUDA acceleration anyway")
                    if _is_wsl2():
                        logger.debug("WSL2 detected - allowing CUDA acceleration attempt with unknown vendor (unofficial support)")
                        # Test CUDA directly - if it works, treat as NVIDIA
                        logger.info(f"  Checking {card_name} (UNKNOWN vendor, attempting CUDA)...")
                        logger.info(f"    Testing CUDA acceleration...")
                        if _test_hwaccel_functionality('cuda'):
                            # CUDA works! Treat as NVIDIA even though we couldn't confirm
                            vendor = 'NVIDIA'
                            logger.warning(f"  ⚠️  CUDA acceleration works but vendor detection failed - treating as NVIDIA")
                            logger.warning(f"  ⚠️  WSL2 environment detected - NVIDIA GPU support is unofficial and may have limitations")
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
        primary_method = accel_config['primary']
        fallback_method = accel_config['fallback']
        
        # Determine device path for testing
        if primary_method == 'CUDA':
            device_path = 'cuda'
        elif primary_method == 'VAAPI':
            device_path = render_device
        else:
            device_path = None
        
        # Test primary acceleration method
        logger.debug(f"  Testing primary method: {primary_method}")
        logger.info(f"    Testing {primary_method} acceleration...")
        if _test_acceleration_method(vendor, primary_method, device_path):
            gpu_name = get_gpu_name(vendor, device_path)
            gpu_info = {
                'name': gpu_name,
                'acceleration': primary_method,
                'device_path': device_path,
                'render_device': render_device,  # Store the actual /dev/dri device
                'card': card_name,
                'driver': driver
            }
            detected_gpus.append((vendor, device_path, gpu_info))
            detected_vendors.add(vendor)
            logger.info(f"  ✅ {card_name}: {vendor} {primary_method} working")
            continue  # Primary worked, skip fallback
        
        # Primary failed, log appropriate message
        if accel_config.get('requires_runtime'):
            logger.warning(f"  ⚠️  {card_name}: {vendor} {primary_method} test failed")
            logger.warning(f"  ⚠️  This usually means the required runtime is not configured")
            if primary_method == 'CUDA':
                logger.warning(f"  ⚠️  See: https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html")
        else:
            logger.debug(f"  ✗ {card_name}: {primary_method} test failed")
        
        # Test fallback method if available
        if fallback_method:
            logger.debug(f"  Testing fallback method: {fallback_method}")
            logger.info(f"    Testing fallback {fallback_method} acceleration...")
            fallback_device_path = render_device if fallback_method == 'VAAPI' else device_path
            
            if _test_acceleration_method(vendor, fallback_method, fallback_device_path):
                gpu_name = get_gpu_name(vendor, fallback_device_path)
                gpu_info = {
                    'name': gpu_name,
                    'acceleration': fallback_method,
                    'device_path': fallback_device_path,
                    'render_device': render_device,
                    'card': card_name,
                    'driver': driver
                }
                detected_gpus.append((vendor, fallback_device_path, gpu_info))
                detected_vendors.add(vendor)
                logger.info(f"  ✅ {card_name}: {vendor} {fallback_method} working (fallback)")
            else:
                logger.warning(f"  ❌ {card_name}: All acceleration methods failed")
        else:
            logger.warning(f"  ❌ {card_name}: No fallback available, GPU unusable")
    
    return detected_gpus


def _detect_windows_gpus() -> List[Tuple[str, str, dict]]:
    """
    Detect Windows GPUs using D3D11VA.
    
    Returns:
        List[Tuple[str, str, dict]]: List of (gpu_type, gpu_device, gpu_info_dict)
    """
    detected_gpus = []
    
    # Try D3D11VA if ffmpeg reports it
    hwaccels = _get_ffmpeg_hwaccels()
    logger.debug(f"Windows platform detected; FFmpeg hwaccels: {hwaccels}")
    if 'd3d11va' in hwaccels:
        logger.info("  Checking Windows D3D11VA GPU...")
        logger.info("    Testing D3D11VA acceleration...")
        if _test_acceleration_method('WINDOWS_GPU', 'D3D11VA', 'd3d11va'):
            gpu_name = "Windows GPU"
            gpu_info = {
                'name': gpu_name,
                'acceleration': 'D3D11VA',
                'device_path': 'd3d11va',
            }
            detected_gpus.append(('WINDOWS_GPU', 'd3d11va', gpu_info))
            logger.info("  ✅ Windows D3D11VA working")
    else:
        logger.debug("d3d11va not reported by FFmpeg; skipping Windows D3D11VA probe")
    
    return detected_gpus


def _detect_macos_gpus() -> List[Tuple[str, str, dict]]:
    """
    Detect macOS GPUs using VideoToolbox.
    
    Returns:
        List[Tuple[str, str, dict]]: List of (gpu_type, gpu_device, gpu_info_dict)
    """
    detected_gpus = []
    
    # Check for macOS VideoToolbox (doesn't use /dev/dri)
    logger.debug("Detected macOS platform, testing VideoToolbox acceleration...")
    logger.info("  Checking Apple GPU...")
    logger.info("    Testing VideoToolbox acceleration...")
    if _test_acceleration_method('APPLE', 'VIDEOTOOLBOX', 'videotoolbox'):
        gpu_name = get_gpu_name('APPLE', 'videotoolbox')
        gpu_info = {
            'name': gpu_name,
            'acceleration': 'VIDEOTOOLBOX',
            'device_path': 'videotoolbox'
        }
        detected_gpus.append(('APPLE', 'videotoolbox', gpu_info))
        logger.info("  ✅ Apple VideoToolbox working")
    else:
        logger.warning("  ❌ Apple VideoToolbox test failed")
    
    return detected_gpus


def detect_all_gpus() -> List[Tuple[str, str, dict]]:
    """
    Detect all available GPU hardware using FFmpeg capability detection.
    
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
    if platform.system() == 'Linux':
        detected_gpus.extend(_detect_linux_gpus())
    elif is_windows():
        detected_gpus.extend(_detect_windows_gpus())
    elif is_macos():
        detected_gpus.extend(_detect_macos_gpus())
    
    logger.debug(f"=== Multi-GPU Detection Complete: Found {len(detected_gpus)} working GPU(s) ===")
    return detected_gpus


