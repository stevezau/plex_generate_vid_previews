"""
Basic functionality tests for media_preview_generator.
"""

from unittest.mock import MagicMock, patch

import pytest


class TestBasicFunctionality:
    """Test basic functionality without complex mocking."""

    def test_package_imports(self):
        """Test that the package can be imported."""
        import re

        import media_preview_generator

        assert hasattr(media_preview_generator, "__version__")
        # Version should be in PEP 440 format (e.g., "2.0.0", "2.1.2.post0", "0.0.0+unknown", "2.3.1.dev5+g1234abc")
        # Pattern matches setuptools-scm generated versions
        version_pattern = r"^\d+\.\d+\.\d+(?:\.(?:post|dev)\d+)?(?:\+[a-zA-Z0-9.-]+)?$"
        assert re.match(version_pattern, media_preview_generator.__version__), (
            f"Version '{media_preview_generator.__version__}' doesn't match PEP 440 format"
        )

    def test_web_module_importable(self, tmp_path):
        """create_app() returns a real Flask app with registered routes (not just a callable)."""
        import flask

        from media_preview_generator.web.app import create_app

        # Pass an explicit writable config_dir so the test runs on CI runners
        # where the production default (/config) isn't writable. Keeping the
        # call real (not patched) means a regression that breaks app
        # construction or blueprint registration still fails this test.
        app = create_app(config_dir=str(tmp_path))
        assert isinstance(app, flask.Flask)
        # The app must have registered URL rules — guards against the failure
        # mode where blueprint registration silently raises on import and
        # leaves an empty Flask app behind.
        rules = [r.rule for r in app.url_map.iter_rules()]
        assert len(rules) > 0
        # /api endpoints are mandatory — the dashboard depends on them.
        assert any(r.startswith("/api/") for r in rules), f"No /api/ routes registered: {rules[:5]}"

    def test_no_cli_module(self):
        """Test that CLI module has been removed."""
        import importlib

        with pytest.raises(ModuleNotFoundError):
            importlib.import_module("media_preview_generator.cli")

    def test_config_validation_error_class(self):
        """Test that ConfigValidationError can be raised."""
        from media_preview_generator.config import ConfigValidationError

        with pytest.raises(ConfigValidationError) as exc_info:
            raise ConfigValidationError(["Missing PLEX_URL", "Missing PLEX_TOKEN"])
        assert "Missing PLEX_URL" in str(exc_info.value)
        assert len(exc_info.value.errors) == 2


class TestConfigFunctions:
    """Test configuration functions directly."""

    def test_get_config_value_cli_precedence(self):
        """Test that CLI args take precedence over env vars."""
        from media_preview_generator.config import get_config_value

        cli_args = MagicMock()
        cli_args.test_field = "cli_value"

        with patch.dict("os.environ", {"TEST_FIELD": "env_value"}):
            result = get_config_value(cli_args, "test_field", "TEST_FIELD", "default")
            assert result == "cli_value"

    def test_get_config_value_env_fallback(self):
        """Test that env vars are used when CLI args are None."""
        from media_preview_generator.config import get_config_value

        cli_args = MagicMock()
        cli_args.test_field = None

        with patch.dict("os.environ", {"TEST_FIELD": "env_value"}):
            result = get_config_value(cli_args, "test_field", "TEST_FIELD", "default")
            assert result == "env_value"

    def test_get_config_value_default_fallback(self):
        """Test that defaults are used when neither CLI nor env are set."""
        from media_preview_generator.config import get_config_value

        cli_args = MagicMock()
        cli_args.test_field = None

        with patch.dict("os.environ", {}, clear=True):
            result = get_config_value(cli_args, "test_field", "TEST_FIELD", "default")
            assert result == "default"


class TestGPUDetection:
    """Test GPU detection functionality."""

    def test_format_gpu_info(self):
        """Test GPU info formatting using the real (gpu_type, gpu_device, gpu_name, acceleration?) signature."""
        from media_preview_generator.gpu import format_gpu_info

        # NVIDIA via the explicit-acceleration path
        nvidia_info = format_gpu_info("NVIDIA", "0", "NVIDIA GeForce RTX 3080", "CUDA")
        assert nvidia_info == "NVIDIA GeForce RTX 3080 (CUDA)"

        # AMD via VAAPI on a DRM render node
        amd_info = format_gpu_info("AMD", "/dev/dri/renderD128", "AMD Radeon RX 6800 XT", "VAAPI")
        assert amd_info == "AMD Radeon RX 6800 XT (VAAPI - /dev/dri/renderD128)"

        # Backward-compat path (no acceleration arg) still resolves NVIDIA -> CUDA
        nvidia_legacy = format_gpu_info("NVIDIA", "0", "NVIDIA GeForce RTX 3080")
        assert nvidia_legacy == "NVIDIA GeForce RTX 3080 (CUDA)"

    def test_ffmpeg_version_check(self):
        """Test FFmpeg version checking."""
        from media_preview_generator.gpu import (
            _check_ffmpeg_version,
            _get_ffmpeg_version,
        )

        # Test version parsing
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="ffmpeg version 7.1.1-1ubuntu1.2 Copyright...")
            version = _get_ffmpeg_version()
            assert version == (7, 1, 1)

        # Test version checking
        with patch("media_preview_generator.gpu.ffmpeg_capabilities._get_ffmpeg_version") as mock_get_version:
            mock_get_version.return_value = (7, 1, 0)
            assert _check_ffmpeg_version() is True

            mock_get_version.return_value = (6, 9, 0)
            assert _check_ffmpeg_version() is False
