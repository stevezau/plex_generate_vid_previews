"""
Tests for CLI functionality.
"""

import signal
from unittest.mock import MagicMock, patch

import pytest

from plex_generate_previews.cli import (
    ApplicationState,
    list_gpus,
    parse_arguments,
    run_processing,
    setup_logging,
    setup_working_directory,
    signal_handler,
)


class TestArgumentParsing:
    """Test command-line argument parsing."""

    def test_parse_arguments_basic(self):
        """Test basic argument parsing."""
        with patch(
            "sys.argv",
            ["plex-generate-previews", "--plex-url", "http://localhost:32400"],
        ):
            args = parse_arguments()
            assert args.plex_url == "http://localhost:32400"

    def test_parse_arguments_help(self):
        """Test help argument."""
        with patch("sys.argv", ["plex-generate-previews", "--help"]):
            with pytest.raises(SystemExit):
                parse_arguments()

    def test_parse_arguments_list_gpus(self):
        """Test list-gpus argument."""
        with patch("sys.argv", ["plex-generate-previews", "--list-gpus"]):
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

    @patch("os.path.isdir")
    @patch("shutil.rmtree")
    def test_cleanup_with_config(self, mock_rmtree, mock_isdir):
        """Test cleanup with configuration."""
        state = ApplicationState()
        config = MagicMock()
        config.working_tmp_folder = "/tmp/test/working"
        config.tmp_folder = "/tmp/test"
        config.tmp_folder_created_by_us = False
        state.set_config(config)

        mock_isdir.return_value = True

        state.cleanup()

        # Should only clean up working folder, not parent tmp_folder
        assert mock_rmtree.call_count == 1
        mock_rmtree.assert_called_once_with("/tmp/test/working")

    def test_cleanup_without_config(self):
        """Test cleanup without configuration."""
        state = ApplicationState()
        # Should not raise any exceptions
        state.cleanup()

    def test_request_shutdown_marks_flag_and_stops_worker_pool(self):
        """Shutdown request should set the flag and stop workers."""
        state = ApplicationState()
        state.worker_pool = MagicMock()

        state.request_shutdown()

        assert state.shutting_down is True
        state.worker_pool.shutdown.assert_called_once()

    @patch("os.path.isdir")
    @patch("shutil.rmtree")
    def test_cleanup_with_created_folder(self, mock_rmtree, mock_isdir):
        """Test cleanup when we created the tmp folder."""
        state = ApplicationState()
        config = MagicMock()
        config.working_tmp_folder = "/tmp/test/working"
        config.tmp_folder = "/tmp/test"
        config.tmp_folder_created_by_us = True
        state.set_config(config)

        mock_isdir.return_value = True

        state.cleanup()

        # Should only clean up working folder (parent folder is persistent even if we created it)
        assert mock_rmtree.call_count == 1
        mock_rmtree.assert_called_once_with("/tmp/test/working")


class TestLogging:
    """Test logging setup."""

    @patch("plex_generate_previews.logging_config.os.makedirs")
    @patch("plex_generate_previews.logging_config.logger")
    def test_setup_logging(self, mock_logger, mock_makedirs):
        """Test logging setup."""
        import plex_generate_previews.logging_config as _lm

        _lm._managed_handler_ids = []
        _lm._initial_setup_done = False

        setup_logging("DEBUG")
        mock_logger.remove.assert_called_once()
        # main sink + error.log + activity.log
        assert mock_logger.add.call_count == 3


