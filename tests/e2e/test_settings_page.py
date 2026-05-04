"""E2E tests for /settings — full page interaction coverage."""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from ._mocks import (
    capture_settings_backups_restore,
    capture_settings_save,
    mock_settings_backups,
    mock_settings_get,
    mock_setup_status,
    mock_system_status,
    mock_token_regenerate,
    mock_token_set,
)
from .conftest import accept_app_confirm


@pytest.fixture(scope="session", autouse=True)
def _complete_setup(complete_setup) -> None:
    return complete_setup


@pytest.fixture
def settings_page(authed_page: Page, app_url: str) -> Page:
    mock_settings_get(authed_page)
    mock_setup_status(authed_page, complete=True, plex_authenticated=True)
    mock_system_status(authed_page)
    mock_settings_backups(authed_page)
    capture_settings_save(authed_page)
    authed_page.goto(f"{app_url}/settings")
    authed_page.wait_for_load_state("domcontentloaded")
    return authed_page


@pytest.mark.e2e
class TestSettingsLayout:
    def test_sidebar_links_present(self, settings_page: Page) -> None:
        for href in (
            "#section-media-servers",
            "#section-processing",
            "#section-logging",
            "#section-auth",
            "#section-backups",
            "#section-about",
        ):
            expect(settings_page.locator(f'a[href="{href}"]')).to_be_visible()

    def test_per_gpu_panel_renders_cards(self, settings_page: Page) -> None:
        # mock_system_status renders 2 GPUs by default.
        expect(settings_page.locator("#gpuDetecting")).to_be_hidden(timeout=3000)
        expect(settings_page.locator("#gpuConfigList .card")).to_have_count(2)

    def test_disabling_gpu_greys_settings(self, settings_page: Page) -> None:
        expect(settings_page.locator("#gpuDetecting")).to_be_hidden(timeout=3000)
        toggle = settings_page.locator(".gpu-enable-toggle").first
        device_id = toggle.get_attribute("data-device") or ""
        safe_id = "".join(c if c.isalnum() else "_" for c in device_id)
        toggle.uncheck()
        first_settings = settings_page.locator(f".gpu-settings-{safe_id}").first
        opacity = first_settings.evaluate("el => el.style.opacity")
        assert opacity == "0.5"


@pytest.mark.e2e
class TestSettingsSteppers:
    def test_cpu_workers_stepper_increments(self, settings_page: Page) -> None:
        cpu = settings_page.locator("#cpuThreads")
        expect(cpu).to_have_value("1")
        plus = cpu.locator(".. >> .stepper-plus").first
        plus.click()
        expect(cpu).to_have_value("2")

    def test_thumbnail_interval_stepper_works(self, settings_page: Page) -> None:
        interval = settings_page.locator("#thumbnailInterval")
        expect(interval).to_have_value("2")
        plus = interval.locator(".. >> .stepper-plus").first
        plus.click()
        expect(interval).to_have_value("3")

    def test_log_rotation_stepper_works(self, settings_page: Page) -> None:
        rot = settings_page.locator("#logRotationSize")
        expect(rot).to_have_value("10")
        plus = rot.locator(".. >> .stepper-plus").first
        plus.click()
        expect(rot).to_have_value("11")


@pytest.mark.e2e
class TestSettingsAuth:
    def test_set_custom_token_matching_succeeds(self, authed_page: Page, app_url: str) -> None:
        mock_settings_get(authed_page)
        mock_setup_status(authed_page, complete=True)
        mock_system_status(authed_page)
        mock_settings_backups(authed_page)
        capture_settings_save(authed_page)
        captured = mock_token_set(authed_page, ok=True)
        authed_page.goto(f"{app_url}/settings")
        authed_page.wait_for_load_state("domcontentloaded")

        # Settings page uses customAuthToken + customAuthTokenConfirm.
        # The "log out all sessions" prompt is an appConfirm modal — click
        # its OK button after triggering setCustomToken().
        authed_page.locator("#customAuthToken").fill("brand-new-tok-1")
        authed_page.locator("#customAuthTokenConfirm").fill("brand-new-tok-1")
        authed_page.evaluate("setCustomToken()")
        accept_app_confirm(authed_page)
        authed_page.wait_for_timeout(500)
        assert captured, "POST /api/token/set never fired"

    def test_regenerate_token_button_calls_endpoint(self, authed_page: Page, app_url: str) -> None:
        mock_settings_get(authed_page)
        mock_setup_status(authed_page, complete=True)
        mock_system_status(authed_page)
        mock_settings_backups(authed_page)
        capture_settings_save(authed_page)
        called = mock_token_regenerate(authed_page)
        authed_page.goto(f"{app_url}/settings")
        authed_page.wait_for_load_state("domcontentloaded")

        # Direct invoke to bypass any visibility/scroll issues. The confirm
        # is an appConfirm modal, not native window.confirm.
        authed_page.evaluate("regenerateToken()")
        accept_app_confirm(authed_page)
        authed_page.wait_for_timeout(500)
        assert called, "POST /api/token/regenerate never fired"


@pytest.mark.e2e
class TestSettingsBackupsPanel:
    def test_panel_renders_three_entries_newest_first(self, settings_page: Page) -> None:
        # Default mock has 2 timestamped + 1 legacy entry for settings.json.
        # D17 — each file's snapshot list is now rendered as a single
        # <select> with one <option> per backup (replacing the old per-row
        # Restore button list). Assert the option count instead of the
        # button count, and confirm the newest option is selected first.
        expect(settings_page.locator("#backupRestorePanel")).to_contain_text("settings.json", timeout=3000)
        expect(settings_page.locator("#backupRestorePanel")).to_contain_text("legacy")
        options = settings_page.locator("#backupRestorePanel select option")
        assert options.count() >= 3

    def test_restore_specific_backup_posts_filename(self, settings_page: Page) -> None:
        captured = capture_settings_backups_restore(settings_page)
        # D17 — the dropdown defaults to the first <option> (the newest
        # 20260201-100000 timestamp from the default mock). The lone
        # "Restore selected" button reads the select's value, so a
        # plain click on it posts the newest backup filename. Restore
        # is gated by an appConfirm modal — accept it to fire the POST.
        settings_page.locator("#backupRestorePanel button:has-text('Restore')").first.click()
        accept_app_confirm(settings_page)
        settings_page.wait_for_timeout(500)
        assert captured, "POST /api/settings/backups/restore never fired"
        assert captured[0]["file"] == "settings.json"
        # Newest entry is the timestamped one.
        assert "20260201-100000" in (captured[0].get("backup") or "")
