"""
Tests for config.py module.

Tests configuration loading, validation, path checking,
FFmpeg detection, and environment-specific behavior.
"""

import os
import tempfile
import pytest
from unittest.mock import MagicMock, patch
from plex_generate_previews.config import (
    get_config_value,
    load_config,
    Config
)


class TestGetConfigValue:
    """Test config value precedence."""
    
    def test_get_config_value_cli_precedence(self):
        """Test that CLI args take precedence over env vars."""
        cli_args = MagicMock()
        cli_args.test_field = "cli_value"
        
        with patch.dict('os.environ', {'TEST_FIELD': 'env_value'}):
            result = get_config_value(cli_args, 'test_field', 'TEST_FIELD', 'default')
            assert result == "cli_value"
    
    def test_get_config_value_env_fallback(self):
        """Test that env vars are used when CLI args are None."""
        cli_args = MagicMock()
        cli_args.test_field = None
        
        with patch.dict('os.environ', {'TEST_FIELD': 'env_value'}):
            result = get_config_value(cli_args, 'test_field', 'TEST_FIELD', 'default')
            assert result == "env_value"
    
    def test_get_config_value_default_fallback(self):
        """Test that defaults are used when neither CLI nor env are set."""
        cli_args = MagicMock()
        cli_args.test_field = None
        
        with patch.dict('os.environ', {}, clear=True):
            result = get_config_value(cli_args, 'test_field', 'TEST_FIELD', 'default')
            assert result == "default"
    
    def test_get_config_value_boolean_conversion(self):
        """Test boolean value conversion."""
        cli_args = MagicMock()
        cli_args.bool_field = None
        
        # Test true values
        for value in ['true', 'True', '1', 'yes', 'YES']:
            with patch.dict('os.environ', {'BOOL_FIELD': value}):
                result = get_config_value(cli_args, 'bool_field', 'BOOL_FIELD', False, bool)
                assert result is True
        
        # Test false values
        for value in ['false', 'False', '0', 'no', 'NO']:
            with patch.dict('os.environ', {'BOOL_FIELD': value}):
                result = get_config_value(cli_args, 'bool_field', 'BOOL_FIELD', True, bool)
                assert result is False
    
    def test_get_config_value_int_conversion(self):
        """Test integer value conversion."""
        cli_args = MagicMock()
        cli_args.int_field = None
        
        with patch.dict('os.environ', {'INT_FIELD': '42'}):
            result = get_config_value(cli_args, 'int_field', 'INT_FIELD', 0, int)
            assert result == 42


