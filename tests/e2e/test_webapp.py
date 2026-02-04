"""
E2E tests for the web application.

Tests core functionality:
- Login page
- Authentication flow
- Page navigation
"""

import pytest
from playwright.sync_api import Page, expect


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
    
    def test_login_page_has_title(self, page: Page, app_url: str):
        """Verify login page has a title."""
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
        
        # Submit the form
        submit_btn = page.locator('button[type="submit"]')
        submit_btn.click()
        
        # Should redirect away from login (may go to setup or dashboard)
        page.wait_for_timeout(2000)
        current_url = page.url
        assert "/login" not in current_url, f"Should redirect away from login, got: {current_url}"
    
    def test_authenticated_user_can_access_protected_pages(self, page: Page, app_url: str, auth_token: str):
        """Verify authenticated user can access the app."""
        # Login first
        page.goto(f"{app_url}/login")
        token_input = page.locator('input[name="token"], input[type="password"]')
        token_input.fill(auth_token)
        page.locator('button[type="submit"]').click()
        page.wait_for_timeout(2000)
        
        # After login, navigate to settings
        page.goto(f"{app_url}/settings")
        page.wait_for_timeout(1000)
        
        # Should be on settings page (not redirected to login)
        current_url = page.url
        assert "/login" not in current_url, f"Should access settings, got: {current_url}"


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
        page.wait_for_timeout(2000)
        
        # Navigate to setup
        page.goto(f"{app_url}/setup")
        page.wait_for_timeout(1000)
        
        # Should load setup page (not error)
        current_url = page.url
        assert "/login" not in current_url or "/setup" in current_url, f"Should access setup, got: {current_url}"


@pytest.mark.e2e
class TestAPIEndpoints:
    """Test API endpoint accessibility."""
    
    def test_health_check_endpoint(self, page: Page, app_url: str):
        """Verify health check endpoint is accessible without auth."""
        response = page.request.get(f"{app_url}/api/health")
        
        # Health check should return 200
        assert response.status == 200
        
        # Should return JSON with status
        data = response.json()
        assert "status" in data or "ok" in str(data).lower()
    
    def test_auth_status_endpoint(self, page: Page, app_url: str):
        """Verify auth status endpoint is accessible."""
        response = page.request.get(f"{app_url}/api/auth/status")
        
        # Should return 200 (even if not authenticated)
        assert response.status == 200
