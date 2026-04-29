"""E2E tests for the dashboard's "Start New Job" + "Manual Trigger" modals."""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, Route, expect

from ._mocks import (
    _fulfill_json,
    mock_dashboard_defaults,
    mock_media_servers_status,
)


@pytest.fixture(scope="session", autouse=True)
def _complete_setup(complete_setup) -> None:
    return complete_setup


@pytest.fixture
def dashboard_page(authed_page: Page, app_url: str) -> Page:
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
            }
        ],
    )
    # Libraries endpoint feeds the modal's library list.
    authed_page.route(
        "**/api/libraries**",
        lambda r: _fulfill_json(r, {"libraries": [{"id": "1", "name": "Movies", "type": "movie"}]}),
    )
    authed_page.goto(f"{app_url}/")
    authed_page.wait_for_load_state("domcontentloaded")
    return authed_page


@pytest.mark.e2e
class TestNewJobModal:
    def test_new_job_modal_opens(self, dashboard_page: Page) -> None:
        dashboard_page.locator('button:has-text("Start New Job")').click()
        expect(dashboard_page.locator("#newJobForm")).to_be_visible(timeout=2000)

    def test_new_job_modal_submits_to_jobs_endpoint(self, dashboard_page: Page) -> None:
        captured: list[dict] = []

        def handler(route: Route) -> None:
            if route.request.method == "POST":
                try:
                    captured.append(route.request.post_data_json or {})
                except Exception:
                    captured.append({})
                _fulfill_json(route, {"id": "job-1", "status": "queued"})
            else:
                route.continue_()

        dashboard_page.route("**/api/jobs", handler)

        dashboard_page.locator('button:has-text("Start New Job")').click()
        expect(dashboard_page.locator("#newJobForm")).to_be_visible(timeout=2000)
        # Wait for libraries to load + tick Movies.
        dashboard_page.wait_for_timeout(500)
        # Submit button (label varies — find by class).
        submit = (
            dashboard_page.locator("#newJobForm")
            .locator("xpath=ancestor::div[contains(@class,'modal-content')]")
            .locator('button:has-text("Start"), button.btn-primary')
            .last
        )
        submit.click()
        dashboard_page.wait_for_timeout(500)
        assert captured, "POST /api/jobs never fired"


@pytest.mark.e2e
class TestManualTriggerModal:
    def test_manual_trigger_modal_opens(self, dashboard_page: Page) -> None:
        dashboard_page.locator('button:has-text("Manual Trigger")').click()
        expect(dashboard_page.locator("#manualFilePaths")).to_be_visible(timeout=2000)
        expect(dashboard_page.locator("#manualServerScope")).to_be_visible()
