"""E2E tests for /logs page (load + basic UI)."""

from __future__ import annotations

import pytest
from playwright.sync_api import Page

from ._mocks import _fulfill_json


@pytest.fixture(scope="session", autouse=True)
def _complete_setup(complete_setup) -> None:
    return complete_setup


@pytest.mark.e2e
class TestLogsPage:
    def test_logs_page_loads(self, authed_page: Page, app_url: str) -> None:
        # Stub the streaming + history endpoints so the page renders.
        authed_page.route(
            "**/api/logs**",
            lambda r: _fulfill_json(r, {"logs": [], "files": []}),
        )
        authed_page.goto(f"{app_url}/logs")
        authed_page.wait_for_load_state("domcontentloaded")
        # Page heading or some logs container element exists.
        assert authed_page.locator("h1, h2, h3, .container-fluid").first.is_visible()

    def test_logs_page_returns_200(self, authed_page: Page, app_url: str, auth_token: str) -> None:
        # Plain GET should not redirect away.
        response = authed_page.request.get(
            f"{app_url}/logs",
            headers={"X-Auth-Token": auth_token},
        )
        assert response.status == 200
