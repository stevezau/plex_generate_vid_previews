"""
Extended GPU detection tests for CI environments.

Comprehensive tests for multi-GPU detection, hwaccel testing,
and device enumeration using extensive mocking.
"""

import json
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from plex_generate_previews.gpu_detection import (
    _build_gpu_error_detail,
    _detect_windows_gpus,
    _format_driver_label,
    _get_ffmpeg_hwaccels,
    _get_gpu_devices,
    _is_hwaccel_available,
    _probe_vaapi_driver,
    _test_acceleration_method,
    _test_hwaccel_functionality,
    detect_all_gpus,
    get_gpu_name,
)


class TestDetectAllGPUs:
    """Test comprehensive GPU detection."""

    @patch("plex_generate_previews.gpu_detection._test_acceleration_method")
    @patch("plex_generate_previews.gpu_detection._get_gpu_devices")
    @patch("plex_generate_previews.gpu_detection.get_gpu_name")
    @patch("platform.system", return_value="Linux")
    def test_detect_all_gpus_nvidia(
        self, mock_platform, mock_name, mock_devices, mock_test
    ):
        """Test NVIDIA CUDA GPU detection."""
        mock_devices.return_value = [("card0", "/dev/dri/renderD128", "nvidia")]
        mock_test.return_value = True
        mock_name.return_value = "NVIDIA GeForce RTX 3080"

        gpus = detect_all_gpus()

        # Should detect NVIDIA GPU via CUDA
        nvidia_gpus = [g for g in gpus if g[0] == "NVIDIA"]
        assert len(nvidia_gpus) > 0
        assert nvidia_gpus[0][1] == "cuda"
        assert "RTX 3080" in nvidia_gpus[0][2]["name"]
        assert nvidia_gpus[0][2]["acceleration"] == "CUDA"

    @patch("plex_generate_previews.gpu_detection._test_acceleration_method")
    @patch("plex_generate_previews.gpu_detection._get_gpu_devices")
    @patch("plex_generate_previews.gpu_detection.get_gpu_name")
    @patch("platform.system", return_value="Linux")
    def test_detect_all_gpus_amd(
        self, mock_platform, mock_name, mock_devices, mock_test
    ):
        """Test AMD VAAPI GPU detection."""
        mock_devices.return_value = [("card0", "/dev/dri/renderD128", "amdgpu")]
        mock_test.return_value = True
        mock_name.return_value = "AMD Radeon RX 6800 XT"

        gpus = detect_all_gpus()

        # Should detect AMD GPU via VAAPI
        amd_gpus = [g for g in gpus if g[0] == "AMD"]
        assert len(amd_gpus) > 0
        assert "/dev/dri/renderD128" in amd_gpus[0][1]
        assert amd_gpus[0][2]["acceleration"] == "VAAPI"

    @patch("plex_generate_previews.gpu_detection._test_acceleration_method")
    @patch("plex_generate_previews.gpu_detection._get_gpu_devices")
    @patch("plex_generate_previews.gpu_detection.get_gpu_name")
    @patch("platform.system", return_value="Linux")
    def test_detect_all_gpus_intel(
        self, mock_platform, mock_name, mock_devices, mock_test
    ):
        """Test Intel VAAPI GPU detection."""
        mock_devices.return_value = [("card0", "/dev/dri/renderD128", "i915")]
        mock_test.return_value = True
        mock_name.return_value = "Intel UHD Graphics 770"

        gpus = detect_all_gpus()

        # Should detect Intel GPU via VAAPI
        intel_gpus = [g for g in gpus if g[0] == "INTEL"]
        assert len(intel_gpus) > 0
        assert intel_gpus[0][1] == "/dev/dri/renderD128"
        assert intel_gpus[0][2]["acceleration"] == "VAAPI"

    @patch("plex_generate_previews.gpu_detection._get_gpu_devices")
    def test_detect_all_gpus_none(self, mock_devices):
        """Test when no GPUs are detected."""
        mock_devices.return_value = []
        # Ensure platform-specific paths (macOS) are not taken in this test
        with (
            patch("plex_generate_previews.gpu_detection.is_macos", return_value=False),
            patch(
                "plex_generate_previews.gpu_detection.is_windows", return_value=False
            ),
            patch(
                "plex_generate_previews.gpu.ffmpeg_capabilities._get_ffmpeg_hwaccels",
                return_value=[],
            ),
            patch(
                "plex_generate_previews.gpu_detection._detect_nvidia_via_nvidia_smi",
                return_value="",
            ),
        ):
            gpus = detect_all_gpus()

        # Should return empty list
        assert gpus == []


class TestHwaccelAvailability:
    """Test hardware acceleration availability checking."""

    @patch("plex_generate_previews.gpu.ffmpeg_capabilities._get_ffmpeg_hwaccels")
    def test_is_hwaccel_available_cuda(self, mock_hwaccels):
        """Test CUDA availability check."""
        mock_hwaccels.return_value = ["cuda", "vaapi"]

        assert _is_hwaccel_available("cuda") is True
        assert _is_hwaccel_available("d3d11va") is False

    @patch("plex_generate_previews.gpu.ffmpeg_capabilities._get_ffmpeg_hwaccels")
    def test_is_hwaccel_available_none(self, mock_hwaccels):
        """Test when no hwaccels are available."""
        mock_hwaccels.return_value = []

        assert _is_hwaccel_available("cuda") is False
        assert _is_hwaccel_available("vaapi") is False


class TestHwaccelFunctionality:
    """Test hardware acceleration functionality testing."""

    @patch("subprocess.run")
    def test_test_hwaccel_functionality_cuda_success(self, mock_run):
        """Test CUDA functionality test success."""
        mock_run.return_value = MagicMock(returncode=0)

        result = _test_hwaccel_functionality("cuda")
        assert result is True

    @patch("subprocess.run")
    def test_test_hwaccel_functionality_cuda_failure(self, mock_run):
        """Test CUDA functionality test failure."""
        mock_run.return_value = MagicMock(returncode=1, stderr=b"Error")

        result = _test_hwaccel_functionality("cuda")
        assert result is False

    @patch("subprocess.run")
    def test_test_hwaccel_functionality_cuda_devnull_error(self, mock_run):
        """Test CUDA failure with /dev/null error (container without nvidia runtime)."""
        mock_run.return_value = MagicMock(
            returncode=255,
            stderr=b"Error opening output file /dev/null.\nError opening output files: Operation not permitted",
        )

        result = _test_hwaccel_functionality("cuda")
        assert result is False

    @patch("subprocess.run")
    def test_test_hwaccel_functionality_cuda_init_error(self, mock_run):
        """Test CUDA initialization failure."""
        mock_run.return_value = MagicMock(
            returncode=255, stderr=b"cuda_check_ret failed\nCUDA initialization failed"
        )

        result = _test_hwaccel_functionality("cuda")
        assert result is False

    @patch("os.access")
    @patch("os.path.exists")
    @patch("subprocess.run")
    def test_test_hwaccel_functionality_vaapi_success(
        self, mock_run, mock_exists, mock_access
    ):
        """Test VAAPI functionality test success."""
        mock_exists.return_value = True  # Device exists
        mock_access.return_value = True  # Device is accessible
        mock_run.return_value = MagicMock(returncode=0)

        result = _test_hwaccel_functionality("vaapi", "/dev/dri/renderD128")
        assert result is True

    @patch("os.access")
    @patch("os.path.exists")
    def test_test_hwaccel_functionality_vaapi_device_not_found(
        self, mock_exists, mock_access
    ):
        """Test VAAPI when device doesn't exist (should fail silently)."""
        mock_exists.return_value = False  # Device doesn't exist

        result = _test_hwaccel_functionality("vaapi", "/dev/dri/renderD128")
        assert result is False

    @patch("os.getgroups", create=True)
    @patch("os.getuid", create=True)
    @patch("os.access")
    @patch("os.path.exists")
    def test_test_hwaccel_functionality_vaapi_permission_denied(
        self, mock_exists, mock_access, mock_uid, mock_groups
    ):
        """Test VAAPI when device exists but permission denied."""
        mock_exists.return_value = True  # Device exists
        mock_access.return_value = False  # But not accessible
        mock_uid.return_value = 1000
        mock_groups.return_value = [1000]

        result = _test_hwaccel_functionality("vaapi", "/dev/dri/renderD128")
        assert result is False

    @patch("os.access")
    @patch("os.path.exists")
    @patch("subprocess.run")
    def test_test_hwaccel_functionality_vaapi_stderr_error(
        self, mock_run, mock_exists, mock_access
    ):
        """Test VAAPI failure with stderr containing permission denied."""
        mock_exists.return_value = True
        mock_access.return_value = True
        mock_run.return_value = MagicMock(
            returncode=1,
            stderr=b"Failed to initialize VAAPI connection\nvaapi: permission denied",
        )

        result = _test_hwaccel_functionality("vaapi", "/dev/dri/renderD128")
        assert result is False

    @patch("subprocess.run")
    def test_test_hwaccel_functionality_timeout(self, mock_run):
        """Test timeout handling."""
        from subprocess import TimeoutExpired

        mock_run.side_effect = TimeoutExpired("ffmpeg", 10)

        result = _test_hwaccel_functionality("cuda")
        assert result is False

    @patch("subprocess.run")
    def test_test_hwaccel_functionality_d3d11va_error(self, mock_run):
        """Test D3D11VA error handling (generic hwaccel path)."""
        mock_run.return_value = MagicMock(
            returncode=1, stderr=b"Failed to initialize D3D11VA\nError creating device"
        )

        result = _test_hwaccel_functionality("d3d11va")
        assert result is False

    @patch("subprocess.run")
    def test_test_hwaccel_functionality_sigpipe(self, mock_run):
        """Test SIGPIPE handling (exit code 141 is acceptable)."""
        mock_run.return_value = MagicMock(returncode=141)

        result = _test_hwaccel_functionality("cuda")
        assert result is True

    @patch("subprocess.run")
    def test_test_hwaccel_functionality_exception(self, mock_run):
        """Test exception handling during test."""
        mock_run.side_effect = RuntimeError("Unexpected error")

        result = _test_hwaccel_functionality("cuda")
        assert result is False

    @patch("subprocess.run")
    def test_test_hwaccel_functionality_empty_stderr(self, mock_run):
        """Test failure with no stderr output."""
        mock_run.return_value = MagicMock(returncode=1, stderr=b"")

        result = _test_hwaccel_functionality("cuda")
        assert result is False

    @patch("subprocess.run")
    def test_test_hwaccel_functionality_stderr_with_empty_lines(self, mock_run):
        """Test stderr parsing with empty lines."""
        mock_run.return_value = MagicMock(
            returncode=1, stderr=b"\n\nError occurred\n\n"
        )

        result = _test_hwaccel_functionality("cuda")
        assert result is False

    @patch("os.access")
    @patch("os.path.exists")
    @patch("subprocess.run")
    def test_test_hwaccel_functionality_vaapi_generic_error(
        self, mock_run, mock_exists, mock_access
    ):
        """Test VAAPI failure without permission denied in stderr."""
        mock_exists.return_value = True
        mock_access.return_value = True
        mock_run.return_value = MagicMock(
            returncode=1, stderr=b"Failed to initialize VAAPI\nDevice error occurred"
        )

        result = _test_hwaccel_functionality("vaapi", "/dev/dri/renderD128")
        assert result is False


class TestGetGPUDevices:
    """Test GPU device enumeration."""

    @patch("plex_generate_previews.gpu_detection.os.path.islink")
    @patch("plex_generate_previews.gpu_detection.os.readlink")
    @patch("plex_generate_previews.gpu_detection.os.listdir")
    @patch("plex_generate_previews.gpu_detection.os.path.exists")
    @patch("plex_generate_previews.gpu_detection.os.path.realpath")
    def test_get_gpu_devices(
        self, mock_realpath, mock_exists, mock_listdir, mock_readlink, mock_islink
    ):
        """Test enumerating GPU devices from /sys/class/drm."""
        mock_exists.return_value = True
        mock_listdir.return_value = [
            "card0",
            "card0-HDMI-A-1",
            "renderD128",
            "card1",
            "renderD129",
        ]
        mock_islink.return_value = True
        mock_readlink.return_value = "amdgpu"
        mock_realpath.side_effect = lambda x: "/sys/devices/pci0000:00/0000:00:01.0"
        # Force Linux code path for this test
        with patch(
            "plex_generate_previews.gpu_detection.platform.system", return_value="Linux"
        ):
            devices = _get_gpu_devices()

        # Should find GPU devices
        assert len(devices) > 0

    @patch("plex_generate_previews.gpu_detection.os.path.exists")
    def test_get_gpu_devices_no_drm(self, mock_exists):
        """Test when /sys/class/drm doesn't exist."""
        mock_exists.return_value = False

        devices = _get_gpu_devices()

        # Should return empty list
        assert devices == []


class TestGetGPUName:
    """Test GPU name retrieval."""

    @patch("subprocess.run")
    def test_get_gpu_name_nvidia(self, mock_run):
        """Test getting NVIDIA GPU name from nvidia-smi."""
        mock_run.return_value = MagicMock(
            returncode=0, stdout="NVIDIA GeForce RTX 3080\n"
        )

        name = get_gpu_name("NVIDIA", "cuda")

        assert "RTX 3080" in name

    @patch("subprocess.run")
    def test_get_gpu_name_nvidia_failure(self, mock_run):
        """Test fallback when nvidia-smi fails."""
        mock_run.return_value = MagicMock(returncode=1)

        name = get_gpu_name("NVIDIA", "cuda")

        assert "NVIDIA" in name
        assert "GPU" in name

    def test_get_gpu_name_windows(self):
        """Test Windows GPU name."""
        name = get_gpu_name("WINDOWS_GPU", "d3d11va")

        assert "Windows" in name
        assert "GPU" in name

    @patch("subprocess.run")
    def test_get_gpu_name_intel_vaapi(self, mock_run):
        """Test getting Intel GPU name for VAAPI."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="00:02.0 VGA compatible controller: Intel Corporation UHD Graphics 770\n",
        )

        name = get_gpu_name("INTEL", "/dev/dri/renderD128")

        assert "Intel" in name or "UHD" in name

    @patch("subprocess.run")
    def test_get_gpu_name_amd_vaapi(self, mock_run):
        """Test getting AMD GPU name from lspci."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="01:00.0 VGA compatible controller: Advanced Micro Devices [AMD/ATI] Navi 21 [Radeon RX 6800/6800 XT]\n",
        )

        name = get_gpu_name("AMD", "/dev/dri/renderD128")

        assert "AMD" in name or "Radeon" in name

    @patch("plex_generate_previews.gpu_detection.os.sep", "/")
    @patch(
        "plex_generate_previews.gpu_detection.os.path.join",
        side_effect=__import__("posixpath").join,
    )
    @patch(
        "plex_generate_previews.gpu_detection.os.path.basename",
        side_effect=__import__("posixpath").basename,
    )
    @patch("plex_generate_previews.gpu_detection.os.path.exists")
    @patch("plex_generate_previews.gpu_detection.os.path.realpath")
    @patch("subprocess.run")
    def test_get_gpu_name_multi_gpu_distinct_per_render_node(
        self, mock_run, mock_realpath, mock_exists, mock_basename, mock_join
    ):
        """Test that multiple /dev/dri/renderD* nodes can resolve to distinct GPU names."""
        # Pretend sysfs paths exist for both devices
        mock_exists.return_value = True

        def realpath_side_effect(path: str) -> str:
            # Return distinct PCI device paths based on the queried render node
            if "renderD128" in path:
                return "/sys/devices/pci0000:00/0000:00:02.0"
            if "renderD129" in path:
                return "/sys/devices/pci0000:00/0000:06:00.0"
            return "/sys/devices/pci0000:00/0000:00:00.0"

        mock_realpath.side_effect = realpath_side_effect

        def run_side_effect(cmd, capture_output=True, text=True, timeout=5, **kwargs):
            # Our implementation queries lspci -s <pci_address>
            if cmd[:3] == ["lspci", "-s", "0000:00:02.0"]:
                return MagicMock(
                    returncode=0,
                    stdout="00:02.0 Display controller: Intel Corporation TigerLake-LP GT2 [Iris Xe Graphics] (rev 01)\n",
                )
            if cmd[:3] == ["lspci", "-s", "0000:06:00.0"]:
                return MagicMock(
                    returncode=0,
                    stdout="06:00.0 VGA compatible controller: Intel Corporation DG2 [Arc A380] (rev 05)\n",
                )
            return MagicMock(returncode=1, stdout="")

        mock_run.side_effect = run_side_effect

        name_0 = get_gpu_name("INTEL", "/dev/dri/renderD128")
        name_1 = get_gpu_name("INTEL", "/dev/dri/renderD129")

        assert name_0 != name_1
        assert "Iris Xe" in name_0 or "TigerLake" in name_0
        assert "Arc" in name_1 or "A380" in name_1


