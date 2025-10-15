"""
Tests for CLI functionality.
"""

import pytest
import sys
import signal
from unittest.mock import patch, MagicMock, Mock, call
from plex_generate_previews.cli import (
    parse_arguments,
    setup_logging,
    list_gpus,
    ApplicationState,
    signal_handler,
    setup_application,
    setup_working_directory,
    detect_and_select_gpus,
    create_progress_displays,
    run_processing,
    main,
    app_state
)


class TestArgumentParsing:
    """Test command-line argument parsing."""
    
    def test_parse_arguments_basic(self):
        """Test basic argument parsing."""
        with patch('sys.argv', ['plex-generate-previews', '--plex-url', 'http://localhost:32400']):
            args = parse_arguments()
            assert args.plex_url == 'http://localhost:32400'
    
    def test_parse_arguments_help(self):
        """Test help argument."""
        with patch('sys.argv', ['plex-generate-previews', '--help']):
            with pytest.raises(SystemExit):
                parse_arguments()
    
    def test_parse_arguments_list_gpus(self):
        """Test list-gpus argument."""
        with patch('sys.argv', ['plex-generate-previews', '--list-gpus']):
            args = parse_arguments()
            assert args.list_gpus is True


class TestApplicationState:
    """Test application state management."""
    
    def test_application_state_init(self):
        """Test application state initialization."""
        state = ApplicationState()
        assert state.config is None
        assert state.console is not None
    
    def test_set_config(self):
        """Test setting configuration."""
        state = ApplicationState()
        config = MagicMock()
        state.set_config(config)
        assert state.config == config
    
    @patch('os.path.isdir')
    @patch('shutil.rmtree')
    def test_cleanup_with_config(self, mock_rmtree, mock_isdir):
        """Test cleanup with configuration."""
        state = ApplicationState()
        config = MagicMock()
        config.working_tmp_folder = '/tmp/test/working'
        config.tmp_folder = '/tmp/test'
        config.tmp_folder_created_by_us = False
        state.set_config(config)
        
        mock_isdir.return_value = True
        
        state.cleanup()
        
        # Should only clean up working folder, not parent tmp_folder
        assert mock_rmtree.call_count == 1
        mock_rmtree.assert_called_once_with('/tmp/test/working')
    
    def test_cleanup_without_config(self):
        """Test cleanup without configuration."""
        state = ApplicationState()
        # Should not raise any exceptions
        state.cleanup()
    
    @patch('os.path.isdir')
    @patch('shutil.rmtree')
    def test_cleanup_with_created_folder(self, mock_rmtree, mock_isdir):
        """Test cleanup when we created the tmp folder."""
        state = ApplicationState()
        config = MagicMock()
        config.working_tmp_folder = '/tmp/test/working'
        config.tmp_folder = '/tmp/test'
        config.tmp_folder_created_by_us = True
        state.set_config(config)
        
        mock_isdir.return_value = True
        
        state.cleanup()
        
        # Should only clean up working folder (parent folder is persistent even if we created it)
        assert mock_rmtree.call_count == 1
        mock_rmtree.assert_called_once_with('/tmp/test/working')


class TestLogging:
    """Test logging setup."""
    
    @patch('plex_generate_previews.logging_config.logger')
    def test_setup_logging(self, mock_logger):
        """Test logging setup."""
        setup_logging('DEBUG')
        mock_logger.remove.assert_called_once()
        mock_logger.add.assert_called_once()


class TestGPUListing:
    """Test GPU listing functionality."""
    
    @patch('plex_generate_previews.cli.detect_all_gpus')
    @patch('plex_generate_previews.cli.logger')
    def test_list_gpus_no_gpus(self, mock_logger, mock_detect):
        """Test listing GPUs when none are detected."""
        mock_detect.return_value = []
        
        list_gpus()
        
        mock_logger.info.assert_any_call('❌ No GPUs detected')
        mock_logger.info.assert_any_call('💡 Use --cpu-threads to run with CPU-only processing')
    
    @patch('plex_generate_previews.cli.detect_all_gpus')
    @patch('plex_generate_previews.cli.format_gpu_info')
    @patch('plex_generate_previews.cli.logger')
    def test_list_gpus_with_gpus(self, mock_logger, mock_format, mock_detect):
        """Test listing GPUs when GPUs are detected."""
        mock_detect.return_value = [
            ('NVIDIA', 'cuda', {'name': 'NVIDIA GeForce RTX 3080', 'acceleration': 'CUDA'}),
            ('AMD', '/dev/dri/renderD128', {'name': 'AMD Radeon RX 6800 XT', 'acceleration': 'VAAPI'})
        ]
        mock_format.side_effect = ['NVIDIA GeForce RTX 3080 (CUDA)', 'AMD Radeon RX 6800 XT (VAAPI)']
        
        list_gpus()
        
        mock_logger.info.assert_any_call('✅ Found 2 GPU(s):')
        mock_logger.info.assert_any_call('  [0] NVIDIA GeForce RTX 3080 (CUDA)')
        mock_logger.info.assert_any_call('  [1] AMD Radeon RX 6800 XT (VAAPI)')


