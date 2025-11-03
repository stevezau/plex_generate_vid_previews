"""
Extended GPU detection tests for CI environments.

Comprehensive tests for multi-GPU detection, hwaccel testing,
and device enumeration using extensive mocking.
"""

import pytest
from unittest.mock import patch, MagicMock
from plex_generate_previews.gpu_detection import (
    detect_all_gpus,
    _is_hwaccel_available,
    _test_hwaccel_functionality,
    _test_acceleration_method,
    _get_gpu_devices,
    get_gpu_name,
    _get_ffmpeg_hwaccels
)


class TestDetectAllGPUs:
    """Test comprehensive GPU detection."""
    
    @patch('plex_generate_previews.gpu_detection._test_acceleration_method')
    @patch('plex_generate_previews.gpu_detection._get_gpu_devices')
    @patch('plex_generate_previews.gpu_detection.get_gpu_name')
    def test_detect_all_gpus_nvidia(self, mock_name, mock_devices, mock_test):
        """Test NVIDIA CUDA GPU detection."""
        mock_devices.return_value = [('card0', '/dev/dri/renderD128', 'nvidia')]
        mock_test.return_value = True
        mock_name.return_value = 'NVIDIA GeForce RTX 3080'
        
        gpus = detect_all_gpus()
        
        # Should detect NVIDIA GPU via CUDA
        nvidia_gpus = [g for g in gpus if g[0] == 'NVIDIA']
        assert len(nvidia_gpus) > 0
        assert nvidia_gpus[0][1] == 'cuda'
        assert 'RTX 3080' in nvidia_gpus[0][2]['name']
        assert nvidia_gpus[0][2]['acceleration'] == 'CUDA'
    
    @patch('plex_generate_previews.gpu_detection._test_acceleration_method')
    @patch('plex_generate_previews.gpu_detection._get_gpu_devices')
    @patch('plex_generate_previews.gpu_detection.get_gpu_name')
    def test_detect_all_gpus_amd(self, mock_name, mock_devices, mock_test):
        """Test AMD VAAPI GPU detection."""
        mock_devices.return_value = [('card0', '/dev/dri/renderD128', 'amdgpu')]
        mock_test.return_value = True
        mock_name.return_value = 'AMD Radeon RX 6800 XT'
        
        gpus = detect_all_gpus()
        
        # Should detect AMD GPU via VAAPI
        amd_gpus = [g for g in gpus if g[0] == 'AMD']
        assert len(amd_gpus) > 0
        assert '/dev/dri/renderD128' in amd_gpus[0][1]
        assert amd_gpus[0][2]['acceleration'] == 'VAAPI'
    
    @patch('plex_generate_previews.gpu_detection._test_acceleration_method')
    @patch('plex_generate_previews.gpu_detection._get_gpu_devices')
    @patch('plex_generate_previews.gpu_detection.get_gpu_name')
    def test_detect_all_gpus_intel(self, mock_name, mock_devices, mock_test):
        """Test Intel VAAPI GPU detection."""
        mock_devices.return_value = [('card0', '/dev/dri/renderD128', 'i915')]
        mock_test.return_value = True
        mock_name.return_value = 'Intel UHD Graphics 770'
        
        gpus = detect_all_gpus()
        
        # Should detect Intel GPU via VAAPI
        intel_gpus = [g for g in gpus if g[0] == 'INTEL']
        assert len(intel_gpus) > 0
        assert intel_gpus[0][1] == '/dev/dri/renderD128'
        assert intel_gpus[0][2]['acceleration'] == 'VAAPI'
    
    @patch('plex_generate_previews.gpu_detection._get_gpu_devices')
    def test_detect_all_gpus_none(self, mock_devices):
        """Test when no GPUs are detected."""
        mock_devices.return_value = []
        # Ensure platform-specific paths (macOS) are not taken in this test
        with patch('plex_generate_previews.gpu_detection.is_macos', return_value=False), \
             patch('plex_generate_previews.gpu_detection.is_windows', return_value=False), \
             patch('plex_generate_previews.gpu_detection._get_ffmpeg_hwaccels', return_value=[]):
            gpus = detect_all_gpus()
        
        # Should return empty list
        assert gpus == []