class TestGetFFmpegHwaccels:
    """Test FFmpeg hwaccel enumeration."""

    @patch("subprocess.run")
    def test_get_ffmpeg_hwaccels(self, mock_run):
        """Test getting list of available hwaccels."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="""Hardware acceleration methods:
cuda
vaapi
d3d11va
""",
        )

        hwaccels = _get_ffmpeg_hwaccels()

        assert "cuda" in hwaccels
        assert "vaapi" in hwaccels
        assert "Hardware acceleration methods:" not in hwaccels

    @patch("subprocess.run")
    def test_get_ffmpeg_hwaccels_failure(self, mock_run):
        """Test handling FFmpeg hwaccels failure."""
        mock_run.return_value = MagicMock(returncode=1)

        hwaccels = _get_ffmpeg_hwaccels()

        assert hwaccels == []


class TestFFmpegVersion:
    """Test FFmpeg version detection."""

    @patch("subprocess.run")
    def test_get_ffmpeg_version_parse_error(self, mock_run):
        """Test FFmpeg version parsing with invalid output."""
        from plex_generate_previews.gpu_detection import _get_ffmpeg_version

        mock_run.return_value = MagicMock(returncode=0, stdout="Invalid version output")

        result = _get_ffmpeg_version()
        assert result is None

    @patch("subprocess.run")
    def test_get_ffmpeg_version_error(self, mock_run):
        """Test FFmpeg version with subprocess error."""
        from plex_generate_previews.gpu_detection import _get_ffmpeg_version

        mock_run.side_effect = Exception("Command failed")

        result = _get_ffmpeg_version()
        assert result is None

    @patch("plex_generate_previews.gpu.ffmpeg_capabilities._get_ffmpeg_version")
    def test_check_ffmpeg_version_none(self, mock_get_version):
        """Test check FFmpeg version when version is None."""
        from plex_generate_previews.gpu_detection import _check_ffmpeg_version

        mock_get_version.return_value = None

        result = _check_ffmpeg_version()
        # Should return True to not fail if version can't be determined
        assert result is True


class TestAppleGPU:
    """Test Apple GPU detection functions."""

    @patch("subprocess.run")
    def test_get_apple_gpu_name_success(self, mock_run):
        """Test getting Apple GPU name successfully."""
        from plex_generate_previews.gpu_detection import _get_apple_gpu_name

        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="Graphics/Displays:\n\n  Chipset Model: Apple M1 Pro\n  Type: GPU",
        )

        result = _get_apple_gpu_name()
        assert result == "Apple M1 Pro"

    @patch("subprocess.run")
    def test_get_apple_gpu_name_error(self, mock_run):
        """Test getting Apple GPU name with error."""
        from plex_generate_previews.gpu_detection import _get_apple_gpu_name

        mock_run.side_effect = Exception("Command failed")

        result = _get_apple_gpu_name()
        assert "Apple" in result  # Should return fallback

    @patch("platform.machine")
    @patch("subprocess.run")
    def test_get_apple_gpu_name_arm64_fallback(self, mock_run, mock_machine):
        """Test getting Apple GPU name with ARM64 fallback."""
        from plex_generate_previews.gpu_detection import _get_apple_gpu_name

        mock_run.return_value = MagicMock(returncode=0, stdout="No chipset info")
        mock_machine.return_value = "arm64"

        result = _get_apple_gpu_name()
        assert result == "Apple Silicon GPU"


class TestLspciGPUDetection:
    """Test lspci GPU detection."""

    @patch("subprocess.run")
    def test_detect_gpu_type_from_lspci_failure(self, mock_run):
        """Test lspci GPU detection with command failure."""
        from plex_generate_previews.gpu_detection import _detect_gpu_type_from_lspci

        mock_run.return_value = MagicMock(returncode=1)

        result = _detect_gpu_type_from_lspci()
        assert result == "UNKNOWN"

    @patch("subprocess.run")
    def test_detect_gpu_type_from_lspci_amd(self, mock_run):
        """Test lspci detecting AMD GPU."""
        from plex_generate_previews.gpu_detection import _detect_gpu_type_from_lspci

        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="00:02.0 VGA compatible controller: Advanced Micro Devices [AMD/ATI] Radeon RX 6800 XT",
        )

        result = _detect_gpu_type_from_lspci()
        assert result == "AMD"

    @patch("subprocess.run")
    def test_detect_gpu_type_from_lspci_intel(self, mock_run):
        """Test lspci detecting Intel GPU."""
        from plex_generate_previews.gpu_detection import _detect_gpu_type_from_lspci

        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="00:02.0 Display controller: Intel Corporation UHD Graphics 630",
        )

        result = _detect_gpu_type_from_lspci()
        assert result == "INTEL"

    @patch("subprocess.run")
    def test_detect_gpu_type_from_lspci_nvidia(self, mock_run):
        """Test lspci detecting NVIDIA GPU."""
        from plex_generate_previews.gpu_detection import _detect_gpu_type_from_lspci

        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="01:00.0 VGA compatible controller: NVIDIA Corporation GeForce RTX 3080",
        )

        result = _detect_gpu_type_from_lspci()
        assert result == "NVIDIA"

    @patch("subprocess.run")
    def test_detect_gpu_type_from_lspci_arm(self, mock_run):
        """Test lspci detecting ARM GPU."""
        from plex_generate_previews.gpu_detection import _detect_gpu_type_from_lspci

        mock_run.return_value = MagicMock(
            returncode=0, stdout="00:02.0 Display controller: ARM Mali GPU"
        )

        result = _detect_gpu_type_from_lspci()
        assert result == "ARM"

    @patch("subprocess.run")
    def test_detect_gpu_type_from_lspci_not_found(self, mock_run):
        """Test lspci when lspci is not installed."""
        from plex_generate_previews.gpu_detection import _detect_gpu_type_from_lspci

        mock_run.side_effect = FileNotFoundError()

        result = _detect_gpu_type_from_lspci()
        assert result == "UNKNOWN"

    @patch("subprocess.run")
    def test_detect_gpu_type_from_lspci_exception(self, mock_run):
        """Test lspci with exception."""
        from plex_generate_previews.gpu_detection import _detect_gpu_type_from_lspci

        mock_run.side_effect = Exception("Unexpected error")

        result = _detect_gpu_type_from_lspci()
        assert result == "UNKNOWN"

    @patch("subprocess.run")
    def test_detect_gpu_type_from_lspci_no_match(self, mock_run):
        """Test lspci with no GPU match."""
        from plex_generate_previews.gpu_detection import _detect_gpu_type_from_lspci

        mock_run.return_value = MagicMock(
            returncode=0, stdout="00:1f.3 Audio device: Intel Corporation"
        )

        result = _detect_gpu_type_from_lspci()
        assert result == "UNKNOWN"


class TestLogSystemInfo:
    """Test system info logging."""

    @patch("platform.system")
    @patch("platform.release")
    @patch("plex_generate_previews.gpu_detection.logger")
    def test_log_system_info(self, mock_logger, mock_release, mock_system):
        """Test logging system information."""
        from plex_generate_previews.gpu_detection import _log_system_info

        mock_system.return_value = "Linux"
        mock_release.return_value = "5.15.0"

        _log_system_info()

        # Should log system information
        assert mock_logger.debug.called


class TestParseLspciGPUName:
    """Test parsing lspci GPU names."""

    @patch("subprocess.run")
    def test_parse_lspci_gpu_name_nvidia(self, mock_run):
        """Test parsing NVIDIA GPU name."""
        from plex_generate_previews.gpu_detection import _parse_lspci_gpu_name

        # Mock lspci failure - should return fallback
        mock_run.return_value = MagicMock(returncode=1)

        result = _parse_lspci_gpu_name("NVIDIA")
        assert result == "NVIDIA GPU"

    @patch("subprocess.run")
    def test_parse_lspci_gpu_name_amd(self, mock_run):
        """Test parsing AMD GPU name."""
        from plex_generate_previews.gpu_detection import _parse_lspci_gpu_name

        # Mock lspci failure - should return fallback
        mock_run.return_value = MagicMock(returncode=1)

        result = _parse_lspci_gpu_name("AMD")
        assert result == "AMD GPU"

    @patch("subprocess.run")
    def test_parse_lspci_gpu_name_intel(self, mock_run):
        """Test parsing Intel GPU name."""
        from plex_generate_previews.gpu_detection import _parse_lspci_gpu_name

        # Mock lspci failure - should return fallback
        mock_run.return_value = MagicMock(returncode=1)

        result = _parse_lspci_gpu_name("INTEL")
        assert result == "INTEL GPU"


class TestAccelerationMethodTesting:
    """Test acceleration method testing."""

    @patch("plex_generate_previews.gpu_detection._test_hwaccel_functionality")
    def test_test_acceleration_method_cuda_failure(self, mock_test):
        """Test CUDA acceleration method failure."""

        mock_test.return_value = False

        result = _test_acceleration_method("nvidia", "CUDA", None)
        assert result is False

    @patch("plex_generate_previews.gpu_detection._test_hwaccel_functionality")
    def test_test_acceleration_method_vaapi_failure(self, mock_test):
        """Test VAAPI acceleration method failure."""

        mock_test.return_value = False

        result = _test_acceleration_method("amd", "VAAPI", "/dev/dri/renderD128")
        assert result is False


class TestNvidiaSmiDetection:
    """Test nvidia-smi fallback detection."""

    @patch("subprocess.run")
    def test_detect_nvidia_via_nvidia_smi_success(self, mock_run):
        from plex_generate_previews.gpu_detection import _detect_nvidia_via_nvidia_smi

        mock_run.return_value = MagicMock(
            returncode=0, stdout="NVIDIA GeForce RTX 3080\n"
        )
        assert _detect_nvidia_via_nvidia_smi() == "NVIDIA"

    @patch("subprocess.run")
    def test_detect_nvidia_via_nvidia_smi_not_installed(self, mock_run):
        from plex_generate_previews.gpu_detection import _detect_nvidia_via_nvidia_smi

        mock_run.side_effect = FileNotFoundError()
        assert _detect_nvidia_via_nvidia_smi() == "UNKNOWN"

    @patch("subprocess.run")
    def test_detect_nvidia_via_nvidia_smi_failure(self, mock_run):
        from plex_generate_previews.gpu_detection import _detect_nvidia_via_nvidia_smi

        mock_run.return_value = MagicMock(returncode=1, stderr="error")
        assert _detect_nvidia_via_nvidia_smi() == "UNKNOWN"

    @patch("subprocess.run")
    def test_detect_nvidia_via_nvidia_smi_empty_output(self, mock_run):
        from plex_generate_previews.gpu_detection import _detect_nvidia_via_nvidia_smi

        mock_run.return_value = MagicMock(returncode=0, stdout="")
        assert _detect_nvidia_via_nvidia_smi() == "UNKNOWN"

    @patch("subprocess.run")
    def test_detect_nvidia_via_nvidia_smi_timeout(self, mock_run):
        from subprocess import TimeoutExpired

        from plex_generate_previews.gpu_detection import _detect_nvidia_via_nvidia_smi

        mock_run.side_effect = TimeoutExpired("nvidia-smi", 5)
        assert _detect_nvidia_via_nvidia_smi() == "UNKNOWN"

    @patch("subprocess.run")
    def test_detect_nvidia_via_nvidia_smi_exception(self, mock_run):
        from plex_generate_previews.gpu_detection import _detect_nvidia_via_nvidia_smi

        mock_run.side_effect = RuntimeError("unexpected")
        assert _detect_nvidia_via_nvidia_smi() == "UNKNOWN"


class TestGPUVendorFromDriver:
    """Test driver-to-vendor mapping."""

    def test_known_drivers(self):
        from plex_generate_previews.gpu_detection import _get_gpu_vendor_from_driver

        with patch(
            "plex_generate_previews.gpu_detection._detect_gpu_type_from_lspci",
            return_value="UNKNOWN",
        ):
            assert _get_gpu_vendor_from_driver("nvidia") == "NVIDIA"
            assert _get_gpu_vendor_from_driver("amdgpu") == "AMD"
            assert _get_gpu_vendor_from_driver("i915") == "INTEL"

    @patch("plex_generate_previews.gpu_detection._is_wsl2", return_value=False)
    @patch("plex_generate_previews.gpu_detection._detect_gpu_type_from_lspci")
    def test_unknown_driver_uses_lspci(self, mock_lspci, _mock_wsl):
        from plex_generate_previews.gpu_detection import _get_gpu_vendor_from_driver

        mock_lspci.return_value = "AMD"
        assert _get_gpu_vendor_from_driver("somedriver") == "AMD"


class TestCheckDeviceAccess:
    """Test device access checking."""

    @patch("os.access", return_value=True)
    @patch("os.path.exists", return_value=True)
    def test_accessible_device(self, _mock_exists, _mock_access):
        from plex_generate_previews.gpu_detection import _check_device_access

        ok, reason = _check_device_access("/dev/dri/renderD128")
        assert ok is True
        assert reason == "accessible"

    @patch("os.path.exists", return_value=False)
    def test_nonexistent_device(self, _mock_exists):
        from plex_generate_previews.gpu_detection import _check_device_access

        ok, reason = _check_device_access("/dev/dri/renderD999")
        assert ok is False
        assert reason == "not_found"


class TestBuildGpuErrorDetail:
    """Test _build_gpu_error_detail error messages."""

    VAAPI_CONFIG = {"primary": "VAAPI", "fallback": None}

    @patch("os.getgroups", return_value=[1000])
    @patch("os.stat")
    @patch("os.access", return_value=False)
    @patch("os.path.exists", return_value=True)
    def test_vaapi_permission_denied_no_group_add_advice(
        self, _exists, _access, mock_stat, _groups
    ):
        """Error detail must not suggest --group-add (it doesn't work through gosu)."""
        mock_stat.return_value = MagicMock(st_gid=105)

        error, detail = _build_gpu_error_detail(
            "VAAPI", "/dev/dri/renderD128", "/dev/dri/renderD128", self.VAAPI_CONFIG
        )
        assert "permission denied" in error.lower()
        assert "--group-add" not in detail
        assert "auto-detect" in detail.lower()

    @patch("os.getgroups", return_value=[105])
    @patch("os.stat")
    @patch("os.access", return_value=False)
    @patch("os.path.exists", return_value=True)
    def test_vaapi_permission_denied_in_group(
        self, _exists, _access, mock_stat, _groups
    ):
        """When already in the device group, suggest checking host permissions."""
        mock_stat.return_value = MagicMock(st_gid=105)

        error, detail = _build_gpu_error_detail(
            "VAAPI", "/dev/dri/renderD128", "/dev/dri/renderD128", self.VAAPI_CONFIG
        )
        assert "permission denied" in error.lower()
        assert "host device permissions" in detail.lower()

    @patch("os.stat", side_effect=OSError("no device"))
    @patch("os.access", return_value=False)
    @patch("os.path.exists", return_value=True)
    def test_vaapi_permission_denied_stat_fails(self, _exists, _access, _stat):
        """Fallback message when stat() fails must not suggest --group-add."""
        error, detail = _build_gpu_error_detail(
            "VAAPI", "/dev/dri/renderD128", "/dev/dri/renderD128", self.VAAPI_CONFIG
        )
        assert "permission denied" in error.lower()
        assert "--group-add" not in detail

    @patch("os.access", return_value=False)
    @patch("os.path.exists", return_value=False)
    def test_vaapi_device_not_found(self, _exists, _access):
        """Missing device should suggest --device flag."""
        error, detail = _build_gpu_error_detail(
            "VAAPI", "/dev/dri/renderD128", "/dev/dri/renderD128", self.VAAPI_CONFIG
        )
        assert "not found" in error.lower()
        assert "--device" in detail


class TestLspciEdgeCases:
    """Additional lspci edge cases."""

    @patch("subprocess.run")
    def test_lspci_empty_output(self, mock_run):
        from plex_generate_previews.gpu_detection import _detect_gpu_type_from_lspci

        mock_run.return_value = MagicMock(returncode=0, stdout="")
        assert _detect_gpu_type_from_lspci() == "UNKNOWN"

    @patch("subprocess.run")
    def test_lspci_timeout(self, mock_run):
        from subprocess import TimeoutExpired

        from plex_generate_previews.gpu_detection import _detect_gpu_type_from_lspci

        mock_run.side_effect = TimeoutExpired("lspci", 5)
        assert _detect_gpu_type_from_lspci() == "UNKNOWN"

    @patch("subprocess.run")
    def test_lspci_no_vga_devices(self, mock_run):
        from plex_generate_previews.gpu_detection import _detect_gpu_type_from_lspci

        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="00:1f.3 Audio device: Sound Corp\n00:00.0 Host bridge: Foo\n",
        )
        assert _detect_gpu_type_from_lspci() == "UNKNOWN"

    @patch("subprocess.run")
    def test_lspci_unrecognized_gpu_vendor(self, mock_run):
        from plex_generate_previews.gpu_detection import _detect_gpu_type_from_lspci

        mock_run.return_value = MagicMock(
            returncode=0, stdout="00:02.0 VGA compatible controller: Matrox Corporation"
        )
        assert _detect_gpu_type_from_lspci() == "UNKNOWN"

    @patch("subprocess.run")
    def test_lspci_with_stderr(self, mock_run):
        from plex_generate_previews.gpu_detection import _detect_gpu_type_from_lspci

        mock_run.return_value = MagicMock(
            returncode=1, stderr="pcilib: Cannot open /proc/bus/pci", stdout=""
        )
        assert _detect_gpu_type_from_lspci() == "UNKNOWN"


