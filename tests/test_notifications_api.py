"""Tests for the notification center API (Change 4 of the DV5 plan).

Covers:
- ``GET /api/system/notifications`` returning active notifications
  assembled by ``web/notifications.py`` (wrapping the existing Vulkan
  software-fallback warning in a notification dict).
- ``POST /api/system/notifications/<id>/dismiss`` session-only dismissal.
- ``POST /api/system/notifications/<id>/dismiss-permanent`` persisting
  the dismissal to ``settings.json``.
- ``POST /api/system/notifications/reset-dismissed`` clearing persistent
  and session dismissals.
- ``SettingsManager`` round-trip for ``dismissed_notifications`` to
  make sure schema migrations (empty, missing, garbage) don't crash.
"""

import json
import os
from unittest.mock import patch

import pytest

from plex_generate_previews.web.app import create_app
from plex_generate_previews.web.notifications import (
    VULKAN_SOFTWARE_FALLBACK_ID,
    build_active_notifications,
    reset_session,
)


@pytest.fixture(autouse=True)
def _reset_notification_session():
    """Clear in-process session dismissals between tests."""
    reset_session()
    yield
    reset_session()


@pytest.fixture(autouse=True)
def _mock_healthy_timezone():
    """Force the timezone probe to report healthy.

    CI runs with ``TZ`` unset and system timezone UTC, which otherwise
    makes ``_build_timezone_misconfigured_notification`` fire in every
    test and breaks assertions that only account for the Vulkan source.
    """
    with patch(
        "plex_generate_previews.web.routes.api_system._get_timezone_info",
        return_value={"timezone": "America/New_York", "tz_env_set": True},
    ):
        yield


@pytest.fixture()
def app_with_config(tmp_path):
    """Create a Flask test app against a temp config directory."""
    config_dir = str(tmp_path / "config")
    os.makedirs(config_dir, exist_ok=True)
    with open(os.path.join(config_dir, "auth.json"), "w") as fh:
        json.dump({"token": "test-token-12345678"}, fh)
    with open(os.path.join(config_dir, "settings.json"), "w") as fh:
        json.dump({"setup_complete": True}, fh)

    with patch.dict(
        os.environ,
        {
            "CONFIG_DIR": config_dir,
            "WEB_AUTH_TOKEN": "test-token-12345678",
            "WEB_PORT": "8099",
        },
    ):
        flask_app = create_app(config_dir=config_dir)
        flask_app.config["TESTING"] = True
        flask_app.config["WTF_CSRF_ENABLED"] = False
        yield flask_app, config_dir


@pytest.fixture()
def client(app_with_config):
    flask_app, _ = app_with_config
    return flask_app.test_client()


