"""E2E tests for wizard step 3: Plex config folder + path mappings.

Regressions covered:

* `#wizardPlexConfigFolder` validates inline (regression for the
  duplicate-id bug where validation silently no-op'd).
* Browse button opens the folder picker modal.
* "Add another mapping" appends a row with Browse + validation hooks.
* Path-mapping local input validates against `/api/settings/validate-local-path`.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from ._mocks import (
    capture_settings_save,
    mock_browse_directories,
    mock_plex_libraries,
    mock_setup_status,
    mock_validate_local_path,
    mock_validate_plex_config_folder,
)


def _drive_to_step3(page: Page, app_url: str) -> None:
    """Walk through step 1 + 2 (mocked) to land on step 3."""
    page.goto(f"{app_url}/setup")
    page.wait_for_load_state("domcontentloaded")
    page.locator('.wizard-vendor-btn[data-vendor="plex"]').click()
    page.evaluate("document.getElementById('manualConnectDetails').open = true")
    page.locator("#manualPlexUrl").fill("http://plex.local:32400")
    page.locator("#manualPlexToken").fill("tok")
    page.locator("#manualPlexTestBtn").click()
    expect(page.locator("#manualPlexResult")).to_contain_text("Connected", timeout=5000)
    page.locator("#step1Next").click()
    expect(page.locator('div.setup-step[data-step="2"]')).to_have_class("setup-step active")
    # Tick the first library so step 2's Next enables.
    page.locator(".library-card").first.click()
    page.locator("#step2Next").click()
    expect(page.locator('div.setup-step[data-step="3"]')).to_have_class("setup-step active")


@pytest.mark.e2e
class TestPlexConfigFolderValidation:
    def test_valid_path_paints_is_valid(self, wizard_page: Page, app_url_wizard: str) -> None:
        mock_plex_libraries(wizard_page)
        capture_settings_save(wizard_page)
        mock_setup_status(wizard_page, complete=False)
        mock_validate_plex_config_folder(wizard_page, valid=True)
        _drive_to_step3(wizard_page, app_url_wizard)

        cfg = wizard_page.locator("#wizardPlexConfigFolder")
        cfg.fill("/plex")
        # Validator is debounced 400ms; wait it out + a buffer.
        wizard_page.wait_for_timeout(700)
        expect(cfg).to_have_class("form-control is-valid")

    def test_invalid_path_paints_is_invalid_with_error(self, wizard_page: Page, app_url_wizard: str) -> None:
        mock_plex_libraries(wizard_page)
        capture_settings_save(wizard_page)
        mock_setup_status(wizard_page, complete=False)
        mock_validate_plex_config_folder(wizard_page, valid=False, error="Folder not found")
        _drive_to_step3(wizard_page, app_url_wizard)

        cfg = wizard_page.locator("#wizardPlexConfigFolder")
        cfg.fill("/nope")
        wizard_page.wait_for_timeout(700)
        expect(cfg).to_have_class("form-control is-invalid")
        # The error message lands in the sibling .invalid-feedback.
        feedback = cfg.locator("..").locator(".invalid-feedback")
        expect(feedback).to_contain_text("Folder not found")


@pytest.mark.e2e
class TestPathMappingRows:
    def test_browse_button_opens_folder_picker(self, wizard_page: Page, app_url_wizard: str) -> None:
        mock_plex_libraries(wizard_page)
        capture_settings_save(wizard_page)
        mock_setup_status(wizard_page, complete=False)
        mock_validate_plex_config_folder(wizard_page, valid=True)
        mock_browse_directories(wizard_page)
        _drive_to_step3(wizard_page, app_url_wizard)

        wizard_page.locator("#wizardPlexConfigFolderBrowseBtn").click()
        # folder_picker.js lazily injects #folderPickerModal on first open.
        expect(wizard_page.locator("#folderPickerModal")).to_be_visible(timeout=2000)

    def test_add_mapping_row_appends_row_with_browse_and_feedback(self, wizard_page: Page, app_url_wizard: str) -> None:
        mock_plex_libraries(wizard_page)
        capture_settings_save(wizard_page)
        mock_setup_status(wizard_page, complete=False)
        mock_validate_plex_config_folder(wizard_page, valid=True)
        _drive_to_step3(wizard_page, app_url_wizard)

        # Step 3 entry calls settingsManager.get() which renders one default
        # empty row. Wait for that to render before counting.
        first_row = wizard_page.locator("#setupPathMappingsContainer .path-mapping-row").first
        expect(first_row).to_be_visible(timeout=2000)
        rows_before = wizard_page.locator("#setupPathMappingsContainer .path-mapping-row").count()
        wizard_page.locator("#setupAddPathMappingBtn").click()
        rows_after = wizard_page.locator("#setupPathMappingsContainer .path-mapping-row").count()
        assert rows_after == rows_before + 1
        # Newest row has the browse button + feedback divs.
        last_row = wizard_page.locator("#setupPathMappingsContainer .path-mapping-row").last
        expect(last_row.locator(".path-mapping-browse")).to_be_visible()
        expect(last_row.locator(".invalid-feedback")).to_be_attached()
        expect(last_row.locator(".valid-feedback")).to_be_attached()

    def test_local_path_invalid_paints_red(self, wizard_page: Page, app_url_wizard: str) -> None:
        mock_plex_libraries(wizard_page)
        capture_settings_save(wizard_page)
        mock_setup_status(wizard_page, complete=False)
        mock_validate_plex_config_folder(wizard_page, valid=True)
        mock_validate_local_path(wizard_page, exists=False, error="Directory not found")
        _drive_to_step3(wizard_page, app_url_wizard)

        local_input = wizard_page.locator("#setupPathMappingsContainer .path-mapping-row .path-mapping-local").first
        local_input.fill("/nope/not/here")
        wizard_page.wait_for_timeout(700)
        expect(local_input).to_have_class("form-control form-control-sm path-mapping-local is-invalid")
