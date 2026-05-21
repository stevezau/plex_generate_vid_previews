"""Notification-center surfacing of unhealthy media mounts.

Mirrors the startup WARNING into the dashboard bell so the operator sees
"this disk looks unmounted" in the UI — not just buried in the log. Born
from job be0151d2; see project_stale_bindmount_missing_on_disk.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from media_preview_generator.web.notifications import (
    MEDIA_MOUNT_UNHEALTHY_ID,
    _build_unhealthy_media_mounts_notification,
)


def _settings_with_servers(servers):
    return SimpleNamespace(get=lambda key, default=None: servers if key == "media_servers" else default)


def _mapping(local):
    return {"plex_prefix": local, "local_prefix": local, "webhook_prefixes": ["/data"]}


class TestUnhealthyMediaMountsNotification:
    def test_fires_with_warning_when_a_mount_is_empty(self, tmp_path):
        empty = tmp_path / "data_16tb3"
        empty.mkdir()
        servers = [{"name": "Plex", "path_mappings": [_mapping(str(empty))]}]

        with patch(
            "media_preview_generator.web.settings_manager.get_settings_manager",
            return_value=_settings_with_servers(servers),
        ):
            notif = _build_unhealthy_media_mounts_notification()

        assert notif is not None
        assert notif["id"] == MEDIA_MOUNT_UNHEALTHY_ID
        assert notif["severity"] == "warning"
        assert str(empty) in notif["body_html"]

    def test_returns_none_when_all_mounts_healthy(self, tmp_path):
        good = tmp_path / "data_16tb"
        good.mkdir()
        (good / "TV Shows").mkdir()
        servers = [{"name": "Plex", "path_mappings": [_mapping(str(good))]}]

        with patch(
            "media_preview_generator.web.settings_manager.get_settings_manager",
            return_value=_settings_with_servers(servers),
        ):
            assert _build_unhealthy_media_mounts_notification() is None

    def test_returns_none_when_no_servers_configured(self):
        with patch(
            "media_preview_generator.web.settings_manager.get_settings_manager",
            return_value=_settings_with_servers([]),
        ):
            assert _build_unhealthy_media_mounts_notification() is None
