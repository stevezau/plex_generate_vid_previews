"""
GPU tests designed for CI environments without GPU hardware.
These tests use mocking to verify GPU detection logic without requiring actual hardware.
"""

import pytest
from unittest.mock import patch, MagicMock
from plex_generate_previews.gpu_detection import (
    _get_ffmpeg_version, 
    _check_ffmpeg_version,
    format_gpu_info
)


class TestFFmpegVersionCI:
    """Test FFmpeg version detection in CI."""
    
    @patch('subprocess.run')
    def test_get_ffmpeg_version_success(self, mock_run):
        """Test successful FFmpeg version detection."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="ffmpeg version 7.1.1-1ubuntu1.2 Copyright (c) 2000-2024 the FFmpeg developers"
        )
        
        version = _get_ffmpeg_version()
        assert version == (7, 1, 1)
    
    @patch('subprocess.run')
    def test_get_ffmpeg_version_failure(self, mock_run):
        """Test FFmpeg version detection failure."""
        mock_run.return_value = MagicMock(returncode=1, stderr="Command not found")
        
        version = _get_ffmpeg_version()
        assert version is None
    
    @patch('plex_generate_previews.gpu_detection._get_ffmpeg_version')
    def test_check_ffmpeg_version_sufficient(self, mock_get_version):
        """Test FFmpeg version check with sufficient version."""
        mock_get_version.return_value = (7, 1, 0)
        
        result = _check_ffmpeg_version()
        assert result is True
    
    @patch('plex_generate_previews.gpu_detection._get_ffmpeg_version')
    def test_check_ffmpeg_version_insufficient(self, mock_get_version):
        """Test FFmpeg version check with insufficient version."""
        mock_get_version.return_value = (6, 9, 0)
        
        result = _check_ffmpeg_version()
        assert result is False


class TestGPUFormattingCI:
    """Test GPU information formatting in CI."""
    
    def test_format_gpu_info_nvidia(self):
        """Test NVIDIA GPU info formatting."""
        info = format_gpu_info('cuda', 0, 'NVIDIA GeForce RTX 3080')
        assert 'NVIDIA' in info
        assert 'RTX 3080' in info
        assert 'cuda' in info.lower()
    
    def test_format_gpu_info_amd(self):
        """Test AMD GPU info formatting."""
        info = format_gpu_info('vaapi', '/dev/dri/renderD128', 'AMD Radeon RX 6800 XT')
        assert 'AMD' in info
        assert 'RX 6800 XT' in info
        assert 'vaapi' in info.lower()
    
    def test_format_gpu_info_intel(self):
        """Test Intel GPU info formatting."""
        info = format_gpu_info('qsv', 0, 'Intel UHD Graphics 770')
        assert 'Intel' in info
        assert 'UHD Graphics 770' in info
        assert 'qsv' in info.lower()