class TestLoadConfig:
    """Test configuration loading and validation."""
    
    @patch('shutil.which')
    @patch('subprocess.run')
    @patch('os.path.exists')
    @patch('os.path.isdir')
    @patch('os.listdir')
    @patch('os.access')
    @patch('os.statvfs')
    @patch('plex_generate_previews.cli.setup_logging')
    def test_load_config_all_required_present(self, mock_logging, mock_statvfs, mock_access, 
                                              mock_listdir, mock_isdir, mock_exists, 
                                              mock_run, mock_which):
        """Test that valid config loads successfully."""
        # Mock FFmpeg
        mock_which.return_value = '/usr/bin/ffmpeg'
        mock_run.return_value = MagicMock(returncode=0, stdout="ffmpeg version 7.0.0")
        
        # Mock file system - need to handle nested directory checks
        def mock_exists_fn(path):
            return True
        
        def mock_listdir_fn(path):
            # Check the specific path to determine what to return
            if 'tmp' in path or path.startswith('/tmp'):
                # Tmp folder should be empty or not exist
                return []
            elif path.endswith('/localhost') or '/localhost' in path and not path.endswith('Media'):
                # Inside localhost directory - return hex folders
                return ['0', '1', '2', '3', '4', '5', '6', '7', '8', '9', 'a', 'b', 'c', 'd', 'e', 'f']
            elif path.endswith('/Media'):
                # Inside Media directory - return localhost
                return ['localhost']
            else:
                # Top-level Plex directory - return standard folders
                return ['Cache', 'Media', 'Metadata', 'Plug-ins', 'Logs']
        
        mock_exists.side_effect = mock_exists_fn
        mock_isdir.return_value = True
        mock_listdir.side_effect = mock_listdir_fn
        mock_access.return_value = True
        
        # Mock disk space
        statvfs_result = MagicMock()
        statvfs_result.f_frsize = 4096
        statvfs_result.f_bavail = 1024 * 1024 * 250  # 1GB+ free
        mock_statvfs.return_value = statvfs_result
        
        # Create args - use SimpleNamespace to avoid MagicMock attribute issues
        from types import SimpleNamespace
        args = SimpleNamespace(
            plex_url="http://localhost:32400",
            plex_token="test_token",
            plex_config_folder="/config/plex/Library/Application Support/Plex Media Server",
            plex_timeout=60,
            plex_libraries=None,
            plex_local_videos_path_mapping=None,
            plex_videos_path_mapping=None,
            plex_bif_frame_interval=None,
            thumbnail_quality=None,
            regenerate_thumbnails=False,
            gpu_threads=None,
            cpu_threads=None,
            gpu_selection=None,
            tmp_folder="/tmp/plex_generate_previews",
            log_level=None
        )
        
        config = load_config(args)
        
        assert config is not None
        assert config.plex_url == "http://localhost:32400"
        assert config.plex_token == "test_token"
    
    @patch('shutil.which')
    @patch('plex_generate_previews.cli.setup_logging')
    def test_load_config_missing_plex_url(self, mock_logging, mock_which):
        """Test error when PLEX_URL is missing."""
        mock_which.return_value = '/usr/bin/ffmpeg'
        
        from types import SimpleNamespace
        args = SimpleNamespace(
            plex_url=None,
            plex_token="token",
            plex_config_folder="/config/plex",
            plex_timeout=None,
            plex_libraries=None,
            plex_local_videos_path_mapping=None,
            plex_videos_path_mapping=None,
            plex_bif_frame_interval=None,
            thumbnail_quality=None,
            regenerate_thumbnails=False,
            gpu_threads=None,
            cpu_threads=None,
            gpu_selection=None,
            tmp_folder=None,
            log_level=None
        )
        
        with patch.dict('os.environ', {}, clear=True):
            config = load_config(args)
            assert config is None
    
    @patch('shutil.which')
    @patch('plex_generate_previews.cli.setup_logging')
    def test_load_config_missing_plex_token(self, mock_logging, mock_which):
        """Test error when PLEX_TOKEN is missing."""
        mock_which.return_value = '/usr/bin/ffmpeg'
        
        from types import SimpleNamespace
        args = SimpleNamespace(
            plex_url="http://localhost:32400",
            plex_token=None,
            plex_config_folder="/config/plex",
            plex_timeout=None,
            plex_libraries=None,
            plex_local_videos_path_mapping=None,
            plex_videos_path_mapping=None,
            plex_bif_frame_interval=None,
            thumbnail_quality=None,
            regenerate_thumbnails=False,
            gpu_threads=None,
            cpu_threads=None,
            gpu_selection=None,
            tmp_folder=None,
            log_level=None
        )
        
        with patch.dict('os.environ', {}, clear=True):
            config = load_config(args)
            assert config is None
    
    @patch('shutil.which')
    @patch('plex_generate_previews.cli.setup_logging')
    def test_load_config_missing_config_folder(self, mock_logging, mock_which):
        """Test error when config folder is missing."""
        mock_which.return_value = '/usr/bin/ffmpeg'
        
        from types import SimpleNamespace
        args = SimpleNamespace(
            plex_url="http://localhost:32400",
            plex_token="token",
            plex_config_folder=None,
            plex_timeout=None,
            plex_libraries=None,
            plex_local_videos_path_mapping=None,
            plex_videos_path_mapping=None,
            plex_bif_frame_interval=None,
            thumbnail_quality=None,
            regenerate_thumbnails=False,
            gpu_threads=None,
            cpu_threads=None,
            gpu_selection=None,
            tmp_folder=None,
            log_level=None
        )
        
        with patch.dict('os.environ', {}, clear=True):
            config = load_config(args)
            assert config is None
    
    @patch('shutil.which')
    @patch('subprocess.run')
    @patch('os.path.exists')
    @patch('plex_generate_previews.cli.setup_logging')
    def test_load_config_invalid_path(self, mock_logging, mock_exists, mock_run, mock_which):
        """Test error when config folder doesn't exist."""
        mock_which.return_value = '/usr/bin/ffmpeg'
        mock_run.return_value = MagicMock(returncode=0, stdout="ffmpeg version 7.0.0")
        mock_exists.return_value = False
        
        from types import SimpleNamespace
        args = SimpleNamespace(
            plex_url="http://localhost:32400",
            plex_token="token",
            plex_config_folder="/nonexistent/path",
            plex_timeout=None,
            plex_libraries=None,
            plex_local_videos_path_mapping=None,
            plex_videos_path_mapping=None,
            plex_bif_frame_interval=None,
            thumbnail_quality=None,
            regenerate_thumbnails=False,
            gpu_threads=None,
            cpu_threads=None,
            gpu_selection=None,
            tmp_folder=None,
            log_level=None
        )
        
        config = load_config(args)
        assert config is None
    
    @patch('shutil.which')
    @patch('subprocess.run')
    @patch('os.listdir')
    @patch('os.path.isdir')
    @patch('os.path.exists')
    @patch('plex_generate_previews.cli.setup_logging')
    def test_load_config_invalid_plex_structure(self, mock_logging, mock_exists,
                                                mock_isdir, mock_listdir, mock_run, mock_which):
        """Test error when folder doesn't have Plex structure."""
        mock_which.return_value = '/usr/bin/ffmpeg'
        mock_run.return_value = MagicMock(returncode=0, stdout="ffmpeg version 7.0.0")
        mock_exists.return_value = True
        mock_isdir.return_value = True
        mock_listdir.return_value = ['random', 'folders']  # Missing Cache and Media
        
        from types import SimpleNamespace
        args = SimpleNamespace(
            plex_url="http://localhost:32400",
            plex_token="token",
            plex_config_folder="/wrong/folder",
            plex_timeout=None,
            plex_libraries=None,
            plex_local_videos_path_mapping=None,
            plex_videos_path_mapping=None,
            plex_bif_frame_interval=None,
            thumbnail_quality=None,
            regenerate_thumbnails=False,
            gpu_threads=None,
            cpu_threads=None,
            gpu_selection=None,
            tmp_folder=None,
            log_level=None
        )
        
        config = load_config(args)
        assert config is None
    
    @patch('shutil.which')
    @patch('subprocess.run')
    @patch('os.statvfs')
    @patch('os.access')
    @patch('os.listdir')
    @patch('os.path.isdir')
    @patch('os.path.exists')
    @patch('plex_generate_previews.cli.setup_logging')
    def test_load_config_validates_numeric_ranges(self, mock_logging, mock_exists, 
                                                  mock_isdir, mock_listdir, mock_access, 
                                                  mock_statvfs, mock_run, mock_which):
        """Test validation of numeric ranges."""
        mock_which.return_value = '/usr/bin/ffmpeg'
        mock_run.return_value = MagicMock(returncode=0)
        mock_exists.return_value = True
        mock_isdir.return_value = True
        mock_listdir.side_effect = lambda path: ['Cache', 'Media'] if 'Plex Media Server' in path else ['0', '1', '2', 'a', 'b', 'c']
        mock_access.return_value = True
        statvfs_result = MagicMock()
        statvfs_result.f_frsize = 4096
        statvfs_result.f_bavail = 1024 * 1024 * 250
        mock_statvfs.return_value = statvfs_result
        
        args = MagicMock()
        args.plex_url = "http://localhost:32400"
        args.plex_token = "token"
        args.plex_config_folder = "/config/plex/Library/Application Support/Plex Media Server"
        args.plex_timeout = None
        args.plex_libraries = None
        args.plex_local_videos_path_mapping = None
        args.plex_videos_path_mapping = None
        args.plex_bif_frame_interval = 100  # Invalid: > 60
        args.thumbnail_quality = None
        args.regenerate_thumbnails = False
        args.gpu_threads = None
        args.cpu_threads = None
        args.gpu_selection = None
        args.tmp_folder = "/tmp/plex_generate_previews"
        args.log_level = None
        
        config = load_config(args)
        
        # Should fail validation due to invalid frame interval
        assert config is None
    
    @patch('shutil.which')
    @patch('subprocess.run')
    @patch('os.statvfs')
    @patch('os.access')
    @patch('os.listdir')
    @patch('os.path.isdir')
    @patch('os.path.exists')
    @patch('plex_generate_previews.cli.setup_logging')
    def test_load_config_validates_thread_counts(self, mock_logging, mock_exists, 
                                                 mock_isdir, mock_listdir, mock_access, 
                                                 mock_statvfs, mock_run, mock_which):
        """Test validation of thread counts."""
        mock_which.return_value = '/usr/bin/ffmpeg'
        mock_run.return_value = MagicMock(returncode=0)
        mock_exists.return_value = True
        mock_isdir.return_value = True
        mock_listdir.side_effect = lambda path: ['Cache', 'Media'] if 'Plex Media Server' in path else ['0', '1', 'a', 'b']
        mock_access.return_value = True
        statvfs_result = MagicMock()
        statvfs_result.f_frsize = 4096
        statvfs_result.f_bavail = 1024 * 1024 * 250
        mock_statvfs.return_value = statvfs_result
        
        args = MagicMock()
        args.plex_url = "http://localhost:32400"
        args.plex_token = "token"
        args.plex_config_folder = "/config/plex/Library/Application Support/Plex Media Server"
        args.plex_timeout = None
        args.plex_libraries = None
        args.plex_local_videos_path_mapping = None
        args.plex_videos_path_mapping = None
        args.plex_bif_frame_interval = None
        args.thumbnail_quality = None
        args.regenerate_thumbnails = False
        args.gpu_threads = 50  # Invalid: > 32
        args.cpu_threads = None
        args.gpu_selection = None
        args.tmp_folder = "/tmp/plex_generate_previews"
        args.log_level = None
        
        config = load_config(args)
        
        # Should fail validation due to invalid thread count
        assert config is None
    
    @patch('shutil.which')
    @patch('subprocess.run')
    @patch('os.statvfs')
    @patch('os.access')
    @patch('os.listdir')
    @patch('os.path.isdir')
    @patch('os.path.exists')
    @patch('plex_generate_previews.cli.setup_logging')
    def test_load_config_validates_gpu_selection(self, mock_logging, mock_exists, 
                                                 mock_isdir, mock_listdir, mock_access, 
                                                 mock_statvfs, mock_run, mock_which):
        """Test validation of GPU selection format."""
        mock_which.return_value = '/usr/bin/ffmpeg'
        mock_run.return_value = MagicMock(returncode=0)
        mock_exists.return_value = True
        mock_isdir.return_value = True
        mock_listdir.side_effect = lambda path: ['Cache', 'Media'] if 'Plex Media Server' in path else ['0', '1', 'a', 'b']
        mock_access.return_value = True
        statvfs_result = MagicMock()
        statvfs_result.f_frsize = 4096
        statvfs_result.f_bavail = 1024 * 1024 * 250
        mock_statvfs.return_value = statvfs_result
        
        args = MagicMock()
        args.plex_url = "http://localhost:32400"
        args.plex_token = "token"
        args.plex_config_folder = "/config/plex/Library/Application Support/Plex Media Server"
        args.plex_timeout = None
        args.plex_libraries = None
        args.plex_local_videos_path_mapping = None
        args.plex_videos_path_mapping = None
        args.plex_bif_frame_interval = None
        args.thumbnail_quality = None
        args.regenerate_thumbnails = False
        args.gpu_threads = None
        args.cpu_threads = None
        args.gpu_selection = "invalid,format,abc"  # Contains non-numeric
        args.tmp_folder = "/tmp/plex_generate_previews"
        args.log_level = None
        
        config = load_config(args)
        
        # Should fail validation due to invalid GPU selection
        assert config is None
    
    @patch('shutil.which')
    @patch('subprocess.run')
    @patch('os.statvfs')
    @patch('os.access')
    @patch('os.listdir')
    @patch('os.path.isdir')
    @patch('os.path.exists')
    @patch('os.makedirs')
    @patch('plex_generate_previews.cli.setup_logging')
    def test_load_config_tmp_folder_auto_creation(self, mock_logging, mock_makedirs, mock_exists, mock_isdir, 
                                                   mock_listdir, mock_access, mock_statvfs, mock_run, mock_which):
        """Test that temp folder is auto-created if it doesn't exist."""
        mock_which.return_value = '/usr/bin/ffmpeg'
        mock_run.return_value = MagicMock(returncode=0, stdout="ffmpeg version 7.0.0")
        
        # Mock that tmp_folder doesn't exist initially, but plex_config_folder does
        def mock_exists_side_effect(path):
            if path == "/tmp/plex_generate_previews":
                return False  # tmp folder doesn't exist
            return True  # other paths exist
        
        def mock_listdir_fn(path):
            if 'tmp' in path or path.startswith('/tmp'):
                return []
            elif path.endswith('/localhost') or '/localhost' in path and not path.endswith('Media'):
                return ['0', '1', '2', '3', '4', '5', '6', '7', '8', '9', 'a', 'b', 'c', 'd', 'e', 'f']
            elif path.endswith('/Media'):
                return ['localhost']
            else:
                return ['Cache', 'Media', 'Metadata', 'Plug-ins', 'Logs']
        
        mock_exists.side_effect = mock_exists_side_effect
        mock_isdir.return_value = True
        mock_listdir.side_effect = mock_listdir_fn
        mock_access.return_value = True
        
        # Mock statvfs for disk space check
        mock_stat = MagicMock()
        mock_stat.f_frsize = 4096
        mock_stat.f_bavail = 1024 * 1024  # Plenty of space
        mock_statvfs.return_value = mock_stat
        
        from types import SimpleNamespace
        args = SimpleNamespace(
            plex_url="http://localhost:32400",
            plex_token="token",
            plex_config_folder="/config/plex",
            tmp_folder="/tmp/plex_generate_previews",
            plex_timeout=None,
            plex_libraries=None,
            plex_local_videos_path_mapping=None,
            plex_videos_path_mapping=None,
            plex_bif_frame_interval=None,
            thumbnail_quality=None,
            regenerate_thumbnails=False,
            gpu_threads=None,
            cpu_threads=None,
            gpu_selection=None,
            log_level=None
        )
        
        config = load_config(args)
        
        # Should succeed and create the folder
        assert config is not None
        assert config.tmp_folder_created_by_us is True
        mock_makedirs.assert_called_once_with("/tmp/plex_generate_previews", exist_ok=True)
    
    @patch('shutil.which')
    @patch('subprocess.run')
    @patch('os.statvfs')
    @patch('os.access')
    @patch('os.listdir')
    @patch('os.path.isdir')
    @patch('os.path.exists')
    @patch('plex_generate_previews.cli.setup_logging')
    def test_load_config_tmp_folder_not_empty(self, mock_logging, mock_exists, mock_isdir, 
                                               mock_listdir, mock_access, mock_statvfs, mock_run, mock_which):
        """Test that config loads successfully even if tmp folder is not empty."""
        mock_which.return_value = '/usr/bin/ffmpeg'
        mock_run.return_value = MagicMock(returncode=0, stdout="ffmpeg version 7.0.0")
        
        def mock_exists_side_effect(path):
            return True  # All paths exist
        
        def mock_listdir_side_effect(path):
            if path == "/tmp/plex_generate_previews":
                return ['file1.txt', 'file2.txt']  # tmp folder has contents - should be OK
            elif path.endswith('/localhost') or '/localhost' in path and not path.endswith('Media'):
                return ['0', '1', '2', '3', '4', '5', '6', '7', '8', '9', 'a', 'b', 'c', 'd', 'e', 'f']
            elif path.endswith('/Media'):
                return ['localhost']
            else:
                return ['Cache', 'Media', 'Metadata', 'Plug-ins', 'Logs']
        
        mock_exists.side_effect = mock_exists_side_effect
        mock_isdir.return_value = True
        mock_listdir.side_effect = mock_listdir_side_effect
        mock_access.return_value = True
        
        # Mock statvfs for disk space check
        mock_stat = MagicMock()
        mock_stat.f_frsize = 4096
        mock_stat.f_bavail = 1024 * 1024  # Plenty of space
        mock_statvfs.return_value = mock_stat
        
        from types import SimpleNamespace
        args = SimpleNamespace(
            plex_url="http://localhost:32400",
            plex_token="token",
            plex_config_folder="/config/plex",
            tmp_folder="/tmp/plex_generate_previews",
            plex_timeout=None,
            plex_libraries=None,
            plex_local_videos_path_mapping=None,
            plex_videos_path_mapping=None,
            plex_bif_frame_interval=None,
            thumbnail_quality=None,
            regenerate_thumbnails=False,
            gpu_threads=None,
            cpu_threads=None,
            gpu_selection=None,
            log_level=None
        )
        
        config = load_config(args)
        
        # Should succeed even though tmp folder is not empty
        assert config is not None
        assert config.tmp_folder == "/tmp/plex_generate_previews"
    
    @patch('shutil.which')
    @patch('plex_generate_previews.cli.setup_logging')
    def test_load_config_ffmpeg_not_found(self, mock_logging, mock_which):
        """Test error when FFmpeg is not found."""
        mock_which.return_value = None
        
        args = MagicMock()
        args.plex_url = "http://localhost:32400"
        args.plex_token = "token"
        args.plex_config_folder = "/config/plex"
        
        with pytest.raises(SystemExit):
            load_config(args)
    
    @patch('shutil.which')
    @patch('subprocess.run')
    @patch('os.statvfs')
    @patch('os.access')
    @patch('os.listdir')
    @patch('os.path.isdir')
    @patch('os.path.exists')
    @patch('plex_generate_previews.utils.is_docker_environment')
    @patch('plex_generate_previews.cli.setup_logging')
    def test_load_config_docker_environment(self, mock_logging, mock_docker, mock_exists, 
                                           mock_isdir, mock_listdir, mock_access, 
                                           mock_statvfs, mock_run, mock_which):
        """Test Docker-specific error messages."""
        mock_docker.return_value = True
        mock_which.return_value = '/usr/bin/ffmpeg'
        mock_run.return_value = MagicMock(returncode=0, stdout="ffmpeg version 7.0.0")
        
        # Setup filesystem mocks (even though we expect early failure)
        mock_exists.return_value = True
        mock_isdir.return_value = True
        mock_listdir.return_value = ['Cache', 'Media']
        mock_access.return_value = True
        statvfs_result = MagicMock()
        statvfs_result.f_frsize = 4096
        statvfs_result.f_bavail = 1024 * 1024 * 250  # 1GB+ free
        mock_statvfs.return_value = statvfs_result
        
        from types import SimpleNamespace
        args = SimpleNamespace(
            plex_url=None,  # Missing required field
            plex_token="token",
            plex_config_folder="/config/plex",
            plex_timeout=None,
            plex_libraries=None,
            plex_local_videos_path_mapping=None,
            plex_videos_path_mapping=None,
            plex_bif_frame_interval=None,
            thumbnail_quality=None,
            regenerate_thumbnails=False,
            gpu_threads=None,
            cpu_threads=None,
            gpu_selection=None,
            tmp_folder=None,
            log_level=None
        )
        
        with patch.dict('os.environ', {}, clear=True):
            config = load_config(args)
            assert config is None
    
    @patch('shutil.which')
    @patch('subprocess.run')
    @patch('os.statvfs')
    @patch('os.access')
    @patch('os.listdir')
    @patch('os.path.isdir')
    @patch('os.path.exists')
    @patch('plex_generate_previews.cli.setup_logging')
    def test_load_config_comma_separated_libraries(self, mock_logging, mock_exists, 
                                                   mock_isdir, mock_listdir, mock_access, 
                                                   mock_statvfs, mock_run, mock_which):
        """Test parsing comma-separated library list."""
        mock_which.return_value = '/usr/bin/ffmpeg'
        mock_run.return_value = MagicMock(returncode=0, stdout="ffmpeg version 7.0.0")
        mock_exists.return_value = True
        mock_isdir.return_value = True
        
        def mock_listdir_fn(path):
            # Check the specific path to determine what to return
            if 'tmp' in path or path.startswith('/tmp'):
                # Tmp folder should be empty or not exist
                return []
            elif path.endswith('/localhost') or '/localhost' in path and not path.endswith('Media'):
                # Inside localhost directory - return hex folders
                return ['0', '1', '2', '3', '4', '5', '6', '7', '8', '9', 'a', 'b', 'c', 'd', 'e', 'f']
            elif path.endswith('/Media'):
                # Inside Media directory - return localhost
                return ['localhost']
            else:
                # Top-level Plex directory - return standard folders
                return ['Cache', 'Media', 'Metadata', 'Plug-ins', 'Logs']
        
        mock_listdir.side_effect = mock_listdir_fn
        mock_access.return_value = True
        statvfs_result = MagicMock()
        statvfs_result.f_frsize = 4096
        statvfs_result.f_bavail = 1024 * 1024 * 250
        mock_statvfs.return_value = statvfs_result
        
        from types import SimpleNamespace
        args = SimpleNamespace(
            plex_url="http://localhost:32400",
            plex_token="token",
            plex_config_folder="/config/plex/Library/Application Support/Plex Media Server",
            plex_timeout=None,
            plex_libraries="Movies, TV Shows, Anime",
            plex_local_videos_path_mapping=None,
            plex_videos_path_mapping=None,
            plex_bif_frame_interval=None,
            thumbnail_quality=None,
            regenerate_thumbnails=False,
            gpu_threads=None,
            cpu_threads=None,
            gpu_selection=None,
            tmp_folder="/tmp/plex_generate_previews",
            log_level=None
        )
        
        config = load_config(args)
        
        assert config is not None
        assert 'movies' in config.plex_libraries
        assert 'tv shows' in config.plex_libraries
        assert 'anime' in config.plex_libraries

