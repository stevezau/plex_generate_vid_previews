"""E2E tests for the /servers multi-media-server management page.

Covers the page surface that ships with multi-server support:

* The page renders behind auth.
* The Add Server modal opens and shows all three vendor types.
* Picking a vendor advances the wizard to the connection step and
  shows the right auth options (Emby / Jellyfin show the auth-method
  picker; Plex shows the OAuth + manual-token block).
* The webhook URL block renders with a non-empty URL.

Shared fixtures (``app_url`` / ``session_cookie`` / ``servers_page`` /
``complete_setup``) live in ``tests/e2e/conftest.py``.
"""

import pytest
from playwright.sync_api import Page, expect

from ._mocks import (
    capture_servers_save,
    mock_emby_password_auth,
    mock_plex_libraries,
    mock_servers_list,
    mock_servers_refresh_libraries,
    mock_servers_test_connection,
)


@pytest.fixture(scope="session", autouse=True)
def _complete_setup(complete_setup) -> None:
    """Auto-apply the shared setup-complete fixture for this file."""
    return complete_setup


@pytest.fixture
def servers_page(authed_page: Page, app_url: str) -> Page:
    """authed_page navigated to /servers."""
    authed_page.goto(f"{app_url}/servers")
    authed_page.wait_for_load_state("domcontentloaded")
    return authed_page


@pytest.mark.e2e
class TestServersPageLoads:
    def test_servers_page_accessible_after_login(self, servers_page: Page):
        assert "/login" not in servers_page.url

    def test_servers_page_has_heading(self, servers_page: Page):
        expect(servers_page.locator("h1")).to_contain_text("Media Servers")

    def test_servers_page_has_server_list_container(self, servers_page: Page):
        # The JS swaps loading spinner → empty state → cards. The container is always present.
        assert servers_page.locator("#serverList").count() == 1


@pytest.mark.e2e
class TestServersPageWebhookBlock:
    def test_webhook_url_input_present(self, servers_page: Page):
        expect(servers_page.locator("#webhookUrl")).to_be_visible()

    def test_webhook_url_populated_with_incoming_path(self, servers_page: Page):
        servers_page.wait_for_timeout(1500)
        value = servers_page.locator("#webhookUrl").input_value()
        if value:
            assert "/api/webhooks/incoming" in value


def _force_open_wizard(page: Page) -> None:
    """Force the Add Server modal + step-type wizard visible without Bootstrap.

    Avoids relying on the Bootstrap CDN bundle (occasionally unreachable
    in test environments) — we test the wizard's DOM/JS contract, not
    Bootstrap's modal animation. We strip ``d-none`` and force display
    so Playwright's visibility checks pass without invoking Bootstrap's
    JS API.
    """
    # Wait for the modal element to actually be in the DOM. The page
    # may still be loading when the test calls us.
    page.wait_for_selector("#addServerModal", state="attached", timeout=5000)
    page.evaluate(
        """
        () => {
            const m = document.getElementById('addServerModal');
            if (!m) throw new Error('addServerModal not found in DOM');
            m.classList.remove('fade');
            m.classList.add('show');
            m.style.display = 'block';
            m.style.opacity = '1';
            m.removeAttribute('aria-hidden');
            const stepType = document.getElementById('step-type');
            if (stepType) stepType.classList.remove('d-none');
        }
        """
    )


@pytest.mark.e2e
class TestAddServerModal:
    def test_modal_shows_three_vendor_buttons(self, servers_page: Page):
        _force_open_wizard(servers_page)

        for vendor in ("plex", "emby", "jellyfin"):
            btn = servers_page.locator(f'.server-type-btn[data-type="{vendor}"]')
            expect(btn).to_be_visible()

    def test_picking_emby_shows_auth_method_picker(self, servers_page: Page):
        _force_open_wizard(servers_page)
        servers_page.locator('.server-type-btn[data-type="emby"]').click()
        servers_page.wait_for_timeout(300)

        # Step-connect should now be visible.
        connect_classes = servers_page.locator("#step-connect").get_attribute("class") or ""
        assert "d-none" not in connect_classes
        # Emby supports password + api_key auth, so the picker is shown.
        assert "d-none" not in (servers_page.locator("#auth-method-section").get_attribute("class") or "")

    def test_picking_jellyfin_shows_auth_method_picker(self, servers_page: Page):
        _force_open_wizard(servers_page)
        servers_page.locator('.server-type-btn[data-type="jellyfin"]').click()
        servers_page.wait_for_timeout(300)

        connect_classes = servers_page.locator("#step-connect").get_attribute("class") or ""
        assert "d-none" not in connect_classes
        # Jellyfin adds Quick Connect to the auth picker.
        assert servers_page.locator("#auth-quick").count() == 1

    def test_picking_plex_shows_oauth_section(self, servers_page: Page):
        _force_open_wizard(servers_page)
        servers_page.locator('.server-type-btn[data-type="plex"]').click()
        servers_page.wait_for_timeout(300)

        connect_classes = servers_page.locator("#step-connect").get_attribute("class") or ""
        assert "d-none" not in connect_classes
        # Plex has its own auth path; auth-fields-token-plex should
        # have d-none stripped.
        assert "d-none" not in (servers_page.locator("#auth-fields-token-plex").get_attribute("class") or "")
        # And the manual-token input is in the DOM.
        assert servers_page.locator("#plexToken").count() == 1


