"""E2E tests for /automation (webhooks/triggers) page.

Regression coverage for the copy cleanup pass — the patronising
"you don't have to open this app every time" line must be gone, and
the new tighter copy must be present.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect


@pytest.fixture(scope="session", autouse=True)
def _complete_setup(complete_setup) -> None:
    return complete_setup


@pytest.mark.e2e
class TestAutomationCopy:
    def test_old_patronising_line_is_gone(self, authed_page: Page, app_url: str) -> None:
        authed_page.goto(f"{app_url}/automation")
        authed_page.wait_for_load_state("domcontentloaded")
        # Regression for the user-flagged copy.
        assert "you don't have to open this app every time" not in authed_page.content()

    def test_overview_intro_is_concise(self, authed_page: Page, app_url: str) -> None:
        authed_page.goto(f"{app_url}/automation")
        authed_page.wait_for_load_state("domcontentloaded")
        # New trimmed copy.
        expect(authed_page.locator("#section-webhooks-overview")).to_contain_text(
            "Webhooks generate previews automatically"
        )


@pytest.mark.e2e
class TestAutomationSections:
    def test_decision_list_renders(self, authed_page: Page, app_url: str) -> None:
        authed_page.goto(f"{app_url}/automation")
        authed_page.wait_for_load_state("domcontentloaded")
        # The "How do you add media?" decision list shows multiple rows.
        rows = authed_page.locator(".decision-row")
        assert rows.count() >= 4

    def test_arr_section_link_present(self, authed_page: Page, app_url: str) -> None:
        authed_page.goto(f"{app_url}/automation")
        authed_page.wait_for_load_state("domcontentloaded")
        # Link to the Sonarr/Radarr section anchor.
        expect(authed_page.locator('a[href="#section-webhooks-sonarr-radarr"]').first).to_be_visible()
