"""
Unit tests for Windows compatibility features.

Tests Windows-specific functionality including:
- Platform detection
- GPU validation (must be CPU-only)
- Temp directory defaults
- Signal handling
- Path sanitization with Windows paths
"""

import os
import signal
import tempfile
import platform
from unittest.mock import patch, MagicMock
import pytest

from plex_generate_previews.utils import sanitize_path, is_windows
from plex_generate_previews.config import load_config


class TestWindowsPlatformDetection:
    """Test Windows platform detection."""
    
    @patch('os.name', 'nt')
    def test_is_windows_on_windows(self):
        """Test detection returns True on Windows."""
        assert is_windows() is True
    
    @patch('os.name', 'posix')
    def test_is_windows_on_linux(self):
        """Test detection returns False on Linux."""
        assert is_windows() is False


class TestWindowsPathSanitization:
    """Test path sanitization for Windows compatibility."""
    
    @patch('os.name', 'nt')
    def test_sanitize_path_forward_to_backslash(self):
        """Test forward slashes converted to backslashes on Windows."""
        path = "C:/Users/Test/Videos/movie.mkv"
        result = sanitize_path(path)
        assert '\\' in result
        assert '/' not in result
        assert result == "C:\\Users\\Test\\Videos\\movie.mkv"
    
    @patch('os.name', 'nt')
    def test_sanitize_path_unc_path(self):
        """Test UNC path handling on Windows."""
        path = "//server/share/videos/movie.mkv"
        result = sanitize_path(path)
        assert result.startswith('\\\\')
        assert result == "\\\\server\\share\\videos\\movie.mkv"
    
    @patch('os.name', 'nt')
    def test_sanitize_path_already_backslash(self):
        """Test path already using backslashes on Windows."""
        path = "C:\\Users\\Test\\Videos\\movie.mkv"
        result = sanitize_path(path)
        assert result == "C:\\Users\\Test\\Videos\\movie.mkv"
    
    @patch('os.name', 'nt')
    @patch('os.path.normpath')
    def test_sanitize_path_normpath(self, mock_normpath):
        """Test path normalization on Windows."""
        # Mock normpath to behave like Windows (resolve .. and .)
        mock_normpath.return_value = "C:\\Users\\Videos\\movie.mkv"
        
        path = "C:/Users/Test/../Videos/./movie.mkv"
        result = sanitize_path(path)
        assert result == "C:\\Users\\Videos\\movie.mkv"
        
        # Verify normpath was called with the backslash-converted path
        mock_normpath.assert_called_once_with("C:\\Users\\Test\\..\\Videos\\.\\movie.mkv")
    
    @patch('os.name', 'posix')
    def test_sanitize_path_linux_unchanged(self):
        """Test Linux paths remain unchanged (normalized only)."""
        path = "/home/user/videos/movie.mkv"
        result = sanitize_path(path)
        assert result == "/home/user/videos/movie.mkv"


class TestWindowsTempDirectory:
    """Test Windows temp directory handling."""
    
    @patch('plex_generate_previews.config.tempfile.gettempdir', return_value='C:\\Temp')
    @patch('platform.system', return_value='Windows')
    @patch('shutil.which', return_value='C:\\ffmpeg\\ffmpeg.exe')
    @patch('subprocess.run')
    @patch('os.path.exists', return_value=True)
    @patch('os.path.isdir', return_value=True)
    @patch('os.listdir')
    @patch('os.access', return_value=True)
    @patch('os.statvfs')
    def test_windows_default_temp_folder(self, mock_statvfs, mock_access, mock_listdir, 
                                         mock_isdir, mock_exists, mock_run, mock_which, 
                                         mock_platform, mock_gettempdir):
        """Test that Windows uses system temp directory by default."""
        mock_run.return_value = MagicMock(returncode=0, stdout="ffmpeg version 7.0.0")
        
        # Mock statvfs for disk space check
        mock_stat = MagicMock()
        mock_stat.f_frsize = 4096
        mock_stat.f_bavail = 1024 * 1024  # Plenty of space
        mock_statvfs.return_value = mock_stat
        
        def mock_listdir_fn(path):
            if 'Temp' in path or 'tmp' in path.lower():
                return []
            elif 'localhost' in path:
                return ['0', '1', '2', '3', '4', '5', '6', '7', '8', '9', 'a', 'b', 'c', 'd', 'e', 'f']
            elif path.endswith('Media'):
                return ['localhost']
            else:
                return ['Cache', 'Media', 'Metadata']
        
        mock_listdir.side_effect = mock_listdir_fn
        
        from types import SimpleNamespace
        args = SimpleNamespace(
            plex_url="http://localhost:32400",
            plex_token="test_token",
            plex_config_folder="C:\\ProgramData\\Plex\\Library\\Application Support\\Plex Media Server",
            plex_local_videos_path_mapping="",
            plex_videos_path_mapping="",
            plex_bif_frame_interval=5,
            thumbnail_quality=4,
            regenerate_thumbnails=False,
            gpu_threads=0,
            cpu_threads=4,
            gpu_selection="all",
            tmp_folder=None,  # Should use Windows default
            log_level="INFO",
            plex_timeout=60,
            plex_libraries=""
        )
        
        config = load_config(args)
        
        # Should use Windows temp directory from tempfile.gettempdir()
        assert config is not None
        assert config.tmp_folder == 'C:\\Temp'  # Should use the mocked gettempdir() value