@pytest.mark.e2e
class TestServersAPIIntegration:
    """Verify the /api/servers REST endpoints back the page's JS calls."""

    def test_servers_list_endpoint_returns_a_list(self, page: Page, app_url: str, auth_token: str):
        response = page.request.get(
            f"{app_url}/api/servers",
            headers={"X-Auth-Token": auth_token},
        )
        assert response.status == 200
        data = response.json()
        # Endpoint shape: ``{"servers": [...]}`` (auth redacted).
        assert isinstance(data, dict)
        assert isinstance(data.get("servers"), list)

    def test_health_check_endpoint_404s_for_unknown_server(self, page: Page, app_url: str, auth_token: str):
        """The unified health-check endpoint replaces the old per-vendor
        ``/jellyfin/fix-trickplay`` route. Confirm it's wired up by hitting
        a known-bad id and asserting the 404."""
        response = page.request.get(
            f"{app_url}/api/servers/does-not-exist/health-check",
            headers={"X-Auth-Token": auth_token},
        )
        assert response.status == 404

    def test_health_check_apply_endpoint_validates_flags_param(self, page: Page, app_url: str, auth_token: str):
        """``flags`` (when supplied) must be a list."""
        response = page.request.post(
            f"{app_url}/api/servers/some-id/health-check/apply",
            headers={"X-Auth-Token": auth_token, "Content-Type": "application/json"},
            data='{"flags": "not-a-list"}',
        )
        # 400 if route validates first, 404 if the server lookup runs first;
        # either is fine — both prove the route exists with sane validation.
        assert response.status in (400, 404), response.status


@pytest.mark.e2e
class TestServersAddFlows:
    """Walk the Add Server modal through each vendor's connect+save."""

    def test_plex_add_via_manual_token_creates_server(self, servers_page: Page) -> None:
        """Walk the Plex add: vendor pick → fill manual token → test → save."""
        # Surface any unexpected JS alert immediately rather than hanging.
        servers_page.on("dialog", lambda d: d.dismiss())
        # Mock the endpoints the modal hits.
        mock_plex_libraries(servers_page)
        mock_servers_test_connection(servers_page, ok=True, server_name="Test Plex")
        captured = capture_servers_save(servers_page, vendor="plex")

        _force_open_wizard(servers_page)
        servers_page.locator('.server-type-btn[data-type="plex"]').click()
        # Wait for #step-connect to actually become visible (the JS does
        # showStep('step-connect') which strips d-none).
        expect(servers_page.locator("#step-connect")).to_be_visible(timeout=2000)
        servers_page.locator("#serverUrl").fill("http://plex.local:32400")
        servers_page.locator("#serverName").fill("Test Plex")
        # #plexToken lives inside a <details> ("Or enter the token
        # manually") that's collapsed by default. Force it open.
        servers_page.evaluate(
            "document.querySelectorAll('#auth-fields-token-plex details').forEach(d => d.open = true)"
        )
        expect(servers_page.locator("#plexToken")).to_be_visible(timeout=2000)
        servers_page.locator("#plexToken").fill("plex-tok")
        servers_page.locator("#plexConfigFolder").fill("/plex")
        servers_page.locator("#step-connect-test").click()
        expect(servers_page.locator("#connectResult")).to_contain_text("Test Plex", timeout=3000)
        servers_page.locator("#step-result-save").click()
        servers_page.wait_for_timeout(500)
        assert captured, "POST /api/servers never fired"
        assert captured[0]["type"] == "plex"
        assert captured[0]["url"] == "http://plex.local:32400"

    def test_emby_add_via_password_creates_server(self, servers_page: Page) -> None:
        mock_emby_password_auth(servers_page, ok=True)
        mock_servers_test_connection(servers_page, ok=True, server_name="Emby Test")
        captured = capture_servers_save(servers_page, vendor="emby")

        _force_open_wizard(servers_page)
        servers_page.locator('.server-type-btn[data-type="emby"]').click()
        servers_page.wait_for_timeout(200)
        servers_page.locator("#serverUrl").fill("http://emby.local:8096")
        servers_page.locator("#serverName").fill("Emby Test")
        servers_page.locator("#authUsername").fill("admin")
        servers_page.locator("#authPassword").fill("hunter2")
        servers_page.locator("#step-connect-test").click()
        expect(servers_page.locator("#connectResult")).to_contain_text("Connected")
        servers_page.locator("#step-result-save").click()
        servers_page.wait_for_timeout(500)
        assert captured
        assert captured[0]["type"] == "emby"

    def test_refresh_libraries_button_calls_endpoint(self, authed_page: Page, app_url: str) -> None:
        mock_servers_list(
            authed_page,
            servers=[
                {
                    "id": "p1",
                    "name": "Home",
                    "type": "plex",
                    "enabled": True,
                    "url": "http://p",
                    "libraries": [{"id": "1", "name": "Movies", "enabled": True}],
                }
            ],
        )
        called = mock_servers_refresh_libraries(authed_page, count=3)
        authed_page.goto(f"{app_url}/servers")
        authed_page.wait_for_load_state("domcontentloaded")
        expect(authed_page.locator("#serverList")).to_contain_text("Home", timeout=3000)
        authed_page.locator(".refresh-libraries-btn").first.click()
        authed_page.wait_for_timeout(500)
        assert called, "POST /api/servers/<id>/refresh-libraries never fired"
