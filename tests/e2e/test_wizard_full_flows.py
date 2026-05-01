"""End-to-end happy-path wizard flows per vendor.

These tests walk the wizard from step 1 → completion, asserting the
expected per-step transitions + the final POST sequence. Per-step
mechanics are covered in the per-step files; this file covers the
*sequencing* — i.e. step 4 follows step 3 follows step 2, etc.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from ._mocks import (
    capture_servers_save,
    capture_settings_save,
    capture_setup_set_token,
    mock_emby_password_auth,
    mock_plex_libraries,
    mock_servers_test_connection,
    mock_settings_get,
    mock_setup_complete,
    mock_setup_status,
    mock_setup_token_info,
    mock_system_status,
    mock_validate_plex_config_folder,
)


@pytest.mark.e2e
class TestPlexHappyPath:
    def test_plex_full_wizard_completes(self, wizard_page: Page, app_url_wizard: str) -> None:
        # Mock every endpoint the Plex wizard touches.
        mock_plex_libraries(wizard_page)
        capture_settings_save(wizard_page)
        mock_setup_status(wizard_page, complete=False)
        mock_validate_plex_config_folder(wizard_page, valid=True)
        mock_settings_get(wizard_page)
        mock_system_status(wizard_page)
        mock_setup_token_info(wizard_page, env_controlled=False)
        captured_token = capture_setup_set_token(wizard_page, ok=True)
        called_complete = mock_setup_complete(wizard_page, redirect="/")
        # Stub the literal "/" navigation only — the previous broader
        # **/ glob caused state-leak hangs across consecutive tests.
        import re

        wizard_page.route(
            re.compile(r"/$"),
            lambda r: r.fulfill(content_type="text/html", body="<html>home</html>"),
        )

        wizard_page.goto(f"{app_url_wizard}/setup")
        wizard_page.wait_for_load_state("domcontentloaded")

        # Step 1 — Plex manual sign-in.
        wizard_page.locator('.wizard-vendor-btn[data-vendor="plex"]').click()
        wizard_page.evaluate("document.getElementById('manualConnectDetails').open = true")
        wizard_page.locator("#manualPlexUrl").fill("http://plex.local:32400")
        wizard_page.locator("#manualPlexToken").fill("plex-tok")
        wizard_page.locator("#manualPlexTestBtn").click()
        expect(wizard_page.locator("#manualPlexResult")).to_contain_text("Connected", timeout=5000)
        # Wizard auto-advances after a successful Plex sign-in — no Next click
        # needed. Wait for step 2 to become active rather than driving it
        # ourselves; this locks the auto-advance behaviour in.
        expect(wizard_page.locator('div.setup-step[data-step="2"]')).to_have_class("setup-step active", timeout=3000)
        wizard_page.locator(".library-card").first.click()
        wizard_page.locator(".library-card").nth(1).click()
        wizard_page.locator("#step2Next").click()

        # Step 3 — paths.
        expect(wizard_page.locator('div.setup-step[data-step="3"]')).to_have_class("setup-step active")
        wizard_page.locator("#wizardPlexConfigFolder").fill("/plex")
        wizard_page.locator("#step3Next").click()

        # Step 4 — processing.
        expect(wizard_page.locator('div.setup-step[data-step="4"]')).to_have_class("setup-step active")
        expect(wizard_page.locator("#gpuDetecting")).to_be_hidden()
        wizard_page.locator("#step4Next").click()

        # Step 5 — security/token.
        expect(wizard_page.locator('div.setup-step[data-step="5"]')).to_have_class("setup-step active")
        wizard_page.locator("#newToken").fill("happy-path-token-1")
        wizard_page.locator("#confirmToken").fill("happy-path-token-1")
        wizard_page.locator("#finishSetup").click()
        wizard_page.wait_for_url("**/", timeout=5000)

        assert captured_token, "set-token never fired"
        assert called_complete, "complete never fired"


@pytest.mark.e2e
class TestEmbyHappyPath:
    def test_emby_skips_plex_specific_steps(self, wizard_page: Page, app_url_wizard: str) -> None:
        capture_settings_save(wizard_page)
        mock_setup_status(wizard_page, complete=False)
        mock_settings_get(wizard_page)
        mock_system_status(wizard_page)
        mock_setup_token_info(wizard_page, env_controlled=False)
        mock_emby_password_auth(wizard_page, ok=True)
        mock_servers_test_connection(wizard_page, ok=True)
        capture_servers_save(wizard_page, vendor="emby")
        captured_token = capture_setup_set_token(wizard_page, ok=True)
        called_complete = mock_setup_complete(wizard_page, redirect="/")
        # Stub the literal "/" navigation only — the previous broader
        # **/ glob caused state-leak hangs across consecutive tests.
        import re

        wizard_page.route(
            re.compile(r"/$"),
            lambda r: r.fulfill(content_type="text/html", body="<html>home</html>"),
        )

        wizard_page.goto(f"{app_url_wizard}/setup")
        wizard_page.wait_for_load_state("domcontentloaded")
        wizard_page.locator('.wizard-vendor-btn[data-vendor="emby"]').click()
        wizard_page.locator("#serverUrl").fill("http://emby.local:8096")
        wizard_page.locator("#serverName").fill("Test Emby")
        wizard_page.locator("#authUsername").fill("admin")
        wizard_page.locator("#authPassword").fill("hunter2")
        wizard_page.locator("#step-connect-test").click()
        expect(wizard_page.locator("#connectResult")).to_contain_text("Connected")
        wizard_page.locator("#step-result-save").click()

        # Wizard jumps to step 4 (skipping Plex-specific 2+3).
        expect(wizard_page.locator('div.setup-step[data-step="4"]')).to_have_class("setup-step active")
        expect(wizard_page.locator("#gpuDetecting")).to_be_hidden()
        wizard_page.locator("#step4Next").click()

        # Step 5 token set.
        wizard_page.locator("#newToken").fill("emby-flow-token-9")
        wizard_page.locator("#confirmToken").fill("emby-flow-token-9")
        wizard_page.locator("#finishSetup").click()
        wizard_page.wait_for_url("**/", timeout=5000)
        assert captured_token
        assert called_complete
