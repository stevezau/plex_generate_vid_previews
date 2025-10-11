"""
GPU detection for video processing acceleration.

Detects available GPU hardware and returns appropriate configuration
for FFmpeg hardware acceleration. Supports NVIDIA, AMD, Intel, and WSL2 GPUs.
"""

import os
import subprocess
import platform
import re
from typing import Tuple, Optional, List
from loguru import logger

# Minimum required FFmpeg version
MIN_FFMPEG_VERSION = (7, 0, 0)  # FFmpeg 7.0.0+ for better hardware acceleration support


def _get_ffmpeg_version() -> Optional[Tuple[int, int, int]]:
    """
    Get FFmpeg version as a tuple of integers.
    
    Returns:
        Optional[Tuple[int, int, int]]: Version tuple (major, minor, patch) or None if failed
    """
    try:
        result = subprocess.run(['ffmpeg', '-version'], capture_output=True, text=True, timeout=5)
        if result.returncode != 0:
            logger.debug(f"Failed to get FFmpeg version: {result.stderr}")
            return None
        
        # Extract version from first line: "ffmpeg version 7.1.1-1ubuntu1.2 Copyright..."
        version_line = result.stdout.split('\n')[0]
        version_match = re.search(r'ffmpeg version (\d+)\.(\d+)\.(\d+)', version_line)
        
        if version_match:
            major, minor, patch = map(int, version_match.groups())
            logger.debug(f"FFmpeg version: {major}.{minor}.{patch}")
            return (major, minor, patch)
        else:
            logger.debug(f"Could not parse FFmpeg version from: {version_line}")
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
        result = subprocess.run(['ffmpeg', '-hwaccels'], capture_output=True, text=True, timeout=5)
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
        # Build FFmpeg command based on acceleration type
        if hwaccel == 'cuda':
            cmd = ['ffmpeg', '-f', 'lavfi', '-i', 'testsrc=duration=0.1:size=320x240:rate=1',
                   '-c:v', 'h264_nvenc', '-t', '0.1', '-f', 'null', '/dev/null']
        elif hwaccel == 'vaapi' and device_path:
            # For VAAPI, test hardware acceleration initialization rather than encoding
            # since encoding often fails due to driver issues even when hwaccel works
            cmd = ['ffmpeg', '-hwaccel', 'vaapi', '-vaapi_device', device_path, 
                   '-f', 'lavfi', '-i', 'testsrc=duration=0.1:size=320x240:rate=1',
                   '-t', '0.1', '-f', 'null', '/dev/null']
        elif hwaccel == 'qsv':
            cmd = ['ffmpeg', '-f', 'lavfi', '-i', 'testsrc=duration=0.1:size=320x240:rate=1',
                   '-c:v', 'h264_qsv', '-t', '0.1', '-f', 'null', '/dev/null']
        elif hwaccel == 'd3d11va':
            cmd = ['ffmpeg', '-f', 'lavfi', '-i', 'testsrc=duration=0.1:size=320x240:rate=1',
                   '-c:v', 'h264_nvenc', '-t', '0.1', '-f', 'null', '/dev/null']  # WSL2 can use NVENC
        else:
            # For other types, just test basic hardware acceleration
            cmd = ['ffmpeg', '-hwaccel', hwaccel, '-f', 'lavfi', '-i', 'testsrc=duration=0.1:size=320x240:rate=1',
                   '-t', '0.1', '-f', 'null', '/dev/null']
        
        logger.debug(f"Testing {hwaccel} functionality: {' '.join(cmd)}")
        result = subprocess.run(cmd, capture_output=True, timeout=10)
        
        # FFmpeg returns 0 for success, 141 for SIGPIPE (which is OK for our test)
        if result.returncode in [0, 141]:
            logger.debug(f"✓ {hwaccel} functionality test passed")
            return True
        else:
            logger.debug(f"✗ {hwaccel} functionality test failed (exit code: {result.returncode})")
            if result.stderr:
                stderr_lines = result.stderr.decode('utf-8', 'ignore').split('\n')[-3:]
                logger.debug(f"Error output: {' '.join(stderr_lines)}")
            return False
            
    except subprocess.TimeoutExpired:
        logger.debug(f"✗ {hwaccel} functionality test timed out")
        return False
    except Exception as e:
        logger.debug(f"✗ {hwaccel} functionality test failed with exception: {e}")
        return False


def _get_gpu_devices() -> List[Tuple[str, str, str]]:
    """
    Get all GPU devices with their render devices and driver information.
    
    Returns:
        List[Tuple[str, str, str]]: List of (card_name, render_device, driver) tuples
    """
    devices = []
    drm_dir = "/sys/class/drm"
    
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
            
            # Get render device for this card
            # The mapping is: card0 -> renderD129, card1 -> renderD128
            render_device = None
            for render_entry in entries:
                if render_entry == f"renderD{129 - card_num}":  # card0 -> renderD129, card1 -> renderD128
                    render_device = f"/dev/dri/{render_entry}"
                    break
            
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


