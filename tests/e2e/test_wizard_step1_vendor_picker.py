"""E2E tests for wizard step 1: vendor picker + per-vendor panel reveal.

Regressions covered:

* Each vendor card reveals the right panel, hides the others.
* Emby/Jellyfin show the **inline** connection panel (no Bootstrap
  modal overlay) — regression for the popup-vs-inline fix.
* The "Pick a different server" link returns to the vendor picker.
* Skip Setup link POSTs /api/setup/skip and redirects.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from ._mocks import mock_setup_skip


@pytest.fixture
def wizard_step1(wizard_page: Page, app_url_wizard: str) -> Page:
    wizard_page.goto(f"{app_url_wizard}/setup")
    wizard_page.wait_for_load_state("domcontentloaded")
    return wizard_page


@pytest.mark.e2e
class TestVendorPicker:
    def test_all_three_vendor_cards_render(self, wizard_step1: Page) -> None:
        for vendor in ("plex", "emby", "jellyfin"):
            btn = wizard_step1.locator(f'.wizard-vendor-btn[data-vendor="{vendor}"]')
            expect(btn).to_be_visible()

    def test_picking_plex_reveals_plex_signin_panel_only(self, wizard_step1: Page) -> None:
        wizard_step1.locator('.wizard-vendor-btn[data-vendor="plex"]').click()
        expect(wizard_step1.locator("#plexSignInPanel")).to_be_visible()
        expect(wizard_step1.locator("#ejConnectPanel")).to_be_hidden()
        expect(wizard_step1.locator("#vendorPicker")).to_be_hidden()
        # Step 1 Next button reveals once Plex is picked + auth succeeds;
        # at this stage only the panel is visible.
        expect(wizard_step1.locator("#plexSignInBtn")).to_be_visible()

    def test_picking_emby_reveals_inline_form_no_modal_popup(self, wizard_step1: Page) -> None:
        """Regression for the popup-vs-inline fix — picking Emby must NOT
        open the Add Server modal as an overlay."""
        wizard_step1.locator('.wizard-vendor-btn[data-vendor="emby"]').click()
        # Inline panel revealed, vendor picker + plex panel hidden.
        expect(wizard_step1.locator("#ejConnectPanel")).to_be_visible()
        expect(wizard_step1.locator("#vendorPicker")).to_be_hidden()
        expect(wizard_step1.locator("#plexSignInPanel")).to_be_hidden()
        # The connection form is included from _server_connection_form.html;
        # its #serverUrl input must be in the DOM and visible.
        expect(wizard_step1.locator("#serverUrl")).to_be_visible()
        # Critical: the Add Server *modal* must NOT be on /setup at all
        # (it was removed to avoid the popup feel).
        assert wizard_step1.locator("#addServerModal").count() == 0

    def test_picking_jellyfin_reveals_inline_form_with_quickconnect(self, wizard_step1: Page) -> None:
        wizard_step1.locator('.wizard-vendor-btn[data-vendor="jellyfin"]').click()
        expect(wizard_step1.locator("#ejConnectPanel")).to_be_visible()
        # Quick Connect radio is Jellyfin-only and must render.
        expect(wizard_step1.locator("#auth-quick")).to_be_attached()
        # Vendor label in the connection form should say "Jellyfin".
        expect(wizard_step1.locator("#step-connect-vendor")).to_have_text("Jellyfin")

    def test_back_link_returns_to_vendor_picker_from_emby(self, wizard_step1: Page) -> None:
        wizard_step1.locator('.wizard-vendor-btn[data-vendor="emby"]').click()
        expect(wizard_step1.locator("#ejConnectPanel")).to_be_visible()
        wizard_step1.locator("#ejVendorBackBtn").click()
        expect(wizard_step1.locator("#vendorPicker")).to_be_visible()
        expect(wizard_step1.locator("#ejConnectPanel")).to_be_hidden()

    def test_back_link_returns_to_vendor_picker_from_plex(self, wizard_step1: Page) -> None:
        wizard_step1.locator('.wizard-vendor-btn[data-vendor="plex"]').click()
        expect(wizard_step1.locator("#plexSignInPanel")).to_be_visible()
        wizard_step1.locator("#vendorPickerBackBtn").click()
        expect(wizard_step1.locator("#vendorPicker")).to_be_visible()
        expect(wizard_step1.locator("#plexSignInPanel")).to_be_hidden()


@pytest.mark.e2e
class TestSkipSetup:
    def test_skip_setup_link_posts_skip_and_redirects(self, wizard_page: Page, app_url_wizard: str) -> None:
        called = mock_setup_skip(wizard_page)
        # Stub /servers with a static page so the JS-side window.location.href
        # navigation succeeds (the wizard subprocess's setup_complete is
        # false, so a real GET /servers would be redirected back to /setup
        # by the before_request middleware).
        wizard_page.route(
            "**/servers",
            lambda r: r.fulfill(content_type="text/html", body="<html><body>stub</body></html>"),
        )
        wizard_page.goto(f"{app_url_wizard}/setup")
        wizard_page.wait_for_load_state("domcontentloaded")

        # Auto-confirm the JS confirm() dialog.
        wizard_page.on("dialog", lambda d: d.accept())
        wizard_page.locator("#skipSetupBtn").click()
        wizard_page.wait_for_url("**/servers", timeout=5000)
        assert called, "POST /api/setup/skip was never called"