class TestGPUListing:
    """Test GPU listing functionality."""

    @patch("plex_generate_previews.cli.detect_all_gpus")
    @patch("plex_generate_previews.cli.logger")
    def test_list_gpus_no_gpus(self, mock_logger, mock_detect):
        """Test listing GPUs when none are detected."""
        mock_detect.return_value = []

        list_gpus()

        mock_logger.info.assert_any_call("❌ No GPUs detected")
        mock_logger.info.assert_any_call(
            "💡 Use --cpu-threads to run with CPU-only processing"
        )

    @patch("plex_generate_previews.cli.detect_all_gpus")
    @patch("plex_generate_previews.cli.format_gpu_info")
    @patch("plex_generate_previews.cli.logger")
    def test_list_gpus_with_gpus(self, mock_logger, mock_format, mock_detect):
        """Test listing GPUs when GPUs are detected."""
        mock_detect.return_value = [
            (
                "NVIDIA",
                "cuda",
                {"name": "NVIDIA GeForce RTX 3080", "acceleration": "CUDA"},
            ),
            (
                "AMD",
                "/dev/dri/renderD128",
                {"name": "AMD Radeon RX 6800 XT", "acceleration": "VAAPI"},
            ),
        ]
        mock_format.side_effect = [
            "NVIDIA GeForce RTX 3080 (CUDA)",
            "AMD Radeon RX 6800 XT (VAAPI)",
        ]

        list_gpus()

        mock_logger.info.assert_any_call("✅ Found 2 GPU(s):")
        mock_logger.info.assert_any_call("  [0] NVIDIA GeForce RTX 3080 (CUDA)")
        mock_logger.info.assert_any_call("  [1] AMD Radeon RX 6800 XT (VAAPI)")


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
        from unittest.mock import MagicMock

        from plex_generate_previews.cli import AnimatedBarColumn

        bar = AnimatedBarColumn()
        task = MagicMock()
        task.total = None

        result = bar.render(task)
        assert result is not None

    def test_animated_bar_column_render_in_progress(self):
        """Test rendering an in-progress task."""
        from unittest.mock import MagicMock

        from plex_generate_previews.cli import AnimatedBarColumn

        bar = AnimatedBarColumn(bar_width=40)
        task = MagicMock()
        task.total = 100
        task.completed = 50
        task.finished = False

        result = bar.render(task)
        assert result is not None

    def test_animated_bar_column_render_finished(self):
        """Test rendering a finished task."""
        from unittest.mock import MagicMock

        from plex_generate_previews.cli import AnimatedBarColumn

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
        from unittest.mock import MagicMock

        from plex_generate_previews.cli import FFmpegDataColumn

        column = FFmpegDataColumn()
        task = MagicMock()
        task.fields = {
            "frame": 100,
            "fps": 30.0,
            "time_str": "00:00:03.33",
            "speed": "1.0x",
        }

        result = column.render(task)
        assert result is not None
        assert "frame" in str(result)

    def test_ffmpeg_data_column_render_no_data(self):
        """Test rendering without FFmpeg data."""
        from unittest.mock import MagicMock

        from plex_generate_previews.cli import FFmpegDataColumn

        column = FFmpegDataColumn()
        task = MagicMock()
        task.fields = {}

        result = column.render(task)
        assert result is not None
        assert "Waiting" in str(result)


