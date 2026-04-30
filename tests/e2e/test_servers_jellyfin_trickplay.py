"""E2E tests for the Jellyfin "Fix trickplay" button on a server card."""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from ._mocks import (
    mock_jellyfin_trickplay_fix,
    mock_jellyfin_trickplay_status,
    mock_servers_list,
)


@pytest.fixture(scope="session", autouse=True)
def _complete_setup(complete_setup) -> None:
    return complete_setup


def _stub_jellyfin_card(page: Page) -> None:
    mock_servers_list(
        page,
        servers=[
            {
                "id": "jf-1",
                "name": "Jellyfin Test",
                "type": "jellyfin",
                "enabled": True,
                "url": "http://jf.local:8096",
            }
        ],
    )


@pytest.mark.e2e
class TestJellyfinTrickplayFix:
    def test_fix_trickplay_button_calls_endpoint(self, authed_page: Page, app_url: str) -> None:
        """A Jellyfin server whose live trickplay-status probe finds at
        least one disabled library shows the Fix button. Clicking it
        POSTs the per-server endpoint and re-runs the probe."""
        _stub_jellyfin_card(authed_page)
        # Probe: at least one library has extraction disabled.
        mock_jellyfin_trickplay_status(authed_page, needs_fix=True)
        called = mock_jellyfin_trickplay_fix(authed_page)

        authed_page.goto(f"{app_url}/servers")
        authed_page.wait_for_load_state("domcontentloaded")
        expect(authed_page.locator("#serverList")).to_contain_text("Jellyfin Test", timeout=3000)

        # Locate by class — the button text changes to "Fixed" after the
        # successful POST, and a text-based locator would lose its target.
        fix_btn = authed_page.locator(".fix-trickplay-btn").first
        # The button is hidden by default and revealed by the probe.
        expect(fix_btn).to_be_visible(timeout=3000)

        # Capture console errors so we catch regressions like the
        # `Cannot set properties of null (setting 'innerHTML')` crash.
        errors: list[str] = []
        authed_page.on(
            "console",
            lambda msg: errors.append(msg.text) if msg.type == "error" else None,
        )
        authed_page.on("dialog", lambda d: d.accept())
        fix_btn.click()
        authed_page.wait_for_timeout(800)
        assert called, "POST /api/servers/<id>/jellyfin/fix-trickplay never fired"
        expect(fix_btn).to_contain_text("Fixed", timeout=2000)
        assert not errors, f"console errors during trickplay fix: {errors}"

    def test_fix_button_hidden_when_all_libraries_already_enabled(self, authed_page: Page, app_url: str) -> None:
        """Regression: the button used to be rendered visible for every
        Jellyfin card regardless of whether trickplay was actually
        disabled, so it kept reappearing on refresh even after a
        successful fix. The per-card probe now hides it whenever the
        live status reports every library is already enabled."""
        _stub_jellyfin_card(authed_page)
        mock_jellyfin_trickplay_status(authed_page, needs_fix=False)

        authed_page.goto(f"{app_url}/servers")
        authed_page.wait_for_load_state("domcontentloaded")
        expect(authed_page.locator("#serverList")).to_contain_text("Jellyfin Test", timeout=3000)

        fix_btn = authed_page.locator(".fix-trickplay-btn").first
        # Give the per-card probe time to resolve and hide the button.
        authed_page.wait_for_timeout(500)
        expect(fix_btn).to_be_hidden()
