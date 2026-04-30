"""E2E tests for the theme toggle (light/dark + localStorage persistence)."""

from __future__ import annotations

import pytest
from playwright.sync_api import Page

from ._mocks import mock_dashboard_defaults


@pytest.fixture(scope="session", autouse=True)
def _complete_setup(complete_setup) -> None:
    return complete_setup


@pytest.mark.e2e
class TestThemeToggle:
    def test_toggle_flips_data_bs_theme(self, authed_page: Page, app_url: str) -> None:
        mock_dashboard_defaults(authed_page)
        authed_page.goto(f"{app_url}/")
        authed_page.wait_for_load_state("domcontentloaded")

        initial = authed_page.evaluate("document.documentElement.getAttribute('data-bs-theme')")
        # Theme toggle button — find by icon class or data attribute.
        toggle = authed_page.locator("#themeToggleBtn")
        toggle.click()
        authed_page.wait_for_timeout(200)
        after = authed_page.evaluate("document.documentElement.getAttribute('data-bs-theme')")
        assert initial != after, f"theme didn't flip: still {after!r}"

    def test_theme_persists_in_localstorage(self, authed_page: Page, app_url: str) -> None:
        mock_dashboard_defaults(authed_page)
        authed_page.goto(f"{app_url}/")
        authed_page.wait_for_load_state("domcontentloaded")

        toggle = authed_page.locator("#themeToggleBtn")
        toggle.click()
        authed_page.wait_for_timeout(200)
        stored = authed_page.evaluate("localStorage.getItem('theme')")
        assert stored in ("light", "dark"), f"expected 'light' or 'dark' in localStorage.theme, got {stored!r}"
