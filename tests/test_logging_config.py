"""
Tests for logging configuration.
"""

import json
import os
from io import StringIO
from unittest.mock import patch, MagicMock
import pytest
from plex_generate_previews.logging_config import setup_logging, _json_sink
import plex_generate_previews.logging_config as _logging_mod


@pytest.fixture(autouse=True)
def _reset_logging_state():
    """Reset setup_logging module globals between tests."""
    _logging_mod._managed_handler_ids = []
    _logging_mod._initial_setup_done = False
    yield
    _logging_mod._managed_handler_ids = []
    _logging_mod._initial_setup_done = False


class TestLoggingConfig:
    """Test logging configuration."""

    @patch("plex_generate_previews.logging_config.os.makedirs")
    @patch("plex_generate_previews.logging_config.logger")
    def test_setup_logging_default(self, mock_logger, mock_makedirs):
        """Test setup logging with default level."""
        setup_logging()

        # Should configure logger
        mock_logger.remove.assert_called()
        mock_logger.add.assert_called()

    @patch("plex_generate_previews.logging_config.os.makedirs")
    @patch("plex_generate_previews.logging_config.logger")
    def test_setup_logging_debug(self, mock_logger, mock_makedirs):
        """Test setup logging with DEBUG level."""
        setup_logging("DEBUG")

        mock_logger.remove.assert_called_once()
        mock_logger.add.assert_called()

    @patch("plex_generate_previews.logging_config.os.makedirs")
    @patch("plex_generate_previews.logging_config.logger")
    def test_setup_logging_with_console(self, mock_logger, mock_makedirs):
        """Test setup logging with console parameter."""
        mock_console = MagicMock()

        setup_logging("INFO", console=mock_console)

        mock_logger.remove.assert_called_once()
        mock_logger.add.assert_called()

    @patch("plex_generate_previews.logging_config.os.makedirs")
    @patch("plex_generate_previews.logging_config.logger")
    def test_setup_logging_adds_error_file_handler(self, mock_logger, mock_makedirs):
        """Test that setup_logging adds a persistent error log file handler."""
        setup_logging()

        # Should have 3 calls to add: stderr + error log file + activity log file
        assert mock_logger.add.call_count == 3

        # Second call should be for the error log file
        error_call = mock_logger.add.call_args_list[1]
        assert error_call.kwargs.get("level") == "ERROR"
        assert error_call.kwargs.get("rotation") == "10 MB"
        assert error_call.kwargs.get("retention") == 5

        # Third call should be for the activity log file
        activity_call = mock_logger.add.call_args_list[2]
        assert activity_call.kwargs.get("level") == "WARNING"
        assert activity_call.kwargs.get("rotation") == "10 MB"
        assert activity_call.kwargs.get("retention") == 5

    @patch("plex_generate_previews.logging_config.os.makedirs")
    @patch("plex_generate_previews.logging_config.logger")
    def test_setup_logging_custom_rotation_retention(self, mock_logger, mock_makedirs):
        """Test setup_logging with custom rotation and retention values."""
        setup_logging(rotation="5 MB", retention=4)

        assert mock_logger.add.call_count == 3

        error_call = mock_logger.add.call_args_list[1]
        assert error_call.kwargs.get("rotation") == "5 MB"
        assert error_call.kwargs.get("retention") == 4

        activity_call = mock_logger.add.call_args_list[2]
        assert activity_call.kwargs.get("rotation") == "5 MB"
        assert activity_call.kwargs.get("retention") == 4

    @patch(
        "plex_generate_previews.logging_config.os.makedirs", side_effect=PermissionError
    )
    @patch("plex_generate_previews.logging_config.logger")
    def test_setup_logging_handles_permission_error(self, mock_logger, mock_makedirs):
        """Test that setup_logging handles permission errors for log directory."""
        setup_logging()

        # Should still add stderr handler but not error file handler
        mock_logger.remove.assert_called_once()
        assert mock_logger.add.call_count == 1

    def test_setup_logging_creates_error_log(self, tmp_path):
        """Test that setup_logging creates the error log file on disk."""
        from loguru import logger

        with patch.dict(os.environ, {"CONFIG_DIR": str(tmp_path)}):
            # Reset logger state
            logger.remove()
            setup_logging()

        # Log directory should have been created
        log_dir = str(tmp_path / "logs")
        assert os.path.isdir(log_dir)

        # Clean up handlers we added
        logger.remove()