class TestHwaccelAvailability:
    """Test hardware acceleration availability checking."""
    
    @patch('plex_generate_previews.gpu_detection._get_ffmpeg_hwaccels')
    def test_is_hwaccel_available_cuda(self, mock_hwaccels):
        """Test CUDA availability check."""
        mock_hwaccels.return_value = ['cuda', 'vaapi']
        
        assert _is_hwaccel_available('cuda') is True
        assert _is_hwaccel_available('d3d11va') is False
    
    @patch('plex_generate_previews.gpu_detection._get_ffmpeg_hwaccels')
    def test_is_hwaccel_available_none(self, mock_hwaccels):
        """Test when no hwaccels are available."""
        mock_hwaccels.return_value = []
        
        assert _is_hwaccel_available('cuda') is False
        assert _is_hwaccel_available('vaapi') is False


class TestHwaccelFunctionality:
    """Test hardware acceleration functionality testing."""
    
    @patch('subprocess.run')
    def test_test_hwaccel_functionality_cuda_success(self, mock_run):
        """Test CUDA functionality test success."""
        mock_run.return_value = MagicMock(returncode=0)
        
        result = _test_hwaccel_functionality('cuda')
        assert result is True
    
    @patch('subprocess.run')
    def test_test_hwaccel_functionality_cuda_failure(self, mock_run):
        """Test CUDA functionality test failure."""
        mock_run.return_value = MagicMock(returncode=1, stderr=b'Error')
        
        result = _test_hwaccel_functionality('cuda')
        assert result is False
    
    @patch('subprocess.run')
    def test_test_hwaccel_functionality_cuda_devnull_error(self, mock_run):
        """Test CUDA failure with /dev/null error (container without nvidia runtime)."""
        mock_run.return_value = MagicMock(
            returncode=255,
            stderr=b'Error opening output file /dev/null.\nError opening output files: Operation not permitted'
        )
        
        result = _test_hwaccel_functionality('cuda')
        assert result is False
    
    @patch('subprocess.run')
    def test_test_hwaccel_functionality_cuda_init_error(self, mock_run):
        """Test CUDA initialization failure."""
        mock_run.return_value = MagicMock(
            returncode=255,
            stderr=b'cuda_check_ret failed\nCUDA initialization failed'
        )
        
        result = _test_hwaccel_functionality('cuda')
        assert result is False
    
    @patch('os.access')
    @patch('os.path.exists')
    @patch('subprocess.run')
    def test_test_hwaccel_functionality_vaapi_success(self, mock_run, mock_exists, mock_access):
        """Test VAAPI functionality test success."""
        mock_exists.return_value = True  # Device exists
        mock_access.return_value = True  # Device is accessible
        mock_run.return_value = MagicMock(returncode=0)
        
        result = _test_hwaccel_functionality('vaapi', '/dev/dri/renderD128')
        assert result is True
    
    @patch('os.access')
    @patch('os.path.exists')
    def test_test_hwaccel_functionality_vaapi_device_not_found(self, mock_exists, mock_access):
        """Test VAAPI when device doesn't exist (should fail silently)."""
        mock_exists.return_value = False  # Device doesn't exist
        
        result = _test_hwaccel_functionality('vaapi', '/dev/dri/renderD128')
        assert result is False
    
    @patch('os.getgroups')
    @patch('os.getuid')
    @patch('os.access')
    @patch('os.path.exists')
    def test_test_hwaccel_functionality_vaapi_permission_denied(self, mock_exists, mock_access, mock_uid, mock_groups):
        """Test VAAPI when device exists but permission denied."""
        mock_exists.return_value = True   # Device exists
        mock_access.return_value = False  # But not accessible
        mock_uid.return_value = 1000
        mock_groups.return_value = [1000]
        
        result = _test_hwaccel_functionality('vaapi', '/dev/dri/renderD128')
        assert result is False
    
    @patch('os.access')
    @patch('os.path.exists')
    @patch('subprocess.run')
    def test_test_hwaccel_functionality_vaapi_stderr_error(self, mock_run, mock_exists, mock_access):
        """Test VAAPI failure with stderr containing permission denied."""
        mock_exists.return_value = True
        mock_access.return_value = True
        mock_run.return_value = MagicMock(
            returncode=1,
            stderr=b'Failed to initialize VAAPI connection\nvaapi: permission denied'
        )
        
        result = _test_hwaccel_functionality('vaapi', '/dev/dri/renderD128')
        assert result is False
    
    @patch('subprocess.run')
    def test_test_hwaccel_functionality_timeout(self, mock_run):
        """Test timeout handling."""
        from subprocess import TimeoutExpired
        mock_run.side_effect = TimeoutExpired('ffmpeg', 10)
        
        result = _test_hwaccel_functionality('cuda')
        assert result is False
    
    @patch('subprocess.run')
    def test_test_hwaccel_functionality_d3d11va_error(self, mock_run):
        """Test D3D11VA error handling (generic hwaccel path)."""
        mock_run.return_value = MagicMock(
            returncode=1,
            stderr=b'Failed to initialize D3D11VA\nError creating device'
        )
        
        result = _test_hwaccel_functionality('d3d11va')
        assert result is False
    
    @patch('subprocess.run')
    def test_test_hwaccel_functionality_sigpipe(self, mock_run):
        """Test SIGPIPE handling (exit code 141 is acceptable)."""
        mock_run.return_value = MagicMock(returncode=141)
        
        result = _test_hwaccel_functionality('cuda')
        assert result is True
    
    @patch('subprocess.run')
    def test_test_hwaccel_functionality_exception(self, mock_run):
        """Test exception handling during test."""
        mock_run.side_effect = RuntimeError("Unexpected error")
        
        result = _test_hwaccel_functionality('cuda')
        assert result is False
    
    @patch('subprocess.run')
    def test_test_hwaccel_functionality_empty_stderr(self, mock_run):
        """Test failure with no stderr output."""
        mock_run.return_value = MagicMock(returncode=1, stderr=b'')
        
        result = _test_hwaccel_functionality('cuda')
        assert result is False
    
    @patch('subprocess.run')
    def test_test_hwaccel_functionality_stderr_with_empty_lines(self, mock_run):
        """Test stderr parsing with empty lines."""
        mock_run.return_value = MagicMock(
            returncode=1,
            stderr=b'\n\nError occurred\n\n'
        )
        
        result = _test_hwaccel_functionality('cuda')
        assert result is False
    
    @patch('os.access')
    @patch('os.path.exists')
    @patch('subprocess.run')
    def test_test_hwaccel_functionality_vaapi_generic_error(self, mock_run, mock_exists, mock_access):
        """Test VAAPI failure without permission denied in stderr."""
        mock_exists.return_value = True
        mock_access.return_value = True
        mock_run.return_value = MagicMock(
            returncode=1,
            stderr=b'Failed to initialize VAAPI\nDevice error occurred'
        )
        
        result = _test_hwaccel_functionality('vaapi', '/dev/dri/renderD128')
        assert result is False


