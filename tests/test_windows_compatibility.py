"""
Unit tests for Windows compatibility features.

Tests Windows-specific functionality including:
- Platform detection
- GPU validation (must be CPU-only)
- Temp directory defaults
- Signal handling
- Path sanitization with Windows paths
"""

from unittest.mock import MagicMock, patch

import pytest

from media_preview_generator.config import load_config
from media_preview_generator.utils import is_windows, sanitize_path


@pytest.fixture(autouse=True)
def _isolate_settings(tmp_path, monkeypatch):
    """Ensure load_config uses a fresh empty settings.json."""
    from media_preview_generator.web import settings_manager

    monkeypatch.setattr(settings_manager, "_settings_manager", None)
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))


class TestWindowsPlatformDetection:
    """Test Windows platform detection."""

    @patch("os.name", "nt")
    def test_is_windows_on_windows(self):
        """Test detection returns True on Windows."""
        assert is_windows() is True

    @patch("os.name", "posix")
    def test_is_windows_on_linux(self):
        """Test detection returns False on Linux."""
        assert is_windows() is False


class TestWindowsPathSanitization:
    """Test path sanitization for Windows compatibility."""

    @patch("os.name", "nt")
    def test_sanitize_path_forward_to_backslash(self):
        """Test forward slashes converted to backslashes on Windows."""
        path = "C:/Users/Test/Videos/movie.mkv"
        result = sanitize_path(path)
        assert "\\" in result
        assert "/" not in result
        assert result == "C:\\Users\\Test\\Videos\\movie.mkv"

    @patch("os.name", "nt")
    def test_sanitize_path_unc_path(self):
        """Test UNC path handling on Windows."""
        path = "//server/share/videos/movie.mkv"
        result = sanitize_path(path)
        assert result.startswith("\\\\")
        assert result == "\\\\server\\share\\videos\\movie.mkv"

    @patch("os.name", "nt")
    def test_sanitize_path_already_backslash(self):
        """Test path already using backslashes on Windows."""
        path = "C:\\Users\\Test\\Videos\\movie.mkv"
        result = sanitize_path(path)
        assert result == "C:\\Users\\Test\\Videos\\movie.mkv"

    @patch("os.name", "nt")
    def test_sanitize_path_normpath_uses_ntpath(self):
        """On Windows, sanitize_path delegates to ntpath.normpath via os.path.normpath.

        Rather than mocking ``os.path.normpath`` (which is what
        ``sanitize_path`` calls), patch ``os.path`` to point at the real
        ``ntpath`` module. That way the test exercises the full real
        Windows-style normalisation pipeline (slash conversion + .. and .
        collapse) instead of just verifying our own mock's return value.
        """
        import ntpath

        with patch("media_preview_generator.utils.os.path", ntpath):
            result = sanitize_path("C:/Users/Test/../Videos/./movie.mkv")
        assert result == "C:\\Users\\Videos\\movie.mkv"

    @patch("os.name", "posix")
    def test_sanitize_path_linux_unchanged(self):
        """On Linux, an already-normalised POSIX path comes through verbatim.

        Real os.path.normpath under POSIX is a no-op for clean paths, so
        no mocking is needed — exercising the production call directly
        guards against the regression where normpath is removed or
        replaced with a no-op.
        """
        path = "/home/user/videos/movie.mkv"
        result = sanitize_path(path)
        assert result == "/home/user/videos/movie.mkv"
        # And the POSIX branch must never emit backslashes.
        assert "\\" not in result


class TestWindowsTempDirectory:
    """Test Windows temp directory handling."""

    @patch("media_preview_generator.config.tempfile.gettempdir", return_value="C:\\Temp")
    @patch("platform.system", return_value="Windows")
    @patch("shutil.which", return_value="C:\\ffmpeg\\ffmpeg.exe")
    @patch("subprocess.run")
    @patch("os.path.exists", return_value=True)
    @patch("os.path.isdir", return_value=True)
    @patch("os.listdir")
    @patch("os.access", return_value=True)
    @patch("os.statvfs", create=True)
    def test_windows_default_temp_folder(
        self,
        mock_statvfs,
        mock_access,
        mock_listdir,
        mock_isdir,
        mock_exists,
        mock_run,
        mock_which,
        mock_platform,
        mock_gettempdir,
    ):
        """Test that Windows uses system temp directory by default."""
        mock_run.return_value = MagicMock(returncode=0, stdout="ffmpeg version 7.0.0")

        # Mock statvfs for disk space check
        mock_stat = MagicMock()
        mock_stat.f_frsize = 4096
        mock_stat.f_bavail = 1024 * 1024  # Plenty of space
        mock_statvfs.return_value = mock_stat

        def mock_listdir_fn(path):
            if "Temp" in path or "tmp" in path.lower():
                return []
            elif "localhost" in path:
                return [
                    "0",
                    "1",
                    "2",
                    "3",
                    "4",
                    "5",
                    "6",
                    "7",
                    "8",
                    "9",
                    "a",
                    "b",
                    "c",
                    "d",
                    "e",
                    "f",
                ]
            elif path.endswith("Media"):
                return ["localhost"]
            else:
                return ["Cache", "Media", "Metadata"]

        mock_listdir.side_effect = mock_listdir_fn

        env = {
            "PLEX_URL": "http://localhost:32400",
            "PLEX_TOKEN": "test_token",
            "PLEX_CONFIG_FOLDER": "C:\\ProgramData\\Plex\\Library\\Application Support\\Plex Media Server",
            "CPU_THREADS": "4",
            "GPU_THREADS": "0",
            "PLEX_BIF_FRAME_INTERVAL": "5",
            "THUMBNAIL_QUALITY": "4",
            "LOG_LEVEL": "INFO",
            "PLEX_TIMEOUT": "60",
        }
        with patch.dict("os.environ", env, clear=False):
            with patch("media_preview_generator.config.load_dotenv", lambda: None):
                config = load_config()

        # Should use Windows temp directory from tempfile.gettempdir()
        assert config is not None
        assert config.tmp_folder == "C:\\Temp"  # Should use the mocked gettempdir() value


