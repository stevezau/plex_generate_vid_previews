"""Backend-real E2E: notifications panel — render, dismiss, persistence.

Audit gap #8: `/api/system/notifications` + dismiss endpoints registered,
`notifications.js` exists, no e2e test ever drove the panel end-to-end.

We seed the deterministic notification builders that ship in the app:

* `_pending_migration_notice` in settings.json triggers the
  "Settings migrated" card via ``_build_schema_migration_notification``.
* `DOCKER_IMAGE_NAME=stevezzau/plex_generate_vid_previews` env var
  triggers the deprecated-image card via
  ``_build_deprecated_image_notification``.

Both fire without needing a Vulkan probe or a UTC container, so they're
reliable across CI environments.
"""

from __future__ import annotations

import time

import pytest
from playwright.sync_api import expect

from media_preview_generator.web.notifications import (
    DEPRECATED_IMAGE_ID,
    DEPRECATED_IMAGE_NAME,
    SCHEMA_MIGRATION_ID,
)

_SEEDED_OVERRIDES = {
    # Setting this synthetic notice triggers the "Settings migrated" card.
    "_pending_migration_notice": {
        "from": 6,
        "to": 11,
        "at": "2026-01-01T00:00:00+00:00",
        "backup": "/tmp/settings.json.bak",
        "notes": ["v7: synthesised media_servers", "v11: seeded frame_reuse"],
    },
    # The reserved `_extra_env` key flows through conftest's
    # backend_real_app fixture into the subprocess environment, where
    # the deprecated-image notification builder reads it.
    "_extra_env": {"DOCKER_IMAGE_NAME": DEPRECATED_IMAGE_NAME},
}


