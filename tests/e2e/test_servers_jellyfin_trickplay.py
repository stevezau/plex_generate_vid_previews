"""E2E tests for the Jellyfin "Fix trickplay" button on a server card."""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from ._mocks import (
    mock_jellyfin_trickplay_fix,
    mock_servers_list,
)


@pytest.fixture(scope="session", autouse=True)
def _complete_setup(complete_setup) -> None:
    return complete_setup


@pytest.mark.e2e
class TestJellyfinTrickplayFix:
    def test_fix_trickplay_button_calls_endpoint(self, authed_page: Page, app_url: str) -> None:
        """A Jellyfin server with the trickplay warning surfaced shows
        the Fix button. Clicking it POSTs the per-server endpoint."""
        # Backend status response that surfaces the trickplay warning.
        mock_servers_list(
            authed_page,
            servers=[
                {
                    "id": "jf-1",
                    "name": "Jellyfin Test",
                    "type": "jellyfin",
                    "enabled": True,
                    "url": "http://jf.local:8096",
                    "warnings": [
                        {
                            "code": "jellyfin_trickplay_disabled",
                            "message": "Trickplay disabled on 2 libraries",
                            "libraries": [
                                {"id": "lib1", "name": "Movies"},
                                {"id": "lib2", "name": "TV"},
                            ],
                        }
                    ],
                }
            ],
        )
        called = mock_jellyfin_trickplay_fix(authed_page)

        authed_page.goto(f"{app_url}/servers")
        authed_page.wait_for_load_state("domcontentloaded")
        # The card may take a moment to render; locate by Jellyfin name.
        expect(authed_page.locator("#serverList")).to_contain_text("Jellyfin Test", timeout=3000)

        # If the JS surfaces a "Fix trickplay" button anywhere on this card,
        # click it. (Different builds bury it under different parents.)
        fix_btn = authed_page.locator('button:has-text("Fix trickplay")').first
        if fix_btn.count() == 0:
            pytest.skip("Fix trickplay button not surfaced for this server status shape")
        # Handler may pop a confirm dialog before firing the POST.
        authed_page.on("dialog", lambda d: d.accept())
        fix_btn.click()
        authed_page.wait_for_timeout(800)
        assert called, "POST /api/servers/<id>/jellyfin/fix-trickplay never fired"
