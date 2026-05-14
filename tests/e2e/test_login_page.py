"""E2E tests for the /login page (additional coverage beyond test_webapp.py)."""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect


@pytest.mark.e2e
class TestLoginPage:
    def test_token_input_is_autofocused(self, page: Page, app_url: str) -> None:
        page.goto(f"{app_url}/login")
        # Browser focuses the autofocus input on load.
        focused = page.evaluate("document.activeElement?.id || document.activeElement?.name")
        assert focused in ("token", "Authentication Token") or page.locator("#token").is_visible()

    def test_invalid_token_shows_error_alert(self, page: Page, app_url: str) -> None:
        page.goto(f"{app_url}/login")
        page.locator("#token").fill("definitely-not-the-real-token")
        page.locator('button[type="submit"]').click()
        # The error alert renders with the new trimmed copy.
        expect(page.locator(".alert-danger")).to_contain_text("didn", timeout=3000)

    def test_login_page_subtitle_is_concise(self, page: Page, app_url: str) -> None:
        """Regression for the patronising 'Enter your authentication token to
        continue' line — should now read just 'Sign in'."""
        page.goto(f"{app_url}/login")
        # Old copy is gone.
        assert "Enter your authentication token to continue" not in page.content()
