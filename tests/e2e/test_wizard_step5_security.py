"""E2E tests for wizard step 5: token enforcement.

Regressions covered (force-token-change pass, commit 96a92e5):

* Both fields required → blank submit shows "Please set a new access token".
* New token <8 chars → "at least 8 characters".
* Mismatched → "Tokens do not match".
* Same-as-current (server returns 400) → surfaces "different from current".
* Valid + matching → POST fires, redirect to `/`.
* env-controlled → form hidden, env-notice shown, Finish proceeds.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from ._mocks import (
    capture_settings_save,
    capture_setup_set_token,
    mock_plex_libraries,
    mock_settings_get,
    mock_setup_complete,
    mock_setup_status,
    mock_setup_token_info,
    mock_system_status,
    mock_validate_plex_config_folder,
)


def _drive_to_step5(page: Page, app_url: str) -> None:
    page.goto(f"{app_url}/setup")
    page.wait_for_load_state("domcontentloaded")
    page.locator('.wizard-vendor-btn[data-vendor="plex"]').click()
    page.evaluate("document.getElementById('manualConnectDetails').open = true")
    page.locator("#manualPlexUrl").fill("http://plex.local:32400")
    page.locator("#manualPlexToken").fill("tok")
    page.locator("#manualPlexTestBtn").click()
    expect(page.locator("#manualPlexResult")).to_contain_text("Connected", timeout=5000)
    page.locator("#step1Next").click()
    page.locator(".library-card").first.click()
    page.locator("#step2Next").click()
    page.locator("#wizardPlexConfigFolder").fill("/plex")
    page.locator("#step3Next").click()
    expect(page.locator("#gpuDetecting")).to_be_hidden()
    page.locator("#step4Next").click()
    expect(page.locator('div.setup-step[data-step="5"]')).to_have_class("setup-step active")


@pytest.fixture
def step5_page(wizard_page: Page, app_url_wizard: str) -> Page:
    """Common wizard mocks + walk to step 5 with env_controlled=false."""
    mock_plex_libraries(wizard_page)
    capture_settings_save(wizard_page)
    mock_setup_status(wizard_page, complete=False)
    mock_validate_plex_config_folder(wizard_page, valid=True)
    mock_settings_get(wizard_page)
    mock_system_status(wizard_page)
    mock_setup_token_info(wizard_page, env_controlled=False)
    _drive_to_step5(wizard_page, app_url_wizard)
    return wizard_page


@pytest.mark.e2e
class TestStep5TokenEnforcement:
    def test_blank_submit_shows_required_error(self, step5_page: Page) -> None:
        capture_setup_set_token(step5_page)
        step5_page.locator("#finishSetup").click()
        expect(step5_page.locator("#tokenError")).to_be_visible()
        expect(step5_page.locator("#tokenError")).to_contain_text("Please set a new access token")

    def test_too_short_shows_length_error(self, step5_page: Page) -> None:
        capture_setup_set_token(step5_page)
        step5_page.locator("#newToken").fill("short")
        step5_page.locator("#confirmToken").fill("short")
        step5_page.locator("#finishSetup").click()
        expect(step5_page.locator("#tokenError")).to_contain_text("at least 8 characters")

    def test_mismatched_tokens_show_mismatch_error(self, step5_page: Page) -> None:
        capture_setup_set_token(step5_page)
        step5_page.locator("#newToken").fill("validtoken1")
        step5_page.locator("#confirmToken").fill("validtoken2")
        step5_page.locator("#finishSetup").click()
        expect(step5_page.locator("#tokenError")).to_contain_text("Tokens do not match")

    def test_same_as_current_server_rejection_surfaces(self, step5_page: Page) -> None:
        capture_setup_set_token(
            step5_page,
            ok=False,
            error="New token must be different from the current one.",
        )
        step5_page.locator("#newToken").fill("validtoken1")
        step5_page.locator("#confirmToken").fill("validtoken1")
        step5_page.locator("#finishSetup").click()
        expect(step5_page.locator("#tokenError")).to_contain_text("different from the current")

    def test_valid_token_posts_and_redirects(self, step5_page: Page) -> None:
        captured = capture_setup_set_token(step5_page, ok=True)
        called_complete = mock_setup_complete(step5_page, redirect="/")
        # Stub / so the redirect navigates without hitting the
        # before_request middleware (setup_complete still false python-side).
        import re

        step5_page.route(
            re.compile(r"/$"),
            lambda r: r.fulfill(content_type="text/html", body="<html>home</html>"),
        )
        step5_page.locator("#newToken").fill("brand-new-token-1")
        step5_page.locator("#confirmToken").fill("brand-new-token-1")
        step5_page.locator("#finishSetup").click()
        # Wait for the token POST to land + the complete POST to fire.
        step5_page.wait_for_url("**/", timeout=5000)
        assert captured, "POST /api/setup/set-token never fired"
        assert captured[0]["token"] == "brand-new-token-1"
        assert called_complete, "POST /api/setup/complete never fired"


@pytest.mark.e2e
class TestEnvControlledToken:
    def test_env_controlled_hides_form_and_proceeds_without_token_post(
        self, wizard_page: Page, app_url_wizard: str
    ) -> None:
        mock_plex_libraries(wizard_page)
        capture_settings_save(wizard_page)
        mock_setup_status(wizard_page, complete=False)
        mock_validate_plex_config_folder(wizard_page, valid=True)
        mock_settings_get(wizard_page)
        mock_system_status(wizard_page)
        mock_setup_token_info(wizard_page, env_controlled=True)
        captured = capture_setup_set_token(wizard_page)
        called_complete = mock_setup_complete(wizard_page, redirect="/")
        import re

        wizard_page.route(
            re.compile(r"/$"),
            lambda r: r.fulfill(content_type="text/html", body="<html>home</html>"),
        )
        _drive_to_step5(wizard_page, app_url_wizard)

        # Env notice visible, custom form hidden.
        expect(wizard_page.locator("#tokenEnvNotice")).to_be_visible()
        expect(wizard_page.locator("#customTokenSection")).to_be_hidden()

        wizard_page.locator("#finishSetup").click()
        wizard_page.wait_for_url("**/", timeout=5000)
        assert not captured, "set-token should NOT have been called when env-controlled"
        assert called_complete, "complete should fire even when env-controlled"
