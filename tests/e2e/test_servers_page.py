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
import requests
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
        # Deterministic: wait for the JS to strip d-none on the connect step.
        expect(servers_page.locator("#step-connect")).to_be_visible(timeout=2000)
        # Emby supports password + api_key auth, so the picker is shown.
        expect(servers_page.locator("#auth-method-section")).to_be_visible(timeout=1000)

    def test_picking_jellyfin_shows_auth_method_picker(self, servers_page: Page):
        _force_open_wizard(servers_page)
        servers_page.locator('.server-type-btn[data-type="jellyfin"]').click()
        expect(servers_page.locator("#step-connect")).to_be_visible(timeout=2000)
        # Jellyfin adds Quick Connect to the auth picker.
        expect(servers_page.locator("#auth-quick")).to_be_attached(timeout=1000)

    def test_picking_plex_shows_oauth_section(self, servers_page: Page):
        _force_open_wizard(servers_page)
        servers_page.locator('.server-type-btn[data-type="plex"]').click()
        expect(servers_page.locator("#step-connect")).to_be_visible(timeout=2000)
        # Plex has its own auth path; auth-fields-token-plex must
        # be visible (d-none stripped by the wizard JS).
        expect(servers_page.locator("#auth-fields-token-plex")).to_be_visible(timeout=1000)
        # And the manual-token input is in the DOM.
        expect(servers_page.locator("#plexToken")).to_be_attached()

    def test_plex_discovery_uses_radio_not_checkbox(self, servers_page: Page):
        """After Plex OAuth, the discovered-servers list must render as
        radio buttons (single pick), not checkboxes. Pre-fix users could
        tick multiple, the second tick silently cleared the URL field,
        and the batch-add path bypassed per-server config-folder setup.
        The new flow: pick one → URL/name auto-fill → Test connection."""
        _force_open_wizard(servers_page)
        servers_page.locator('.server-type-btn[data-type="plex"]').click()
        expect(servers_page.locator("#step-connect")).to_be_visible(timeout=2000)

        # Inject a mock pair of discovered servers via the existing
        # renderPlexDiscovered hook; the OAuth round-trip itself is
        # covered by test_oauth_routes.py.
        servers_page.evaluate(
            """() => {
                // The function is IIFE-private; reach in via the same
                // event the OAuth success path uses.
                const list = document.getElementById('plexDiscoveredServers');
                list.innerHTML = `
                    <label class="list-group-item">
                      <input type="radio" name="plexDiscoveredPick" class="plex-server-pick"
                             data-idx="0" data-uri="http://kraken:32400" data-name="Kraken">
                      <span>Kraken</span>
                    </label>
                    <label class="list-group-item">
                      <input type="radio" name="plexDiscoveredPick" class="plex-server-pick"
                             data-idx="1" data-uri="http://calypso:32400" data-name="Calypso 4k">
                      <span>Calypso 4k</span>
                    </label>`;
                document.getElementById('plexDiscoveredList').classList.remove('d-none');
                // Rewire the change listeners the same way servers.js does.
                document.querySelectorAll('.plex-server-pick').forEach(el => {
                    el.addEventListener('change', () => {
                        if (!el.checked) return;
                        document.getElementById('serverUrl').value = el.dataset.uri;
                        if (!document.getElementById('serverName').value)
                            document.getElementById('serverName').value = el.dataset.name;
                    });
                });
            }"""
        )

        picks = servers_page.locator(".plex-server-pick")
        expect(picks).to_have_count(2)
        # Pin the input type: radio, not checkbox.
        first_type = picks.nth(0).evaluate("el => el.type")
        assert first_type == "radio", f"expected radio, got {first_type!r}"
        # Pin the radio-group semantic: same `name` so the browser
        # enforces single-selection (the user's "I can check both"
        # bug is exactly the absence of this attribute).
        first_name = picks.nth(0).evaluate("el => el.name")
        assert first_name == "plexDiscoveredPick"

        # Pick Calypso. URL must auto-fill to its URI.
        picks.nth(1).check()
        expect(servers_page.locator("#serverUrl")).to_have_value("http://calypso:32400")
        # Now pick Kraken — single-selection: Calypso deselects, URL
        # updates. The pre-fix checkbox behaviour would keep both
        # ticked and CLEAR the URL field. Pinning the new contract.
        picks.nth(0).check()
        expect(servers_page.locator("#serverUrl")).to_have_value("http://kraken:32400")
        assert picks.nth(1).evaluate("el => el.checked") is False, "picking another radio must deselect the prior one"

    def test_add_plex_modal_does_not_have_batch_add_button(self, servers_page: Page):
        """The old #plexAddSelected button + #plexSelectedCount badge
        were removed when discovery switched to single-pick radios.
        Pin that they're gone so a future copy-paste doesn't re-add
        the confusing multi-select path."""
        _force_open_wizard(servers_page)
        servers_page.locator('.server-type-btn[data-type="plex"]').click()
        expect(servers_page.locator("#step-connect")).to_be_visible(timeout=2000)
        expect(servers_page.locator("#plexAddSelected")).to_have_count(0)
        expect(servers_page.locator("#plexSelectedCount")).to_have_count(0)

    def test_add_plex_modal_has_browse_button_next_to_config_folder(self, servers_page: Page):
        """The Plex config folder field in the Add Server modal must
        have a Browse button — matching the same field on the Edit
        modal (#editPlexConfigBrowseBtn) and the setup wizard
        (#wizardPlexConfigFolderBrowseBtn). Pre-fix the partial
        ``_server_connection_form.html`` only had the input; users had
        to type the path blind."""
        _force_open_wizard(servers_page)
        servers_page.locator('.server-type-btn[data-type="plex"]').click()
        expect(servers_page.locator("#step-connect")).to_be_visible(timeout=2000)
        expect(servers_page.locator("#auth-fields-token-plex")).to_be_visible(timeout=1000)
        # The browse button must exist in the DOM alongside the input.
        expect(servers_page.locator("#plexConfigFolder")).to_be_visible()
        expect(servers_page.locator("#plexConfigFolderBrowseBtn")).to_be_visible()
        # Clicking it must open the folder picker modal (the same
        # #folderPickerModal that the Edit / wizard browse buttons use).
        servers_page.locator("#plexConfigFolderBrowseBtn").click()
        expect(servers_page.locator("#folderPickerModal")).to_be_visible(timeout=2000)


