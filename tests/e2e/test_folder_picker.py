"""E2E tests for the shared folder picker modal (folder_picker.js)."""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from ._mocks import (
    capture_settings_save,
    mock_browse_directories,
    mock_setup_status,
    mock_validate_plex_config_folder,
)


def _open_picker_via_wizard(page: Page, app_url: str) -> None:
    """Drive to wizard step 3 and click the Plex config folder Browse button."""
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
    expect(page.locator('div.setup-step[data-step="3"]')).to_have_class("setup-step active")
    page.locator("#wizardPlexConfigFolderBrowseBtn").click()
    expect(page.locator("#folderPickerModal")).to_be_visible(timeout=2000)


def _wizard_mocks(page: Page) -> None:
    from ._mocks import mock_plex_libraries

    mock_plex_libraries(page)
    capture_settings_save(page)
    mock_setup_status(page, complete=False)
    mock_validate_plex_config_folder(page, valid=True)


@pytest.mark.e2e
class TestFolderPicker:
    def test_picker_opens_with_initial_path(self, wizard_page: Page, app_url_wizard: str) -> None:
        _wizard_mocks(wizard_page)
        mock_browse_directories(
            wizard_page,
            entries=[{"name": "data", "path": "/data"}, {"name": "plex", "path": "/plex"}],
            path="/plex",
        )
        _open_picker_via_wizard(wizard_page, app_url_wizard)
        # Path input pre-filled with /plex (from the input value).
        expect(wizard_page.locator("#folderPickerPathInput")).to_have_value("/plex")

    def test_typing_and_enter_navigates(self, wizard_page: Page, app_url_wizard: str) -> None:
        _wizard_mocks(wizard_page)
        # Echo-mode mock: returns whatever path the picker requested.
        mock_browse_directories(wizard_page)
        _open_picker_via_wizard(wizard_page, app_url_wizard)

        path_input = wizard_page.locator("#folderPickerPathInput")
        path_input.fill("/data")
        path_input.press("Enter")
        wizard_page.wait_for_timeout(300)
        expect(path_input).to_have_value("/data")

    def test_up_button_disabled_at_root(self, wizard_page: Page, app_url_wizard: str) -> None:
        _wizard_mocks(wizard_page)
        mock_browse_directories(wizard_page, path="/")
        _open_picker_via_wizard(wizard_page, app_url_wizard)

        path_input = wizard_page.locator("#folderPickerPathInput")
        path_input.fill("/")
        path_input.press("Enter")
        wizard_page.wait_for_timeout(300)
        expect(wizard_page.locator("#folderPickerUpBtn")).to_be_disabled()

    def test_clicking_folder_row_drills_in(self, wizard_page: Page, app_url_wizard: str) -> None:
        _wizard_mocks(wizard_page)
        # Echo mode — drilling in returns the clicked folder's path.
        mock_browse_directories(wizard_page)
        _open_picker_via_wizard(wizard_page, app_url_wizard)

        wizard_page.locator("#folderPickerPathInput").fill("/")
        wizard_page.locator("#folderPickerPathInput").press("Enter")
        wizard_page.wait_for_timeout(300)
        wizard_page.locator("#folderPickerList button").first.click()
        wizard_page.wait_for_timeout(300)
        expect(wizard_page.locator("#folderPickerPathInput")).not_to_have_value("/")

    def test_pick_button_closes_modal_and_populates_input(self, wizard_page: Page, app_url_wizard: str) -> None:
        _wizard_mocks(wizard_page)
        mock_browse_directories(wizard_page, path="/plex")
        _open_picker_via_wizard(wizard_page, app_url_wizard)

        wizard_page.locator("#folderPickerConfirmBtn").click()
        # Modal hides.
        expect(wizard_page.locator("#folderPickerModal")).to_be_hidden(timeout=2000)
        # Source input gets the picked path.
        expect(wizard_page.locator("#wizardPlexConfigFolder")).to_have_value("/plex")
