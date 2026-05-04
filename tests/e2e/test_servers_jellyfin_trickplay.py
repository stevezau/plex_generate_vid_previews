"""E2E tests for the per-server settings health-check pill on a Servers card.

Replaces the previous Jellyfin-only "Fix trickplay" button tests once
the unified ``/health-check`` panel covered the same ground for every
vendor (Jellyfin/Plex/Emby) with one common UI surface. Filename kept
for git-history continuity.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from ._mocks import (
    mock_server_health_check,
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
class TestServerHealthPill:
    def test_health_pill_visible_when_critical_issue(self, authed_page: Page, app_url: str) -> None:
        """Card shows a red 'N issue' pill when the live health check
        finds a critical mis-set flag. Replaces the older Jellyfin-only
        "Fix trickplay" button visibility test."""
        _stub_jellyfin_card(authed_page)
        mock_server_health_check(authed_page)  # default: 1 critical issue

        authed_page.goto(f"{app_url}/servers")
        authed_page.wait_for_load_state("domcontentloaded")
        expect(authed_page.locator("#serverList")).to_contain_text("Jellyfin Test", timeout=3000)

        pill = authed_page.locator(".server-health-pill").first
        expect(pill).to_be_visible(timeout=3000)
        expect(pill).to_contain_text("issue")
        # Critical issues paint the pill red. The pill renders as a
        # Bootstrap button now, so the colour is conveyed by ``btn-danger``
        # (button styling) rather than ``bg-danger`` (background utility).
        pill_class = pill.get_attribute("class") or ""
        assert "btn-danger" in pill_class, f"expected critical pill colour, got class={pill_class!r}"

    def test_health_pill_hidden_when_all_good(self, authed_page: Page, app_url: str) -> None:
        """Regression of the older 'button always visible even after fix'
        bug. With zero issues the pill must stay hidden."""
        _stub_jellyfin_card(authed_page)
        mock_server_health_check(authed_page, issues=[])

        authed_page.goto(f"{app_url}/servers")
        authed_page.wait_for_load_state("domcontentloaded")
        expect(authed_page.locator("#serverList")).to_contain_text("Jellyfin Test", timeout=3000)

        pill = authed_page.locator(".server-health-pill").first
        # Give the per-card probe time to resolve and hide the pill.
        authed_page.wait_for_timeout(500)
        expect(pill).to_be_hidden()