class TestCheckFFmpegVersion:
    """Test FFmpeg version validation."""

    @patch("plex_generate_previews.gpu.ffmpeg_capabilities._get_ffmpeg_version")
    def test_version_meets_minimum(self, mock_version):
        from plex_generate_previews.gpu_detection import _check_ffmpeg_version

        mock_version.return_value = (7, 1, 1)
        assert _check_ffmpeg_version() is True

    @patch("plex_generate_previews.gpu.ffmpeg_capabilities._get_ffmpeg_version")
    def test_version_below_minimum(self, mock_version):
        from plex_generate_previews.gpu_detection import _check_ffmpeg_version

        mock_version.return_value = (4, 0, 0)
        assert _check_ffmpeg_version() is False


class TestGetFFmpegVersionParsing:
    """Test FFmpeg version parsing edge cases."""

    @patch("subprocess.run")
    def test_date_based_git_build(self, mock_run):
        from plex_generate_previews.gpu_detection import _get_ffmpeg_version

        mock_run.return_value = MagicMock(
            returncode=0, stdout="ffmpeg version 2025-10-12-git-abcdef"
        )
        assert _get_ffmpeg_version() is None

    @patch("subprocess.run")
    def test_two_part_version(self, mock_run):
        from plex_generate_previews.gpu_detection import _get_ffmpeg_version

        mock_run.return_value = MagicMock(
            returncode=0, stdout="ffmpeg version 8.0 Copyright"
        )
        assert _get_ffmpeg_version() == (8, 0, 0)

    @patch("subprocess.run")
    def test_version_with_suffix(self, mock_run):
        from plex_generate_previews.gpu_detection import _get_ffmpeg_version

        mock_run.return_value = MagicMock(
            returncode=0, stdout="ffmpeg version 7.1.1-1ubuntu1.2 Copyright"
        )
        assert _get_ffmpeg_version() == (7, 1, 1)

    @patch("subprocess.run")
    def test_ffmpeg_not_found(self, mock_run):
        from plex_generate_previews.gpu_detection import _get_ffmpeg_version

        mock_run.return_value = MagicMock(returncode=1, stderr="command not found")
        assert _get_ffmpeg_version() is None


class TestDetectAllGPUsEdgeCases:
    """Test edge cases in detect_all_gpus."""

    @patch("plex_generate_previews.gpu_detection.is_macos")
    @patch("plex_generate_previews.gpu_detection.is_windows")
    @patch("plex_generate_previews.gpu_detection.platform.system")
    @patch("plex_generate_previews.gpu_detection._test_acceleration_method")
    @patch("plex_generate_previews.gpu_detection.get_gpu_name")
    def test_detect_all_gpus_macos_videotoolbox(
        self,
        mock_gpu_name,
        mock_test,
        mock_platform_system,
        mock_is_windows,
        mock_is_macos,
    ):
        """Test macOS VideoToolbox detection."""
        mock_is_macos.return_value = True
        mock_is_windows.return_value = False
        mock_platform_system.return_value = "Darwin"
        mock_test.return_value = True
        mock_gpu_name.return_value = "Apple M1 Max"

        gpus = detect_all_gpus()

        # Should detect Apple GPU
        apple_gpus = [g for g in gpus if g[0] == "APPLE"]
        assert len(apple_gpus) > 0
        assert "M1 Max" in apple_gpus[0][2]["name"]

    @patch("plex_generate_previews.gpu_detection.is_macos")
    @patch("plex_generate_previews.gpu_detection.is_windows")
    @patch("plex_generate_previews.gpu_detection.platform.system")
    @patch("plex_generate_previews.gpu_detection._get_gpu_devices")
    @patch("plex_generate_previews.gpu_detection._test_acceleration_method")
    def test_detect_all_gpus_nvidia_nvenc(
        self,
        mock_test,
        mock_devices,
        mock_platform_system,
        mock_is_windows,
        mock_is_macos,
    ):
        """Test NVIDIA NVENC detection."""
        mock_is_macos.return_value = False
        mock_is_windows.return_value = False
        mock_platform_system.return_value = "Linux"
        mock_devices.return_value = [("card0", "/dev/dri/renderD128", "nvidia")]

        def test_side_effect(vendor, accel, device):
            if accel == "CUDA":
                return True
            elif accel == "NVENC":
                return True
            return False

        mock_test.side_effect = test_side_effect

        gpus = detect_all_gpus()

        # Should detect NVIDIA with both CUDA and NVENC
        nvidia_gpus = [g for g in gpus if g[0] == "NVIDIA"]
        assert len(nvidia_gpus) >= 1


class TestWSL2NoDRMDevices:
    """Test WSL2 CUDA detection when /dev/dri has no card/renderD devices.

    WSL2 kernels 6.6+ no longer load CONFIG_DRM_VGEM by default, so /dev/dri
    may contain only a 'version' file. CUDA still works via /dev/dxg
    paravirtualization, so detection should succeed without DRM entries.
    """

    @patch("plex_generate_previews.gpu_detection.is_macos", return_value=False)
    @patch("plex_generate_previews.gpu_detection.is_windows", return_value=False)
    @patch("platform.system", return_value="Linux")
    @patch("plex_generate_previews.gpu_detection._get_gpu_devices", return_value=[])
    @patch("plex_generate_previews.gpu_detection._is_wsl2", return_value=True)
    @patch(
        "plex_generate_previews.gpu.ffmpeg_capabilities._get_ffmpeg_hwaccels",
        return_value=["cuda", "vaapi"],
    )
    @patch(
        "plex_generate_previews.gpu_detection._detect_nvidia_via_nvidia_smi",
        return_value="NVIDIA",
    )
    @patch(
        "plex_generate_previews.gpu_detection._test_hwaccel_functionality",
        return_value=True,
    )
    @patch(
        "plex_generate_previews.gpu_detection.get_gpu_name",
        return_value="NVIDIA GeForce RTX 5080",
    )
    def test_wsl2_no_drm_cuda_detected(
        self,
        _mock_name,
        _mock_test,
        _mock_nvidia_smi,
        _mock_hwaccels,
        _mock_wsl2,
        _mock_devices,
        _mock_platform,
        _mock_windows,
        _mock_macos,
    ):
        """WSL2 + no DRM devices + CUDA available + nvidia-smi confirms -> GPU detected."""
        gpus = detect_all_gpus()

        assert len(gpus) == 1
        gpu_type, gpu_device, gpu_info = gpus[0]
        assert gpu_type == "NVIDIA"
        assert gpu_device == "cuda"
        assert gpu_info["acceleration"] == "CUDA"
        assert gpu_info["render_device"] is None
        assert gpu_info["card"] == "wsl2"
        assert "RTX 5080" in gpu_info["name"]

    @patch("plex_generate_previews.gpu_detection.is_macos", return_value=False)
    @patch("plex_generate_previews.gpu_detection.is_windows", return_value=False)
    @patch("platform.system", return_value="Linux")
    @patch("plex_generate_previews.gpu_detection._get_gpu_devices", return_value=[])
    @patch("plex_generate_previews.gpu_detection._is_wsl2", return_value=True)
    @patch(
        "plex_generate_previews.gpu.ffmpeg_capabilities._get_ffmpeg_hwaccels",
        return_value=[],
    )
    @patch(
        "plex_generate_previews.gpu_detection._scan_dev_dri_render_devices",
        return_value=[],
    )
    def test_wsl2_no_drm_no_cuda_hwaccel(
        self,
        _mock_scan,
        _mock_hwaccels,
        _mock_wsl2,
        _mock_devices,
        _mock_platform,
        _mock_windows,
        _mock_macos,
    ):
        """WSL2 + no DRM devices + CUDA not in FFmpeg hwaccels -> no GPU."""
        gpus = detect_all_gpus()
        assert gpus == []

    @patch("plex_generate_previews.gpu_detection.is_macos", return_value=False)
    @patch("plex_generate_previews.gpu_detection.is_windows", return_value=False)
    @patch("platform.system", return_value="Linux")
    @patch("plex_generate_previews.gpu_detection._get_gpu_devices", return_value=[])
    @patch("plex_generate_previews.gpu_detection._is_wsl2", return_value=False)
    @patch(
        "plex_generate_previews.gpu.ffmpeg_capabilities._get_ffmpeg_hwaccels",
        return_value=["cuda"],
    )
    @patch(
        "plex_generate_previews.gpu_detection._detect_nvidia_via_nvidia_smi",
        return_value="UNKNOWN",
    )
    @patch(
        "plex_generate_previews.gpu_detection._scan_dev_dri_render_devices",
        return_value=[],
    )
    def test_not_wsl2_no_drm_no_render_devices(
        self,
        _mock_scan,
        _mock_nvidia_smi,
        _mock_hwaccels,
        _mock_wsl2,
        _mock_devices,
        _mock_platform,
        _mock_windows,
        _mock_macos,
    ):
        """Not WSL2 + no DRM + no render devices in /dev/dri -> no GPU."""
        gpus = detect_all_gpus()
        assert gpus == []

    @patch("plex_generate_previews.gpu_detection.is_macos", return_value=False)
    @patch("plex_generate_previews.gpu_detection.is_windows", return_value=False)
    @patch("platform.system", return_value="Linux")
    @patch("plex_generate_previews.gpu_detection._get_gpu_devices", return_value=[])
    @patch("plex_generate_previews.gpu_detection._is_wsl2", return_value=False)
    @patch(
        "plex_generate_previews.gpu.ffmpeg_capabilities._get_ffmpeg_hwaccels",
        return_value=["vaapi"],
    )
    @patch(
        "plex_generate_previews.gpu_detection._scan_dev_dri_render_devices",
        return_value=["/dev/dri/renderD128"],
    )
    @patch(
        "plex_generate_previews.gpu_detection._detect_gpu_type_from_lspci",
        return_value="INTEL",
    )
    @patch(
        "plex_generate_previews.gpu_detection._test_hwaccel_functionality",
        return_value=True,
    )
    @patch(
        "plex_generate_previews.gpu_detection.get_gpu_name",
        return_value="Intel Arc A770",
    )
    def test_container_no_sysfs_vaapi_detected(
        self,
        _mock_name,
        _mock_test,
        _mock_lspci,
        _mock_scan,
        _mock_hwaccels,
        _mock_wsl2,
        _mock_devices,
        _mock_platform,
        _mock_windows,
        _mock_macos,
    ):
        """Container with /dev/dri passthrough but no sysfs -> VAAPI GPU detected."""
        gpus = detect_all_gpus()
        assert len(gpus) == 1
        vendor, device, info = gpus[0]
        assert vendor == "INTEL"
        assert device == "/dev/dri/renderD128"
        assert info["acceleration"] == "VAAPI"
        assert info["name"] == "Intel Arc A770"

    @patch("plex_generate_previews.gpu_detection.is_macos", return_value=False)
    @patch("plex_generate_previews.gpu_detection.is_windows", return_value=False)
    @patch("platform.system", return_value="Linux")
    @patch("plex_generate_previews.gpu_detection._get_gpu_devices", return_value=[])
    @patch("plex_generate_previews.gpu_detection._is_wsl2", return_value=False)
    @patch(
        "plex_generate_previews.gpu.ffmpeg_capabilities._get_ffmpeg_hwaccels",
        return_value=["vaapi"],
    )
    @patch(
        "plex_generate_previews.gpu_detection._scan_dev_dri_render_devices",
        return_value=["/dev/dri/renderD128"],
    )
    @patch(
        "plex_generate_previews.gpu_detection._detect_gpu_type_from_lspci",
        return_value="UNKNOWN",
    )
    @patch(
        "plex_generate_previews.gpu_detection._test_hwaccel_functionality",
        return_value=True,
    )
    def test_container_no_sysfs_unknown_vendor(
        self,
        _mock_test,
        _mock_lspci,
        _mock_scan,
        _mock_hwaccels,
        _mock_wsl2,
        _mock_devices,
        _mock_platform,
        _mock_windows,
        _mock_macos,
    ):
        """Container fallback with unknown vendor -> GPU detected with generic name."""
        gpus = detect_all_gpus()
        assert len(gpus) == 1
        vendor, device, info = gpus[0]
        assert vendor == "UNKNOWN"
        assert device == "/dev/dri/renderD128"
        assert info["acceleration"] == "VAAPI"
        assert info["name"] == "GPU"

    @patch("plex_generate_previews.gpu_detection.is_macos", return_value=False)
    @patch("plex_generate_previews.gpu_detection.is_windows", return_value=False)
    @patch("platform.system", return_value="Linux")
    @patch("plex_generate_previews.gpu_detection._get_gpu_devices", return_value=[])
    @patch("plex_generate_previews.gpu_detection._is_wsl2", return_value=False)
    @patch(
        "plex_generate_previews.gpu.ffmpeg_capabilities._get_ffmpeg_hwaccels",
        return_value=["vaapi"],
    )
    @patch(
        "plex_generate_previews.gpu_detection._scan_dev_dri_render_devices",
        return_value=["/dev/dri/renderD128"],
    )
    @patch(
        "plex_generate_previews.gpu_detection._detect_gpu_type_from_lspci",
        return_value="INTEL",
    )
    @patch(
        "plex_generate_previews.gpu_detection._test_hwaccel_functionality",
        return_value=False,
    )
    def test_container_no_sysfs_vaapi_fails(
        self,
        _mock_test,
        _mock_lspci,
        _mock_scan,
        _mock_hwaccels,
        _mock_wsl2,
        _mock_devices,
        _mock_platform,
        _mock_windows,
        _mock_macos,
    ):
        """Container with render device but VAAPI test fails -> failed GPU entry."""
        gpus = detect_all_gpus()
        assert len(gpus) == 1
        _vendor, device, info = gpus[0]
        assert device == "/dev/dri/renderD128"
        assert info["status"] == "failed"
        assert info["acceleration"] == "VAAPI"

    @patch("plex_generate_previews.gpu_detection.is_macos", return_value=False)
    @patch("plex_generate_previews.gpu_detection.is_windows", return_value=False)
    @patch("platform.system", return_value="Linux")
    @patch("plex_generate_previews.gpu_detection._get_gpu_devices", return_value=[])
    @patch("plex_generate_previews.gpu_detection._is_wsl2", return_value=False)
    @patch(
        "plex_generate_previews.gpu.ffmpeg_capabilities._get_ffmpeg_hwaccels",
        return_value=["vaapi"],
    )
    @patch(
        "plex_generate_previews.gpu_detection._scan_dev_dri_render_devices",
        return_value=["/dev/dri/renderD128", "/dev/dri/renderD129"],
    )
    @patch(
        "plex_generate_previews.gpu_detection._detect_gpu_type_from_lspci",
        return_value="INTEL",
    )
    @patch(
        "plex_generate_previews.gpu_detection._test_hwaccel_functionality",
        return_value=True,
    )
    @patch(
        "plex_generate_previews.gpu_detection.get_gpu_name",
        return_value="Intel Arc A770",
    )
    def test_container_no_sysfs_multiple_render_devices(
        self,
        _mock_name,
        _mock_test,
        _mock_lspci,
        _mock_scan,
        _mock_hwaccels,
        _mock_wsl2,
        _mock_devices,
        _mock_platform,
        _mock_windows,
        _mock_macos,
    ):
        """Container with multiple render devices -> all working ones detected."""
        gpus = detect_all_gpus()
        assert len(gpus) == 2
        assert gpus[0][1] == "/dev/dri/renderD128"
        assert gpus[1][1] == "/dev/dri/renderD129"
        for _, _, info in gpus:
            assert info["acceleration"] == "VAAPI"

    @patch("plex_generate_previews.gpu_detection.is_macos", return_value=False)
    @patch("plex_generate_previews.gpu_detection.is_windows", return_value=False)
    @patch("platform.system", return_value="Linux")
    @patch("plex_generate_previews.gpu_detection._get_gpu_devices", return_value=[])
    @patch("plex_generate_previews.gpu_detection._is_wsl2", return_value=True)
    @patch(
        "plex_generate_previews.gpu.ffmpeg_capabilities._get_ffmpeg_hwaccels",
        return_value=["cuda"],
    )
    @patch(
        "plex_generate_previews.gpu_detection._detect_nvidia_via_nvidia_smi",
        return_value="UNKNOWN",
    )
    @patch(
        "plex_generate_previews.gpu_detection._scan_dev_dri_render_devices",
        return_value=[],
    )
    def test_wsl2_no_drm_nvidia_smi_fails(
        self,
        _mock_scan,
        _mock_nvidia_smi,
        _mock_hwaccels,
        _mock_wsl2,
        _mock_devices,
        _mock_platform,
        _mock_windows,
        _mock_macos,
    ):
        """WSL2 + CUDA available + nvidia-smi returns UNKNOWN -> no GPU."""
        gpus = detect_all_gpus()
        assert gpus == []

    @patch("plex_generate_previews.gpu_detection.is_macos", return_value=False)
    @patch("plex_generate_previews.gpu_detection.is_windows", return_value=False)
    @patch("platform.system", return_value="Linux")
    @patch("plex_generate_previews.gpu_detection._get_gpu_devices", return_value=[])
    @patch("plex_generate_previews.gpu_detection._is_wsl2", return_value=True)
    @patch(
        "plex_generate_previews.gpu.ffmpeg_capabilities._get_ffmpeg_hwaccels",
        return_value=["cuda"],
    )
    @patch(
        "plex_generate_previews.gpu_detection._detect_nvidia_via_nvidia_smi",
        return_value="NVIDIA",
    )
    @patch(
        "plex_generate_previews.gpu_detection._test_hwaccel_functionality",
        return_value=False,
    )
    @patch(
        "plex_generate_previews.gpu_detection._scan_dev_dri_render_devices",
        return_value=[],
    )
    def test_wsl2_no_drm_cuda_test_fails(
        self,
        _mock_scan,
        _mock_test,
        _mock_nvidia_smi,
        _mock_hwaccels,
        _mock_wsl2,
        _mock_devices,
        _mock_platform,
        _mock_windows,
        _mock_macos,
    ):
        """WSL2 + nvidia-smi confirms + CUDA functional test fails -> no GPU."""
        gpus = detect_all_gpus()
        assert gpus == []