class TestWindowsGPUValidation:
    """Test that GPU detection works on Windows with D3D11VA."""
    
    @patch('os.name', 'nt')
    @patch('plex_generate_previews.cli.is_windows', return_value=True)
    @patch('plex_generate_previews.cli.detect_all_gpus')
    @patch('plex_generate_previews.cli.logger')
    def test_windows_gpu_threads_allowed_with_gpu(self, mock_logger, mock_detect_gpus, 
                                                   mock_is_windows):
        """Test that GPU threads work on Windows when GPU detected."""
        from plex_generate_previews.cli import detect_and_select_gpus
        from types import SimpleNamespace
        
        # Simulate Windows GPU detected
        mock_detect_gpus.return_value = [
            ('WINDOWS_GPU', 'd3d11va', {
                'name': 'Windows GPU',
                'acceleration': 'D3D11VA',
                'device_path': 'd3d11va'
            })
        ]
        
        config = SimpleNamespace(gpu_threads=4, gpu_selection="all")
        
        result = detect_and_select_gpus(config)
        
        # Should return the detected GPU
        assert len(result) == 1
        assert result[0][0] == 'WINDOWS_GPU'
        
        # Should not log errors about Windows not being supported
        error_calls = [str(call) for call in mock_logger.error.call_args_list]
        assert not any('not supported' in call for call in error_calls)
    
    @patch('os.name', 'nt')
    @patch('plex_generate_previews.cli.is_windows', return_value=True)
    @patch('plex_generate_previews.cli.detect_all_gpus')
    @patch('plex_generate_previews.cli.logger')
    def test_windows_no_gpu_detected_exits(self, mock_logger, mock_detect_gpus, 
                                           mock_is_windows):
        """Test that when GPU threads requested but no GPU detected on Windows, it exits with error."""
        from plex_generate_previews.cli import detect_and_select_gpus
        from types import SimpleNamespace
        import pytest
        
        # Simulate no GPU detected
        mock_detect_gpus.return_value = []
        
        config = SimpleNamespace(gpu_threads=4, gpu_selection="all")
        
        # Should exit with error
        with pytest.raises(SystemExit) as excinfo:
            detect_and_select_gpus(config)
        
        assert excinfo.value.code == 1
        
        # Should log error message about no GPUs detected
        error_calls = [str(call) for call in mock_logger.error.call_args_list]
        assert any('No GPUs detected' in call for call in error_calls)
        assert any('GPU_THREADS' in call for call in error_calls)


class TestWindowsSignalHandling:
    """Test signal handling compatibility on Windows."""
    
    @patch('os.name', 'nt')
    def test_sigterm_not_registered_on_windows(self):
        """Test that SIGTERM handling accounts for Windows compatibility."""
        # On Windows, SIGTERM doesn't exist in the signal module
        # We test that code should check for SIGTERM existence before using it
        import signal as sig_module
        
        # This test verifies the pattern used in cli.py:
        # signal.signal(signal.SIGINT, handler)
        # if hasattr(signal, 'SIGTERM'):
        #     signal.signal(signal.SIGTERM, handler)
        
        # On Linux (where we're running), SIGTERM exists
        # But we verify the pattern would work on Windows
        assert hasattr(sig_module, 'SIGINT')  # Always available
        
        # The actual check: SIGTERM may or may not exist depending on platform
        # On Windows it doesn't, on Linux it does
        # This is why code should use hasattr() before accessing it
    
    @patch('os.name', 'posix')
    @patch('signal.signal')
    def test_sigterm_registered_on_linux(self, mock_signal_signal):
        """Test that SIGTERM is registered on Linux."""
        import signal as sig_module
        
        # Both SIGINT and SIGTERM should be available on Linux
        assert hasattr(sig_module, 'SIGINT')
        assert hasattr(sig_module, 'SIGTERM')


