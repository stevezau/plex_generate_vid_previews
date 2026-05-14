"""
Tests for logging configuration.
"""

import json
import os
from io import StringIO
from unittest.mock import MagicMock, patch

import pytest

import media_preview_generator.logging_config as _logging_mod
from media_preview_generator.logging_config import (
    _json_sink,
    _jsonl_record_patcher,
    get_app_log_path,
    setup_logging,
)


@pytest.fixture(autouse=True)
def _reset_logging_state():
    """Reset setup_logging module globals AND loguru sinks between tests.

    Without snapshotting loguru's own _core.handlers, tests that call
    setup_logging() (which removes existing handlers and adds new ones
    bound to test-scoped StringIO objects) leave the global loguru in a
    broken state. Background threads from other test modules — the job
    dispatcher, scheduler, retry queue, webhook timers — keep emitting
    into those torn-down sinks and can crash with
    "I/O operation on closed file" when the captured StringIO is GC'd.

    Snapshot loguru's handler set on entry, restore on exit so the
    next test (or a daemon thread that survives this test) sees a sane
    sink configuration.
    """
    from loguru import logger as _loguru_logger

    snapshot = dict(_loguru_logger._core.handlers)  # noqa: SLF001
    _logging_mod._managed_handler_ids = []
    _logging_mod._initial_setup_done = False
    old_broadcaster = _logging_mod._broadcaster
    _logging_mod._broadcaster = None
    try:
        yield
    finally:
        _logging_mod._managed_handler_ids = []
        _logging_mod._initial_setup_done = False
        _logging_mod._broadcaster = old_broadcaster
        # Restore loguru's pre-test handler set so test-scoped StringIO
        # sinks can't outlive the test that created them.
        _loguru_logger.remove()
        for handler in snapshot.values():
            try:
                _loguru_logger.add(
                    handler._sink,  # noqa: SLF001
                    level=handler.levelno,
                )
            except Exception:
                # Best-effort restore; some handlers (Rich console) carry
                # state that doesn't round-trip cleanly. The next call to
                # setup_logging() in production code will re-establish
                # the production sinks anyway.
                pass


