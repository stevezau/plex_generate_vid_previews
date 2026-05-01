"""E2E tests for the −/+ stepper widget across every numeric input on /settings.

Covers the inputs not exercised by test_settings_page.py:
* logRetentionCount
* jobHistoryDays

Plus general "stepper button is rendered" smoke for all five.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from ._mocks import (
    capture_settings_save,
    mock_settings_backups,
    mock_settings_get,
    mock_setup_status,
    mock_system_status,
)


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
class TestEveryStepper:
    @pytest.mark.parametrize(
        "input_id",
        [
            "cpuThreads",
            "thumbnailInterval",
            "logRotationSize",
            "logRetentionCount",
            "jobHistoryDays",
        ],
    )
    def test_stepper_buttons_render(self, settings_page: Page, input_id: str) -> None:
        """Every stepper-flagged input gets −/+ siblings."""
        inp = settings_page.locator(f"#{input_id}")
        expect(inp).to_be_attached()
        # Both buttons are siblings within the wrapping input-group.
        expect(inp.locator(".. >> .stepper-minus").first).to_be_attached()
        expect(inp.locator(".. >> .stepper-plus").first).to_be_attached()

    def test_log_retention_increments(self, settings_page: Page) -> None:
        ret = settings_page.locator("#logRetentionCount")
        expect(ret).to_have_value("5")
        ret.locator(".. >> .stepper-plus").first.click()
        expect(ret).to_have_value("6")

    def test_job_history_days_increments(self, settings_page: Page) -> None:
        days = settings_page.locator("#jobHistoryDays")
        expect(days).to_have_value("30")
        days.locator(".. >> .stepper-plus").first.click()
        expect(days).to_have_value("31")