class TestNotificationsAPI:
    """Tests for the /api/system/notifications endpoints."""

    def test_list_empty_when_vulkan_healthy(self, client):
        """Healthy Vulkan probe → no active notifications."""
        with patch(
            "plex_generate_previews.gpu_detection.get_vulkan_device_info",
            return_value={
                "device": "NVIDIA RTX 4090 (discrete) (0x2684)",
                "is_software": False,
            },
        ):
            resp = client.get("/api/system/notifications")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data == {"notifications": []}

    def test_list_contains_vulkan_warning_when_software(self, client):
        """Software Vulkan probe → vulkan_software_fallback notification."""
        with patch(
            "plex_generate_previews.gpu_detection.get_vulkan_device_info",
            return_value={
                "device": "llvmpipe (LLVM 18.1.3, 256 bits) (software) (0x0)",
                "is_software": True,
            },
        ):
            resp = client.get("/api/system/notifications")
        assert resp.status_code == 200
        notifications = resp.get_json()["notifications"]
        assert len(notifications) == 1
        entry = notifications[0]
        assert entry["id"] == VULKAN_SOFTWARE_FALLBACK_ID
        assert entry["severity"] == "warning"
        assert "Dolby Vision Profile 5" in entry["title"]
        assert "body_html" in entry
        assert entry["body_html"]  # non-empty HTML
        assert entry["dismissable"] is True
        assert entry["source"] == "vulkan_probe"

    def test_session_dismiss_hides_notification(self, client):
        """POST /dismiss hides the notification for the session without persisting."""
        with patch(
            "plex_generate_previews.gpu_detection.get_vulkan_device_info",
            return_value={
                "device": "llvmpipe (software)",
                "is_software": True,
            },
        ):
            first = client.get("/api/system/notifications").get_json()
            assert len(first["notifications"]) == 1

            dismiss_resp = client.post(
                f"/api/system/notifications/{VULKAN_SOFTWARE_FALLBACK_ID}/dismiss"
            )
            assert dismiss_resp.status_code == 200
            assert dismiss_resp.get_json() == {
                "ok": True,
                "id": VULKAN_SOFTWARE_FALLBACK_ID,
                "persisted": False,
            }

            second = client.get("/api/system/notifications").get_json()
            assert second == {"notifications": []}

    def test_session_dismiss_does_not_touch_settings_file(
        self, client, app_with_config
    ):
        """Session dismissal must not write to settings.json."""
        _, config_dir = app_with_config
        settings_path = os.path.join(config_dir, "settings.json")
        with open(settings_path) as fh:
            before = json.load(fh)

        with patch(
            "plex_generate_previews.gpu_detection.get_vulkan_device_info",
            return_value={"device": "llvmpipe", "is_software": True},
        ):
            client.post(
                f"/api/system/notifications/{VULKAN_SOFTWARE_FALLBACK_ID}/dismiss"
            )

        with open(settings_path) as fh:
            after = json.load(fh)
        assert after.get("dismissed_notifications") in (None, [])
        assert after == before

    def test_permanent_dismiss_persists_to_settings(self, client, app_with_config):
        """POST /dismiss-permanent persists to settings.json."""
        _, config_dir = app_with_config

        with patch(
            "plex_generate_previews.gpu_detection.get_vulkan_device_info",
            return_value={"device": "llvmpipe", "is_software": True},
        ):
            resp = client.post(
                f"/api/system/notifications/"
                f"{VULKAN_SOFTWARE_FALLBACK_ID}/dismiss-permanent"
            )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["ok"] is True
        assert body["persisted"] is True

        with open(os.path.join(config_dir, "settings.json")) as fh:
            stored = json.load(fh)
        assert VULKAN_SOFTWARE_FALLBACK_ID in stored.get("dismissed_notifications", [])

    def test_permanent_dismiss_is_idempotent(self, client, app_with_config):
        """POST /dismiss-permanent twice must not duplicate the entry."""
        _, config_dir = app_with_config
        with patch(
            "plex_generate_previews.gpu_detection.get_vulkan_device_info",
            return_value={"device": "llvmpipe", "is_software": True},
        ):
            client.post(
                f"/api/system/notifications/"
                f"{VULKAN_SOFTWARE_FALLBACK_ID}/dismiss-permanent"
            )
            client.post(
                f"/api/system/notifications/"
                f"{VULKAN_SOFTWARE_FALLBACK_ID}/dismiss-permanent"
            )
        with open(os.path.join(config_dir, "settings.json")) as fh:
            stored = json.load(fh)
        assert stored["dismissed_notifications"].count(VULKAN_SOFTWARE_FALLBACK_ID) == 1

    def test_permanent_dismiss_filters_from_list(self, client):
        """After permanent dismiss, the notification no longer appears."""
        with patch(
            "plex_generate_previews.gpu_detection.get_vulkan_device_info",
            return_value={"device": "llvmpipe", "is_software": True},
        ):
            client.post(
                f"/api/system/notifications/"
                f"{VULKAN_SOFTWARE_FALLBACK_ID}/dismiss-permanent"
            )
            resp = client.get("/api/system/notifications")
        assert resp.get_json() == {"notifications": []}

    def test_reset_dismissed_restores_notification(self, client, app_with_config):
        """POST /reset-dismissed clears persistent + session dismissals."""
        with patch(
            "plex_generate_previews.gpu_detection.get_vulkan_device_info",
            return_value={"device": "llvmpipe", "is_software": True},
        ):
            client.post(
                f"/api/system/notifications/"
                f"{VULKAN_SOFTWARE_FALLBACK_ID}/dismiss-permanent"
            )
            reset_resp = client.post(
                "/api/system/notifications/reset-dismissed",
                headers={"Authorization": "Bearer test-token-12345678"},
            )
            assert reset_resp.status_code == 200
            assert reset_resp.get_json() == {"ok": True}

            restored = client.get("/api/system/notifications").get_json()
        assert len(restored["notifications"]) == 1
        assert restored["notifications"][0]["id"] == VULKAN_SOFTWARE_FALLBACK_ID


