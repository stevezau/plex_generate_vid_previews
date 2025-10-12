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
    _get_gpu_devices,
    _determine_vaapi_gpu_type,
    get_gpu_name,
    _get_ffmpeg_hwaccels
)


class TestDetectAllGPUs:
    """Test comprehensive GPU detection."""
    
    @patch('plex_generate_previews.gpu_detection._test_hwaccel_functionality')
    @patch('plex_generate_previews.gpu_detection._is_hwaccel_available')
    @patch('plex_generate_previews.gpu_detection.get_gpu_name')
    def test_detect_all_gpus_nvidia(self, mock_name, mock_available, mock_test):
        """Test NVIDIA CUDA GPU detection."""
        mock_available.side_effect = lambda x: x == 'cuda'
        mock_test.return_value = True
        mock_name.return_value = 'NVIDIA GeForce RTX 3080'
        
        gpus = detect_all_gpus()
        
        # Should detect NVIDIA GPU
        nvidia_gpus = [g for g in gpus if g[0] == 'NVIDIA']
        assert len(nvidia_gpus) > 0
        assert nvidia_gpus[0][1] == 'cuda'
        assert 'RTX 3080' in nvidia_gpus[0][2]['name']
    
    @patch('plex_generate_previews.gpu_detection._find_all_vaapi_devices')
    @patch('plex_generate_previews.gpu_detection._test_hwaccel_functionality')
    @patch('plex_generate_previews.gpu_detection._is_hwaccel_available')
    @patch('plex_generate_previews.gpu_detection._determine_vaapi_gpu_type')
    @patch('plex_generate_previews.gpu_detection.get_gpu_name')
    def test_detect_all_gpus_amd(self, mock_name, mock_type, mock_available, mock_test, mock_find):
        """Test AMD VAAPI GPU detection."""
        mock_available.side_effect = lambda x: x == 'vaapi'
        mock_find.return_value = ['/dev/dri/renderD128']
        mock_test.return_value = True
        mock_type.return_value = 'AMD'
        mock_name.return_value = 'AMD Radeon RX 6800 XT'
        
        gpus = detect_all_gpus()
        
        # Should detect AMD GPU
        amd_gpus = [g for g in gpus if g[0] == 'AMD']
        assert len(amd_gpus) > 0
        assert '/dev/dri/renderD128' in amd_gpus[0][1]
    
    @patch('plex_generate_previews.gpu_detection._test_hwaccel_functionality')
    @patch('plex_generate_previews.gpu_detection._is_hwaccel_available')
    @patch('plex_generate_previews.gpu_detection.get_gpu_name')
    def test_detect_all_gpus_intel(self, mock_name, mock_available, mock_test):
        """Test Intel QSV GPU detection."""
        mock_available.side_effect = lambda x: x == 'qsv'
        mock_test.return_value = True
        mock_name.return_value = 'Intel UHD Graphics 770'
        
        gpus = detect_all_gpus()
        
        # Should detect Intel GPU
        intel_gpus = [g for g in gpus if g[0] == 'INTEL']
        assert len(intel_gpus) > 0
        assert intel_gpus[0][1] == 'qsv'
    
    @patch('plex_generate_previews.gpu_detection._test_hwaccel_functionality')
    @patch('plex_generate_previews.gpu_detection._is_hwaccel_available')
    def test_detect_all_gpus_none(self, mock_available, mock_test):
        """Test when no GPUs are detected."""
        mock_available.return_value = False
        mock_test.return_value = False
        
        gpus = detect_all_gpus()
        
        # Should return empty list
        assert gpus == []


class TestHwaccelAvailability:
    """Test hardware acceleration availability checking."""
    
    @patch('plex_generate_previews.gpu_detection._get_ffmpeg_hwaccels')
    def test_is_hwaccel_available_cuda(self, mock_hwaccels):
        """Test CUDA availability check."""
        mock_hwaccels.return_value = ['cuda', 'vaapi', 'qsv']
        
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
    def test_test_hwaccel_functionality_vaapi_success(self, mock_run):
        """Test VAAPI functionality test success."""
        mock_run.return_value = MagicMock(returncode=0)
        
        result = _test_hwaccel_functionality('vaapi', '/dev/dri/renderD128')
        assert result is True
    
    @patch('subprocess.run')
    def test_test_hwaccel_functionality_timeout(self, mock_run):
        """Test timeout handling."""
        from subprocess import TimeoutExpired
        mock_run.side_effect = TimeoutExpired('ffmpeg', 10)
        
        result = _test_hwaccel_functionality('cuda')
        assert result is False


