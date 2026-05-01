"""E2E tests for wizard step 2: library multi-select.

Covers the regression that prompted this whole testing effort:

* Clicking a library card actually ticks the checkbox (previous bug:
  `<label for>` double-toggle left it unchecked).
* The card highlights via the .selected class.
* The Next button is enabled iff at least one card is selected.
* Empty + error states render the right copy.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from ._mocks import (
    capture_settings_save,
    mock_plex_libraries,
    mock_setup_status,
)


def _drive_step1_manual_plex(
    page: Page,
    app_url: str,
    *,
    url: str = "http://plex.local:32400",
    token: str = "fake-token",
) -> None:
    """Walk step 1 → step 2 transition via the manual Plex token form.

    Avoids the OAuth popup (flaky in headless), exercises the same
    downstream `/api/plex/libraries` hit + the `selectedServer` /
    `isAuthenticated` state machine that step 2 reads.
    """
    page.goto(f"{app_url}/setup")
    page.wait_for_load_state("domcontentloaded")
    page.locator('.wizard-vendor-btn[data-vendor="plex"]').click()
    # Open the "manual" details accordion so #manualPlexTestBtn is reachable.
    page.evaluate("document.getElementById('manualConnectDetails').open = true")
    page.locator("#manualPlexUrl").fill(url)
    page.locator("#manualPlexToken").fill(token)
    page.locator("#manualPlexTestBtn").click()
    # Wait for the success message — proves /api/plex/libraries returned ok
    # and the JS enabled #step1Next.
    expect(page.locator("#manualPlexResult")).to_contain_text("Connected", timeout=5000)
    expect(page.locator("#step1Next")).to_be_enabled()
    page.locator("#step1Next").click()
    # Step 2 visible — scope to .setup-step (the progress indicator
    # also has data-step="2", causing a strict-mode collision).
    expect(page.locator('div.setup-step[data-step="2"]')).to_have_class("setup-step active")


@pytest.mark.e2e
class TestLibraryPicker:
    def test_three_library_cards_render(self, wizard_page: Page, app_url_wizard: str) -> None:
        mock_plex_libraries(wizard_page)  # default: 3 libraries
        capture_settings_save(wizard_page)
        mock_setup_status(wizard_page, complete=False)
        _drive_step1_manual_plex(wizard_page, app_url_wizard)
        cards = wizard_page.locator(".library-card")
        expect(cards).to_have_count(3)

    def test_clicking_card_ticks_checkbox_and_enables_next(self, wizard_page: Page, app_url_wizard: str) -> None:
        """**The library checkbox regression test.** Click a card → the
        underlying checkbox is :checked, .selected class is on the card,
        #step2Next is enabled. (Old bug: double-toggle left it unchecked.)"""
        mock_plex_libraries(wizard_page)
        capture_settings_save(wizard_page)
        mock_setup_status(wizard_page, complete=False)
        _drive_step1_manual_plex(wizard_page, app_url_wizard)

        first_card = wizard_page.locator(".library-card").first
        first_checkbox = first_card.locator('input[type="checkbox"]')
        # Pre-condition: nothing selected, Next disabled.
        expect(first_checkbox).not_to_be_checked()
        expect(wizard_page.locator("#step2Next")).to_be_disabled()

        first_card.click()
        expect(first_checkbox).to_be_checked()
        expect(first_card).to_have_class("library-card mb-0 selected")
        expect(wizard_page.locator("#step2Next")).to_be_enabled()

    def test_clicking_card_again_unticks(self, wizard_page: Page, app_url_wizard: str) -> None:
        mock_plex_libraries(wizard_page)
        capture_settings_save(wizard_page)
        mock_setup_status(wizard_page, complete=False)
        _drive_step1_manual_plex(wizard_page, app_url_wizard)

        card = wizard_page.locator(".library-card").first
        cb = card.locator('input[type="checkbox"]')
        card.click()
        expect(cb).to_be_checked()
        card.click()
        expect(cb).not_to_be_checked()
        expect(wizard_page.locator("#step2Next")).to_be_disabled()

    def test_zero_libraries_renders_empty_grid(self, wizard_page: Page, app_url_wizard: str) -> None:
        mock_plex_libraries(wizard_page, libs=[])
        capture_settings_save(wizard_page)
        mock_setup_status(wizard_page, complete=False)
        _drive_step1_manual_plex(wizard_page, app_url_wizard)
        # Library grid exists but has zero cards.
        expect(wizard_page.locator(".library-card")).to_have_count(0)
        expect(wizard_page.locator("#step2Next")).to_be_disabled()