@pytest.mark.e2e
@pytest.mark.parametrize(
    "backend_real_app",
    [_SEEDED_OVERRIDES],
    indirect=True,
)
class TestNotificationsLifecycle:
    def test_two_notifications_render_in_panel(
        self,
        backend_real_page,
        backend_real_app: tuple[str, str],
    ) -> None:
        """Both seeded notifications must show in /api/system/notifications.

        This is the contract the bell-icon dropdown depends on. If the
        endpoint silently drops one, the user only sees half their alerts.
        """
        app_url, _ = backend_real_app

        resp = backend_real_page.request.get(f"{app_url}/api/system/notifications")
        assert resp.ok, f"GET /api/system/notifications: {resp.status}"
        data = resp.json()
        notifs = data.get("notifications", [])
        ids = {n["id"] for n in notifs}

        assert SCHEMA_MIGRATION_ID in ids, (
            f"Schema-migration notification missing from list. Got IDs: {ids}. "
            "Either _pending_migration_notice wasn't read, or the builder dropped it."
        )
        assert DEPRECATED_IMAGE_ID in ids, (
            f"Deprecated-image notification missing despite DOCKER_IMAGE_NAME being set. Got IDs: {ids}"
        )

    def test_dismiss_session_removes_one_notification_from_subsequent_list(
        self,
        backend_real_page,
        backend_real_app: tuple[str, str],
    ) -> None:
        """Dismiss the deprecated-image card; a refetch must omit it.

        Bug class: the dismiss POST returns 200 but the in-memory session
        set never gets the ID, so the card reappears on next page load.
        """
        app_url, _ = backend_real_app

        # Sanity: both present before dismiss.
        before = backend_real_page.request.get(f"{app_url}/api/system/notifications").json().get("notifications", [])
        ids_before = {n["id"] for n in before}
        assert DEPRECATED_IMAGE_ID in ids_before
        assert SCHEMA_MIGRATION_ID in ids_before

        # Dismiss the deprecated-image card via real endpoint.
        dismiss_resp = backend_real_page.request.post(
            f"{app_url}/api/system/notifications/{DEPRECATED_IMAGE_ID}/dismiss"
        )
        assert dismiss_resp.ok
        assert dismiss_resp.json().get("ok") is True

        # Refetch — the dismissed one must be gone, the other must remain.
        after = backend_real_page.request.get(f"{app_url}/api/system/notifications").json().get("notifications", [])
        ids_after = {n["id"] for n in after}
        assert DEPRECATED_IMAGE_ID not in ids_after, (
            f"Session-dismiss did not suppress the notification on next list. Got IDs: {ids_after}"
        )
        assert SCHEMA_MIGRATION_ID in ids_after, (
            f"Dismissing one notification accidentally hid the other. Got IDs: {ids_after}"
        )

    def test_dismiss_permanent_persists_to_settings_json(
        self,
        backend_real_page,
        backend_real_app: tuple[str, str],
    ) -> None:
        """Permanent dismiss writes to settings.json so it survives restart.

        The lifecycle the user expects: click "Dismiss permanently" →
        notification gone forever. If the persist fails, the user sees
        the same warning on every restart and learns to ignore notifications.
        """
        import json

        app_url, config_dir = backend_real_app

        dismiss_resp = backend_real_page.request.post(
            f"{app_url}/api/system/notifications/{DEPRECATED_IMAGE_ID}/dismiss-permanent"
        )
        assert dismiss_resp.ok, dismiss_resp.text()
        assert dismiss_resp.json().get("ok") is True
        assert dismiss_resp.json().get("persisted") is True

        # Confirm on disk — the strongest assertion against "endpoint
        # returned 200 but the write failed silently".
        with open(f"{config_dir}/settings.json") as f:
            on_disk = json.load(f)
        dismissed = on_disk.get("dismissed_notifications", [])
        assert DEPRECATED_IMAGE_ID in dismissed, (
            "Permanent-dismiss endpoint returned ok but settings.json's "
            f"dismissed_notifications list is {dismissed}. The persist failed silently."
        )

        # And the GET endpoint must filter it out.
        listing = backend_real_page.request.get(f"{app_url}/api/system/notifications").json().get("notifications", [])
        assert DEPRECATED_IMAGE_ID not in {n["id"] for n in listing}

    def test_bell_dropdown_renders_notification_in_dom(
        self,
        backend_real_page,
        backend_real_app: tuple[str, str],
    ) -> None:
        """Open the dashboard; assert the bell badge + dropdown reflect notifications.

        notifications.js's loadNotifications() runs on DOMContentLoaded
        and writes entries into #notificationList. If loadNotifications
        doesn't fire, the bell badge stays at "0" and the dropdown is
        empty — even though the API returned data.
        """
        app_url, _ = backend_real_app

        backend_real_page.goto(f"{app_url}/")
        backend_real_page.wait_for_load_state("domcontentloaded")

        # Wait for loadNotifications() to populate the list.
        try:
            backend_real_page.wait_for_function(
                """() => {
                    const list = document.getElementById('notificationList');
                    if (!list) return false;
                    return list.querySelectorAll('.notification-entry').length > 0;
                }""",
                timeout=8000,
            )
        except Exception:
            list_html = backend_real_page.locator("#notificationList").inner_html()
            raise AssertionError(
                "Bell-icon dropdown never rendered any .notification-entry rows even though "
                f"the API returned notifications. List innerHTML: {list_html[:500]}"
            ) from None

        # And the badge must show a count >= 2.
        badge = backend_real_page.locator("#notificationBellBadge")
        expect(badge).not_to_have_class("d-none", timeout=2000)
        count = int((badge.text_content() or "0").strip())
        assert count >= 2, (
            f"Notification bell badge shows count={count} but two notifications "
            "should be active. Some renderer suppressed them silently."
        )

    def test_dismissed_notifications_stay_dismissed_across_simulated_reload(
        self,
        backend_real_page,
        backend_real_app: tuple[str, str],
    ) -> None:
        """Permanent dismiss + page reload → notification still hidden.

        Reload simulates the user closing + reopening the tab. Ensures the
        dismiss is read back through the GET endpoint correctly (not just
        cached in JS memory).
        """
        app_url, _ = backend_real_app

        backend_real_page.request.post(f"{app_url}/api/system/notifications/{DEPRECATED_IMAGE_ID}/dismiss-permanent")

        backend_real_page.goto(f"{app_url}/")
        backend_real_page.wait_for_load_state("domcontentloaded")
        # Give loadNotifications() a moment.
        time.sleep(0.5)

        listing = backend_real_page.request.get(f"{app_url}/api/system/notifications").json().get("notifications", [])
        ids = {n["id"] for n in listing}
        assert DEPRECATED_IMAGE_ID not in ids, (
            f"After reload, the permanently-dismissed notification reappeared. Got IDs: {ids}"
        )