def _determine_vaapi_gpu_type(device_path: str) -> str:
    """
    Determine GPU type for VAAPI device by checking driver information.
    
    Args:
        device_path: Path to VAAPI device
        
    Returns:
        str: GPU type ('AMD', 'INTEL', 'NVIDIA', 'ARM', 'VIDEOCORE', or 'UNKNOWN')
    """
    logger.debug(f"Determining GPU type for VAAPI device: {device_path}")
    
    try:
        drm_dir = "/sys/class/drm"
        if not os.path.exists(drm_dir):
            logger.debug(f"DRM directory {drm_dir} does not exist")
            return 'UNKNOWN'
        
        entries = os.listdir(drm_dir)
        logger.debug(f"Found DRM entries: {entries}")
        
        for entry in entries:
            if not entry.startswith("card"):
                continue
            
            driver_path = os.path.join(drm_dir, entry, "device", "driver")
            if os.path.islink(driver_path):
                driver_name = os.path.basename(os.readlink(driver_path))
                logger.debug(f"Driver for {entry}: {driver_name}")
                
                # Intel drivers
                if driver_name == "i915":
                    logger.debug("Detected Intel i915 driver - GPU type: INTEL")
                    return 'INTEL'
                
                # AMD drivers
                elif driver_name in ("amdgpu", "radeon"):
                    logger.debug(f"Detected AMD driver {driver_name} - GPU type: AMD")
                    return 'AMD'
                
                # ARM Mali drivers
                elif driver_name == "panfrost":
                    logger.debug("Detected ARM Mali panfrost driver - GPU type: ARM")
                    return 'ARM'
                
                # VideoCore (Raspberry Pi)
                elif driver_name == "vc4":
                    logger.debug("Detected VideoCore vc4 driver - GPU type: VIDEOCORE")
                    return 'VIDEOCORE'
                
                # Other drivers - try to detect from lspci
                else:
                    logger.debug(f"Unknown driver {driver_name}, attempting lspci detection")
                    gpu_type = _detect_gpu_type_from_lspci()
                    if gpu_type != 'UNKNOWN':
                        return gpu_type
        
        logger.debug("No suitable driver found, defaulting to UNKNOWN")
        return 'UNKNOWN'
    except Exception as e:
        logger.debug(f"Error determining VAAPI GPU type: {e}")
        return 'UNKNOWN'


def _detect_gpu_type_from_lspci() -> str:
    """
    Detect GPU type using lspci as fallback when driver detection fails.
    
    Returns:
        str: GPU type ('AMD', 'INTEL', 'NVIDIA', 'ARM', or 'UNKNOWN')
    """
    try:
        result = subprocess.run(['lspci'], capture_output=True, text=True, timeout=5)
        if result.returncode != 0:
            logger.debug("lspci command failed")
            return 'UNKNOWN'
        
        for line in result.stdout.split('\n'):
            if 'VGA' in line or 'Display' in line:
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
        
        logger.debug("lspci did not identify GPU type")
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
    
    # Log GPU device mapping
    gpu_devices = _get_gpu_devices()
    if gpu_devices:
        logger.debug("GPU device mapping:")
        for card_name, render_device, driver in gpu_devices:
            logger.debug(f"  {card_name} -> {render_device} (driver: {driver})")
    else:
        logger.debug("No GPU devices found")
    
    logger.debug("=== End System Information ===")