class TestGetGPUDevices:
    """Test GPU device enumeration."""
    
    @patch('os.path.islink')
    @patch('os.readlink')
    @patch('os.listdir')
    @patch('os.path.exists')
    def test_get_gpu_devices(self, mock_exists, mock_listdir, mock_readlink, mock_islink):
        """Test enumerating GPU devices from /sys/class/drm."""
        mock_exists.return_value = True
        mock_listdir.return_value = ['card0', 'card0-HDMI-A-1', 'renderD129', 'card1', 'renderD128']
        mock_islink.return_value = True
        mock_readlink.return_value = '/path/to/amdgpu'
        
        devices = _get_gpu_devices()
        
        # Should find GPU devices
        assert len(devices) > 0
    
    @patch('os.path.exists')
    def test_get_gpu_devices_no_drm(self, mock_exists):
        """Test when /sys/class/drm doesn't exist."""
        mock_exists.return_value = False
        
        devices = _get_gpu_devices()
        
        # Should return empty list
        assert devices == []


class TestDetermineVAAPIGPUType:
    """Test VAAPI GPU type determination."""
    
    @patch('os.readlink')
    @patch('os.path.islink')
    @patch('os.listdir')
    @patch('os.path.exists')
    def test_determine_vaapi_gpu_type_intel(self, mock_exists, mock_listdir, mock_islink, mock_readlink):
        """Test detecting Intel GPU via i915 driver."""
        mock_exists.return_value = True
        mock_listdir.return_value = ['card0']
        mock_islink.return_value = True
        mock_readlink.return_value = '/path/to/i915'
        
        gpu_type = _determine_vaapi_gpu_type('/dev/dri/renderD129')
        
        assert gpu_type == 'INTEL'
    
    @patch('os.readlink')
    @patch('os.path.islink')
    @patch('os.listdir')
    @patch('os.path.exists')
    def test_determine_vaapi_gpu_type_amd(self, mock_exists, mock_listdir, mock_islink, mock_readlink):
        """Test detecting AMD GPU via amdgpu driver."""
        mock_exists.return_value = True
        mock_listdir.return_value = ['card0']
        mock_islink.return_value = True
        mock_readlink.return_value = '/path/to/amdgpu'
        
        gpu_type = _determine_vaapi_gpu_type('/dev/dri/renderD128')
        
        assert gpu_type == 'AMD'
    
    @patch('os.readlink')
    @patch('os.path.islink')
    @patch('os.listdir')
    @patch('os.path.exists')
    def test_determine_vaapi_gpu_type_radeon(self, mock_exists, mock_listdir, mock_islink, mock_readlink):
        """Test detecting AMD GPU via radeon driver."""
        mock_exists.return_value = True
        mock_listdir.return_value = ['card0']
        mock_islink.return_value = True
        mock_readlink.return_value = '/path/to/radeon'
        
        gpu_type = _determine_vaapi_gpu_type('/dev/dri/renderD128')
        
        assert gpu_type == 'AMD'
    
    @patch('os.path.exists')
    def test_determine_vaapi_gpu_type_unknown(self, mock_exists):
        """Test unknown GPU type."""
        mock_exists.return_value = False
        
        gpu_type = _determine_vaapi_gpu_type('/dev/dri/renderD128')
        
        assert gpu_type == 'UNKNOWN'


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
        assert 'CUDA' in name
    
    def test_get_gpu_name_wsl2(self):
        """Test WSL2 GPU name."""
        name = get_gpu_name('WSL2', 'd3d11va')
        
        assert 'WSL2' in name
        assert 'D3D11VA' in name
    
    @patch('subprocess.run')
    def test_get_gpu_name_intel_qsv(self, mock_run):
        """Test getting Intel GPU name for QSV."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout='00:02.0 VGA compatible controller: Intel Corporation UHD Graphics 770\n'
        )
        
        name = get_gpu_name('INTEL', 'qsv')
        
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
qsv
d3d11va
'''
        )
        
        hwaccels = _get_ffmpeg_hwaccels()
        
        assert 'cuda' in hwaccels
        assert 'vaapi' in hwaccels
        assert 'qsv' in hwaccels
        assert 'Hardware acceleration methods:' not in hwaccels
    
    @patch('subprocess.run')
    def test_get_ffmpeg_hwaccels_failure(self, mock_run):
        """Test handling FFmpeg hwaccels failure."""
        mock_run.return_value = MagicMock(returncode=1)
        
        hwaccels = _get_ffmpeg_hwaccels()
        
        assert hwaccels == []