class TestLinuxContainerNvidiaFallback:
    """Test NVIDIA CUDA fallback for Linux containers without DRM render nodes.

    nvidia-container-runtime exposes GPUs via /dev/nvidia* but does NOT mount
    /dev/dri/renderD* nodes. When /sys/class/drm is passed through (common on
    Unraid), _get_gpu_devices() sees card0/card1 but skips them because their
    render nodes are absent. Detection must fall back to probing CUDA directly.
    """

    @patch("plex_generate_previews.gpu_detection.is_macos", return_value=False)
    @patch("plex_generate_previews.gpu_detection.is_windows", return_value=False)
    @patch("platform.system", return_value="Linux")
    @patch("plex_generate_previews.gpu_detection._get_gpu_devices", return_value=[])
    @patch("plex_generate_previews.gpu_detection._is_wsl2", return_value=False)
    @patch(
        "plex_generate_previews.gpu.ffmpeg_capabilities._get_ffmpeg_hwaccels",
        return_value=["cuda", "vaapi"],
    )
    @patch(
        "plex_generate_previews.gpu_detection._detect_nvidia_via_nvidia_smi",
        return_value="NVIDIA",
    )
    @patch(
        "plex_generate_previews.gpu_detection._test_hwaccel_functionality",
        return_value=True,
    )
    @patch(
        "plex_generate_previews.gpu_detection.get_gpu_name",
        return_value="NVIDIA GeForce RTX 3080",
    )
    @patch(
        "plex_generate_previews.gpu_detection._scan_dev_dri_render_devices",
        return_value=[],
    )
    def test_linux_container_cuda_detected(
        self,
        _mock_scan,
        _mock_name,
        _mock_test,
        _mock_nvidia_smi,
        _mock_hwaccels,
        _mock_wsl2,
        _mock_devices,
        _mock_platform,
        _mock_windows,
        _mock_macos,
    ):
        """Non-WSL2 Linux + no physical GPUs + nvidia-smi + CUDA works -> NVIDIA GPU."""
        gpus = detect_all_gpus()

        assert len(gpus) == 1
        gpu_type, gpu_device, gpu_info = gpus[0]
        assert gpu_type == "NVIDIA"
        assert gpu_device == "cuda"
        assert gpu_info["acceleration"] == "CUDA"
        assert gpu_info["render_device"] is None
        assert gpu_info["card"] == "nvidia-container"
        assert gpu_info["driver"] == "nvidia"
        assert gpu_info["status"] == "ok"
        assert "RTX 3080" in gpu_info["name"]

    @patch("plex_generate_previews.gpu_detection.is_macos", return_value=False)
    @patch("plex_generate_previews.gpu_detection.is_windows", return_value=False)
    @patch("platform.system", return_value="Linux")
    @patch("plex_generate_previews.gpu_detection._get_gpu_devices", return_value=[])
    @patch("plex_generate_previews.gpu_detection._is_wsl2", return_value=False)
    @patch(
        "plex_generate_previews.gpu.ffmpeg_capabilities._get_ffmpeg_hwaccels",
        return_value=["cuda"],
    )
    @patch(
        "plex_generate_previews.gpu_detection._detect_nvidia_via_nvidia_smi",
        return_value="UNKNOWN",
    )
    @patch(
        "plex_generate_previews.gpu_detection._scan_dev_dri_render_devices",
        return_value=[],
    )
    def test_linux_container_nvidia_smi_unknown(
        self,
        _mock_scan,
        _mock_nvidia_smi,
        _mock_hwaccels,
        _mock_wsl2,
        _mock_devices,
        _mock_platform,
        _mock_windows,
        _mock_macos,
    ):
        """Non-WSL2 Linux + CUDA available + nvidia-smi returns UNKNOWN -> no GPU."""
        gpus = detect_all_gpus()
        assert gpus == []

    @patch("plex_generate_previews.gpu_detection.is_macos", return_value=False)
    @patch("plex_generate_previews.gpu_detection.is_windows", return_value=False)
    @patch("platform.system", return_value="Linux")
    @patch("plex_generate_previews.gpu_detection._get_gpu_devices", return_value=[])
    @patch("plex_generate_previews.gpu_detection._is_wsl2", return_value=False)
    @patch(
        "plex_generate_previews.gpu.ffmpeg_capabilities._get_ffmpeg_hwaccels",
        return_value=["vaapi"],
    )
    @patch(
        "plex_generate_previews.gpu_detection._detect_nvidia_via_nvidia_smi",
        return_value="NVIDIA",
    )
    @patch(
        "plex_generate_previews.gpu_detection._scan_dev_dri_render_devices",
        return_value=[],
    )
    def test_linux_container_no_cuda_hwaccel(
        self,
        _mock_scan,
        _mock_nvidia_smi,
        _mock_hwaccels,
        _mock_wsl2,
        _mock_devices,
        _mock_platform,
        _mock_windows,
        _mock_macos,
    ):
        """Non-WSL2 Linux + CUDA not compiled into FFmpeg -> NVIDIA fallback skipped, no GPU."""
        gpus = detect_all_gpus()
        assert gpus == []

    @patch("plex_generate_previews.gpu_detection.is_macos", return_value=False)
    @patch("plex_generate_previews.gpu_detection.is_windows", return_value=False)
    @patch("platform.system", return_value="Linux")
    @patch("plex_generate_previews.gpu_detection._get_gpu_devices", return_value=[])
    @patch("plex_generate_previews.gpu_detection._is_wsl2", return_value=False)
    @patch(
        "plex_generate_previews.gpu.ffmpeg_capabilities._get_ffmpeg_hwaccels",
        return_value=["cuda"],
    )
    @patch(
        "plex_generate_previews.gpu_detection._detect_nvidia_via_nvidia_smi",
        return_value="NVIDIA",
    )
    @patch(
        "plex_generate_previews.gpu_detection._test_hwaccel_functionality",
        return_value=False,
    )
    @patch(
        "plex_generate_previews.gpu_detection._scan_dev_dri_render_devices",
        return_value=[],
    )
    def test_linux_container_cuda_test_fails(
        self,
        _mock_scan,
        _mock_test,
        _mock_nvidia_smi,
        _mock_hwaccels,
        _mock_wsl2,
        _mock_devices,
        _mock_platform,
        _mock_windows,
        _mock_macos,
    ):
        """Non-WSL2 Linux + nvidia-smi confirms + CUDA functional test fails -> no GPU.

        This is the scenario where the container has nvidia-smi but is missing the
        'video' driver capability (NVIDIA_DRIVER_CAPABILITIES lacks 'video'), so
        NVDEC/NVENC cannot initialize.
        """
        gpus = detect_all_gpus()
        assert gpus == []


class TestDetectWindowsGPUs:
    """Tests for _detect_windows_gpus().

    Covers the priority order: NVIDIA CUDA first, D3D11VA fallback.
    """

    @patch(
        "plex_generate_previews.gpu_detection._get_ffmpeg_hwaccels",
        return_value=["cuda", "d3d11va"],
    )
    @patch(
        "plex_generate_previews.gpu_detection._detect_nvidia_via_nvidia_smi",
        return_value="NVIDIA",
    )
    @patch(
        "plex_generate_previews.gpu_detection._test_hwaccel_functionality",
        return_value=True,
    )
    @patch(
        "plex_generate_previews.gpu_detection.get_gpu_name",
        return_value="NVIDIA GeForce RTX 5080",
    )
    def test_nvidia_cuda_detected(
        self, _mock_name, _mock_test, _mock_smi, _mock_hwaccels
    ):
        """NVIDIA confirmed by nvidia-smi + CUDA functional test passes -> NVIDIA/cuda GPU."""
        gpus = _detect_windows_gpus()
        assert len(gpus) == 1
        gpu_type, device, info = gpus[0]
        assert gpu_type == "NVIDIA"
        assert device == "cuda"
        assert info["acceleration"] == "CUDA"
        assert "RTX 5080" in info["name"]

    @patch(
        "plex_generate_previews.gpu_detection._get_ffmpeg_hwaccels",
        return_value=["cuda", "d3d11va"],
    )
    @patch(
        "plex_generate_previews.gpu_detection._detect_nvidia_via_nvidia_smi",
        return_value="NVIDIA",
    )
    @patch(
        "plex_generate_previews.gpu_detection._test_hwaccel_functionality",
        return_value=False,
    )
    @patch(
        "plex_generate_previews.gpu_detection._test_acceleration_method",
        return_value=True,
    )
    def test_cuda_test_fails_falls_back_to_d3d11va(
        self, _mock_accel, _mock_test, _mock_smi, _mock_hwaccels
    ):
        """nvidia-smi confirms NVIDIA but CUDA functional test fails -> falls back to D3D11VA."""
        gpus = _detect_windows_gpus()
        assert len(gpus) == 1
        gpu_type, device, info = gpus[0]
        assert gpu_type == "WINDOWS_GPU"
        assert device == "d3d11va"
        assert info["acceleration"] == "D3D11VA"

    @patch(
        "plex_generate_previews.gpu_detection._get_ffmpeg_hwaccels",
        return_value=["cuda", "d3d11va"],
    )
    @patch(
        "plex_generate_previews.gpu_detection._detect_nvidia_via_nvidia_smi",
        return_value="",
    )
    @patch(
        "plex_generate_previews.gpu_detection._test_acceleration_method",
        return_value=True,
    )
    def test_no_nvidia_smi_falls_back_to_d3d11va(
        self, _mock_accel, _mock_smi, _mock_hwaccels
    ):
        """nvidia-smi finds no NVIDIA GPU -> skips CUDA and uses D3D11VA."""
        gpus = _detect_windows_gpus()
        assert len(gpus) == 1
        assert gpus[0][0] == "WINDOWS_GPU"
        assert gpus[0][1] == "d3d11va"

    @patch(
        "plex_generate_previews.gpu_detection._get_ffmpeg_hwaccels",
        return_value=["d3d11va"],
    )
    @patch(
        "plex_generate_previews.gpu_detection._test_acceleration_method",
        return_value=True,
    )
    def test_no_cuda_hwaccel_uses_d3d11va(self, _mock_accel, _mock_hwaccels):
        """CUDA not in FFmpeg hwaccels -> goes straight to D3D11VA."""
        gpus = _detect_windows_gpus()
        assert len(gpus) == 1
        assert gpus[0][0] == "WINDOWS_GPU"
        assert gpus[0][1] == "d3d11va"

    @patch(
        "plex_generate_previews.gpu_detection._get_ffmpeg_hwaccels",
        return_value=[],
    )
    def test_no_hwaccels_returns_empty(self, _mock_hwaccels):
        """No hwaccels available at all -> empty list."""
        gpus = _detect_windows_gpus()
        assert gpus == []

    @patch(
        "plex_generate_previews.gpu_detection._get_ffmpeg_hwaccels",
        return_value=["cuda", "d3d11va"],
    )
    @patch(
        "plex_generate_previews.gpu_detection._detect_nvidia_via_nvidia_smi",
        return_value="NVIDIA",
    )
    @patch(
        "plex_generate_previews.gpu_detection._test_hwaccel_functionality",
        return_value=True,
    )
    @patch(
        "plex_generate_previews.gpu_detection.get_gpu_name",
        return_value="NVIDIA GeForce RTX 5080",
    )
    def test_nvidia_cuda_skips_d3d11va(
        self, _mock_name, _mock_test, _mock_smi, _mock_hwaccels
    ):
        """When CUDA succeeds, D3D11VA is not added (early return)."""
        gpus = _detect_windows_gpus()
        types = [g[0] for g in gpus]
        assert "WINDOWS_GPU" not in types
        assert len(gpus) == 1