class TestAnimatedBarColumn:
    """Test animated bar column for progress display."""
    
    def test_animated_bar_column_init(self):
        """Test AnimatedBarColumn initialization."""
        from plex_generate_previews.cli import AnimatedBarColumn
        
        bar = AnimatedBarColumn(bar_width=40)
        assert bar.bar_width == 40
        assert bar._animation_offset == 0
    
    def test_animated_bar_column_render_no_total(self):
        """Test rendering when task has no total."""
        from plex_generate_previews.cli import AnimatedBarColumn
        from rich.progress import Task
        from unittest.mock import MagicMock
        
        bar = AnimatedBarColumn()
        task = MagicMock()
        task.total = None
        
        result = bar.render(task)
        assert result is not None
    
    def test_animated_bar_column_render_in_progress(self):
        """Test rendering an in-progress task."""
        from plex_generate_previews.cli import AnimatedBarColumn
        from unittest.mock import MagicMock
        
        bar = AnimatedBarColumn(bar_width=40)
        task = MagicMock()
        task.total = 100
        task.completed = 50
        task.finished = False
        
        result = bar.render(task)
        assert result is not None
    
    def test_animated_bar_column_render_finished(self):
        """Test rendering a finished task."""
        from plex_generate_previews.cli import AnimatedBarColumn
        from unittest.mock import MagicMock
        
        bar = AnimatedBarColumn(bar_width=40)
        task = MagicMock()
        task.total = 100
        task.completed = 100
        task.finished = True
        
        result = bar.render(task)
        assert result is not None


class TestFFmpegDataColumn:
    """Test FFmpeg data column for progress display."""
    
    def test_ffmpeg_data_column_render_with_data(self):
        """Test rendering with FFmpeg data."""
        from plex_generate_previews.cli import FFmpegDataColumn
        from unittest.mock import MagicMock
        
        column = FFmpegDataColumn()
        task = MagicMock()
        task.fields = {
            'frame': 100,
            'fps': 30.0,
            'time_str': '00:00:03.33',
            'speed': '1.0x'
        }
        
        result = column.render(task)
        assert result is not None
        assert 'frame' in str(result)
    
    def test_ffmpeg_data_column_render_no_data(self):
        """Test rendering without FFmpeg data."""
        from plex_generate_previews.cli import FFmpegDataColumn
        from unittest.mock import MagicMock
        
        column = FFmpegDataColumn()
        task = MagicMock()
        task.fields = {}
        
        result = column.render(task)
        assert result is not None
        assert 'Waiting' in str(result)


class TestSignalHandler:
    """Test signal handling functionality."""
    
    @patch('plex_generate_previews.cli.app_state')
    @patch('sys.exit')
    @patch('plex_generate_previews.cli.logger')
    def test_signal_handler_interrupt(self, mock_logger, mock_exit, mock_state):
        """Test handling interrupt signal."""
        signal_handler(signal.SIGINT, None)
        
        mock_logger.info.assert_called_once_with("Received interrupt signal, shutting down gracefully...")
        mock_state.cleanup.assert_called_once()
        mock_exit.assert_called_once_with(0)
    
    @patch('plex_generate_previews.cli.app_state')
    @patch('sys.exit')
    @patch('plex_generate_previews.cli.logger')
    def test_signal_handler_term(self, mock_logger, mock_exit, mock_state):
        """Test handling terminate signal."""
        signal_handler(signal.SIGTERM, None)
        
        mock_logger.info.assert_called_once_with("Received interrupt signal, shutting down gracefully...")
        mock_state.cleanup.assert_called_once()
        mock_exit.assert_called_once_with(0)




class TestSetupWorkingDirectory:
    """Test working directory setup."""
    
    @patch('plex_generate_previews.cli.create_working_directory')
    @patch('plex_generate_previews.cli.logger')
    def test_setup_working_directory_success(self, mock_logger, mock_create_dir):
        """Test successful working directory setup."""
        mock_config = MagicMock()
        mock_config.tmp_folder = '/tmp/test'
        mock_create_dir.return_value = '/tmp/test/working'
        
        setup_working_directory(mock_config)
        
        assert mock_config.working_tmp_folder == '/tmp/test/working'
        mock_logger.debug.assert_called_once()
    
    @patch('plex_generate_previews.cli.create_working_directory')
    @patch('plex_generate_previews.cli.logger')
    @patch('sys.exit')
    def test_setup_working_directory_failure(self, mock_exit, mock_logger, mock_create_dir):
        """Test working directory setup failure."""
        mock_config = MagicMock()
        mock_config.tmp_folder = '/tmp/test'
        mock_create_dir.side_effect = Exception("Failed to create directory")
        
        setup_working_directory(mock_config)
        
        mock_logger.error.assert_called_once()
        mock_exit.assert_called_once_with(1)