# -----------------------------------------------------------------------
# Structured JSON logging (Item 36)
# -----------------------------------------------------------------------


class TestJSONLogging:
    """Test LOG_FORMAT=json structured logging output."""

    @patch("plex_generate_previews.logging_config.os.makedirs")
    @patch("plex_generate_previews.logging_config.logger")
    def test_json_format_adds_json_sink(self, mock_logger, mock_makedirs):
        """When log_format='json', _json_sink should be registered."""
        setup_logging(log_format="json")
        mock_logger.remove.assert_called_once()

        # First add call should use _json_sink
        first_add = mock_logger.add.call_args_list[0]
        assert first_add.args[0] is _json_sink

    @patch("plex_generate_previews.logging_config.os.makedirs")
    @patch("plex_generate_previews.logging_config.logger")
    def test_json_format_via_env_var(self, mock_logger, mock_makedirs):
        """LOG_FORMAT env var should be respected when log_format is None."""
        with patch.dict(os.environ, {"LOG_FORMAT": "json"}):
            setup_logging()
        first_add = mock_logger.add.call_args_list[0]
        assert first_add.args[0] is _json_sink

    @patch("plex_generate_previews.logging_config.os.makedirs")
    @patch("plex_generate_previews.logging_config.logger")
    def test_pretty_format_ignores_json_sink(self, mock_logger, mock_makedirs):
        """Explicit log_format='pretty' should NOT use _json_sink."""
        setup_logging(log_format="pretty")
        first_add = mock_logger.add.call_args_list[0]
        assert first_add.args[0] is not _json_sink

    @patch("plex_generate_previews.logging_config.os.makedirs")
    @patch("plex_generate_previews.logging_config.logger")
    def test_console_ignored_when_json(self, mock_logger, mock_makedirs):
        """Providing a console alongside json format should still use JSON."""
        mock_console = MagicMock()
        setup_logging(log_format="json", console=mock_console)
        first_add = mock_logger.add.call_args_list[0]
        assert first_add.args[0] is _json_sink

    def test_json_sink_produces_valid_json(self, tmp_path):
        """_json_sink should write valid JSON lines to stderr."""
        from loguru import logger

        captured = StringIO()
        logger.remove()

        with patch("plex_generate_previews.logging_config.sys.stderr", captured):
            with patch.dict(os.environ, {"CONFIG_DIR": str(tmp_path)}):
                setup_logging(log_format="json")
                logger.info("hello structured world")
                # Allow enqueued message to flush
                import time

                time.sleep(0.2)

        logger.remove()
        output = captured.getvalue().strip()
        assert output, "Expected JSON output on stderr"

        # Should be parseable JSON
        line = output.splitlines()[-1]
        record = json.loads(line)
        assert record["level"] == "INFO"
        assert "hello structured world" in record["message"]
        assert "timestamp" in record
        assert "function" in record

    def test_json_sink_includes_exception(self, tmp_path):
        """When an exception is logged, it should appear in the JSON payload."""
        from loguru import logger

        captured = StringIO()
        logger.remove()

        with patch("plex_generate_previews.logging_config.sys.stderr", captured):
            with patch.dict(os.environ, {"CONFIG_DIR": str(tmp_path)}):
                setup_logging(log_format="json")
                try:
                    raise ValueError("boom")
                except ValueError:
                    logger.exception("caught error")
                import time

                time.sleep(0.2)

        logger.remove()
        output = captured.getvalue().strip()
        lines = output.splitlines()
        # Find the line with "caught error"
        for line in lines:
            record = json.loads(line)
            if "caught error" in record["message"]:
                assert "exception" in record
                assert "boom" in record["exception"]
                break
        else:
            pytest.fail("Expected 'caught error' log line in JSON output")