class TestProbeVulkanDevice:
    """Unit tests for the libplacebo Vulkan device probe (DV5 green-bug detector)."""

    def setup_method(self):
        from plex_generate_previews.gpu_detection import _reset_vulkan_device_cache

        _reset_vulkan_device_cache()

    @patch(
        "plex_generate_previews.gpu.vulkan_probe._is_hwaccel_available",
        return_value=True,
    )
    @patch("plex_generate_previews.gpu.vulkan_probe.subprocess.run")
    def test_parses_intel_hardware_device(self, mock_run, _mock_vk):
        from plex_generate_previews.gpu_detection import _probe_vulkan_device

        mock_run.return_value = MagicMock(
            returncode=0,
            stderr=(
                "[Vulkan @ 0x7f00] Supported layers:\n"
                "[Vulkan @ 0x7f00] GPU listing:\n"
                "[Vulkan @ 0x7f00]     0: Intel(R) Graphics (RPL-S) (integrated) (0xa780)\n"
                "[Vulkan @ 0x7f00]     1: llvmpipe (LLVM 18.1.3, 256 bits) (software) (0x0)\n"
                "[Vulkan @ 0x7f00] Device 0 selected: Intel(R) Graphics (RPL-S) (integrated) (0xa780)\n"
            ),
        )
        assert (
            _probe_vulkan_device() == "Intel(R) Graphics (RPL-S) (integrated) (0xa780)"
        )

    @patch(
        "plex_generate_previews.gpu.vulkan_probe._is_hwaccel_available",
        return_value=True,
    )
    @patch("plex_generate_previews.gpu.vulkan_probe.subprocess.run")
    def test_parses_llvmpipe_software_device(self, mock_run, _mock_vk):
        from plex_generate_previews.gpu_detection import _probe_vulkan_device

        mock_run.return_value = MagicMock(
            returncode=0,
            stderr=(
                "[Vulkan @ 0x7f00] GPU listing:\n"
                "[Vulkan @ 0x7f00]     0: llvmpipe (LLVM 18.1.3, 256 bits) (software) (0x0)\n"
                "[Vulkan @ 0x7f00] Device 0 selected: llvmpipe (LLVM 18.1.3, 256 bits) (software) (0x0)\n"
            ),
        )
        assert (
            _probe_vulkan_device()
            == "llvmpipe (LLVM 18.1.3, 256 bits) (software) (0x0)"
        )

    @patch(
        "plex_generate_previews.gpu.vulkan_probe._is_hwaccel_available",
        return_value=False,
    )
    def test_no_vulkan_support_returns_none(self, _mock_vk):
        from plex_generate_previews.gpu_detection import _probe_vulkan_device

        # Must not invoke subprocess when Vulkan hwaccel is absent.
        with patch(
            "plex_generate_previews.gpu.vulkan_probe.subprocess.run"
        ) as mock_run:
            assert _probe_vulkan_device() is None
            mock_run.assert_not_called()

    @patch(
        "plex_generate_previews.gpu.vulkan_probe._is_hwaccel_available",
        return_value=True,
    )
    @patch("plex_generate_previews.gpu.vulkan_probe.subprocess.run")
    def test_subprocess_timeout_returns_none(self, mock_run, _mock_vk):
        import subprocess as _sp

        from plex_generate_previews.gpu_detection import _probe_vulkan_device

        mock_run.side_effect = _sp.TimeoutExpired(cmd=["ffmpeg"], timeout=10)
        assert _probe_vulkan_device() is None

    @patch(
        "plex_generate_previews.gpu.vulkan_probe._is_hwaccel_available",
        return_value=True,
    )
    @patch("plex_generate_previews.gpu.vulkan_probe.subprocess.run")
    def test_stderr_without_device_line_returns_none(self, mock_run, _mock_vk):
        from plex_generate_previews.gpu_detection import _probe_vulkan_device

        mock_run.return_value = MagicMock(
            returncode=1,
            stderr="ffmpeg error: no Vulkan driver available\n",
        )
        assert _probe_vulkan_device() is None

    # --- Layer 3 multi-strategy probe tests ---------------------------------

    @staticmethod
    def _stderr_with_device(device_name: str) -> str:
        return f"[Vulkan @ 0x7f00] Device 0 selected: {device_name}\n"

    @patch(
        "plex_generate_previews.gpu.vulkan_probe._is_hwaccel_available",
        return_value=True,
    )
    @patch("plex_generate_previews.gpu.vulkan_probe._find_nvidia_egl_vendor_json")
    @patch("plex_generate_previews.gpu.vulkan_probe._find_nvidia_icd_json")
    @patch("plex_generate_previews.gpu.vulkan_probe.subprocess.run")
    def test_strategy_2_egl_vendor_override_succeeds(
        self, mock_run, mock_find_icd, mock_find_egl, _mock_vk
    ):
        """Strategy 1 returns llvmpipe, Strategy 2 forces
        __EGL_VENDOR_LIBRARY_FILENAMES → subprocess picks up NVIDIA →
        env override is cached and the NVIDIA device is returned. The
        VK_DRIVER_FILES fallback (Strategy 2b) does NOT fire.
        """
        from plex_generate_previews.gpu_detection import (
            _probe_vulkan_device,
            get_vulkan_env_overrides,
        )

        mock_find_egl.return_value = "/usr/share/glvnd/egl_vendor.d/10_nvidia.json"
        mock_find_icd.return_value = "/etc/vulkan/icd.d/nvidia_icd.json"
        mock_run.side_effect = [
            MagicMock(
                returncode=0,
                stderr=self._stderr_with_device(
                    "llvmpipe (LLVM 18.1.3, 256 bits) (software) (0x0)"
                ),
            ),
            MagicMock(
                returncode=0,
                stderr=self._stderr_with_device("NVIDIA TITAN RTX (discrete) (0x1e02)"),
            ),
        ]

        device = _probe_vulkan_device()
        assert device == "NVIDIA TITAN RTX (discrete) (0x1e02)"
        # Two probes fired: Strategy 1 + Strategy 2. Strategy 2b and 3
        # did NOT fire because Strategy 2 succeeded.
        assert mock_run.call_count == 2
        strategy_2_call = mock_run.call_args_list[1]
        env_arg = strategy_2_call.kwargs.get("env") or {}
        assert (
            env_arg.get("__EGL_VENDOR_LIBRARY_FILENAMES")
            == "/usr/share/glvnd/egl_vendor.d/10_nvidia.json"
        )
        # Strategy 2 does NOT set VK_DRIVER_FILES — it's a gentler fix
        # that still lets the loader enumerate other ICDs for Mesa
        # fallback on dual-GPU hosts.
        assert "VK_DRIVER_FILES" not in env_arg
        assert get_vulkan_env_overrides() == {
            "__EGL_VENDOR_LIBRARY_FILENAMES": (
                "/usr/share/glvnd/egl_vendor.d/10_nvidia.json"
            )
        }

    @patch(
        "plex_generate_previews.gpu.vulkan_probe._is_hwaccel_available",
        return_value=True,
    )
    @patch("plex_generate_previews.gpu.vulkan_probe._find_nvidia_egl_vendor_json")
    @patch("plex_generate_previews.gpu.vulkan_probe._find_nvidia_icd_json")
    @patch("plex_generate_previews.gpu.vulkan_probe.subprocess.run")
    def test_strategy_2b_vk_driver_files_fallback_when_egl_retry_fails(
        self, mock_run, mock_find_icd, mock_find_egl, _mock_vk
    ):
        """Strategy 1 fails, Strategy 2 (EGL vendor override) also
        fails, Strategy 2b (VK_DRIVER_FILES + EGL) succeeds → cached
        env override carries BOTH keys."""
        from plex_generate_previews.gpu_detection import (
            _probe_vulkan_device,
            get_vulkan_env_overrides,
        )

        mock_find_egl.return_value = "/usr/share/glvnd/egl_vendor.d/10_nvidia.json"
        mock_find_icd.return_value = "/etc/vulkan/icd.d/nvidia_icd.json"
        mock_run.side_effect = [
            # Strategy 1: llvmpipe
            MagicMock(
                returncode=0,
                stderr=self._stderr_with_device("llvmpipe (software) (0x0)"),
            ),
            # Strategy 2: still llvmpipe
            MagicMock(
                returncode=0,
                stderr=self._stderr_with_device("llvmpipe (software) (0x0)"),
            ),
            # Strategy 2b: success
            MagicMock(
                returncode=0,
                stderr=self._stderr_with_device("NVIDIA GeForce RTX 4090 (discrete)"),
            ),
        ]

        device = _probe_vulkan_device()
        assert device == "NVIDIA GeForce RTX 4090 (discrete)"
        assert mock_run.call_count == 3
        strategy_2b_call = mock_run.call_args_list[2]
        env_arg = strategy_2b_call.kwargs.get("env") or {}
        assert env_arg.get("VK_DRIVER_FILES") == "/etc/vulkan/icd.d/nvidia_icd.json"
        assert (
            env_arg.get("__EGL_VENDOR_LIBRARY_FILENAMES")
            == "/usr/share/glvnd/egl_vendor.d/10_nvidia.json"
        )
        assert get_vulkan_env_overrides() == {
            "VK_DRIVER_FILES": "/etc/vulkan/icd.d/nvidia_icd.json",
            "__EGL_VENDOR_LIBRARY_FILENAMES": (
                "/usr/share/glvnd/egl_vendor.d/10_nvidia.json"
            ),
        }

    @patch(
        "plex_generate_previews.gpu.vulkan_probe._is_hwaccel_available",
        return_value=True,
    )
    @patch("plex_generate_previews.gpu.vulkan_probe._find_libegl_nvidia")
    @patch("plex_generate_previews.gpu.vulkan_probe._find_nvidia_egl_vendor_json")
    @patch("plex_generate_previews.gpu.vulkan_probe._find_nvidia_icd_json")
    @patch("plex_generate_previews.gpu.vulkan_probe.subprocess.run")
    def test_strategy_2c_synthesises_vendor_json_when_missing_but_libegl_present(
        self,
        mock_run,
        mock_find_icd,
        mock_find_egl,
        mock_find_libegl,
        _mock_vk,
        tmp_path,
    ):
        """Strategy 2c: no GLVND vendor JSON on disk but libEGL_nvidia.so.0
        IS present → synthesise a JSON into a tempfile, point
        ``__EGL_VENDOR_LIBRARY_FILENAMES`` at it, re-probe, and cache
        the env override on success.

        This is the in-container fix for users whose
        ``nvidia-container-toolkit`` mounts the NVIDIA libraries but
        omits the three-line GLVND vendor config that routes libEGL
        lookups at NVIDIA.  Per NVIDIA's own minimum Dockerfile
        guidance (forums.developer.nvidia.com thread 242883), a bare
        ``libEGL_nvidia.so.0`` ``library_path`` is sufficient.
        """
        from plex_generate_previews.gpu_detection import (
            _probe_vulkan_device,
            get_vulkan_env_overrides,
        )

        mock_find_egl.return_value = None  # no GLVND vendor JSON on disk
        mock_find_icd.return_value = "/etc/vulkan/icd.d/nvidia_icd.json"
        mock_find_libegl.return_value = (
            "/usr/lib/x86_64-linux-gnu/libEGL_nvidia.so.580.119.02"
        )

        mock_run.side_effect = [
            # Strategy 1: llvmpipe (no vendor JSON on disk → NVIDIA ICD
            # init fails internally and loader falls back to llvmpipe)
            MagicMock(
                returncode=0,
                stderr=self._stderr_with_device("llvmpipe (software) (0x0)"),
            ),
            # Strategy 2c: probe with the synthesised vendor JSON →
            # NVIDIA ICD init succeeds and the loader returns the real
            # Quadro.
            MagicMock(
                returncode=0,
                stderr=self._stderr_with_device("Quadro P4000 (discrete) (0x1bb1)"),
            ),
        ]

        # Point the tempfile root at pytest's tmp_path for cleanliness.
        with patch(
            "plex_generate_previews.gpu.vulkan_probe.tempfile.gettempdir",
            return_value=str(tmp_path),
        ):
            device = _probe_vulkan_device()

        assert device == "Quadro P4000 (discrete) (0x1bb1)"

        # Strategy 1 ran, Strategy 2 was skipped (no vendor on disk),
        # Strategy 2c synthesised + re-probed.  Total subprocess runs: 2.
        assert mock_run.call_count == 2

        # Synthesised JSON must exist and have the exact three-line
        # payload NVIDIA's own minimum Dockerfile uses.
        synth_path = tmp_path / "plex_previews_nvidia_egl_vendor.json"
        assert synth_path.exists()
        payload = json.loads(synth_path.read_text())
        assert payload == {
            "file_format_version": "1.0.0",
            "ICD": {"library_path": "libEGL_nvidia.so.0"},
        }

        # Strategy 2c probe was invoked with the synthesised path as
        # __EGL_VENDOR_LIBRARY_FILENAMES.
        strategy_2c_call = mock_run.call_args_list[1]
        env_arg = strategy_2c_call.kwargs.get("env") or {}
        assert env_arg.get("__EGL_VENDOR_LIBRARY_FILENAMES") == str(synth_path)
        assert "VK_DRIVER_FILES" not in env_arg

        # Env override is cached so ``get_vulkan_env_overrides()``
        # propagates it into the real FFmpeg subprocess on the
        # libplacebo DV5 path.
        assert get_vulkan_env_overrides() == {
            "__EGL_VENDOR_LIBRARY_FILENAMES": str(synth_path),
        }

    @patch(
        "plex_generate_previews.gpu.vulkan_probe._is_hwaccel_available",
        return_value=True,
    )
    @patch("plex_generate_previews.gpu.vulkan_probe._find_libegl_nvidia")
    @patch("plex_generate_previews.gpu.vulkan_probe._find_nvidia_egl_vendor_json")
    @patch("plex_generate_previews.gpu.vulkan_probe._find_nvidia_icd_json")
    @patch("plex_generate_previews.gpu.vulkan_probe.subprocess.run")
    def test_strategy_2c_skipped_when_libegl_nvidia_missing(
        self, mock_run, mock_find_icd, mock_find_egl, mock_find_libegl, _mock_vk
    ):
        """Strategy 2c must not fabricate a vendor JSON when
        ``libEGL_nvidia.so.0`` is absent — the synthesised file would
        point at a non-existent library.  Falls through to Strategy 2b.
        """
        from plex_generate_previews.gpu_detection import _probe_vulkan_device

        mock_find_egl.return_value = None
        mock_find_icd.return_value = "/etc/vulkan/icd.d/nvidia_icd.json"
        mock_find_libegl.return_value = None  # the critical gate

        mock_run.side_effect = [
            # Strategy 1: llvmpipe
            MagicMock(
                returncode=0,
                stderr=self._stderr_with_device("llvmpipe (software) (0x0)"),
            ),
            # Strategy 2b (VK_DRIVER_FILES): also fails
            MagicMock(
                returncode=0,
                stderr=self._stderr_with_device("llvmpipe (software) (0x0)"),
            ),
            # Strategy 3: diagnostic capture
            MagicMock(
                returncode=0,
                stderr="[Vulkan Loader] Diagnostic capture\n"
                + self._stderr_with_device("llvmpipe (software) (0x0)"),
            ),
        ]

        device = _probe_vulkan_device()
        assert device == "llvmpipe (software) (0x0)"
        # Strategy 2 skipped (no vendor JSON), Strategy 2c skipped (no
        # libEGL_nvidia target), Strategy 2b runs, Strategy 3 captures.
        # Total: Strategy 1 + 2b + 3 = 3 calls.
        assert mock_run.call_count == 3

    @patch(
        "plex_generate_previews.gpu.vulkan_probe._is_hwaccel_available",
        return_value=True,
    )
    @patch("plex_generate_previews.gpu.vulkan_probe._find_libegl_nvidia")
    @patch("plex_generate_previews.gpu.vulkan_probe._find_nvidia_egl_vendor_json")
    @patch("plex_generate_previews.gpu.vulkan_probe._find_nvidia_icd_json")
    @patch("plex_generate_previews.gpu.vulkan_probe.subprocess.run")
    def test_strategy_2c_skipped_when_vendor_json_already_present(
        self, mock_run, mock_find_icd, mock_find_egl, mock_find_libegl, _mock_vk
    ):
        """Strategy 2c must not synthesise a file when the real GLVND
        vendor JSON is already on disk — Strategy 2 handles that case.
        """
        from plex_generate_previews.gpu_detection import _probe_vulkan_device

        mock_find_egl.return_value = "/usr/share/glvnd/egl_vendor.d/10_nvidia.json"
        mock_find_icd.return_value = "/etc/vulkan/icd.d/nvidia_icd.json"
        mock_find_libegl.return_value = "/usr/lib/x86_64-linux-gnu/libEGL_nvidia.so.0"

        mock_run.side_effect = [
            MagicMock(
                returncode=0,
                stderr=self._stderr_with_device("llvmpipe (software) (0x0)"),
            ),
            MagicMock(
                returncode=0,
                stderr=self._stderr_with_device("NVIDIA RTX A4000 (discrete)"),
            ),
        ]

        device = _probe_vulkan_device()
        assert device == "NVIDIA RTX A4000 (discrete)"
        # Strategy 1 + Strategy 2 (real vendor JSON succeeds) = 2 calls.
        # Strategy 2c is gated on ``nvidia_egl_vendor is None`` and
        # must not invoke ``_find_libegl_nvidia``.
        assert mock_run.call_count == 2
        mock_find_libegl.assert_not_called()

    @patch(
        "plex_generate_previews.gpu.vulkan_probe._is_hwaccel_available",
        return_value=True,
    )
    @patch("plex_generate_previews.gpu.vulkan_probe._find_libegl_nvidia")
    @patch("plex_generate_previews.gpu.vulkan_probe._find_nvidia_egl_vendor_json")
    @patch("plex_generate_previews.gpu.vulkan_probe._find_nvidia_icd_json")
    @patch("plex_generate_previews.gpu.vulkan_probe.subprocess.run")
    def test_all_retries_skipped_when_nothing_to_retry(
        self, mock_run, mock_find_icd, mock_find_egl, mock_find_libegl, _mock_vk
    ):
        """No NVIDIA ICD JSON, no EGL vendor JSON, no libEGL_nvidia →
        Strategies 2, 2c, and 2b are all skipped → Strategy 3 runs a
        VK_LOADER_DEBUG=all capture → only two subprocess calls
        (Strategy 1 + Strategy 3).
        """
        from plex_generate_previews.gpu_detection import (
            _probe_vulkan_device,
            get_vulkan_env_overrides,
        )

        mock_find_egl.return_value = None
        mock_find_icd.return_value = None
        # Strategy 2c gate: no libEGL_nvidia on the host means
        # synthesising a vendor JSON would point at a non-existent
        # library, so 2c correctly no-ops.
        mock_find_libegl.return_value = None
        mock_run.side_effect = [
            MagicMock(
                returncode=0,
                stderr=self._stderr_with_device("llvmpipe (software) (0x0)"),
            ),
            MagicMock(
                returncode=0,
                stderr=(
                    "[Vulkan Loader] Searching ICDs in /etc/vulkan/icd.d/\n"
                    "[Vulkan Loader] No ICDs found\n"
                    + self._stderr_with_device("llvmpipe (software) (0x0)")
                ),
            ),
        ]

        device = _probe_vulkan_device()
        assert device == "llvmpipe (software) (0x0)"
        assert mock_run.call_count == 2
        assert get_vulkan_env_overrides() == {}

    @patch(
        "plex_generate_previews.gpu.vulkan_probe._is_hwaccel_available",
        return_value=True,
    )
    @patch("plex_generate_previews.gpu.vulkan_probe._find_nvidia_egl_vendor_json")
    @patch("plex_generate_previews.gpu.vulkan_probe._find_nvidia_icd_json")
    @patch("plex_generate_previews.gpu.vulkan_probe.subprocess.run")
    def test_strategy_3_diagnostic_capture_populates_debug_buffer(
        self, mock_run, mock_find_icd, mock_find_egl, _mock_vk
    ):
        """Strategy 1, 2, and 2b all fail → Strategy 3 captures the
        VK_LOADER_DEBUG=all stderr into the module-level debug buffer
        that ``/api/system/vulkan/debug`` exposes.
        """
        # Use the public entry point `get_vulkan_device_info` so the
        # cache flag is set correctly and the auto-trigger in
        # `get_vulkan_debug_buffer` does not re-probe.
        from plex_generate_previews.gpu_detection import (
            get_vulkan_debug_buffer,
            get_vulkan_device_info,
        )

        mock_find_egl.return_value = "/usr/share/glvnd/egl_vendor.d/10_nvidia.json"
        mock_find_icd.return_value = "/etc/vulkan/icd.d/nvidia_icd.json"
        diagnostic_stderr = (
            "[Vulkan Loader] VK_LOADER_DEBUG=all\n"
            "[Vulkan Loader] Scanning /etc/vulkan/icd.d/nvidia_icd.json\n"
            "[Vulkan Loader] ERROR: libnvidia-glvkspirv.so: cannot open shared object file\n"
            "[Vulkan Loader] Skipping ICD\n"
            + self._stderr_with_device("llvmpipe (software) (0x0)")
        )
        mock_run.side_effect = [
            # Strategy 1: software
            MagicMock(
                returncode=0,
                stderr=self._stderr_with_device("llvmpipe (software) (0x0)"),
            ),
            # Strategy 2: still software
            MagicMock(
                returncode=0,
                stderr=self._stderr_with_device("llvmpipe (software) (0x0)"),
            ),
            # Strategy 2b: still software
            MagicMock(
                returncode=0,
                stderr=self._stderr_with_device("llvmpipe (software) (0x0)"),
            ),
            # Strategy 3: diagnostic capture
            MagicMock(returncode=0, stderr=diagnostic_stderr),
        ]

        get_vulkan_device_info()
        assert mock_run.call_count == 4
        strategy_3_call = mock_run.call_args_list[3]
        env_arg = strategy_3_call.kwargs.get("env") or {}
        assert env_arg.get("VK_LOADER_DEBUG") == "all"
        assert env_arg.get("VK_DRIVER_FILES") == "/etc/vulkan/icd.d/nvidia_icd.json"
        assert (
            env_arg.get("__EGL_VENDOR_LIBRARY_FILENAMES")
            == "/usr/share/glvnd/egl_vendor.d/10_nvidia.json"
        )
        buf = get_vulkan_debug_buffer()
        assert "libnvidia-glvkspirv.so: cannot open" in buf
        assert "VK_LOADER_DEBUG=all" in buf

    @patch(
        "plex_generate_previews.gpu.vulkan_probe._is_hwaccel_available",
        return_value=True,
    )
    @patch("plex_generate_previews.gpu.vulkan_probe._find_nvidia_egl_vendor_json")
    @patch("plex_generate_previews.gpu.vulkan_probe._find_nvidia_icd_json")
    @patch("plex_generate_previews.gpu.vulkan_probe.subprocess.run")
    def test_get_vulkan_env_overrides_auto_triggers_probe(
        self, mock_run, mock_find_icd, mock_find_egl, _mock_vk
    ):
        """Calling get_vulkan_env_overrides() before the probe has run
        must trigger the probe synchronously so that workers on the
        libplacebo DV5 path don't see an empty override dict just
        because no HTTP endpoint has warmed the cache yet.

        This is the regression guard for the startup-timing bug where
        the Plex scheduler revived a pending job before the first
        /api/system/vulkan poll fired.
        """
        from plex_generate_previews.gpu.vulkan_probe import _VULKAN_DEVICE_PROBED
        from plex_generate_previews.gpu_detection import get_vulkan_env_overrides

        # Sanity: the setup_method reset made _VULKAN_DEVICE_PROBED False.
        assert _VULKAN_DEVICE_PROBED is False

        mock_find_egl.return_value = "/usr/share/glvnd/egl_vendor.d/10_nvidia.json"
        mock_find_icd.return_value = "/etc/vulkan/icd.d/nvidia_icd.json"
        mock_run.side_effect = [
            # Strategy 1: llvmpipe
            MagicMock(
                returncode=0,
                stderr=self._stderr_with_device("llvmpipe (software) (0x0)"),
            ),
            # Strategy 2: NVIDIA via EGL override
            MagicMock(
                returncode=0,
                stderr=self._stderr_with_device("NVIDIA TITAN RTX (discrete)"),
            ),
        ]

        # First call: no probe has run. The accessor must trigger one.
        overrides = get_vulkan_env_overrides()
        assert overrides == {
            "__EGL_VENDOR_LIBRARY_FILENAMES": (
                "/usr/share/glvnd/egl_vendor.d/10_nvidia.json"
            )
        }
        assert mock_run.call_count == 2  # Strategy 1 + Strategy 2

        # Second call: cached. Must NOT re-probe.
        overrides_again = get_vulkan_env_overrides()
        assert overrides_again == overrides
        assert mock_run.call_count == 2  # unchanged

    @patch(
        "plex_generate_previews.gpu.vulkan_probe._is_hwaccel_available",
        return_value=True,
    )
    @patch("plex_generate_previews.gpu.vulkan_probe._find_nvidia_egl_vendor_json")
    @patch("plex_generate_previews.gpu.vulkan_probe._find_nvidia_icd_json")
    @patch("plex_generate_previews.gpu.vulkan_probe.subprocess.run")
    def test_strategy_1_success_does_not_touch_debug_buffer_or_overrides(
        self, mock_run, mock_find_icd, mock_find_egl, _mock_vk
    ):
        """Happy path: Strategy 1 returns a real hardware device → no
        retries, no diagnostic capture, no env override."""
        from plex_generate_previews.gpu_detection import (
            _probe_vulkan_device,
            get_vulkan_debug_buffer,
            get_vulkan_env_overrides,
        )

        mock_run.return_value = MagicMock(
            returncode=0,
            stderr=self._stderr_with_device(
                "NVIDIA GeForce RTX 4090 (discrete) (0x2684)"
            ),
        )

        device = _probe_vulkan_device()
        assert device == "NVIDIA GeForce RTX 4090 (discrete) (0x2684)"
        assert mock_run.call_count == 1
        # File-discovery helpers must NOT be called on the happy path.
        mock_find_egl.assert_not_called()
        mock_find_icd.assert_not_called()
        assert get_vulkan_env_overrides() == {}
        assert get_vulkan_debug_buffer() == ""

    @patch(
        "plex_generate_previews.gpu.vulkan_probe._is_hwaccel_available",
        return_value=True,
    )
    @patch("plex_generate_previews.gpu.vulkan_probe._find_nvidia_egl_vendor_json")
    @patch("plex_generate_previews.gpu.vulkan_probe._find_nvidia_icd_json")
    @patch("plex_generate_previews.gpu.vulkan_probe.subprocess.run")
    def test_strategy_1_intel_with_nvidia_icd_falls_through_to_retries(
        self, mock_run, mock_find_icd, mock_find_egl, _mock_vk
    ):
        """Dual-GPU host (Intel iGPU + NVIDIA dGPU) under --runtime=nvidia:
        strategy 1 picks Intel ANV by default, but NVIDIA ICD is present on
        disk.  The probe must fall through to strategy 2/2b rather than
        accepting Intel, otherwise NVIDIA-worker DV5 jobs run libplacebo
        on the Intel iGPU (cross-GPU shuffle, ~40% speed hit, steals Intel
        worker cycles).
        """
        from plex_generate_previews.gpu_detection import (
            _probe_vulkan_device,
            get_vulkan_env_overrides,
        )

        mock_find_egl.return_value = "/usr/share/glvnd/egl_vendor.d/10_nvidia.json"
        mock_find_icd.return_value = "/etc/vulkan/icd.d/nvidia_icd.json"
        mock_run.side_effect = [
            # Strategy 1: Intel ANV (hardware, non-NVIDIA)
            MagicMock(
                returncode=0,
                stderr=self._stderr_with_device(
                    "Intel(R) Graphics (RPL-S) (integrated) (0xa780)"
                ),
            ),
            # Strategy 2: __EGL_VENDOR_LIBRARY_FILENAMES — still Intel
            MagicMock(
                returncode=0,
                stderr=self._stderr_with_device(
                    "Intel(R) Graphics (RPL-S) (integrated) (0xa780)"
                ),
            ),
            # Strategy 2b: VK_DRIVER_FILES + EGL together → NVIDIA wins
            MagicMock(
                returncode=0,
                stderr=self._stderr_with_device("NVIDIA TITAN RTX (discrete) (0x1e02)"),
            ),
        ]

        device = _probe_vulkan_device()
        assert device == "NVIDIA TITAN RTX (discrete) (0x1e02)"
        # Strategies 1, 2, and 2b all fired (3 probes).
        assert mock_run.call_count == 3
        # Final env overrides must carry BOTH keys — this is the combo
        # that actually works on the user's container; EGL alone returns
        # Intel, VK_DRIVER_FILES alone returns VK_ERROR_INCOMPATIBLE_DRIVER.
        assert get_vulkan_env_overrides() == {
            "VK_DRIVER_FILES": "/etc/vulkan/icd.d/nvidia_icd.json",
            "__EGL_VENDOR_LIBRARY_FILENAMES": (
                "/usr/share/glvnd/egl_vendor.d/10_nvidia.json"
            ),
        }

    @patch(
        "plex_generate_previews.gpu.vulkan_probe._is_hwaccel_available",
        return_value=True,
    )
    @patch("plex_generate_previews.gpu.vulkan_probe._find_nvidia_egl_vendor_json")
    @patch("plex_generate_previews.gpu.vulkan_probe._find_nvidia_icd_json")
    @patch("plex_generate_previews.gpu.vulkan_probe.subprocess.run")
    def test_strategy_1_intel_without_nvidia_icd_accepts_intel(
        self, mock_run, mock_find_icd, mock_find_egl, _mock_vk
    ):
        """Intel-only host (no NVIDIA anywhere): strategy 1 returns Intel
        and short-circuits.  NVIDIA-specific retries must NOT fire — the
        user has no NVIDIA GPU to route to."""
        from plex_generate_previews.gpu_detection import (
            _probe_vulkan_device,
            get_vulkan_env_overrides,
        )

        mock_find_icd.return_value = None  # no NVIDIA ICD on this host
        mock_run.return_value = MagicMock(
            returncode=0,
            stderr=self._stderr_with_device(
                "Intel(R) Graphics (RPL-S) (integrated) (0xa780)"
            ),
        )

        device = _probe_vulkan_device()
        assert device == "Intel(R) Graphics (RPL-S) (integrated) (0xa780)"
        assert mock_run.call_count == 1
        # The EGL helper should NOT be called — we short-circuit once we
        # know there's no NVIDIA ICD to route to.
        mock_find_egl.assert_not_called()
        assert get_vulkan_env_overrides() == {}


