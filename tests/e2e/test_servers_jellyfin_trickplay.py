"""E2E tests for the inline Setup Health readiness glyph on a Servers card.

Replaces the previous hover-pill test (`.server-health-pill`) that was
driven by `/health-check`. The glyph architecture (Mar 2026):

  * Per-card inline ✓/⚠/❗ glyph next to the server name, sourced from
    GET /api/servers/<id>/previews-readiness (NOT /health-check).
  * Class `.server-readiness-glyph`, populated by `probeServerReadiness`
    after the per-card connection probe resolves.
  * Glyph is hidden when probe hasn't run yet OR connection failed OR
    server is disabled. Visible with `text-danger` / `text-warning` /
    `text-success` when critical / recommended / ok respectively.

Filename kept for git-history continuity even though the tests no
longer mention trickplay.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from ._mocks import (
    mock_server_connection_probe,
    mock_server_previews_readiness,
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
class TestServerReadinessGlyph:
    def test_glyph_is_critical_red_when_readiness_has_critical_issue(self, authed_page: Page, app_url: str) -> None:
        """Card shows a red ❗ glyph next to the server name when the
        readiness probe reports a critical issue. Clicking it opens the
        Edit modal on the Setup Health tab — but that flow is covered
        by other tests; this one just pins the visual state."""
        _stub_jellyfin_card(authed_page)
        mock_server_connection_probe(authed_page, ok=True)
        mock_server_previews_readiness(authed_page, critical_count=1)

        authed_page.goto(f"{app_url}/servers")
        authed_page.wait_for_load_state("domcontentloaded")
        expect(authed_page.locator("#serverList")).to_contain_text("Jellyfin Test", timeout=3000)

        # Sequential probes run after the card renders. Wait for the
        # glyph to paint itself; class flips from `d-none` to one of
        # text-success / text-warning / text-danger once probeServerReadiness
        # resolves.
        glyph = authed_page.locator(".server-readiness-glyph").first
        expect(glyph).to_be_visible(timeout=3000)
        # Critical readiness → red.
        glyph_class = glyph.get_attribute("class") or ""
        assert "text-danger" in glyph_class, f"expected critical glyph colour (text-danger), got class={glyph_class!r}"
        # Tooltip affordance cites "click to fix".
        title = glyph.get_attribute("title") or ""
        assert "click" in title.lower(), f"expected click affordance in tooltip, got {title!r}"

    def test_glyph_is_amber_warning_for_recommended_only(self, authed_page: Page, app_url: str) -> None:
        """Regression guard for the 4-state rollup — non-critical issues
        paint amber ⚠, not red or green. Matches _deriveBadgeState."""
        _stub_jellyfin_card(authed_page)
        mock_server_connection_probe(authed_page, ok=True)
        mock_server_previews_readiness(authed_page, critical_count=0, recommended_count=2)

        authed_page.goto(f"{app_url}/servers")
        authed_page.wait_for_load_state("domcontentloaded")
        expect(authed_page.locator("#serverList")).to_contain_text("Jellyfin Test", timeout=3000)

        glyph = authed_page.locator(".server-readiness-glyph").first
        expect(glyph).to_be_visible(timeout=3000)
        glyph_class = glyph.get_attribute("class") or ""
        assert "text-warning" in glyph_class, (
            f"expected recommended glyph colour (text-warning), got class={glyph_class!r}"
        )

    def test_glyph_is_green_when_readiness_all_ok(self, authed_page: Page, app_url: str) -> None:
        """All green ✓ when everything passes. Regression of the older
        'button always visible even after fix' shape — the NEW glyph
        stays visible but flips colour / tooltip, so this test just
        asserts the happy-path visual contract rather than visibility."""
        _stub_jellyfin_card(authed_page)
        mock_server_connection_probe(authed_page, ok=True)
        mock_server_previews_readiness(authed_page, critical_count=0, recommended_count=0)

        authed_page.goto(f"{app_url}/servers")
        authed_page.wait_for_load_state("domcontentloaded")
        expect(authed_page.locator("#serverList")).to_contain_text("Jellyfin Test", timeout=3000)

        glyph = authed_page.locator(".server-readiness-glyph").first
        expect(glyph).to_be_visible(timeout=3000)
        glyph_class = glyph.get_attribute("class") or ""
        assert "text-success" in glyph_class, f"expected healthy glyph colour (text-success), got class={glyph_class!r}"
        title = glyph.get_attribute("title") or ""
        assert "healthy" in title.lower() or "setup healthy" in title.lower(), (
            f"expected healthy tooltip, got {title!r}"
        )