class TestLoggingConfig:
    """Test logging configuration."""

    @patch("media_preview_generator.logging_config.os.makedirs")
    @patch("media_preview_generator.logging_config.logger")
    def test_setup_logging_default(self, mock_logger, mock_makedirs):
        """Default level: stderr handler at INFO + JSONL app.log handler."""
        setup_logging()

        mock_logger.remove.assert_called_once()
        # Expect stderr + app.log handlers (2 add calls minimum).
        assert mock_logger.add.call_count == 2
        stderr_call = mock_logger.add.call_args_list[0]
        assert stderr_call.kwargs.get("level") == "INFO"

    @patch("media_preview_generator.logging_config.os.makedirs")
    @patch("media_preview_generator.logging_config.logger")
    def test_setup_logging_debug(self, mock_logger, mock_makedirs):
        """DEBUG level propagates to the stderr handler config."""
        setup_logging("DEBUG")

        mock_logger.remove.assert_called_once()
        assert mock_logger.add.call_count == 2
        stderr_call = mock_logger.add.call_args_list[0]
        assert stderr_call.kwargs.get("level") == "DEBUG"
        # The app.log handler must keep rotation/retention even at DEBUG.
        app_log_call = mock_logger.add.call_args_list[1]
        assert app_log_call.kwargs.get("rotation") == "10 MB"
        assert app_log_call.kwargs.get("retention") == 5

    @patch("media_preview_generator.logging_config.os.makedirs")
    @patch("media_preview_generator.logging_config.logger")
    def test_setup_logging_with_console(self, mock_logger, mock_makedirs):
        """Passing a Rich console should bind the stderr handler to it.

        Strengthened: prove the sink actually routes to ``console.print``
        instead of just trusting that the level + handler-count checks
        imply correct wiring. Production wraps the console in a
        ``lambda msg: console.print(msg, end="")`` (logging_config.py:181) —
        invoke the bound sink and verify console.print was called.
        """
        mock_console = MagicMock()

        setup_logging("INFO", console=mock_console)

        mock_logger.remove.assert_called_once()
        assert mock_logger.add.call_count == 2
        stderr_call = mock_logger.add.call_args_list[0]
        assert stderr_call.kwargs.get("level") == "INFO"

        # The sink must be a callable that forwards to console.print —
        # invoke it and assert the console was actually called. A regression
        # that bound the sink to sys.stderr (ignoring the console arg)
        # would fail here.
        sink = stderr_call.args[0]
        assert callable(sink), f"first add() arg must be a callable sink, got {type(sink).__name__}"
        sink("test log message\n")
        mock_console.print.assert_called_once_with("test log message\n", end="")

    @patch("media_preview_generator.logging_config.os.makedirs")
    @patch("media_preview_generator.logging_config.logger")
    def test_setup_logging_adds_app_log_handler(self, mock_logger, mock_makedirs):
        """Test that setup_logging adds the consolidated JSONL app.log handler."""
        setup_logging()

        # stderr + app.log = 2 handlers
        assert mock_logger.add.call_count == 2

        app_log_call = mock_logger.add.call_args_list[1]
        assert app_log_call.kwargs.get("level") == "INFO"
        assert app_log_call.kwargs.get("rotation") == "10 MB"
        assert app_log_call.kwargs.get("retention") == 5
        assert "{extra[_jsonl]}" in (app_log_call.kwargs.get("format") or "")

    @patch("media_preview_generator.logging_config.os.makedirs")
    @patch("media_preview_generator.logging_config.logger")
    def test_setup_logging_custom_rotation_retention(self, mock_logger, mock_makedirs):
        """Test setup_logging with custom rotation and retention values."""
        setup_logging(rotation="5 MB", retention=4)

        assert mock_logger.add.call_count == 2

        app_log_call = mock_logger.add.call_args_list[1]
        assert app_log_call.kwargs.get("rotation") == "5 MB"
        assert app_log_call.kwargs.get("retention") == 4

    @patch("media_preview_generator.logging_config.os.makedirs", side_effect=PermissionError)
    @patch("media_preview_generator.logging_config.logger")
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

    @patch("media_preview_generator.logging_config.os.makedirs")
    @patch("media_preview_generator.logging_config.logger")
    def test_json_format_adds_json_sink(self, mock_logger, mock_makedirs):
        """When log_format='json', _json_sink should be registered."""
        setup_logging(log_format="json")
        mock_logger.remove.assert_called_once()

        # First add call should use _json_sink
        first_add = mock_logger.add.call_args_list[0]
        assert first_add.args[0] is _json_sink

    @patch("media_preview_generator.logging_config.os.makedirs")
    @patch("media_preview_generator.logging_config.logger")
    def test_json_format_via_env_var(self, mock_logger, mock_makedirs):
        """LOG_FORMAT env var should be respected when log_format is None."""
        with patch.dict(os.environ, {"LOG_FORMAT": "json"}):
            setup_logging()
        first_add = mock_logger.add.call_args_list[0]
        assert first_add.args[0] is _json_sink

    @patch("media_preview_generator.logging_config.os.makedirs")
    @patch("media_preview_generator.logging_config.logger")
    def test_pretty_format_ignores_json_sink(self, mock_logger, mock_makedirs):
        """Explicit log_format='pretty' should NOT use _json_sink."""
        setup_logging(log_format="pretty")
        first_add = mock_logger.add.call_args_list[0]
        assert first_add.args[0] is not _json_sink

    @patch("media_preview_generator.logging_config.os.makedirs")
    @patch("media_preview_generator.logging_config.logger")
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

        with patch("media_preview_generator.logging_config.sys.stderr", captured):
            with patch.dict(os.environ, {"CONFIG_DIR": str(tmp_path)}):
                setup_logging(log_format="json")
                logger.info("hello structured world")
                logger.complete()

        logger.remove()
        output = captured.getvalue().strip()
        assert output, "Expected JSON output on stderr"

        # Find the line containing our test message (background threads may
        # inject other log lines, so we can't assume a fixed position).
        record = None
        for line in output.splitlines():
            try:
                parsed = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if "hello structured world" in parsed.get("message", ""):
                record = parsed
                break
        assert record is not None, f"Expected JSON line with 'hello structured world' in output:\n{output}"
        assert record["level"] == "INFO"
        assert "timestamp" in record
        assert "function" in record

    def test_json_sink_includes_exception(self, tmp_path):
        """When an exception is logged, it should appear in the JSON payload."""
        from loguru import logger

        captured = StringIO()
        logger.remove()

        with patch("media_preview_generator.logging_config.sys.stderr", captured):
            with patch.dict(os.environ, {"CONFIG_DIR": str(tmp_path)}):
                setup_logging(log_format="json")
                try:
                    raise ValueError("boom")
                except ValueError:
                    logger.exception("caught error")
                logger.complete()

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