def _parse_lspci_gpu_name(gpu_type: str) -> str:
    """
    Parse GPU name from lspci output.
    
    Args:
        gpu_type: Type of GPU ('AMD', 'INTEL')
        
    Returns:
        str: GPU name or fallback description
    """
    try:
        result = subprocess.run(['lspci'], capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            for line in result.stdout.split('\n'):
                if 'VGA' in line and (gpu_type == 'AMD' and 'AMD' in line or gpu_type == 'INTEL' and 'Intel' in line):
                    parts = line.split(':')
                    if len(parts) > 2:
                        return parts[2].strip()
    except Exception as e:
        logger.debug(f"Error parsing lspci for {gpu_type}: {e}")
    
    return f"{gpu_type} GPU"


def get_gpu_name(gpu_type: str, gpu_device: str) -> str:
    """
    Extract GPU model name from system.
    
    Args:
        gpu_type: Type of GPU ('NVIDIA', 'AMD', 'INTEL', 'WSL2')
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
            return "NVIDIA GPU (CUDA)"
            
        elif gpu_type == 'WSL2':
            return "WSL2 GPU (D3D11VA)"
            
        elif gpu_type == 'INTEL' and gpu_device == 'qsv':
            # Try to get Intel GPU info
            gpu_name = _parse_lspci_gpu_name('INTEL')
            return f"{gpu_name} (QSV)"
            
        elif gpu_type in ('AMD', 'INTEL') and gpu_device.startswith('/dev/dri/'):
            # Try to get GPU info from lspci
            gpu_name = _parse_lspci_gpu_name(gpu_type)
            return f"{gpu_name} (VAAPI)"
            
    except Exception as e:
        logger.debug(f"Error getting GPU name for {gpu_type}: {e}")
    
    # Fallback
    return f"{gpu_type} GPU"


def format_gpu_info(gpu_type: str, gpu_device: str, gpu_name: str) -> str:
    """
    Format GPU information for display.
    
    Args:
        gpu_type: Type of GPU
        gpu_device: GPU device path or info
        gpu_name: GPU model name
        
    Returns:
        str: Formatted GPU description
    """
    if gpu_type == 'NVIDIA':
        return f"{gpu_name} (CUDA)"
    elif gpu_type == 'WSL2':
        return f"{gpu_name} (D3D11VA)"
    elif gpu_type == 'INTEL' and gpu_device == 'qsv':
        return f"{gpu_name} (QSV)"
    elif gpu_type in ('AMD', 'INTEL', 'ARM', 'VIDEOCORE') and gpu_device.startswith('/dev/dri/'):
        return f"{gpu_name} (VAAPI - {gpu_device})"
    elif gpu_type == 'UNKNOWN':
        return f"{gpu_name} (Unknown GPU)"
    else:
        return f"{gpu_name} ({gpu_type})"


def detect_all_gpus() -> List[Tuple[str, str, dict]]:
    """
    Detect all available GPU hardware using FFmpeg capability detection.
    
    Checks FFmpeg's available hardware acceleration capabilities and returns
    all working GPUs instead of just the first one.
    
    Returns:
        List[Tuple[str, str, dict]]: List of (gpu_type, gpu_device, gpu_info_dict)
            - gpu_type: 'NVIDIA', 'AMD', 'INTEL', 'WSL2'
            - gpu_device: Device path or info string
            - gpu_info_dict: Dictionary with GPU details (name, vram, etc.)
    """
    logger.debug("=== Starting Multi-GPU Detection ===")
    _log_system_info()
    logger.debug("Checking FFmpeg hardware acceleration capabilities for all GPUs")
    
    detected_gpus = []
    
    # Check NVIDIA CUDA (can have multiple GPUs)
    logger.debug("1. Checking NVIDIA CUDA acceleration...")
    if _is_hwaccel_available('cuda') and _test_hwaccel_functionality('cuda'):
        logger.debug("✓ NVIDIA CUDA hardware acceleration is available and working")
        gpu_name = get_gpu_name('NVIDIA', 'cuda')
        gpu_info = {
            'name': gpu_name,
            'acceleration': 'CUDA',
            'device_path': 'cuda'
        }
        detected_gpus.append(('NVIDIA', 'cuda', gpu_info))
    
    # Check WSL2 D3D11VA (usually single GPU)
    logger.debug("2. Checking WSL2 D3D11VA acceleration...")
    if _is_hwaccel_available('d3d11va') and _test_hwaccel_functionality('d3d11va'):
        logger.debug("✓ WSL2 D3D11VA hardware acceleration is available and working")
        gpu_name = get_gpu_name('WSL2', 'd3d11va')
        gpu_info = {
            'name': gpu_name,
            'acceleration': 'D3D11VA',
            'device_path': 'd3d11va'
        }
        detected_gpus.append(('WSL2', 'd3d11va', gpu_info))
    
    # Check Intel QSV (usually single GPU)
    logger.debug("3. Checking Intel QSV acceleration...")
    if _is_hwaccel_available('qsv') and _test_hwaccel_functionality('qsv'):
        logger.debug("✓ Intel QSV hardware acceleration is available and working")
        gpu_name = get_gpu_name('INTEL', 'qsv')
        gpu_info = {
            'name': gpu_name,
            'acceleration': 'QSV',
            'device_path': 'qsv'
        }
        detected_gpus.append(('INTEL', 'qsv', gpu_info))
    
    # Check VAAPI (can have multiple devices)
    logger.debug("4. Checking VAAPI acceleration...")
    if _is_hwaccel_available('vaapi'):
        logger.debug("VAAPI acceleration is available, searching for devices...")
        vaapi_devices = _find_all_vaapi_devices()
        for device_path in vaapi_devices:
            if _test_hwaccel_functionality('vaapi', device_path):
                gpu_type = _determine_vaapi_gpu_type(device_path)
                gpu_name = get_gpu_name(gpu_type, device_path)
                gpu_info = {
                    'name': gpu_name,
                    'acceleration': 'VAAPI',
                    'device_path': device_path
                }
                detected_gpus.append((gpu_type, device_path, gpu_info))
                logger.debug(f"✓ {gpu_type} VAAPI hardware acceleration is available and working with device {device_path}")
    
    logger.debug(f"=== Multi-GPU Detection Complete: Found {len(detected_gpus)} GPUs ===")
    return detected_gpus


def _find_all_vaapi_devices() -> List[str]:
    """
    Find all available VAAPI devices.
    
    Returns:
        List[str]: List of VAAPI device paths
    """
    devices = []
    gpu_devices = _get_gpu_devices()
    
    if not gpu_devices:
        logger.debug("No GPU devices found for VAAPI")
        return devices
    
    # Add all GPU devices as potential VAAPI devices
    for card_name, render_device, driver in gpu_devices:
        devices.append(render_device)
        logger.debug(f"Found potential VAAPI device: {render_device} (card: {card_name}, driver: {driver})")
    
    return devices

