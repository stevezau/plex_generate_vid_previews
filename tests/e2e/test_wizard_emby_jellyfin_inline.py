"""E2E tests for the Emby/Jellyfin inline connection panel in step 1.

After the popup-vs-inline fix, picking Emby or Jellyfin reveals the
shared connection form INSIDE step 1's card (no Bootstrap modal).
After save, `mediaServerAdded` event jumps the wizard to step 4.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from ._mocks import (
    capture_servers_save,
    capture_settings_save,
    mock_emby_password_auth,
    mock_jellyfin_quick_connect,
    mock_servers_test_connection,
    mock_settings_get,
    mock_setup_status,
    mock_setup_token_info,
    mock_system_status,
)


@pytest.mark.e2e
class TestEmbyInline:
    def test_emby_password_save_advances_to_step4(self, wizard_page: Page, app_url_wizard: str) -> None:
        capture_settings_save(wizard_page)
        mock_setup_status(wizard_page, complete=False)
        mock_settings_get(wizard_page)
        mock_system_status(wizard_page)
        mock_setup_token_info(wizard_page, env_controlled=False)
        mock_emby_password_auth(wizard_page, ok=True)
        mock_servers_test_connection(wizard_page, ok=True, server_name="Test Emby")
        captured = capture_servers_save(wizard_page, vendor="emby")

        wizard_page.goto(f"{app_url_wizard}/setup")
        wizard_page.wait_for_load_state("domcontentloaded")
        wizard_page.locator('.wizard-vendor-btn[data-vendor="emby"]').click()
        # Inline form visible, no modal popup.
        expect(wizard_page.locator("#ejConnectPanel")).to_be_visible()
        assert wizard_page.locator("#addServerModal").count() == 0

        # Fill connection details.
        wizard_page.locator("#serverUrl").fill("http://emby.local:8096")
        wizard_page.locator("#serverName").fill("Test Emby")
        # Default auth method is password — fields are visible.
        wizard_page.locator("#authUsername").fill("admin")
        wizard_page.locator("#authPassword").fill("hunter2")
        wizard_page.locator("#step-connect-test").click()

        # Connection test result panel renders.
        expect(wizard_page.locator("#connectResult")).to_contain_text("Connected")
        wizard_page.locator("#step-result-save").click()

        # Wizard advances to step 4 because the mediaServerAdded event
        # listener short-circuits past steps 2+3 for non-Plex saves.
        expect(wizard_page.locator('div.setup-step[data-step="4"]')).to_have_class("setup-step active")
        assert captured, "POST /api/servers never fired"
        assert captured[0]["type"] == "emby"


@pytest.mark.e2e
class TestJellyfinInline:
    def test_jellyfin_quick_connect_save_advances_to_step4(self, wizard_page: Page, app_url_wizard: str) -> None:
        capture_settings_save(wizard_page)
        mock_setup_status(wizard_page, complete=False)
        mock_settings_get(wizard_page)
        mock_system_status(wizard_page)
        mock_setup_token_info(wizard_page, env_controlled=False)
        mock_jellyfin_quick_connect(wizard_page, code="ABC123", poll_attempts_until_authenticated=1)
        mock_servers_test_connection(wizard_page, ok=True, server_name="Test Jellyfin")
        captured = capture_servers_save(wizard_page, vendor="jellyfin")

        wizard_page.goto(f"{app_url_wizard}/setup")
        wizard_page.wait_for_load_state("domcontentloaded")
        wizard_page.locator('.wizard-vendor-btn[data-vendor="jellyfin"]').click()
        expect(wizard_page.locator("#ejConnectPanel")).to_be_visible()

        wizard_page.locator("#serverUrl").fill("http://jellyfin.local:8096")
        wizard_page.locator("#serverName").fill("Test Jellyfin")
        # Switch to Quick Connect auth method.
        wizard_page.locator("#auth-quick").check()
        wizard_page.locator("#quickConnectStart").click()
        # The displayed code matches the mock.
        expect(wizard_page.locator("#quickConnectCode")).to_contain_text("ABC123")
        # Poll fires every 2s; wait for "Approved" status.
        expect(wizard_page.locator("#quickConnectCode")).to_contain_text("Approved", timeout=5000)
        wizard_page.locator("#step-connect-test").click()
        expect(wizard_page.locator("#connectResult")).to_contain_text("Connected")
        wizard_page.locator("#step-result-save").click()

        expect(wizard_page.locator('div.setup-step[data-step="4"]')).to_have_class("setup-step active")
        assert captured, "POST /api/servers never fired"
        assert captured[0]["type"] == "jellyfin"