# -----------------------------------------------------------------------
# SocketIO live-log broadcaster
# -----------------------------------------------------------------------


class TestSocketIOLogBroadcaster:
    """Tests for the SocketIOLogBroadcaster and its module-level accessors."""

    def test_get_set_broadcaster(self):
        """get/set_log_broadcaster round-trips correctly."""
        from media_preview_generator.logging_config import (
            SocketIOLogBroadcaster,
            get_log_broadcaster,
            set_log_broadcaster,
        )

        assert get_log_broadcaster() is None
        mock_sio = MagicMock()
        b = SocketIOLogBroadcaster(mock_sio)
        set_log_broadcaster(b)
        assert get_log_broadcaster() is b
        set_log_broadcaster(None)
        assert get_log_broadcaster() is None

    def test_sink_emits_to_correct_room(self):
        """sink() should call socketio.emit with room=level_name."""
        from media_preview_generator.logging_config import SocketIOLogBroadcaster

        mock_sio = MagicMock()
        broadcaster = SocketIOLogBroadcaster(mock_sio)

        record = MagicMock()
        record.record = {
            "level": MagicMock(name="WARNING"),
            "time": MagicMock(),
            "message": "test warning",
            "name": "media_preview_generator.worker",
            "function": "run",
            "line": 42,
        }
        record.record["level"].name = "WARNING"
        record.record["time"].strftime.return_value = "2026-03-22 09:10:18.123"

        broadcaster.sink(record)

        mock_sio.emit.assert_called_once()
        call_kwargs = mock_sio.emit.call_args
        assert call_kwargs.args[0] == "log_message"
        assert call_kwargs.kwargs["namespace"] == "/logs"
        assert call_kwargs.kwargs["room"] == "WARNING"

        payload = call_kwargs.args[1]
        assert payload["level"] == "WARNING"
        assert payload["msg"] == "test warning"
        assert payload["mod"] == "worker"

    def test_sink_filters_out_trace_level(self):
        """sink() should silently drop TRACE-level records."""
        from media_preview_generator.logging_config import SocketIOLogBroadcaster

        mock_sio = MagicMock()
        broadcaster = SocketIOLogBroadcaster(mock_sio)

        record = MagicMock()
        record.record = {
            "level": MagicMock(name="TRACE"),
            "time": MagicMock(),
            "message": "trace msg",
            "name": "x",
            "function": "f",
            "line": 1,
        }
        record.record["level"].name = "TRACE"

        broadcaster.sink(record)
        mock_sio.emit.assert_not_called()

    def test_sink_swallows_emit_errors(self):
        """sink() must not raise when socketio.emit fails."""
        from media_preview_generator.logging_config import SocketIOLogBroadcaster

        mock_sio = MagicMock()
        mock_sio.emit.side_effect = RuntimeError("boom")
        broadcaster = SocketIOLogBroadcaster(mock_sio)

        record = MagicMock()
        record.record = {
            "level": MagicMock(name="INFO"),
            "time": MagicMock(),
            "message": "test",
            "name": "mod",
            "function": "fn",
            "line": 1,
        }
        record.record["level"].name = "INFO"
        record.record["time"].strftime.return_value = "2026-03-22 09:10:18.123"

        broadcaster.sink(record)  # should not raise

    @patch("media_preview_generator.logging_config.os.makedirs")
    @patch("media_preview_generator.logging_config.logger")
    def test_setup_logging_attaches_broadcaster(self, mock_logger, mock_makedirs):
        """When a broadcaster is registered, setup_logging adds it as a handler."""
        from media_preview_generator.logging_config import (
            SocketIOLogBroadcaster,
            set_log_broadcaster,
        )

        mock_sio = MagicMock()
        set_log_broadcaster(SocketIOLogBroadcaster(mock_sio))

        setup_logging()

        # 2 base handlers (stderr + app.log) + 1 broadcaster = 3
        assert mock_logger.add.call_count == 3
        broadcaster_call = mock_logger.add.call_args_list[2]
        assert broadcaster_call.kwargs.get("level") == "INFO"


