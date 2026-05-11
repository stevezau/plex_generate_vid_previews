"""E2E tests for the Preview Inspector page (/bif-viewer).

Regression coverage for the Plex registry bug — when /api/servers
returned a Plex entry, the inspector used to silently skip it because
the registry's `legacy_config is None` branch raised. Now the Plex
server appears in the picker dropdown like any other vendor.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, Route, expect

from ._mocks import _fulfill_json, mock_servers_list


def _mock_inspector_defaults(page: Page) -> None:
    """Stub the BIF endpoints so the page renders without errors."""

    def stub_search(route: Route) -> None:
        _fulfill_json(route, {"results": []})

    def stub_info(route: Route) -> None:
        _fulfill_json(route, {"frames": [], "interval_ms": 2000})

    page.route("**/api/bif/servers/*/search**", stub_search)
    page.route("**/api/bif/info**", stub_info)
    page.route("**/api/bif/trickplay/info**", stub_info)


@pytest.fixture(scope="session", autouse=True)
def _complete_setup(complete_setup) -> None:
    return complete_setup


@pytest.mark.e2e
class TestPreviewInspectorServerPicker:
    def test_plex_server_appears_in_picker(self, authed_page: Page, app_url: str) -> None:
        """Regression: Plex server entry must appear in the picker dropdown.

        Previously the registry skipped Plex with "could not initialise"
        and the picker was empty for Plex-only setups.
        """
        mock_servers_list(
            authed_page,
            servers=[
                {
                    "id": "plex-1",
                    "name": "Home Plex",
                    "type": "plex",
                    "enabled": True,
                    "url": "http://plex.local:32400",
                }
            ],
        )
        _mock_inspector_defaults(authed_page)
        authed_page.goto(f"{app_url}/bif-viewer")
        authed_page.wait_for_load_state("domcontentloaded")

        select = authed_page.locator("#serverSelect")
        # JS populates the dropdown with the configured server's name.
        expect(select).to_contain_text("Home Plex", timeout=3000)

    def test_multi_vendor_servers_all_appear(self, authed_page: Page, app_url: str) -> None:
        mock_servers_list(
            authed_page,
            servers=[
                {"id": "plex-1", "name": "Plex Test", "type": "plex", "enabled": True, "url": "http://p"},
                {"id": "emby-1", "name": "Emby Test", "type": "emby", "enabled": True, "url": "http://e"},
                {"id": "jf-1", "name": "Jellyfin Test", "type": "jellyfin", "enabled": True, "url": "http://j"},
            ],
        )
        _mock_inspector_defaults(authed_page)
        authed_page.goto(f"{app_url}/bif-viewer")
        authed_page.wait_for_load_state("domcontentloaded")

        select = authed_page.locator("#serverSelect")
        expect(select).to_contain_text("Plex Test", timeout=3000)
        expect(select).to_contain_text("Emby Test")
        expect(select).to_contain_text("Jellyfin Test")


@pytest.mark.e2e
class TestPreviewInspectorPlexClickThrough:
    """2026-05-12 regression: clicking a Plex search result returned
    'Invalid or missing BIF file path' because the frontend sent the
    .mkv path to /api/bif/info (which only accepts .bif paths). Now
    the backend resolves the BIF path eagerly so preview_path is
    always a real BIF and the row is loadable.
    """

    def test_plex_result_click_loads_viewer_without_bif_error(self, authed_page: Page, app_url: str) -> None:
        mock_servers_list(
            authed_page,
            servers=[
                {
                    "id": "plex-1",
                    "name": "Plex Test",
                    "type": "plex",
                    "enabled": True,
                    "url": "http://p:32400",
                }
            ],
        )

        # Search returns one result with a fully resolved BIF path
        # (the post-fix shape — preview_path ends in index-sd.bif and
        # preview_exists is True).
        def stub_search(route: Route) -> None:
            _fulfill_json(
                route,
                {
                    "server_id": "plex-1",
                    "server_type": "plex",
                    "results": [
                        {
                            "title": "Breaking Bad S01E01",
                            "type": "episode",
                            "media_file": "/data/TV/BB/S01E01.mkv",
                            "preview_kind": "bif",
                            "preview_path": "/plex/Media/localhost/a/bcd.bundle/Contents/Indexes/index-sd.bif",
                            "preview_exists": True,
                        }
                    ],
                },
            )

        # /api/bif/info must be called with the BIF path, NOT the .mkv —
        # the regression we're guarding against.
        info_calls: list[str] = []

        def stub_info(route: Route) -> None:
            url = route.request.url
            info_calls.append(url)
            _fulfill_json(
                route,
                {
                    "frame_count": 5,
                    "frame_interval_ms": 2000,
                    "file_size": 100,
                    "avg_frame_size": 20,
                    "suspect_frame_count": 0,
                    "created_at": None,
                },
            )

        authed_page.route("**/api/bif/servers/*/search**", stub_search)
        authed_page.route("**/api/bif/info**", stub_info)
        authed_page.route("**/api/bif/frame**", lambda r: r.fulfill(status=200, body=b""))

        authed_page.goto(f"{app_url}/bif-viewer")
        authed_page.wait_for_load_state("domcontentloaded")
        authed_page.wait_for_function(
            "() => document.querySelector('#serverSelect option[value=\"plex-1\"]')",
            timeout=3000,
        )

        authed_page.locator("#searchInput").fill("Breaking Bad S01E01")
        authed_page.locator("#searchBtn").click()
        result = authed_page.locator(".result-item").first
        expect(result).to_be_visible(timeout=3000)
        # The success badge proves the post-fix shape is in play.
        expect(result.locator(".badge.bg-success")).to_be_visible()

        result.click()

        # The viewer must open AND the toast that previously appeared
        # ("Invalid or missing BIF file path") must NOT.
        expect(authed_page.locator("#viewerPanel")).not_to_have_class("d-none", timeout=3000)
        # Boundary-call assertion: /api/bif/info was called with the BIF
        # path's URL-encoded form, NOT the .mkv path. (.bif keyword
        # appears in encoded form as "%2Findex-sd.bif".)
        assert info_calls, "Click should trigger /api/bif/info"
        assert any("index-sd.bif" in u for u in info_calls), (
            f"/api/bif/info must receive the BIF path, not the .mkv. URLs: {info_calls!r}"
        )
        assert not any(".mkv" in u for u in info_calls), (
            f"/api/bif/info must NOT receive a .mkv path (the pre-fix bug). URLs: {info_calls!r}"
        )


@pytest.mark.e2e
class TestPreviewInspectorTabs:
    def test_search_and_path_tabs_render(self, authed_page: Page, app_url: str) -> None:
        mock_servers_list(authed_page, servers=[])
        _mock_inspector_defaults(authed_page)
        authed_page.goto(f"{app_url}/bif-viewer")
        authed_page.wait_for_load_state("domcontentloaded")

        # Both tab buttons should exist.
        expect(authed_page.locator('button[data-bs-target="#tabSearch"]')).to_be_visible()
        expect(authed_page.locator('button[data-bs-target="#tabPath"]')).to_be_visible()

    def test_path_input_visible_after_tab_switch(self, authed_page: Page, app_url: str) -> None:
        mock_servers_list(authed_page, servers=[])
        _mock_inspector_defaults(authed_page)
        authed_page.goto(f"{app_url}/bif-viewer")
        authed_page.wait_for_load_state("domcontentloaded")

        authed_page.locator('button[data-bs-target="#tabPath"]').click()
        expect(authed_page.locator("#pathInput")).to_be_visible()
        expect(authed_page.locator("#loadPathBtn")).to_be_visible()