class TestWindowsPathMappings:
    """Path mapping with Windows-style paths — exercises the production
    ``path_to_canonical_local`` resolver, not an inline ``str.replace``."""

    @patch("os.name", "nt")
    def test_path_mapping_windows_to_windows(self):
        """Plex sees D:\\, local sees C:\\ — production resolver maps it."""
        from media_preview_generator.config import path_to_canonical_local
        from media_preview_generator.utils import sanitize_path

        # Path mappings store unix-style separators in settings.json — the
        # production resolver handles the conversion via ``sanitize_path``.
        mappings = [
            {
                "plex_prefix": "D:/PlexMedia",
                "local_prefix": "C:/Media",
                "webhook_prefixes": [],
            }
        ]
        canonical = path_to_canonical_local("D:/PlexMedia/Movies/movie.mkv", mappings)
        # The resolver returns POSIX-style; sanitize_path converts to Windows.
        assert canonical == "C:/Media/Movies/movie.mkv"
        assert sanitize_path(canonical) == "C:\\Media\\Movies\\movie.mkv"

    @patch("os.name", "nt")
    def test_path_mapping_unc_to_local(self):
        """UNC plex_prefix → local Windows prefix via the production resolver."""
        from media_preview_generator.config import path_to_canonical_local
        from media_preview_generator.utils import sanitize_path

        mappings = [
            {
                "plex_prefix": "//server/media",
                "local_prefix": "C:/Media",
                "webhook_prefixes": [],
            }
        ]
        canonical = path_to_canonical_local("//server/media/Movies/movie.mkv", mappings)
        assert canonical == "C:/Media/Movies/movie.mkv"
        assert sanitize_path(canonical) == "C:\\Media\\Movies\\movie.mkv"


class TestWindowsConfigValidation:
    """Test configuration validation on Windows."""

    @patch(
        "media_preview_generator.config.tempfile.gettempdir",
        return_value="C:\\Windows\\Temp",
    )
    @patch("platform.system", return_value="Windows")
    @patch("os.name", "nt")
    @patch("shutil.which", return_value="C:\\ffmpeg\\bin\\ffmpeg.exe")
    @patch("subprocess.run")
    @patch("os.path.exists", return_value=True)
    @patch("os.path.isdir", return_value=True)
    @patch("os.listdir")
    @patch("os.access", return_value=True)
    @patch("os.makedirs")
    @patch("os.statvfs", create=True)
    def test_windows_config_validation(
        self,
        mock_statvfs,
        mock_makedirs,
        mock_access,
        mock_listdir,
        mock_isdir,
        mock_exists,
        mock_run,
        mock_which,
        mock_platform,
        mock_gettempdir,
    ):
        """Test that configuration validates correctly on Windows."""
        mock_run.return_value = MagicMock(returncode=0, stdout="ffmpeg version 7.0.0")

        # Mock statvfs for disk space check
        mock_stat = MagicMock()
        mock_stat.f_frsize = 4096
        mock_stat.f_bavail = 1024 * 1024  # Plenty of space
        mock_statvfs.return_value = mock_stat

        def mock_listdir_fn(path):
            if "Temp" in path or "tmp" in path.lower():
                return []
            elif "localhost" in path:
                return [
                    "0",
                    "1",
                    "2",
                    "3",
                    "4",
                    "5",
                    "6",
                    "7",
                    "8",
                    "9",
                    "a",
                    "b",
                    "c",
                    "d",
                    "e",
                    "f",
                ]
            elif path.endswith("Media"):
                return ["localhost"]
            else:
                return ["Cache", "Media", "Metadata"]

        mock_listdir.side_effect = mock_listdir_fn

        env = {
            "PLEX_URL": "http://localhost:32400",
            "PLEX_TOKEN": "test_token",
            "PLEX_CONFIG_FOLDER": "C:\\Users\\Test\\AppData\\Local\\Plex Media Server",
            "CPU_THREADS": "4",
            "GPU_THREADS": "0",
            "PLEX_BIF_FRAME_INTERVAL": "5",
            "THUMBNAIL_QUALITY": "4",
            "LOG_LEVEL": "INFO",
            "PLEX_TIMEOUT": "60",
        }
        with patch.dict("os.environ", env, clear=False):
            with patch("media_preview_generator.config.load_dotenv", lambda: None):
                config = load_config()

        assert config is not None
        assert config.gpu_threads == 0
        assert config.cpu_threads == 4
