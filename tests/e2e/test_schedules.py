"""E2E tests for the Schedules UI on /automation.

Phase E regression: the create-schedule and update-schedule endpoints used
to reject any schedule pinned to a non-Plex server with a 400. After the
multi-server completion every vendor's processor implements
scan_recently_added + list_canonical_paths, so non-Plex schedules now
save and run.

These tests exercise the UI end-to-end against mocked /api/schedules and
/api/servers responses to confirm the previously-blocked options now go
through cleanly.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, Route, expect

from ._mocks import _fulfill_json, mock_dashboard_defaults, mock_media_servers_status, mock_servers_list


@pytest.fixture(scope="session", autouse=True)
def _complete_setup(complete_setup) -> None:
    return complete_setup


def _seed_servers_for_schedule_modal(page: Page) -> None:
    """Common setup for the schedule modal: dashboard defaults + server list +
    /api/schedules with one Jellyfin schedule and one Plex schedule already in
    place so the table renders with vendor badges."""
    mock_dashboard_defaults(page)
    servers = [
        {
            "id": "plex-1",
            "name": "Home Plex",
            "type": "plex",
            "enabled": True,
            "status": "connected",
            "url": "http://plex.local:32400",
        },
        {
            "id": "jf-1",
            "name": "Home Jellyfin",
            "type": "jellyfin",
            "enabled": True,
            "status": "connected",
            "url": "http://jf.local:8096",
        },
        {
            "id": "emby-1",
            "name": "Home Emby",
            "type": "emby",
            "enabled": True,
            "status": "connected",
            "url": "http://emby.local:8096",
        },
    ]
    mock_media_servers_status(page, servers=servers)
    # The schedule modal's server picker reads /api/servers — separate
    # from /api/system/media-servers/status which feeds the status panel.
    mock_servers_list(page, servers=servers)
    # Libraries endpoint — returns a mixed set so the modal's library
    # picker has something to render regardless of which server is pinned.
    page.route(
        "**/api/libraries**",
        lambda r: _fulfill_json(
            r,
            {
                "libraries": [
                    {"id": "lib-1", "name": "Movies", "type": "movie", "server_id": "jf-1"},
                ]
            },
        ),
    )
    # Schedules list (empty initially so the table renders without
    # cluttering up the assertions).
    page.route(
        "**/api/schedules",
        lambda r: _fulfill_json(r, {"schedules": []}) if r.request.method == "GET" else r.continue_(),
    )


@pytest.mark.e2e
class TestScheduleNonPlex:
    def test_save_recently_added_schedule_against_jellyfin_succeeds(self, authed_page: Page, app_url: str) -> None:
        """Phase E regression: api_schedules.create_schedule used to 400 on
        non-Plex pins for recently_added. Now it saves cleanly."""
        _seed_servers_for_schedule_modal(authed_page)

        captured: list[dict] = []

        def handler(route: Route) -> None:
            method = route.request.method
            if method == "POST":
                try:
                    captured.append(route.request.post_data_json or {})
                except Exception:
                    captured.append({})
                _fulfill_json(
                    route,
                    {"id": "sch-1", "name": "Recent JF", "server_id": "jf-1", "enabled": True},
                    status=201,
                )
            elif method == "GET":
                _fulfill_json(route, {"schedules": []})
            else:
                route.continue_()

        authed_page.route("**/api/schedules", handler)

        authed_page.goto(f"{app_url}/automation#schedules")
        authed_page.wait_for_load_state("domcontentloaded")
        # Click "Add Schedule" — opens the modal.
        authed_page.locator('button:has-text("Add Schedule")').first.click()
        expect(authed_page.locator("#newScheduleForm")).to_be_visible(timeout=2000)

        # Fill the form: name + Recently Added scan mode + Jellyfin server.
        authed_page.locator("#scheduleName").fill("Recent JF")
        authed_page.locator("#scanModeRecent").check()
        # Wait for the JS populator (it loads the server list async).
        authed_page.wait_for_timeout(500)
        authed_page.locator("#scheduleServer").select_option("jf-1")
        # Submit — the form's primary button.
        save_btn = authed_page.locator(
            "#newScheduleModal button.btn-primary, #newScheduleModal button:has-text('Save')"
        ).last
        save_btn.click()
        authed_page.wait_for_timeout(800)

        assert captured, "POST /api/schedules never fired"
        body = captured[0]
        assert body.get("server_id") == "jf-1", body
        cfg = body.get("config") or {}
        assert cfg.get("job_type") == "recently_added", body

    def test_save_full_library_schedule_against_emby_succeeds(self, authed_page: Page, app_url: str) -> None:
        """Phase E regression: api_schedules.create_schedule used to 400 on
        non-Plex pins for full_library too."""
        _seed_servers_for_schedule_modal(authed_page)

        captured: list[dict] = []

        def handler(route: Route) -> None:
            method = route.request.method
            if method == "POST":
                try:
                    captured.append(route.request.post_data_json or {})
                except Exception:
                    captured.append({})
                _fulfill_json(
                    route,
                    {"id": "sch-2", "name": "Nightly Emby", "server_id": "emby-1", "enabled": True},
                    status=201,
                )
            elif method == "GET":
                _fulfill_json(route, {"schedules": []})
            else:
                route.continue_()

        authed_page.route("**/api/schedules", handler)

        authed_page.goto(f"{app_url}/automation#schedules")
        authed_page.wait_for_load_state("domcontentloaded")
        authed_page.locator('button:has-text("Add Schedule")').first.click()
        expect(authed_page.locator("#newScheduleForm")).to_be_visible(timeout=2000)

        authed_page.locator("#scheduleName").fill("Nightly Emby")
        # Full library is the default scanMode; just pick the server.
        authed_page.wait_for_timeout(500)
        authed_page.locator("#scheduleServer").select_option("emby-1")

        save_btn = authed_page.locator(
            "#newScheduleModal button.btn-primary, #newScheduleModal button:has-text('Save')"
        ).last
        save_btn.click()
        authed_page.wait_for_timeout(800)

        assert captured, "POST /api/schedules never fired"
        body = captured[0]
        assert body.get("server_id") == "emby-1", body
        cfg = body.get("config") or {}
        # Either omitted (defaults to full_library) or explicitly set.
        assert cfg.get("job_type") in (None, "full_library"), body


@pytest.mark.e2e
class TestScheduleServerDropdownVendorBadges:
    def test_schedule_server_dropdown_shows_vendor_in_option_text(self, authed_page: Page, app_url: str) -> None:
        """Phase F regression: the schedule server picker labels each option
        with a vendor suffix so users can disambiguate same-named servers."""
        _seed_servers_for_schedule_modal(authed_page)
        authed_page.goto(f"{app_url}/automation#schedules")
        authed_page.wait_for_load_state("domcontentloaded")
        authed_page.locator('button:has-text("Add Schedule")').first.click()
        expect(authed_page.locator("#scheduleServer")).to_be_visible(timeout=2000)
        authed_page.wait_for_timeout(600)

        option_texts = authed_page.locator("#scheduleServer option").all_text_contents()
        joined = " | ".join(option_texts)
        assert "(PLEX)" in joined, f"PLEX badge missing: {option_texts}"
        assert "(EMBY)" in joined, f"EMBY badge missing: {option_texts}"
        assert "(JELLYFIN)" in joined, f"JELLYFIN badge missing: {option_texts}"
