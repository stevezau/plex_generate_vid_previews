"""E2E tests for /logs page (load + basic UI)."""

from __future__ import annotations

import pytest
import requests
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

    def test_logs_page_returns_200(self, app_url: str, session_cookie: dict) -> None:
        # Plain GET should NOT redirect to /login. /logs is @login_required
        # (session-cookie auth, NOT X-Auth-Token), so we must carry the
        # session cookie from the test app's login flow. allow_redirects=False
        # so a regression that auth-bounces the route is caught — without
        # it, requests follows the 302 to /login (which returns 200) and
        # the test passes for the wrong reason.
        response = requests.get(
            f"{app_url}/logs",
            cookies={session_cookie["name"]: session_cookie["value"]},
            allow_redirects=False,
            timeout=30,
        )
        assert response.status_code == 200, (
            f"GET /logs should return 200 directly (no redirect); got {response.status_code}. "
            "A 302 here means the session cookie was rejected or the route now bounces to /login."
        )