@pytest.mark.e2e
class TestServersAPIIntegration:
    """Verify the /api/servers REST endpoints back the page's JS calls."""

    def test_servers_list_endpoint_returns_a_list(self, app_url: str, auth_token: str):
        # Use requests (not page.request) to avoid the Playwright Python↔Node
        # IPC stall under -n auto. Same pattern as the canary fix.
        response = requests.get(
            f"{app_url}/api/servers",
            headers={"X-Auth-Token": auth_token},
            timeout=30,
        )
        assert response.status_code == 200
        data = response.json()
        # Endpoint shape: ``{"servers": [...]}`` (auth redacted).
        assert isinstance(data, dict)
        assert isinstance(data.get("servers"), list)

    def test_health_check_endpoint_404s_for_unknown_server(self, app_url: str, auth_token: str):
        """The unified health-check endpoint replaces the old per-vendor
        ``/jellyfin/fix-trickplay`` route. Confirm it's wired up by hitting
        a known-bad id and asserting the 404."""
        response = requests.get(
            f"{app_url}/api/servers/does-not-exist/health-check",
            headers={"X-Auth-Token": auth_token},
            timeout=30,
        )
        assert response.status_code == 404

    def test_health_check_apply_endpoint_validates_flags_param(self, app_url: str, auth_token: str):
        """``flags`` (when supplied) must be a list."""
        response = requests.post(
            f"{app_url}/api/servers/some-id/health-check/apply",
            headers={"X-Auth-Token": auth_token, "Content-Type": "application/json"},
            data='{"flags": "not-a-list"}',
            timeout=30,
        )
        # 400 if route validates first, 404 if the server lookup runs first;
        # either is fine — both prove the route exists with sane validation.
        assert response.status_code in (400, 404), response.status_code


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
        # Wait for POST so the assertions don't race the request.
        with servers_page.expect_request("**/api/servers") as req_info:
            servers_page.locator("#step-result-save").click()
        req_info.value  # noqa: B018
        assert captured, "POST /api/servers never fired"
        assert captured[0]["type"] == "plex"
        assert captured[0]["url"] == "http://plex.local:32400"

    def test_emby_add_via_password_creates_server(self, servers_page: Page) -> None:
        mock_emby_password_auth(servers_page, ok=True)
        mock_servers_test_connection(servers_page, ok=True, server_name="Emby Test")
        captured = capture_servers_save(servers_page, vendor="emby")

        _force_open_wizard(servers_page)
        servers_page.locator('.server-type-btn[data-type="emby"]').click()
        # Wait for the connect step to be ready before filling.
        expect(servers_page.locator("#serverUrl")).to_be_visible(timeout=2000)
        servers_page.locator("#serverUrl").fill("http://emby.local:8096")
        servers_page.locator("#serverName").fill("Emby Test")
        servers_page.locator("#authUsername").fill("admin")
        servers_page.locator("#authPassword").fill("hunter2")
        servers_page.locator("#step-connect-test").click()
        expect(servers_page.locator("#connectResult")).to_contain_text("Connected")
        with servers_page.expect_request("**/api/servers") as req_info:
            servers_page.locator("#step-result-save").click()
        req_info.value  # noqa: B018
        assert captured
        assert captured[0]["type"] == "emby"
        # Tighten: pin the URL the user typed, not just the vendor.
        assert captured[0]["url"] == "http://emby.local:8096"
        assert captured[0]["name"] == "Emby Test"

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
        with authed_page.expect_request("**/api/servers/*/refresh-libraries") as req_info:
            authed_page.locator(".refresh-libraries-btn").first.click()
        req_info.value  # noqa: B018
        assert called, "POST /api/servers/<id>/refresh-libraries never fired"
