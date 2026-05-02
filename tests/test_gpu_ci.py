"""
GPU tests designed for CI environments without GPU hardware.
These tests use mocking to verify GPU detection logic without requiring actual hardware.
"""

from unittest.mock import MagicMock, patch

from media_preview_generator.gpu import (
    _check_ffmpeg_version,
    _get_ffmpeg_version,
    format_gpu_info,
)


class TestFFmpegVersionCI:
    """Test FFmpeg version detection in CI."""

    @patch("subprocess.run")
    def test_get_ffmpeg_version_success(self, mock_run):
        """Test successful FFmpeg version detection."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="ffmpeg version 7.1.1-1ubuntu1.2 Copyright (c) 2000-2024 the FFmpeg developers",
        )

        version = _get_ffmpeg_version()
        assert version == (7, 1, 1)

    @patch("subprocess.run")
    def test_get_ffmpeg_version_failure(self, mock_run):
        """Test FFmpeg version detection failure."""
        mock_run.return_value = MagicMock(returncode=1, stderr="Command not found")

        version = _get_ffmpeg_version()
        assert version is None

    @patch("media_preview_generator.gpu.ffmpeg_capabilities._get_ffmpeg_version")
    def test_check_ffmpeg_version_sufficient(self, mock_get_version):
        """Test FFmpeg version check with sufficient version."""
        mock_get_version.return_value = (7, 1, 0)

        result = _check_ffmpeg_version()
        assert result is True

    @patch("media_preview_generator.gpu.ffmpeg_capabilities._get_ffmpeg_version")
    def test_check_ffmpeg_version_insufficient(self, mock_get_version):
        """Test FFmpeg version check with insufficient version."""
        mock_get_version.return_value = (6, 9, 0)

        result = _check_ffmpeg_version()
        assert result is False


class TestGPUFormattingCI:
    """Test GPU information formatting in CI."""

    def test_format_gpu_info_nvidia(self):
        """NVIDIA + CUDA renders as ``<name> (CUDA)``."""
        info = format_gpu_info("NVIDIA", "cuda", "NVIDIA GeForce RTX 3080", "CUDA")
        assert info == "NVIDIA GeForce RTX 3080 (CUDA)"

    def test_format_gpu_info_amd(self):
        """AMD + VAAPI on a DRM render node renders as ``<name> (VAAPI - <device>)``."""
        info = format_gpu_info("AMD", "/dev/dri/renderD128", "AMD Radeon RX 6800 XT", "VAAPI")
        assert info == "AMD Radeon RX 6800 XT (VAAPI - /dev/dri/renderD128)"

    def test_format_gpu_info_intel(self):
        """Intel + VAAPI on a DRM render node renders as ``<name> (VAAPI - <device>)``."""
        info = format_gpu_info("INTEL", "/dev/dri/renderD128", "Intel UHD Graphics 770", "VAAPI")
        assert info == "Intel UHD Graphics 770 (VAAPI - /dev/dri/renderD128)"
