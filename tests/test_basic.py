"""
Basic functionality tests for plex_generate_previews.
"""

from unittest.mock import MagicMock, patch

import pytest


class TestBasicFunctionality:
    """Test basic functionality without complex mocking."""

    def test_package_imports(self):
        """Test that the package can be imported."""
        import re

        import plex_generate_previews

        assert hasattr(plex_generate_previews, "__version__")
        # Version should be in PEP 440 format (e.g., "2.0.0", "2.1.2.post0", "0.0.0+unknown", "2.3.1.dev5+g1234abc")
        # Pattern matches setuptools-scm generated versions
        version_pattern = r"^\d+\.\d+\.\d+(?:\.(?:post|dev)\d+)?(?:\+[a-zA-Z0-9.-]+)?$"
        assert re.match(version_pattern, plex_generate_previews.__version__), (
            f"Version '{plex_generate_previews.__version__}' doesn't match PEP 440 format"
        )

    def test_web_module_importable(self):
        """Test that the web module can be imported."""
        from plex_generate_previews.web.app import create_app

        assert callable(create_app)

    def test_no_cli_module(self):
        """Test that CLI module has been removed."""
        import importlib

        with pytest.raises(ModuleNotFoundError):
            importlib.import_module("plex_generate_previews.cli")

    def test_config_validation_error_class(self):
        """Test that ConfigValidationError can be raised."""
        from plex_generate_previews.config import ConfigValidationError

        with pytest.raises(ConfigValidationError) as exc_info:
            raise ConfigValidationError(["Missing PLEX_URL", "Missing PLEX_TOKEN"])
        assert "Missing PLEX_URL" in str(exc_info.value)
        assert len(exc_info.value.errors) == 2


class TestConfigFunctions:
    """Test configuration functions directly."""

    def test_get_config_value_cli_precedence(self):
        """Test that CLI args take precedence over env vars."""
        from plex_generate_previews.config import get_config_value

        cli_args = MagicMock()
        cli_args.test_field = "cli_value"

        with patch.dict("os.environ", {"TEST_FIELD": "env_value"}):
            result = get_config_value(cli_args, "test_field", "TEST_FIELD", "default")
            assert result == "cli_value"

    def test_get_config_value_env_fallback(self):
        """Test that env vars are used when CLI args are None."""
        from plex_generate_previews.config import get_config_value

        cli_args = MagicMock()
        cli_args.test_field = None

        with patch.dict("os.environ", {"TEST_FIELD": "env_value"}):
            result = get_config_value(cli_args, "test_field", "TEST_FIELD", "default")
            assert result == "env_value"

    def test_get_config_value_default_fallback(self):
        """Test that defaults are used when neither CLI nor env are set."""
        from plex_generate_previews.config import get_config_value

        cli_args = MagicMock()
        cli_args.test_field = None

        with patch.dict("os.environ", {}, clear=True):
            result = get_config_value(cli_args, "test_field", "TEST_FIELD", "default")
            assert result == "default"


class TestGPUDetection:
    """Test GPU detection functionality."""

    def test_format_gpu_info(self):
        """Test GPU info formatting."""
        from plex_generate_previews.gpu_detection import format_gpu_info

        # Test NVIDIA formatting
        nvidia_info = format_gpu_info("cuda", 0, "NVIDIA GeForce RTX 3080")
        assert "NVIDIA" in nvidia_info
        assert "RTX 3080" in nvidia_info
        assert "cuda" in nvidia_info.lower()

        # Test AMD formatting
        amd_info = format_gpu_info(
            "vaapi", "/dev/dri/renderD128", "AMD Radeon RX 6800 XT"
        )
        assert "AMD" in amd_info
        assert "RX 6800 XT" in amd_info
        assert "vaapi" in amd_info.lower()

    def test_ffmpeg_version_check(self):
        """Test FFmpeg version checking."""
        from plex_generate_previews.gpu_detection import (
            _check_ffmpeg_version,
            _get_ffmpeg_version,
        )

        # Test version parsing
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout="ffmpeg version 7.1.1-1ubuntu1.2 Copyright..."
            )
            version = _get_ffmpeg_version()
            assert version == (7, 1, 1)

        # Test version checking
        with patch(
            "plex_generate_previews.gpu_detection._get_ffmpeg_version"
        ) as mock_get_version:
            mock_get_version.return_value = (7, 1, 0)
            assert _check_ffmpeg_version() is True

            mock_get_version.return_value = (6, 9, 0)
            assert _check_ffmpeg_version() is False


class TestProcessingModule:
    """Test processing module exists and is importable."""

    def test_run_processing_importable(self):
        """Test that run_processing can be imported from job_orchestrator module."""
        from plex_generate_previews.job_orchestrator import run_processing

        assert callable(run_processing)