class TestGetVulkanDeviceInfo:
    """Unit tests for get_vulkan_device_info() — the cached info builder."""

    def setup_method(self):
        from plex_generate_previews.gpu_detection import _reset_vulkan_device_cache

        _reset_vulkan_device_cache()

    @patch("plex_generate_previews.gpu.vulkan_probe._probe_vulkan_device")
    def test_intel_hardware_is_not_software(self, mock_probe):
        from plex_generate_previews.gpu_detection import get_vulkan_device_info

        mock_probe.return_value = "Intel(R) Graphics (RPL-S) (integrated) (0xa780)"
        info = get_vulkan_device_info()
        assert info.device.startswith("Intel(R) Graphics")
        assert info.is_software is False

    @patch("plex_generate_previews.gpu.vulkan_probe._probe_vulkan_device")
    def test_llvmpipe_is_software(self, mock_probe):
        from plex_generate_previews.gpu_detection import get_vulkan_device_info

        mock_probe.return_value = "llvmpipe (LLVM 18.1.3, 256 bits) (software) (0x0)"
        info = get_vulkan_device_info()
        assert "llvmpipe" in info.device
        assert info.is_software is True

    @patch("plex_generate_previews.gpu.vulkan_probe._probe_vulkan_device")
    def test_lavapipe_is_software(self, mock_probe):
        from plex_generate_previews.gpu_detection import get_vulkan_device_info

        mock_probe.return_value = "lavapipe (whatever) (software)"
        info = get_vulkan_device_info()
        assert info.is_software is True

    @patch("plex_generate_previews.gpu.vulkan_probe._probe_vulkan_device")
    def test_none_device_not_software(self, mock_probe):
        from plex_generate_previews.gpu_detection import get_vulkan_device_info

        mock_probe.return_value = None
        info = get_vulkan_device_info()
        assert info.device is None
        assert info.is_software is False

    @patch("plex_generate_previews.gpu.vulkan_probe._probe_vulkan_device")
    def test_probe_is_called_once_and_cached(self, mock_probe):
        from plex_generate_previews.gpu_detection import get_vulkan_device_info

        mock_probe.return_value = "Intel(R) Graphics"
        get_vulkan_device_info()
        get_vulkan_device_info()
        get_vulkan_device_info()
        assert mock_probe.call_count == 1