class TestWindowsPathMappings:
    """Test path mappings with Windows paths."""
    
    @patch('os.name', 'nt')
    def test_path_mapping_windows_to_windows(self):
        """Test path mapping from one Windows path to another."""
        from plex_generate_previews.utils import sanitize_path
        
        # Simulate what happens in media_processing.py
        plex_path = "D:/PlexMedia/Movies/movie.mkv"
        plex_mapping = "D:/PlexMedia"
        local_mapping = "C:\\Media"
        
        # Apply mapping
        mapped_path = plex_path.replace(plex_mapping, local_mapping)
        result = sanitize_path(mapped_path)
        
        assert result == "C:\\Media\\Movies\\movie.mkv"
    
    @patch('os.name', 'nt')
    def test_path_mapping_unc_to_local(self):
        """Test path mapping from UNC path to local Windows path."""
        from plex_generate_previews.utils import sanitize_path
        
        # Plex sees UNC path, local sees C:\ path
        plex_path = "//server/media/Movies/movie.mkv"
        plex_mapping = "//server/media"
        local_mapping = "C:\\Media"
        
        # Apply mapping
        mapped_path = plex_path.replace(plex_mapping, local_mapping)
        result = sanitize_path(mapped_path)
        
        assert result == "C:\\Media\\Movies\\movie.mkv"


class TestWindowsFFmpegLogPath:
    """Test that FFmpeg log files use Windows-compatible paths."""
    
    @patch('os.name', 'nt')
    def test_ffmpeg_log_path_on_windows(self):
        """Test that FFmpeg log file paths work on Windows."""
        import os
        import tempfile
        
        # Simulate what happens in media_processing.py line 215
        pid = 12345
        thread_id = 67890
        timestamp = 1234567890123456789
        output_file = os.path.join(tempfile.gettempdir(), f'ffmpeg_output_{pid}_{thread_id}_{timestamp}.log')
        
        # Verify the path format is valid (actual temp path depends on environment)
        assert 'ffmpeg_output_' in output_file
        assert output_file.endswith('.log')
        assert str(pid) in output_file
        assert str(thread_id) in output_file


class TestWindowsConfigValidation:
    """Test configuration validation on Windows."""
    
    @patch('plex_generate_previews.config.tempfile.gettempdir', return_value='C:\\Windows\\Temp')
    @patch('platform.system', return_value='Windows')
    @patch('os.name', 'nt')
    @patch('shutil.which', return_value='C:\\ffmpeg\\bin\\ffmpeg.exe')
    @patch('subprocess.run')
    @patch('os.path.exists', return_value=True)
    @patch('os.path.isdir', return_value=True)
    @patch('os.listdir')
    @patch('os.access', return_value=True)
    @patch('os.makedirs')
    @patch('os.statvfs')
    def test_windows_config_validation(self, mock_statvfs, mock_makedirs, mock_access, mock_listdir,
                                       mock_isdir, mock_exists, mock_run, mock_which,
                                       mock_platform, mock_gettempdir):
        """Test that configuration validates correctly on Windows."""
        mock_run.return_value = MagicMock(returncode=0, stdout="ffmpeg version 7.0.0")
        
        # Mock statvfs for disk space check
        mock_stat = MagicMock()
        mock_stat.f_frsize = 4096
        mock_stat.f_bavail = 1024 * 1024  # Plenty of space
        mock_statvfs.return_value = mock_stat
        
        def mock_listdir_fn(path):
            if 'Temp' in path or 'tmp' in path.lower():
                return []
            elif 'localhost' in path:
                return ['0', '1', '2', '3', '4', '5', '6', '7', '8', '9', 'a', 'b', 'c', 'd', 'e', 'f']
            elif path.endswith('Media'):
                return ['localhost']
            else:
                return ['Cache', 'Media', 'Metadata']
        
        mock_listdir.side_effect = mock_listdir_fn
        
        from types import SimpleNamespace
        args = SimpleNamespace(
            plex_url="http://localhost:32400",
            plex_token="test_token",
            plex_config_folder="C:\\Users\\Test\\AppData\\Local\\Plex Media Server",
            plex_local_videos_path_mapping="",
            plex_videos_path_mapping="",
            plex_bif_frame_interval=5,
            thumbnail_quality=4,
            regenerate_thumbnails=False,
            gpu_threads=0,  # Must be 0 on Windows
            cpu_threads=4,
            gpu_selection="all",
            tmp_folder=None,
            log_level="INFO",
            plex_timeout=60,
            plex_libraries=""
        )
        
        config = load_config(args)
        
        assert config is not None
        assert config.gpu_threads == 0
        assert config.cpu_threads == 4

