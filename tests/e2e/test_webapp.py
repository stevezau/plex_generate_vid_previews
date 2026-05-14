"""
E2E tests for the web application.

Tests core functionality:
- Login page
- Authentication flow
- Page navigation
"""

import pytest
import requests
from playwright.sync_api import Page, expect

# Use the requests library for API-contract checks rather than
# Playwright's page.request — the Python↔Node IPC stalls under
# pytest-xdist `-n auto` (playwright#26739). Same pattern as the
# canary fix in test_journey_schedule_lifecycle.py.
_API_TIMEOUT = 30


@pytest.mark.e2e
class TestLoginPage:
    """Test the login page functionality."""

    def test_login_page_loads(self, page: Page, app_url: str):
        """Verify login page loads and displays login form."""
        page.goto(f"{app_url}/login")

        # Should have a token input field
        token_input = page.locator('input[name="token"], input[type="password"]')
        expect(token_input).to_be_visible()

        # Should have a submit button
        submit_btn = page.locator('button[type="submit"]')
        expect(submit_btn).to_be_visible()

    def test_login_page_has_heading(self, page: Page, app_url: str):
        """Verify login page has a heading element."""
        page.goto(f"{app_url}/login")

        # Page should have some heading
        heading = page.locator("h1, h2, h3").first
        expect(heading).to_be_visible()


@pytest.mark.e2e
class TestAuthentication:
    """Test authentication flow."""

    def test_valid_token_redirects_away_from_login(self, page: Page, app_url: str, auth_token: str):
        """Verify valid token grants access and redirects."""
        page.goto(f"{app_url}/login")

        # Fill in the token
        token_input = page.locator('input[name="token"], input[type="password"]')
        token_input.fill(auth_token)

        # Submit the form, then deterministically wait for the
        # redirect to land via wait_for_url. The redirect may go to
        # /, /setup, or /servers depending on setup-complete state —
        # use a regex that matches any non-/login destination.
        page.locator('button[type="submit"]').click()
        page.wait_for_url(lambda url: "/login" not in url, timeout=10000)
        assert "/login" not in page.url, f"Should redirect away from login, got: {page.url}"

    def test_authenticated_user_can_access_protected_pages(self, page: Page, app_url: str, auth_token: str):
        """Verify authenticated user can access the app."""
        # Login first
        page.goto(f"{app_url}/login")
        token_input = page.locator('input[name="token"], input[type="password"]')
        token_input.fill(auth_token)
        page.locator('button[type="submit"]').click()
        # Wait for the login redirect to land before navigating away.
        page.wait_for_url(lambda url: "/login" not in url, timeout=10000)

        # After login, navigate to settings.
        page.goto(f"{app_url}/settings")
        # Pin the actual destination — accept either /settings (setup
        # complete) or /setup (the redirect when setup isn't done).
        # A regression returning 500 or 404 would fall through neither
        # branch and fail visibly.
        page.wait_for_load_state("domcontentloaded")
        assert page.url.endswith("/settings") or page.url.endswith("/setup"), (
            f"Expected /settings or /setup after login; got {page.url!r}"
        )


@pytest.mark.e2e
class TestSetupWizard:
    """Test setup wizard accessibility."""

    def test_setup_page_accessible_after_login(self, page: Page, app_url: str, auth_token: str):
        """Verify setup page is accessible after authentication."""
        # Login first
        page.goto(f"{app_url}/login")
        token_input = page.locator('input[name="token"], input[type="password"]')
        token_input.fill(auth_token)
        page.locator('button[type="submit"]').click()
        page.wait_for_url(lambda url: "/login" not in url, timeout=10000)

        # Navigate to setup.
        page.goto(f"{app_url}/setup")
        page.wait_for_load_state("domcontentloaded")

        # Setup page either renders the wizard (when setup isn't
        # complete) or 302s elsewhere. Pin: the destination URL must
        # NOT be /login. A regression that 500'd or auth-bounced
        # would land back on /login and fail here.
        assert "/login" not in page.url, f"Should access setup, got: {page.url}"


@pytest.mark.e2e
class TestAPIEndpoints:
    """Test API endpoint accessibility."""

    def test_health_check_endpoint(self, app_url: str):
        """Verify health check endpoint is accessible without auth."""
        # Use requests (not page.request) to avoid the Playwright
        # Python↔Node IPC stall under -n auto.
        response = requests.get(f"{app_url}/api/health", timeout=_API_TIMEOUT)

        # Health check should return 200.
        assert response.status_code == 200

        # Pin the actual response shape: {"status": "healthy"} (the
        # documented contract). Earlier accepted either "status" in
        # data OR "ok" in str(data) — too permissive; a regression
        # that swapped the field name would have silently passed.
        data = response.json()
        assert data.get("status") == "healthy", f"Health check body must be {{'status': 'healthy'}}; got {data!r}"

    def test_auth_status_endpoint(self, app_url: str):
        """Verify auth status endpoint is accessible."""
        response = requests.get(f"{app_url}/api/auth/status", timeout=_API_TIMEOUT)

        # Endpoint reachable without auth (200 even when unauthenticated).
        assert response.status_code == 200


@pytest.mark.e2e
class TestSetupWizardStep5:
    """Test setup wizard Step 5 (Security) functionality."""

    def test_setup_wizard_has_5_steps(self, page: Page, app_url: str, auth_token: str):
        """Verify setup wizard now has 5 progress steps."""
        # Setup page is accessible without login and avoids auth/session state
        # leakage from prior E2E tests.
        page.goto(f"{app_url}/setup")
        # Deterministic: wait for the first progress step to render
        # instead of a hardcoded sleep.
        expect(page.locator(".progress-step").first).to_be_visible(timeout=5000)

        # Should have 5 progress steps
        progress_steps = page.locator(".progress-step")
        assert progress_steps.count() == 5

    def test_step5_has_security_label(self, page: Page, app_url: str, auth_token: str):
        """Verify Step 5 is labeled 'Security'."""
        # Setup page is accessible without login and avoids auth/session state
        # leakage from prior E2E tests.
        page.goto(f"{app_url}/setup")
        # Step 5 should have 'Security' text — to_contain_text auto-retries.
        step5 = page.locator('.progress-step[data-step="5"]')
        expect(step5).to_contain_text("Security", timeout=5000)

    def test_step5_has_new_token_inputs(self, page: Page, app_url: str, auth_token: str):
        """Verify Step 5 offers the set-new-token form (new + confirm)."""
        # Setup page is accessible without login and avoids auth/session state
        # leakage from prior E2E tests.
        page.goto(f"{app_url}/setup")
        expect(page.locator(".progress-step").first).to_be_visible(timeout=5000)

        # Step 5 mirrors /settings → Web Authentication: no current-token
        # display, just two password fields the user fills (or skips blank).
        assert page.locator("#newToken").count() == 1
        assert page.locator("#confirmToken").count() == 1
        # The old "Current Access Token" display + "use custom" checkbox are
        # gone — the form is the primary action now.
        assert page.locator("#currentToken").count() == 0
        assert page.locator("#useCustomToken").count() == 0

    def test_step5_has_finish_button(self, page: Page, app_url: str, auth_token: str):
        """Verify Step 5 has the Complete Setup button."""
        # Setup page is accessible without login and avoids auth/session state
        # leakage from prior E2E tests.
        page.goto(f"{app_url}/setup")
        expect(page.locator(".progress-step").first).to_be_visible(timeout=5000)

        # Finish button should exist
        finish_btn = page.locator("#finishSetup")
        assert finish_btn.count() == 1