class TestGetVulkanInfoAPI:
    """Unit tests for _get_vulkan_info() in api_system (the HTML warning builder)."""

    def setup_method(self):
        from plex_generate_previews.gpu_detection import _reset_vulkan_device_cache
        from plex_generate_previews.web.routes._helpers import _gpu_cache

        _reset_vulkan_device_cache()
        # Force-populate the shared GPU cache with an empty list so
        # _ensure_gpu_cache() inside _get_vulkan_info() skips real
        # hardware detection.  Individual tests override this via
        # _set_gpus() to exercise specific branches.
        _gpu_cache["result"] = []

    def teardown_method(self):
        from plex_generate_previews.web.routes._helpers import _gpu_cache

        # Leave the cache in the default "not yet detected" state so
        # unrelated tests that follow behave like a fresh process.
        _gpu_cache["result"] = None

    @staticmethod
    def _set_gpus(gpus):
        from plex_generate_previews.web.routes._helpers import _gpu_cache

        _gpu_cache["result"] = gpus

    @patch("plex_generate_previews.gpu.vulkan_probe._probe_vulkan_device")
    def test_hardware_device_returns_no_warning(self, mock_probe):
        from plex_generate_previews.web.routes.api_system import _get_vulkan_info

        mock_probe.return_value = "Intel(R) Graphics (RPL-S)"
        info = _get_vulkan_info()
        assert info["device"].startswith("Intel(R) Graphics")
        assert "warning" not in info

    @patch("plex_generate_previews.gpu.vulkan_probe._probe_vulkan_device")
    def test_llvmpipe_returns_warning_with_fix_instructions(self, mock_probe):
        from plex_generate_previews.web.routes.api_system import _get_vulkan_info

        mock_probe.return_value = "llvmpipe (software)"
        # setup_method sets an empty GPU cache → Case E (no GPU detected);
        # that branch still contains the shared-header plain-English
        # summary and the generic /dev/dri forwarding hint.
        info = _get_vulkan_info()
        assert "warning" in info
        warning = info["warning"]
        assert "Dolby Vision Profile 5" in warning
        assert "green" in warning  # "green rectangle" / "green overlay"
        assert "software rendering" in warning
        assert "/dev/dri" in warning

    @patch("plex_generate_previews.gpu.vulkan_probe._probe_vulkan_device")
    def test_no_vulkan_returns_no_warning(self, mock_probe):
        from plex_generate_previews.web.routes.api_system import _get_vulkan_info

        mock_probe.return_value = None
        info = _get_vulkan_info()
        assert info["device"] is None
        assert "warning" not in info

    # --- GPU-aware branches -------------------------------------------------

    # --- Case A (pure NVIDIA) diagnostic dispatch --------------------------
    #
    # Case A now splits into four sub-cases based on what
    # _diagnose_vulkan_environment() returns. Each test mocks the
    # diagnostic helper to target one sub-case deterministically.

    @staticmethod
    def _diag_fixture(
        *,
        has_graphics: bool = True,
        icd_path: str | None = "/etc/vulkan/icd.d/nvidia_icd.json",
        glvkspirv: bool = True,
        libegl_nvidia: bool = True,
        egl_vendor_json_path: str | None = None,
        drm_loaded: bool = True,
        driver_version: str | None = "580.0.0",
    ) -> dict:
        """Build a _diagnose_vulkan_environment() return-value fixture."""
        return {
            "nvidia_capabilities": "all" if has_graphics else "compute,video,utility",
            "nvidia_capabilities_has_graphics": has_graphics,
            "nvidia_icd_json_path": icd_path,
            "libnvidia_glvkspirv_found": glvkspirv,
            "libegl_nvidia_found": libegl_nvidia,
            "nvidia_egl_vendor_json_path": egl_vendor_json_path,
            "nvidia_drm_loaded": drm_loaded,
            "nvidia_driver_version": driver_version,
        }

    @patch("plex_generate_previews.web.routes.api_system._diagnose_vulkan_environment")
    @patch("plex_generate_previews.web.routes.api_system.glob.glob")
    @patch("plex_generate_previews.gpu.vulkan_probe._probe_vulkan_device")
    def test_case_a1_missing_graphics_capability(
        self, mock_probe, mock_glob, mock_diag
    ):
        """Case A1: NVIDIA-only + NVIDIA_DRIVER_CAPABILITIES missing 'graphics'
        → warning names the specific env var and gives the fix.
        """
        from plex_generate_previews.web.routes.api_system import _get_vulkan_info

        mock_probe.return_value = "llvmpipe (software)"
        mock_glob.return_value = []
        mock_diag.return_value = self._diag_fixture(has_graphics=False)
        self._set_gpus(
            [
                {
                    "type": "NVIDIA",
                    "device": "/dev/nvidia0",
                    "name": "NVIDIA GeForce RTX 3080",
                }
            ]
        )

        warning = _get_vulkan_info()["warning"]
        assert "NVIDIA GeForce RTX 3080" in warning
        # Case A1 specifically names the missing capability and the fix.
        assert "graphics" in warning
        assert "NVIDIA_DRIVER_CAPABILITIES" in warning
        assert "NVIDIA_DRIVER_CAPABILITIES=all" in warning
        # Must NOT trigger the other sub-cases.
        assert "VK_ERROR_INCOMPATIBLE_DRIVER" not in warning
        assert "nvidia-container-toolkit#1559" not in warning
        assert "already forwarded" not in warning

    @patch("plex_generate_previews.web.routes.api_system._diagnose_vulkan_environment")
    @patch("plex_generate_previews.web.routes.api_system.glob.glob")
    @patch("plex_generate_previews.gpu.vulkan_probe._probe_vulkan_device")
    def test_case_a2_graphics_set_but_icd_json_missing(
        self, mock_probe, mock_glob, mock_diag
    ):
        """Case A2: 'graphics' capability is set but the NVIDIA ICD JSON
        is missing → blame the driver 570–579 regression or CDI mode.
        """
        from plex_generate_previews.web.routes.api_system import _get_vulkan_info

        mock_probe.return_value = "llvmpipe (software)"
        mock_glob.return_value = []
        mock_diag.return_value = self._diag_fixture(
            has_graphics=True,
            icd_path=None,
            driver_version="572.56",
        )
        self._set_gpus(
            [
                {
                    "type": "NVIDIA",
                    "device": "/dev/nvidia0",
                    "name": "NVIDIA TITAN RTX",
                }
            ]
        )

        warning = _get_vulkan_info()["warning"]
        assert "NVIDIA TITAN RTX" in warning
        # Case A2 cites the specific driver regression and the issue link.
        assert "nvidia-container-toolkit#1041" in warning
        assert "570" in warning
        assert "572.56" in warning  # echoes the detected driver version back

    @patch("plex_generate_previews.web.routes.api_system._diagnose_vulkan_environment")
    @patch("plex_generate_previews.web.routes.api_system.glob.glob")
    @patch("plex_generate_previews.gpu.vulkan_probe._probe_vulkan_device")
    def test_case_a3_icd_present_but_libnvidia_glvkspirv_missing(
        self, mock_probe, mock_glob, mock_diag
    ):
        """Case A3: ICD JSON is present but libnvidia-glvkspirv.so is not
        → blame the CDI manifest bug and point at legacy-mode workaround.
        """
        from plex_generate_previews.web.routes.api_system import _get_vulkan_info

        mock_probe.return_value = "llvmpipe (software)"
        mock_glob.return_value = []
        mock_diag.return_value = self._diag_fixture(
            has_graphics=True,
            icd_path="/etc/vulkan/icd.d/nvidia_icd.json",
            glvkspirv=False,
        )
        self._set_gpus(
            [
                {
                    "type": "NVIDIA",
                    "device": "/dev/nvidia0",
                    "name": "NVIDIA GeForce RTX 4090",
                }
            ]
        )

        warning = _get_vulkan_info()["warning"]
        assert "NVIDIA GeForce RTX 4090" in warning
        assert "libnvidia-glvkspirv" in warning
        assert "nvidia-container-toolkit#1559" in warning
        assert "legacy" in warning  # points at mode = "legacy" workaround

    @patch("plex_generate_previews.web.routes.api_system._diagnose_vulkan_environment")
    @patch("plex_generate_previews.web.routes.api_system.glob.glob")
    @patch("plex_generate_previews.gpu.vulkan_probe._probe_vulkan_device")
    def test_case_a4_all_checks_pass_but_loader_still_rejected(
        self, mock_probe, mock_glob, mock_diag
    ):
        """Case A4: all the usual NVIDIA container requirements are
        satisfied but Vulkan still fell back to software → tell the
        user to file an issue with the diagnostic bundle.
        """
        from plex_generate_previews.web.routes.api_system import _get_vulkan_info

        mock_probe.return_value = "llvmpipe (software)"
        mock_glob.return_value = []
        mock_diag.return_value = self._diag_fixture(
            has_graphics=True,
            icd_path="/etc/vulkan/icd.d/nvidia_icd.json",
            glvkspirv=True,
        )
        self._set_gpus(
            [
                {
                    "type": "NVIDIA",
                    "device": "/dev/nvidia0",
                    "name": "NVIDIA Quadro P2000",
                }
            ]
        )

        warning = _get_vulkan_info()["warning"]
        assert "NVIDIA Quadro P2000" in warning
        assert "diagnostic bundle" in warning
        assert "/api/system/vulkan/debug" in warning

    @patch("plex_generate_previews.web.routes.api_system._diagnose_vulkan_environment")
    @patch("plex_generate_previews.web.routes.api_system.glob.glob")
    @patch("plex_generate_previews.gpu.vulkan_probe._probe_vulkan_device")
    def test_pure_nvidia_with_dri_mapped_still_dispatches_into_case_a(
        self, mock_probe, mock_glob, mock_diag
    ):
        """Regression guard: pure-NVIDIA host with /dev/dri already
        forwarded should STILL go through the Case A dispatch, not fall
        through to Case D (which is for Mesa hosts whose setup is
        broken). Mounting /dev/dri on a Mesa-less host does nothing —
        the fix has to come from the NVIDIA side.
        """
        from plex_generate_previews.web.routes.api_system import _get_vulkan_info

        mock_probe.return_value = "llvmpipe (software)"
        mock_glob.return_value = ["/dev/dri/renderD128"]  # IS mapped
        mock_diag.return_value = self._diag_fixture(has_graphics=False)
        self._set_gpus(
            [
                {
                    "type": "NVIDIA",
                    "device": "/dev/nvidia0",
                    "name": "NVIDIA TITAN RTX",
                }
            ]
        )

        warning = _get_vulkan_info()["warning"]
        # Should hit A1 (missing graphics), not Case D.
        assert "NVIDIA TITAN RTX" in warning
        assert "NVIDIA_DRIVER_CAPABILITIES" in warning
        assert "already forwarded" not in warning  # Case D text
        assert "vainfo" not in warning  # Case D text

    @patch("plex_generate_previews.web.routes.api_system.glob.glob")
    @patch("plex_generate_previews.gpu.vulkan_probe._probe_vulkan_device")
    def test_nvidia_plus_intel_no_dri_recommends_mount(self, mock_probe, mock_glob):
        """Case B: NVIDIA + Intel with no /dev/dri → names both GPUs,
        gives the mount fix, notes NVIDIA decoding is independent.
        """
        from plex_generate_previews.web.routes.api_system import _get_vulkan_info

        mock_probe.return_value = "llvmpipe (software)"
        mock_glob.return_value = []
        self._set_gpus(
            [
                {
                    "type": "NVIDIA",
                    "device": "/dev/nvidia0",
                    "name": "NVIDIA GeForce RTX 4090",
                },
                {
                    "type": "INTEL",
                    "device": "/dev/dri/renderD128",
                    "name": "Intel UHD Graphics 770",
                },
            ]
        )

        warning = _get_vulkan_info()["warning"]
        assert "NVIDIA GeForce RTX 4090" in warning
        assert "Intel UHD Graphics 770" in warning
        assert "Your GPUs:" in warning
        assert "Docker Compose:" in warning
        assert "two paths are independent" in warning
        # Case B must not imply the NVIDIA-only workaround list.
        assert "Pure NVIDIA hosts" not in warning

    @patch("plex_generate_previews.web.routes.api_system.glob.glob")
    @patch("plex_generate_previews.gpu.vulkan_probe._probe_vulkan_device")
    def test_intel_only_no_dri_recommends_mount(self, mock_probe, mock_glob):
        """Case C: Intel-only host with no /dev/dri → names the Intel
        GPU and gives the mount fix, without dragging in NVIDIA-specific
        language that would only confuse the user.
        """
        from plex_generate_previews.web.routes.api_system import _get_vulkan_info

        mock_probe.return_value = "llvmpipe (software)"
        mock_glob.return_value = []
        self._set_gpus(
            [
                {
                    "type": "INTEL",
                    "device": "/dev/dri/renderD128",
                    "name": "Intel UHD Graphics 770",
                }
            ]
        )

        warning = _get_vulkan_info()["warning"]
        assert "Intel UHD Graphics 770" in warning
        assert "Docker Compose:" in warning
        assert "NVIDIA" not in warning
        assert "VK_ERROR_INCOMPATIBLE_DRIVER" not in warning

    @patch("plex_generate_previews.web.routes.api_system.glob.glob")
    @patch("plex_generate_previews.gpu.vulkan_probe._probe_vulkan_device")
    def test_intel_with_dri_mapped_points_at_drivers_or_perms(
        self, mock_probe, mock_glob
    ):
        """Case D: Intel host with /dev/dri already forwarded but
        rendering still fell back → diagnose host drivers or render-node
        permissions rather than repeating the mount instructions.
        """
        from plex_generate_previews.web.routes.api_system import _get_vulkan_info

        mock_probe.return_value = "llvmpipe (software)"
        mock_glob.return_value = ["/dev/dri/renderD128"]
        self._set_gpus(
            [
                {
                    "type": "INTEL",
                    "device": "/dev/dri/renderD128",
                    "name": "Intel UHD Graphics 630",
                }
            ]
        )

        warning = _get_vulkan_info()["warning"]
        assert "Intel UHD Graphics 630" in warning
        assert "already forwarded" in warning
        assert "vainfo" in warning
        assert "permissions" in warning
        # Already mounted — must not re-recommend mounting.
        assert "Docker Compose:" not in warning

    @patch("plex_generate_previews.web.routes.api_system.glob.glob")
    @patch("plex_generate_previews.gpu.vulkan_probe._probe_vulkan_device")
    def test_no_gpu_detected_explains_missing_hardware(self, mock_probe, mock_glob):
        """Case E: Vulkan is llvmpipe and no GPU is detected at all →
        explain that no hardware is visible to the container and give
        per-vendor forwarding hints.
        """
        from plex_generate_previews.web.routes.api_system import _get_vulkan_info

        mock_probe.return_value = "llvmpipe (software)"
        mock_glob.return_value = []
        self._set_gpus([])

        warning = _get_vulkan_info()["warning"]
        assert "No GPU detected" in warning
        assert "--runtime=nvidia" in warning
        assert "/dev/dri" in warning


