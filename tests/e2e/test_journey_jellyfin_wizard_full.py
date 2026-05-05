"""Jellyfin happy-path wizard flow through all 5 steps.

The audit identified this gap explicitly: existing wizard tests cover
Plex (test_wizard_full_flows.py::TestPlexHappyPath) and Emby
(test_wizard_full_flows.py::TestEmbyHappyPath) but **NO Jellyfin** full
walk through every step. The Quick-Connect inline test only verifies
the step-1 to step-4 jump, not the full step-5 token + complete flow.

Pattern follows TestEmbyHappyPath (Jellyfin also skips steps 2+3 because
those are Plex-specific) — but uses the Quick Connect device-code flow
so the full Jellyfin authentication ceremony is exercised.

Mirrors the existing wizard pattern: vendor APIs are mocked client-side
because the wizard subprocess otherwise has no connectivity to a real
Jellyfin. The signal is "the wizard JS sequences the steps + emits the
right events to the real Flask backend." Full real-backend wizard
coverage would require a live Jellyfin and is out of scope.
"""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect

from ._mocks import (
    capture_servers_save,
    capture_settings_save,
    capture_setup_set_token,
    mock_jellyfin_quick_connect,
    mock_servers_test_connection,
    mock_settings_get,
    mock_setup_complete,
    mock_setup_status,
    mock_setup_token_info,
    mock_system_status,
)


@pytest.mark.e2e
class TestJellyfinFullWizard:
    def test_jellyfin_quick_connect_full_5_step_flow(self, wizard_page: Page, app_url_wizard: str) -> None:
        # Mock every endpoint the Jellyfin wizard touches.
        capture_settings_save(wizard_page)
        mock_setup_status(wizard_page, complete=False)
        mock_settings_get(wizard_page)
        mock_system_status(wizard_page)
        mock_setup_token_info(wizard_page, env_controlled=False)
        mock_jellyfin_quick_connect(
            wizard_page,
            code="JFCODE7",
            poll_attempts_until_authenticated=1,
        )
        mock_servers_test_connection(wizard_page, ok=True, server_name="Test Jellyfin")
        captured_servers = capture_servers_save(wizard_page, vendor="jellyfin")
        captured_token = capture_setup_set_token(wizard_page, ok=True)
        called_complete = mock_setup_complete(wizard_page, redirect="/")

        # Stub the literal "/" navigation to keep the wizard test isolated
        # (matches the pattern in test_wizard_full_flows.py — using the
        # broader "**/" glob caused state-leak hangs).
        wizard_page.route(
            re.compile(r"/$"),
            lambda r: r.fulfill(content_type="text/html", body="<html>home</html>"),
        )

        # Step 1 — vendor pick + Quick Connect.
        wizard_page.goto(f"{app_url_wizard}/setup")
        wizard_page.wait_for_load_state("domcontentloaded")
        wizard_page.locator('.wizard-vendor-btn[data-vendor="jellyfin"]').click()
        expect(wizard_page.locator("#ejConnectPanel")).to_be_visible()

        wizard_page.locator("#serverUrl").fill("http://jellyfin.local:8096")
        wizard_page.locator("#serverName").fill("Test Jellyfin")

        # Switch to Quick Connect auth method and walk the device-code flow.
        wizard_page.locator("#auth-quick").check()
        wizard_page.locator("#quickConnectStart").click()
        # The mocked initiate returns code="JFCODE7"; the panel renders it.
        expect(wizard_page.locator("#quickConnectCode")).to_contain_text("JFCODE7", timeout=3000)
        # Poll fires every 2s; mock flips to authenticated after 1 poll.
        expect(wizard_page.locator("#quickConnectCode")).to_contain_text("Approved", timeout=8000)

        # Test the connection through the connection-test panel.
        wizard_page.locator("#step-connect-test").click()
        expect(wizard_page.locator("#connectResult")).to_contain_text("Connected", timeout=5000)
        wizard_page.locator("#step-result-save").click()

        # Wizard skips Plex-specific steps 2+3 for Jellyfin.
        # mediaServerAdded event listener jumps directly to step 4.
        expect(wizard_page.locator('div.setup-step[data-step="4"]')).to_have_class("setup-step active", timeout=5000)
        expect(wizard_page.locator("#gpuDetecting")).to_be_hidden(timeout=3000)
        wizard_page.locator("#step4Next").click()

        # Step 5 — security token.
        expect(wizard_page.locator('div.setup-step[data-step="5"]')).to_have_class("setup-step active")
        wizard_page.locator("#newToken").fill("jf-flow-token-42")
        wizard_page.locator("#confirmToken").fill("jf-flow-token-42")
        wizard_page.locator("#finishSetup").click()
        wizard_page.wait_for_url("**/", timeout=5000)

        # Assertions on the contract the wizard is supposed to satisfy.
        assert captured_servers, "POST /api/servers never fired during Jellyfin wizard"
        assert captured_servers[0]["type"] == "jellyfin", f"Server save sent wrong type: {captured_servers[0]}"
        assert captured_servers[0].get("name") == "Test Jellyfin", f"Server save sent wrong name: {captured_servers[0]}"
        assert captured_token, "POST /api/setup/set-token never fired"
        # The token POSTed must match what we typed.
        assert captured_token[0].get("token") == "jf-flow-token-42", (
            f"Token captured was {captured_token[0]} — wizard sent the wrong value."
        )
        assert called_complete, "POST /api/setup/complete never fired"