class TestGetGPUDevices:
    """Test GPU device enumeration."""
    
    @patch('plex_generate_previews.gpu_detection.os.path.islink')
    @patch('plex_generate_previews.gpu_detection.os.readlink')
    @patch('plex_generate_previews.gpu_detection.os.listdir')
    @patch('plex_generate_previews.gpu_detection.os.path.exists')
    @patch('plex_generate_previews.gpu_detection.os.path.realpath')
    def test_get_gpu_devices(self, mock_realpath, mock_exists, mock_listdir, mock_readlink, mock_islink):
        """Test enumerating GPU devices from /sys/class/drm."""
        mock_exists.return_value = True
        mock_listdir.return_value = ['card0', 'card0-HDMI-A-1', 'renderD128', 'card1', 'renderD129']
        mock_islink.return_value = True
        mock_readlink.return_value = 'amdgpu'
        mock_realpath.side_effect = lambda x: '/sys/devices/pci0000:00/0000:00:01.0'
        # Force Linux code path for this test
        with patch('plex_generate_previews.gpu_detection.platform.system', return_value='Linux'):
            devices = _get_gpu_devices()
        
        # Should find GPU devices
        assert len(devices) > 0
    
    @patch('plex_generate_previews.gpu_detection.os.path.exists')
    def test_get_gpu_devices_no_drm(self, mock_exists):
        """Test when /sys/class/drm doesn't exist."""
        mock_exists.return_value = False
        
        devices = _get_gpu_devices()
        
        # Should return empty list
        assert devices == []