class TestDiagnoseVulkanEnvironment:
    """Unit tests for _diagnose_vulkan_environment() — the Layer-4 helper
    that reads ``os.environ`` and the filesystem to power the Case A
    dispatch and the ``/api/system/vulkan/debug`` endpoint."""

    def _diag(self):
        from plex_generate_previews.web.routes.api_system import (
            _diagnose_vulkan_environment,
        )

        return _diagnose_vulkan_environment()

    def test_graphics_capability_detected_when_all(self, monkeypatch):
        monkeypatch.setenv("NVIDIA_DRIVER_CAPABILITIES", "all")
        assert self._diag()["nvidia_capabilities_has_graphics"] is True

    def test_graphics_capability_detected_when_explicit(self, monkeypatch):
        monkeypatch.setenv(
            "NVIDIA_DRIVER_CAPABILITIES", "compute,video,utility,graphics"
        )
        assert self._diag()["nvidia_capabilities_has_graphics"] is True

    def test_graphics_capability_missing_with_common_default(self, monkeypatch):
        monkeypatch.setenv("NVIDIA_DRIVER_CAPABILITIES", "compute,video,utility")
        diag = self._diag()
        assert diag["nvidia_capabilities_has_graphics"] is False
        assert diag["nvidia_capabilities"] == "compute,video,utility"

    def test_graphics_capability_missing_when_env_unset(self, monkeypatch):
        monkeypatch.delenv("NVIDIA_DRIVER_CAPABILITIES", raising=False)
        diag = self._diag()
        assert diag["nvidia_capabilities_has_graphics"] is False
        assert diag["nvidia_capabilities"] is None

    @patch("plex_generate_previews.web.routes.api_system.os.path.exists")
    def test_nvidia_icd_json_found_at_etc_path(self, mock_exists, monkeypatch):
        monkeypatch.setenv("NVIDIA_DRIVER_CAPABILITIES", "all")
        # Return True only for the /etc/ path, False elsewhere.
        mock_exists.side_effect = lambda p: p == "/etc/vulkan/icd.d/nvidia_icd.json"
        diag = self._diag()
        assert diag["nvidia_icd_json_path"] == "/etc/vulkan/icd.d/nvidia_icd.json"

    @patch("plex_generate_previews.web.routes.api_system.os.path.exists")
    def test_nvidia_icd_json_found_at_usr_share_path(self, mock_exists, monkeypatch):
        monkeypatch.setenv("NVIDIA_DRIVER_CAPABILITIES", "all")
        mock_exists.side_effect = lambda p: (
            p == "/usr/share/vulkan/icd.d/nvidia_icd.json"
        )
        diag = self._diag()
        assert diag["nvidia_icd_json_path"] == "/usr/share/vulkan/icd.d/nvidia_icd.json"

    @patch("plex_generate_previews.web.routes.api_system.os.path.exists")
    def test_nvidia_icd_json_absent_returns_none(self, mock_exists, monkeypatch):
        monkeypatch.setenv("NVIDIA_DRIVER_CAPABILITIES", "all")
        mock_exists.return_value = False
        diag = self._diag()
        assert diag["nvidia_icd_json_path"] is None
        assert diag["nvidia_drm_loaded"] is False

    @patch("plex_generate_previews.web.routes.api_system.glob.glob")
    def test_libnvidia_glvkspirv_found_when_any_glob_matches(
        self, mock_glob, monkeypatch
    ):
        monkeypatch.setenv("NVIDIA_DRIVER_CAPABILITIES", "all")
        # Make the first glob path return a match, others empty.
        mock_glob.side_effect = lambda p: (
            ["/usr/lib/x86_64-linux-gnu/libnvidia-glvkspirv.so.580.0.0"]
            if "x86_64" in p
            else []
        )
        assert self._diag()["libnvidia_glvkspirv_found"] is True

    @patch("plex_generate_previews.web.routes.api_system.glob.glob")
    def test_libnvidia_glvkspirv_missing_when_all_globs_empty(
        self, mock_glob, monkeypatch
    ):
        monkeypatch.setenv("NVIDIA_DRIVER_CAPABILITIES", "all")
        mock_glob.return_value = []
        assert self._diag()["libnvidia_glvkspirv_found"] is False

    @patch("plex_generate_previews.web.routes.api_system.glob.glob")
    def test_libegl_nvidia_found_when_any_glob_matches(self, mock_glob, monkeypatch):
        """Strategy 2c gate: ``libEGL_nvidia.so.0`` present in the
        container must be reflected in the diagnostic dict so the
        debug bundle can tell users why Strategy 2c did or didn't run.
        """
        monkeypatch.setenv("NVIDIA_DRIVER_CAPABILITIES", "all")
        mock_glob.side_effect = lambda p: (
            ["/usr/lib/x86_64-linux-gnu/libEGL_nvidia.so.580.119.02"]
            if "libEGL_nvidia" in p
            else []
        )
        diag = self._diag()
        assert diag["libegl_nvidia_found"] is True

    @patch("plex_generate_previews.web.routes.api_system.glob.glob")
    def test_libegl_nvidia_missing_when_all_globs_empty(self, mock_glob, monkeypatch):
        monkeypatch.setenv("NVIDIA_DRIVER_CAPABILITIES", "all")
        mock_glob.return_value = []
        assert self._diag()["libegl_nvidia_found"] is False

    @patch("plex_generate_previews.web.routes.api_system.os.path.exists")
    def test_nvidia_egl_vendor_json_path_found_at_usr_share(
        self, mock_exists, monkeypatch
    ):
        """Strategy 2c informational field: the GLVND vendor JSON path
        if present at either standard location, or None otherwise.
        """
        monkeypatch.setenv("NVIDIA_DRIVER_CAPABILITIES", "all")
        mock_exists.side_effect = lambda p: (
            p == "/usr/share/glvnd/egl_vendor.d/10_nvidia.json"
        )
        diag = self._diag()
        assert (
            diag["nvidia_egl_vendor_json_path"]
            == "/usr/share/glvnd/egl_vendor.d/10_nvidia.json"
        )

    @patch("plex_generate_previews.web.routes.api_system.os.path.exists")
    def test_nvidia_egl_vendor_json_path_absent_returns_none(
        self, mock_exists, monkeypatch
    ):
        monkeypatch.setenv("NVIDIA_DRIVER_CAPABILITIES", "all")
        mock_exists.return_value = False
        diag = self._diag()
        assert diag["nvidia_egl_vendor_json_path"] is None

    @patch("plex_generate_previews.web.routes.api_system.os.path.exists")
    def test_nvidia_driver_version_parsed_from_proc(
        self, mock_exists, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("NVIDIA_DRIVER_CAPABILITIES", "all")
        mock_exists.side_effect = lambda p: (
            p
            in (
                "/proc/driver/nvidia",
                "/proc/driver/nvidia/version",
            )
        )
        # Point the open() inside _diagnose_vulkan_environment at a real
        # temp file whose content looks like a real NVRM version line.
        proc_file = tmp_path / "nvidia_version"
        proc_file.write_text(
            "NVRM version: NVIDIA UNIX x86_64 Kernel Module  570.133.07  "
            "Thu Mar 20 14:50:40 UTC 2025\n"
            "GCC version:  gcc (Debian 12.2.0-14) 12.2.0\n"
        )

        original_open = open

        def fake_open(path, *args, **kwargs):
            if path == "/proc/driver/nvidia/version":
                return original_open(proc_file, *args, **kwargs)
            return original_open(path, *args, **kwargs)

        monkeypatch.setattr(
            "plex_generate_previews.web.routes.api_system.open",
            fake_open,
            raising=False,
        )
        diag = self._diag()
        assert diag["nvidia_drm_loaded"] is True
        assert diag["nvidia_driver_version"] == "570.133.07"


class TestProbeVaapiDriver:
    """Test the vainfo probe that identifies the user-space VA-API driver.

    The probe lets the GPU detection log line distinguish between the
    kernel DRM driver (``i915``/``xe``) and the VA-API backend
    (``iHD``/``i965``) so users do not mistake one for the other (see
    issue #216).
    """

    @pytest.fixture(autouse=True)
    def _clear_probe_cache(self):
        """Reset the lru_cache between tests so each test sees a fresh
        subprocess invocation rather than a value cached from an earlier
        test that used the same render device path.
        """
        _probe_vaapi_driver.cache_clear()
        yield
        _probe_vaapi_driver.cache_clear()

    @patch("plex_generate_previews.gpu.vulkan_probe.subprocess.run")
    def test_probe_returns_driver_version_on_success(self, mock_run):
        """Probe extracts the Driver version line from vainfo stdout."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=(
                "libva info: VA-API version 1.23.0\n"
                "vainfo: VA-API version: 1.23 (libva 2.12.0)\n"
                "vainfo: Driver version: "
                "Intel iHD driver for Intel(R) Gen Graphics - 25.3.4 ()\n"
            ),
        )
        result = _probe_vaapi_driver("/dev/dri/renderD128")
        assert result == "Intel iHD driver for Intel(R) Gen Graphics - 25.3.4 ()"

    @patch("plex_generate_previews.gpu.vulkan_probe.subprocess.run")
    def test_probe_returns_none_when_vainfo_missing(self, mock_run):
        """FileNotFoundError from subprocess.run collapses to None."""
        mock_run.side_effect = FileNotFoundError("vainfo not installed")
        assert _probe_vaapi_driver("/dev/dri/renderD128") is None

    @patch("plex_generate_previews.gpu.vulkan_probe.subprocess.run")
    def test_probe_returns_none_on_timeout(self, mock_run):
        """TimeoutExpired from subprocess.run collapses to None."""
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="vainfo", timeout=5)
        assert _probe_vaapi_driver("/dev/dri/renderD128") is None

    @patch("plex_generate_previews.gpu.vulkan_probe.subprocess.run")
    def test_probe_returns_none_when_driver_line_absent(self, mock_run):
        """stdout that lacks a Driver version: line yields None."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="libva info: VA-API version 1.23.0\n",
        )
        assert _probe_vaapi_driver("/dev/dri/renderD128") is None

    @patch("plex_generate_previews.gpu.vulkan_probe.subprocess.run")
    def test_probe_returns_none_when_driver_line_is_empty(self, mock_run):
        """An empty Driver version: value yields None rather than ''."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="vainfo: Driver version:   \n",
        )
        assert _probe_vaapi_driver("/dev/dri/renderD128") is None


class TestFormatDriverLabel:
    """Test the driver label formatter used in GPU detection log lines.

    For Intel GPUs the label combines the kernel driver and the VA-API
    driver. For other vendors (or when vainfo is unavailable) the label
    falls back to the legacy ``driver: <kernel_driver>`` format so
    nothing regresses on systems without ``vainfo``.
    """

    @patch("plex_generate_previews.gpu.vaapi_probe._probe_vaapi_driver")
    def test_intel_label_includes_both_drivers(self, mock_probe):
        """i915 + successful vainfo probe produces a two-driver label."""
        mock_probe.return_value = "Intel iHD driver for Intel(R) Gen Graphics - 25.3.4"
        label = _format_driver_label("/dev/dri/renderD128", "i915")
        assert label == (
            "kernel driver: i915, "
            "va-api driver: Intel iHD driver for Intel(R) Gen Graphics - 25.3.4"
        )

    @patch("plex_generate_previews.gpu.vaapi_probe._probe_vaapi_driver")
    def test_xe_driver_is_treated_as_intel(self, mock_probe):
        """xe (new Intel DRM driver) also triggers the vainfo probe."""
        mock_probe.return_value = "Intel iHD driver for Intel(R) Gen Graphics - 25.3.4"
        label = _format_driver_label("/dev/dri/renderD128", "xe")
        assert label.startswith("kernel driver: xe, va-api driver: Intel iHD")
        mock_probe.assert_called_once_with("/dev/dri/renderD128")

    @patch("plex_generate_previews.gpu.vaapi_probe._probe_vaapi_driver")
    def test_intel_falls_back_to_legacy_when_probe_fails(self, mock_probe):
        """Missing vainfo must not regress the log format on Intel."""
        mock_probe.return_value = None
        label = _format_driver_label("/dev/dri/renderD128", "i915")
        assert label == "driver: i915"

    @patch("plex_generate_previews.gpu.vaapi_probe._probe_vaapi_driver")
    def test_non_intel_driver_never_probes(self, mock_probe):
        """nvidia/amdgpu GPUs produce the legacy label and skip vainfo."""
        label = _format_driver_label("/dev/dri/renderD128", "nvidia")
        assert label == "driver: nvidia"
        mock_probe.assert_not_called()

        label = _format_driver_label("/dev/dri/renderD128", "amdgpu")
        assert label == "driver: amdgpu"
        assert mock_probe.call_count == 0