class TestRunProcessing:
    """Test run_processing orchestration behavior."""

    @patch("plex_generate_previews.cli.os.path.isdir", return_value=False)
    @patch("plex_generate_previews.cli.plex_server")
    @patch("plex_generate_previews.cli.WorkerPool")
    @patch("plex_generate_previews.cli.get_library_sections")
    def test_headless_merges_libraries_into_single_queue(
        self,
        mock_get_library_sections,
        mock_worker_pool_cls,
        mock_plex_server,
        _mock_isdir,
    ):
        """Headless mode should process all libraries through one shared queue."""
        section_a = MagicMock()
        section_a.title = "Movies"
        section_b = MagicMock()
        section_b.title = "TV Shows"
        mock_get_library_sections.return_value = [
            (section_a, [("m1", "Movie 1", "movie")]),
            (
                section_b,
                [("e1", "Episode 1", "episode"), ("e2", "Episode 2", "episode")],
            ),
        ]
        pool = MagicMock()
        pool.process_items_headless.return_value = {
            "completed": 3,
            "failed": 0,
            "cancelled": False,
        }
        mock_worker_pool_cls.return_value = pool
        mock_plex_server.return_value = MagicMock()

        config = MagicMock()
        config.webhook_paths = []
        config.gpu_threads = 1
        config.cpu_threads = 0
        config.fallback_cpu_threads = 0
        config.working_tmp_folder = "/tmp/pgvp-working"
        progress_callback = MagicMock()

        run_processing(
            config,
            selected_gpus=[],
            headless=True,
            progress_callback=progress_callback,
        )

        pool.process_items_headless.assert_called_once()
        args, kwargs = pool.process_items_headless.call_args
        assert len(args[0]) == 3
        assert kwargs["library_name"] == "All Libraries"
        progress_callback.assert_any_call(0, 3, "Processing all selected libraries (2)")

    @patch("plex_generate_previews.cli.os.path.isdir", return_value=False)
    @patch("plex_generate_previews.cli.plex_server")
    @patch("plex_generate_previews.cli.WorkerPool")
    @patch("plex_generate_previews.cli.get_library_sections")
    def test_headless_cancel_before_dispatch_skips_pool_processing(
        self,
        mock_get_library_sections,
        mock_worker_pool_cls,
        mock_plex_server,
        _mock_isdir,
    ):
        """Cancellation check before dispatch should skip pooled processing."""
        section = MagicMock()
        section.title = "Movies"
        mock_get_library_sections.return_value = [
            (section, [("m1", "Movie 1", "movie")]),
        ]
        pool = MagicMock()
        mock_worker_pool_cls.return_value = pool
        mock_plex_server.return_value = MagicMock()

        config = MagicMock()
        config.webhook_paths = []
        config.gpu_threads = 1
        config.cpu_threads = 0
        config.fallback_cpu_threads = 0
        config.working_tmp_folder = "/tmp/pgvp-working"

        run_processing(
            config,
            selected_gpus=[],
            headless=True,
            cancel_check=lambda: True,
        )

        pool.process_items_headless.assert_not_called()


class TestSignalHandler:
    """Test signal handling functionality."""

    @patch("plex_generate_previews.cli.app_state")
    @patch("plex_generate_previews.cli.logger")
    def test_signal_handler_interrupt(self, mock_logger, mock_state):
        """Test handling interrupt signal."""
        with pytest.raises(KeyboardInterrupt):
            signal_handler(signal.SIGINT, None)

        mock_logger.info.assert_called_once_with(
            "Received interrupt signal, shutting down gracefully..."
        )
        mock_state.request_shutdown.assert_called_once()

    @patch("plex_generate_previews.cli.app_state")
    @patch("plex_generate_previews.cli.logger")
    def test_signal_handler_term(self, mock_logger, mock_state):
        """Test handling terminate signal."""
        with pytest.raises(KeyboardInterrupt):
            signal_handler(signal.SIGTERM, None)

        mock_logger.info.assert_called_once_with(
            "Received interrupt signal, shutting down gracefully..."
        )
        mock_state.request_shutdown.assert_called_once()


class TestSetupWorkingDirectory:
    """Test working directory setup."""

    @patch("plex_generate_previews.cli.create_working_directory")
    @patch("plex_generate_previews.cli.logger")
    def test_setup_working_directory_success(self, mock_logger, mock_create_dir):
        """Test successful working directory setup."""
        mock_config = MagicMock()
        mock_config.tmp_folder = "/tmp/test"
        mock_create_dir.return_value = "/tmp/test/working"

        setup_working_directory(mock_config)

        assert mock_config.working_tmp_folder == "/tmp/test/working"
        mock_logger.debug.assert_called_once()

    @patch("plex_generate_previews.cli.create_working_directory")
    @patch("plex_generate_previews.cli.logger")
    @patch("sys.exit")
    def test_setup_working_directory_failure(
        self, mock_exit, mock_logger, mock_create_dir
    ):
        """Test working directory setup failure."""
        mock_config = MagicMock()
        mock_config.tmp_folder = "/tmp/test"
        mock_create_dir.side_effect = Exception("Failed to create directory")

        setup_working_directory(mock_config)

        mock_logger.error.assert_called_once()
        mock_exit.assert_called_once_with(1)