# -----------------------------------------------------------------------
# JSONL file sink and app.log helpers
# -----------------------------------------------------------------------


class TestJsonlRecordPatcher:
    """Tests for the _jsonl_record_patcher filter and get_app_log_path."""

    def test_patcher_stores_jsonl_in_extra(self):
        """_jsonl_record_patcher should store a valid JSON string in extra._jsonl."""
        record = {
            "time": MagicMock(),
            "level": MagicMock(),
            "message": "hello world",
            "name": "media_preview_generator.worker",
            "function": "run",
            "line": 42,
            "extra": {},
        }
        record["time"].strftime.return_value = "2026-03-22 09:10:18.123000"
        record["level"].name = "INFO"

        result = _jsonl_record_patcher(record)

        assert result is True
        jsonl_str = record["extra"]["_jsonl"]
        parsed = json.loads(jsonl_str)
        assert parsed["ts"] == "2026-03-22 09:10:18.123"
        assert parsed["level"] == "INFO"
        assert parsed["msg"] == "hello world"
        assert parsed["mod"] == "worker"
        assert parsed["func"] == "run"
        assert parsed["line"] == 42

    def test_patcher_handles_empty_name(self):
        """When record name is empty, mod should be empty."""
        record = {
            "time": MagicMock(),
            "level": MagicMock(),
            "message": "test",
            "name": "",
            "function": None,
            "line": 1,
            "extra": {},
        }
        record["time"].strftime.return_value = "2026-01-01 00:00:00.000000"
        record["level"].name = "DEBUG"

        _jsonl_record_patcher(record)
        parsed = json.loads(record["extra"]["_jsonl"])
        assert parsed["mod"] == ""
        assert parsed["func"] == ""

    def test_patcher_escapes_json_in_message(self):
        """Messages with quotes/backslashes must produce valid JSON."""
        record = {
            "time": MagicMock(),
            "level": MagicMock(),
            "message": 'path "C:\\Users\\test"',
            "name": "mod",
            "function": "f",
            "line": 1,
            "extra": {},
        }
        record["time"].strftime.return_value = "2026-01-01 00:00:00.000000"
        record["level"].name = "WARNING"

        _jsonl_record_patcher(record)
        parsed = json.loads(record["extra"]["_jsonl"])
        assert parsed["msg"] == 'path "C:\\Users\\test"'

    def test_get_app_log_path_default(self):
        """get_app_log_path returns /config/logs/app.log by default."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CONFIG_DIR", None)
            assert get_app_log_path() == "/config/logs/app.log"

    def test_get_app_log_path_custom_config_dir(self):
        """get_app_log_path respects CONFIG_DIR."""
        with patch.dict(os.environ, {"CONFIG_DIR": "/my/config"}):
            assert get_app_log_path() == "/my/config/logs/app.log"

    def test_app_log_written_as_jsonl(self, tmp_path):
        """Integration: setup_logging writes valid JSONL lines to app.log."""
        from loguru import logger

        with patch.dict(os.environ, {"CONFIG_DIR": str(tmp_path)}):
            logger.remove()
            setup_logging(log_level="DEBUG")
            logger.info("integration test message")
            logger.complete()

        logger.remove()
        app_log = tmp_path / "logs" / "app.log"
        assert app_log.exists(), "app.log should have been created"
        lines = [ln for ln in app_log.read_text().strip().splitlines() if ln.strip()]
        assert len(lines) >= 1

        for line in lines:
            record = json.loads(line)
            assert "ts" in record
            assert "level" in record
            assert "msg" in record