class TestGetGPUName:
    """Test GPU name retrieval."""
    
    @patch('subprocess.run')
    def test_get_gpu_name_nvidia(self, mock_run):
        """Test getting NVIDIA GPU name from nvidia-smi."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout='NVIDIA GeForce RTX 3080\n'
        )
        
        name = get_gpu_name('NVIDIA', 'cuda')
        
        assert 'RTX 3080' in name
    
    @patch('subprocess.run')
    def test_get_gpu_name_nvidia_failure(self, mock_run):
        """Test fallback when nvidia-smi fails."""
        mock_run.return_value = MagicMock(returncode=1)
        
        name = get_gpu_name('NVIDIA', 'cuda')
        
        assert 'NVIDIA' in name
        assert 'GPU' in name
    
    def test_get_gpu_name_windows(self):
        """Test Windows GPU name."""
        name = get_gpu_name('WINDOWS_GPU', 'd3d11va')
        
        assert 'Windows' in name
        assert 'GPU' in name
    
    @patch('subprocess.run')
    def test_get_gpu_name_intel_vaapi(self, mock_run):
        """Test getting Intel GPU name for VAAPI."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout='00:02.0 VGA compatible controller: Intel Corporation UHD Graphics 770\n'
        )
        
        name = get_gpu_name('INTEL', '/dev/dri/renderD128')
        
        assert 'Intel' in name or 'UHD' in name
    
    @patch('subprocess.run')
    def test_get_gpu_name_amd_vaapi(self, mock_run):
        """Test getting AMD GPU name from lspci."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout='01:00.0 VGA compatible controller: Advanced Micro Devices [AMD/ATI] Navi 21 [Radeon RX 6800/6800 XT]\n'
        )
        
        name = get_gpu_name('AMD', '/dev/dri/renderD128')
        
        assert 'AMD' in name or 'Radeon' in name


class TestGetFFmpegHwaccels:
    """Test FFmpeg hwaccel enumeration."""
    
    @patch('subprocess.run')
    def test_get_ffmpeg_hwaccels(self, mock_run):
        """Test getting list of available hwaccels."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout='''Hardware acceleration methods:
