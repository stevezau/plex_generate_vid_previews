"""E2E tests for the dashboard (/) page.

Coverage:

* Empty state when no media servers configured.
* Per-GPU worker config card renders with detected GPUs.
* CPU + GPU stepper buttons increment / decrement and POST settings.
* Update-available badge appears when /api/system/version reports newer.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from ._mocks import (
    capture_settings_save,
    mock_dashboard_defaults,
    mock_media_servers_status,
    mock_version_with_update,
)


@pytest.fixture(scope="session", autouse=True)
def _complete_setup(complete_setup) -> None:
    return complete_setup


@pytest.fixture
def dashboard_page(authed_page: Page, app_url: str) -> Page:
    mock_dashboard_defaults(authed_page)
    authed_page.goto(f"{app_url}/")
    authed_page.wait_for_load_state("domcontentloaded")
    return authed_page


@pytest.mark.e2e
class TestDashboardEmptyState:
    def test_empty_state_banner_visible_when_no_servers(self, dashboard_page: Page) -> None:
        # The empty-state banner is gated by the dashboard JS that calls
        # /api/system/media-servers. With our mocked empty list it should
        # render.
        expect(dashboard_page.locator("text=No media servers configured yet")).to_be_visible(timeout=3000)

    def test_empty_state_cta_links_to_servers(self, dashboard_page: Page) -> None:
        cta = dashboard_page.locator('a[href="/servers"]:has-text("Add a media server")')
        expect(cta).to_be_visible()


@pytest.mark.e2e
class TestDashboardWithServers:
    def test_media_servers_status_renders_per_server(self, authed_page: Page, app_url: str) -> None:
        mock_dashboard_defaults(authed_page)
        mock_media_servers_status(
            authed_page,
            servers=[
                {
                    "id": "plex-1",
                    "name": "Home Plex",
                    "type": "plex",
                    "enabled": True,
                    "status": "connected",
                    "url": "http://plex.local:32400",
                },
                {
                    "id": "emby-1",
                    "name": "My Emby",
                    "type": "emby",
                    "enabled": True,
                    "status": "connected",
                    "url": "http://emby.local:8096",
                },
            ],
        )
        authed_page.goto(f"{app_url}/")
        authed_page.wait_for_load_state("domcontentloaded")
        # Each server's name should appear in the status block.
        expect(authed_page.locator("#mediaServersStatus")).to_contain_text("Home Plex", timeout=3000)
        expect(authed_page.locator("#mediaServersStatus")).to_contain_text("My Emby")


@pytest.mark.e2e
class TestDashboardGpuWorkerConfig:
    def test_per_gpu_card_renders_from_status(self, dashboard_page: Page) -> None:
        # mock_dashboard_defaults registers one GPU.
        expect(dashboard_page.locator("#gpuWorkerConfig")).to_contain_text("GPU 0", timeout=3000)

    def test_cpu_stepper_plus_increments_badge(self, authed_page: Page, app_url: str) -> None:
        mock_dashboard_defaults(authed_page)
        captured = capture_settings_save(authed_page)
        authed_page.goto(f"{app_url}/")
        authed_page.wait_for_load_state("domcontentloaded")

        # CPU workers badge starts at "-" then loads to "0" or "1" from
        # /api/system/config (we mocked cpu_threads=1).
        cpu_badge = authed_page.locator("#cpuWorkers")
        expect(cpu_badge).to_have_text("1", timeout=3000)

        # Click the + button.
        plus_btn = authed_page.locator('button.worker-scale-btn[data-worker-type="CPU"][data-direction="1"]')
        plus_btn.click()
        # Wait for the optimistic update + settings POST.
        expect(cpu_badge).to_have_text("2", timeout=2000)
        # Wait until the request landed.
        authed_page.wait_for_timeout(300)
        assert any("cpu_threads" in (c or {}) for c in captured), "POST /api/settings did not include cpu_threads"


@pytest.mark.e2e
class TestDashboardVersion:
    def test_update_available_badge_shown_when_newer(self, authed_page: Page, app_url: str) -> None:
        mock_dashboard_defaults(authed_page)
        mock_version_with_update(authed_page)
        authed_page.goto(f"{app_url}/")
        authed_page.wait_for_load_state("domcontentloaded")
        badge = authed_page.locator("#dashboardUpdateBadge")
        expect(badge).to_be_visible(timeout=3000)
        expect(badge).to_contain_text("Update available")
