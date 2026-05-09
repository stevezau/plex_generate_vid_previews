"""Tests that Jellyfin / Emby refresh success now logs at INFO level.

Pre-fix every refresh log line in jellyfin.py was logger.debug() — at
INFO level the job log was completely silent about whether refresh
fired. Plex logs ``[Plex] Triggered partial scan...`` at INFO, so
operators tailing the log can see *which* refresh succeeded for
*which* path. Match that format on Jellyfin and Emby.

These tests sniff loguru via a temporary INFO-level sink so we can
assert exact log strings independent of logger configuration.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from loguru import logger

from media_preview_generator.servers.base import ServerConfig, ServerType
from media_preview_generator.servers.emby import EmbyServer
from media_preview_generator.servers.jellyfin import JellyfinServer


@pytest.fixture
def info_log_sink():
    """Temporary INFO-level loguru sink — yields the captured records list."""
    records: list[dict] = []

    def _sink(message):
        # message.record is the structured dict — we capture text + level.
        records.append(
            {
                "level": message.record["level"].name,
                "message": message.record["message"],
            }
        )

    sink_id = logger.add(_sink, level="INFO")
    yield records
    logger.remove(sink_id)


def _ok():
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.status_code = 204
    resp.text = ""
    return resp


def _jelly():
    return JellyfinServer(
        ServerConfig(
            id="jelly-1",
            type=ServerType.JELLYFIN,
            name="Test Jellyfin",
            enabled=True,
            url="http://jellyfin:8096",
            auth={"method": "quick_connect", "access_token": "tok", "user_id": "u"},
        )
    )


def _emby():
    return EmbyServer(
        ServerConfig(
            id="emby-1",
            type=ServerType.EMBY,
            name="Test Emby",
            enabled=True,
            url="http://emby:8096",
            auth={"method": "api_key", "api_key": "k"},
        )
    )


# ---------------------------------------------------------------------------
# Jellyfin
# ---------------------------------------------------------------------------


class TestJellyfinRefreshLogging:
    def test_path_refresh_success_logs_INFO_with_server_name_and_path(self, info_log_sink):
        jelly = _jelly()
        with patch.object(JellyfinServer, "_request", return_value=_ok()):
            jelly._trigger_path_refresh("/movies/X/NEW.mkv")

        info_lines = [r for r in info_log_sink if r["level"] == "INFO"]
        assert any(
            "Test Jellyfin" in r["message"]
            and "/movies/X/NEW.mkv" in r["message"]
            and "Triggered partial scan" in r["message"]
            for r in info_lines
        ), f"Expected INFO line; got: {info_log_sink!r}"

    def test_item_refresh_success_logs_INFO_with_item_id(self, info_log_sink):
        jelly = _jelly()
        # Two responses: plugin bridge (204), then per-item refresh (200).
        plugin_resp = _ok()
        refresh_resp = _ok()
        with patch.object(JellyfinServer, "_request", side_effect=[plugin_resp, refresh_resp]):
            jelly._trigger_item_refresh("42")

        info_lines = [r for r in info_log_sink if r["level"] == "INFO"]
        # Plugin-bridge success line.
        assert any(
            "Test Jellyfin" in r["message"] and "Media Preview Bridge" in r["message"] and "42" in r["message"]
            for r in info_lines
        ), f"Expected plugin-bridge INFO; got: {info_log_sink!r}"
        # /Items/{id}/Refresh success line.
        assert any(
            "Test Jellyfin" in r["message"] and "Triggered item refresh" in r["message"] and "42" in r["message"]
            for r in info_lines
        ), f"Expected per-item refresh INFO; got: {info_log_sink!r}"

    def test_path_refresh_failure_does_NOT_emit_info_success_line(self, info_log_sink):
        """Failure paths stay at debug — only success promotes to INFO."""
        jelly = _jelly()

        bad = MagicMock()
        bad.raise_for_status.side_effect = RuntimeError("boom")
        bad.status_code = 503

        # Also patch _maybe_trigger_full_refresh so the fallback's own
        # INFO line doesn't muddy the assertion.
        with (
            patch.object(JellyfinServer, "_request", return_value=bad),
            patch.object(jelly, "_maybe_trigger_full_refresh"),
        ):
            jelly._trigger_path_refresh("/movies/X/NEW.mkv")

        info_lines = [r for r in info_log_sink if r["level"] == "INFO"]
        assert not any("Triggered partial scan" in r["message"] for r in info_lines), (
            "Failure path must not log a success INFO line"
        )

    def test_plugin_bridge_404_does_NOT_emit_registration_info(self, info_log_sink):
        """Plugin not installed (404) → quiet at INFO, only the per-item
        /Items/{id}/Refresh INFO fires when that succeeds."""
        jelly = _jelly()
        plugin_404 = MagicMock(status_code=404, text="not found")
        plugin_404.raise_for_status.return_value = None
        refresh_resp = _ok()
        with patch.object(JellyfinServer, "_request", side_effect=[plugin_404, refresh_resp]):
            jelly._trigger_item_refresh("42")

        info_lines = [r for r in info_log_sink if r["level"] == "INFO"]
        # No plugin-bridge "Registered" INFO when 404.
        assert not any("Registered trickplay via Media Preview Bridge" in r["message"] for r in info_lines)
        # Per-item refresh INFO still fires.
        assert any("Triggered item refresh" in r["message"] for r in info_lines)


# ---------------------------------------------------------------------------
# Emby
# ---------------------------------------------------------------------------


class TestEmbyRefreshLogging:
    def test_path_refresh_success_logs_INFO_with_server_name(self, info_log_sink):
        emby = _emby()
        with patch.object(EmbyServer, "_request", return_value=_ok()):
            emby._trigger_path_refresh("/movies/X/NEW.mkv")

        info_lines = [r for r in info_log_sink if r["level"] == "INFO"]
        assert any(
            "Test Emby" in r["message"]
            and "/movies/X/NEW.mkv" in r["message"]
            and "Triggered partial scan" in r["message"]
            for r in info_lines
        ), f"Expected Emby INFO line; got: {info_log_sink!r}"

    def test_item_refresh_success_logs_INFO(self, info_log_sink):
        emby = _emby()
        with patch.object(EmbyServer, "_request", return_value=_ok()):
            emby._trigger_item_refresh("99")

        info_lines = [r for r in info_log_sink if r["level"] == "INFO"]
        assert any(
            "Test Emby" in r["message"] and "Triggered item refresh" in r["message"] and "99" in r["message"]
            for r in info_lines
        ), f"Expected Emby per-item refresh INFO; got: {info_log_sink!r}"

    def test_deleted_path_nudge_logs_INFO(self, info_log_sink):
        """Emby UpdateType:'Deleted' nudge logs INFO too — same observability gain."""
        emby = _emby()
        with patch.object(EmbyServer, "_request", return_value=_ok()):
            emby._trigger_path_deleted("/movies/X/OLD.mkv")

        info_lines = [r for r in info_log_sink if r["level"] == "INFO"]
        assert any(
            "Test Emby" in r["message"]
            and "Notified deleted path" in r["message"]
            and "/movies/X/OLD.mkv" in r["message"]
            for r in info_lines
        ), f"Expected Emby deleted-path INFO; got: {info_log_sink!r}"