cuda
vaapi
d3d11va
'''
        )
        
        hwaccels = _get_ffmpeg_hwaccels()
        
        assert 'cuda' in hwaccels
        assert 'vaapi' in hwaccels
        assert 'Hardware acceleration methods:' not in hwaccels
    
    @patch('subprocess.run')
    def test_get_ffmpeg_hwaccels_failure(self, mock_run):
        """Test handling FFmpeg hwaccels failure."""
        mock_run.return_value = MagicMock(returncode=1)
        
        hwaccels = _get_ffmpeg_hwaccels()
        
        assert hwaccels == []


class TestFFmpegVersion:
    """Test FFmpeg version detection."""
    
    @patch('subprocess.run')
    def test_get_ffmpeg_version_parse_error(self, mock_run):
        """Test FFmpeg version parsing with invalid output."""
        from plex_generate_previews.gpu_detection import _get_ffmpeg_version
        
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout='Invalid version output'
        )
        
        result = _get_ffmpeg_version()
        assert result is None
    
    @patch('subprocess.run')
    def test_get_ffmpeg_version_error(self, mock_run):
        """Test FFmpeg version with subprocess error."""
        from plex_generate_previews.gpu_detection import _get_ffmpeg_version
        
        mock_run.side_effect = Exception("Command failed")
        
        result = _get_ffmpeg_version()
        assert result is None
    
    @patch('plex_generate_previews.gpu_detection._get_ffmpeg_version')
    def test_check_ffmpeg_version_none(self, mock_get_version):
        """Test check FFmpeg version when version is None."""
        from plex_generate_previews.gpu_detection import _check_ffmpeg_version
        
        mock_get_version.return_value = None
        
        result = _check_ffmpeg_version()
        # Should return True to not fail if version can't be determined
        assert result is True




class TestAppleGPU:
    """Test Apple GPU detection functions."""
    
    @patch('subprocess.run')
    def test_get_apple_gpu_name_success(self, mock_run):
        """Test getting Apple GPU name successfully."""
        from plex_generate_previews.gpu_detection import _get_apple_gpu_name
        
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout='Graphics/Displays:\n\n  Chipset Model: Apple M1 Pro\n  Type: GPU'
        )
        
        result = _get_apple_gpu_name()
        assert result == 'Apple M1 Pro'
    
    @patch('subprocess.run')
    def test_get_apple_gpu_name_error(self, mock_run):
        """Test getting Apple GPU name with error."""
        from plex_generate_previews.gpu_detection import _get_apple_gpu_name
        
        mock_run.side_effect = Exception("Command failed")
        
        result = _get_apple_gpu_name()
        assert 'Apple' in result  # Should return fallback
    
    @patch('platform.machine')
    @patch('subprocess.run')
    def test_get_apple_gpu_name_arm64_fallback(self, mock_run, mock_machine):
        """Test getting Apple GPU name with ARM64 fallback."""
        from plex_generate_previews.gpu_detection import _get_apple_gpu_name
        
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout='No chipset info'
        )
        mock_machine.return_value = 'arm64'
        
        result = _get_apple_gpu_name()
        assert result == 'Apple Silicon GPU'


class TestLspciGPUDetection:
    """Test lspci GPU detection."""
    
    @patch('subprocess.run')
    def test_detect_gpu_type_from_lspci_failure(self, mock_run):
        """Test lspci GPU detection with command failure."""
        from plex_generate_previews.gpu_detection import _detect_gpu_type_from_lspci
        
        mock_run.return_value = MagicMock(returncode=1)
        
        result = _detect_gpu_type_from_lspci()
        assert result == 'UNKNOWN'
    
    @patch('subprocess.run')
    def test_detect_gpu_type_from_lspci_amd(self, mock_run):
        """Test lspci detecting AMD GPU."""
        from plex_generate_previews.gpu_detection import _detect_gpu_type_from_lspci
        
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout='00:02.0 VGA compatible controller: Advanced Micro Devices [AMD/ATI] Radeon RX 6800 XT'
        )
        
        result = _detect_gpu_type_from_lspci()
        assert result == 'AMD'
    
    @patch('subprocess.run')
    def test_detect_gpu_type_from_lspci_intel(self, mock_run):
        """Test lspci detecting Intel GPU."""
        from plex_generate_previews.gpu_detection import _detect_gpu_type_from_lspci
        
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout='00:02.0 Display controller: Intel Corporation UHD Graphics 630'
        )
        
        result = _detect_gpu_type_from_lspci()
        assert result == 'INTEL'
    
    @patch('subprocess.run')
    def test_detect_gpu_type_from_lspci_nvidia(self, mock_run):
        """Test lspci detecting NVIDIA GPU."""
        from plex_generate_previews.gpu_detection import _detect_gpu_type_from_lspci
        
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout='01:00.0 VGA compatible controller: NVIDIA Corporation GeForce RTX 3080'
        )
        
        result = _detect_gpu_type_from_lspci()
        assert result == 'NVIDIA'
    
    @patch('subprocess.run')
    def test_detect_gpu_type_from_lspci_arm(self, mock_run):
        """Test lspci detecting ARM GPU."""
        from plex_generate_previews.gpu_detection import _detect_gpu_type_from_lspci
        
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout='00:02.0 Display controller: ARM Mali GPU'
        )
        
        result = _detect_gpu_type_from_lspci()
        assert result == 'ARM'
    
    @patch('subprocess.run')
    def test_detect_gpu_type_from_lspci_not_found(self, mock_run):
        """Test lspci when lspci is not installed."""
        from plex_generate_previews.gpu_detection import _detect_gpu_type_from_lspci
        
        mock_run.side_effect = FileNotFoundError()
        
        result = _detect_gpu_type_from_lspci()
        assert result == 'UNKNOWN'
    
    @patch('subprocess.run')
    def test_detect_gpu_type_from_lspci_exception(self, mock_run):
        """Test lspci with exception."""
        from plex_generate_previews.gpu_detection import _detect_gpu_type_from_lspci
        
        mock_run.side_effect = Exception("Unexpected error")
        
        result = _detect_gpu_type_from_lspci()
        assert result == 'UNKNOWN'
    
    @patch('subprocess.run')
    def test_detect_gpu_type_from_lspci_no_match(self, mock_run):
        """Test lspci with no GPU match."""
        from plex_generate_previews.gpu_detection import _detect_gpu_type_from_lspci
        
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout='00:1f.3 Audio device: Intel Corporation'
        )
        
        result = _detect_gpu_type_from_lspci()
        assert result == 'UNKNOWN'


class TestLogSystemInfo:
    """Test system info logging."""
    
    @patch('platform.system')
    @patch('platform.release')
    @patch('plex_generate_previews.gpu_detection.logger')
    def test_log_system_info(self, mock_logger, mock_release, mock_system):
        """Test logging system information."""
        from plex_generate_previews.gpu_detection import _log_system_info
        
        mock_system.return_value = 'Linux'
        mock_release.return_value = '5.15.0'
        
        _log_system_info()
        
        # Should log system information
        assert mock_logger.debug.called


class TestParseLspciGPUName:
    """Test parsing lspci GPU names."""
    
    @patch('subprocess.run')
    def test_parse_lspci_gpu_name_nvidia(self, mock_run):
        """Test parsing NVIDIA GPU name."""
        from plex_generate_previews.gpu_detection import _parse_lspci_gpu_name
        
        # Mock lspci failure - should return fallback
        mock_run.return_value = MagicMock(returncode=1)
        
        result = _parse_lspci_gpu_name('NVIDIA')
        assert result == 'NVIDIA GPU'
    
    @patch('subprocess.run')
    def test_parse_lspci_gpu_name_amd(self, mock_run):
        """Test parsing AMD GPU name."""
        from plex_generate_previews.gpu_detection import _parse_lspci_gpu_name
        
        # Mock lspci failure - should return fallback
        mock_run.return_value = MagicMock(returncode=1)
        
        result = _parse_lspci_gpu_name('AMD')
        assert result == 'AMD GPU'
    
    @patch('subprocess.run')
    def test_parse_lspci_gpu_name_intel(self, mock_run):
        """Test parsing Intel GPU name."""
        from plex_generate_previews.gpu_detection import _parse_lspci_gpu_name
        
        # Mock lspci failure - should return fallback
        mock_run.return_value = MagicMock(returncode=1)
        
        result = _parse_lspci_gpu_name('INTEL')
        assert result == 'INTEL GPU'


class TestAccelerationMethodTesting:
    """Test acceleration method testing."""
    
    @patch('plex_generate_previews.gpu_detection._test_hwaccel_functionality')
    def test_test_acceleration_method_cuda_failure(self, mock_test):
        """Test CUDA acceleration method failure."""
        from plex_generate_previews.gpu_detection import _test_acceleration_method
        
        mock_test.return_value = False
        
        result = _test_acceleration_method('nvidia', 'CUDA', None)
        assert result is False
    
    @patch('plex_generate_previews.gpu_detection._test_hwaccel_functionality')
    def test_test_acceleration_method_vaapi_failure(self, mock_test):
        """Test VAAPI acceleration method failure."""
        from plex_generate_previews.gpu_detection import _test_acceleration_method
        
        mock_test.return_value = False
        
        result = _test_acceleration_method('amd', 'VAAPI', '/dev/dri/renderD128')
        assert result is False




class TestDetectAllGPUsEdgeCases:
    """Test edge cases in detect_all_gpus."""
    
    @patch('plex_generate_previews.gpu_detection.is_macos')
    @patch('plex_generate_previews.gpu_detection.is_windows')
    @patch('plex_generate_previews.gpu_detection.platform.system')
    @patch('plex_generate_previews.gpu_detection._test_acceleration_method')
    @patch('plex_generate_previews.gpu_detection.get_gpu_name')
    def test_detect_all_gpus_macos_videotoolbox(self, mock_gpu_name, mock_test, mock_platform_system, mock_is_windows, mock_is_macos):
        """Test macOS VideoToolbox detection."""
        mock_is_macos.return_value = True
        mock_is_windows.return_value = False
        mock_platform_system.return_value = 'Darwin'
        mock_test.return_value = True
        mock_gpu_name.return_value = 'Apple M1 Max'
        
        gpus = detect_all_gpus()
        
        # Should detect Apple GPU
        apple_gpus = [g for g in gpus if g[0] == 'APPLE']
        assert len(apple_gpus) > 0
        assert 'M1 Max' in apple_gpus[0][2]['name']
    
    @patch('plex_generate_previews.gpu_detection.is_macos')
    @patch('plex_generate_previews.gpu_detection.is_windows')
    @patch('plex_generate_previews.gpu_detection.platform.system')
    @patch('plex_generate_previews.gpu_detection._get_gpu_devices')
    @patch('plex_generate_previews.gpu_detection._test_acceleration_method')
    def test_detect_all_gpus_nvidia_nvenc(self, mock_test, mock_devices, mock_platform_system, mock_is_windows, mock_is_macos):
        """Test NVIDIA NVENC detection."""
        mock_is_macos.return_value = False
        mock_is_windows.return_value = False
        mock_platform_system.return_value = 'Linux'
        mock_devices.return_value = [('card0', '/dev/dri/renderD128', 'nvidia')]
        
        def test_side_effect(vendor, accel, device):
            if accel == 'CUDA':
                return True
            elif accel == 'NVENC':
                return True
            return False
        
        mock_test.side_effect = test_side_effect
        
        gpus = detect_all_gpus()
        
        # Should detect NVIDIA with both CUDA and NVENC
        nvidia_gpus = [g for g in gpus if g[0] == 'NVIDIA']
        assert len(nvidia_gpus) >= 1