class TestBuildActiveNotifications:
    """Unit tests for the pure builder, without the Flask app fixture."""

    def test_builder_returns_empty_list_when_healthy(self):
        with patch(
            "plex_generate_previews.gpu_detection.get_vulkan_device_info",
            return_value={"device": "NVIDIA", "is_software": False},
        ):
            notifications = build_active_notifications()
        assert notifications == []

    def test_builder_includes_vulkan_warning_when_software(self):
        with patch(
            "plex_generate_previews.gpu_detection.get_vulkan_device_info",
            return_value={"device": "llvmpipe", "is_software": True},
        ):
            notifications = build_active_notifications()
        ids = [n["id"] for n in notifications]
        assert VULKAN_SOFTWARE_FALLBACK_ID in ids

    def test_builder_suppresses_permanently_dismissed(self):
        with patch(
            "plex_generate_previews.gpu_detection.get_vulkan_device_info",
            return_value={"device": "llvmpipe", "is_software": True},
        ):
            notifications = build_active_notifications(
                dismissed_permanent=[VULKAN_SOFTWARE_FALLBACK_ID]
            )
        assert notifications == []


class TestSettingsManagerDismissedNotifications:
    """Tests for the dismissed_notifications property + helpers."""

    def _fresh_manager(self, tmp_path, initial_settings=None):
        from plex_generate_previews.web.settings_manager import SettingsManager

        settings_path = tmp_path / "settings.json"
        if initial_settings is not None:
            settings_path.write_text(json.dumps(initial_settings))
        return SettingsManager(config_dir=str(tmp_path))

    def test_dismissed_notifications_defaults_to_empty_list(self, tmp_path):
        mgr = self._fresh_manager(tmp_path)
        assert mgr.dismissed_notifications == []

    def test_dismissed_notifications_empty_when_garbage_stored(self, tmp_path):
        mgr = self._fresh_manager(tmp_path, {"dismissed_notifications": "not-a-list"})
        assert mgr.dismissed_notifications == []

    def test_dismiss_notification_permanent_persists(self, tmp_path):
        mgr = self._fresh_manager(tmp_path)
        mgr.dismiss_notification_permanent("foo")
        assert mgr.dismissed_notifications == ["foo"]

        reloaded = self._fresh_manager(tmp_path)
        assert reloaded.dismissed_notifications == ["foo"]

    def test_dismiss_notification_permanent_is_idempotent(self, tmp_path):
        mgr = self._fresh_manager(tmp_path)
        mgr.dismiss_notification_permanent("foo")
        mgr.dismiss_notification_permanent("foo")
        assert mgr.dismissed_notifications == ["foo"]

    def test_undismiss_notification_removes_entry(self, tmp_path):
        mgr = self._fresh_manager(tmp_path, {"dismissed_notifications": ["foo", "bar"]})
        mgr.undismiss_notification("foo")
        assert mgr.dismissed_notifications == ["bar"]

    def test_reset_dismissed_clears_all(self, tmp_path):
        mgr = self._fresh_manager(tmp_path, {"dismissed_notifications": ["foo", "bar"]})
        mgr.reset_dismissed_notifications()
        assert mgr.dismissed_notifications == []
