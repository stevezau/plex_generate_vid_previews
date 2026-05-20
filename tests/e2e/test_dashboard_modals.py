"""E2E tests for the dashboard's "Start New Job" + "Manual Trigger" modals."""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, Route, expect

from ._mocks import (
    _fulfill_json,
    mock_dashboard_defaults,
    mock_media_servers_status,
    mock_servers_list,
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


@pytest.fixture
def jellyfin_dashboard_page(authed_page: Page, app_url: str) -> Page:
    """Dashboard with a Jellyfin-only registry — proves the new multi-server
    full-scan path is reachable from the UI on a non-Plex install."""
    mock_dashboard_defaults(authed_page)
    jellyfin_server = {
        "id": "jf-1",
        "name": "Home Jellyfin",
        "type": "jellyfin",
        "enabled": True,
        "status": "connected",
        "url": "http://jf.local:8096",
    }
    mock_media_servers_status(authed_page, servers=[jellyfin_server])
    # The dashboard's server-picker JS reads /api/servers — that's what
    # populates the dropdown options. /api/system/media-servers/status is
    # for the dashboard's status panel, separate from the picker.
    mock_servers_list(authed_page, servers=[jellyfin_server])
    # Libraries endpoint scoped to the Jellyfin server.
    authed_page.route(
        "**/api/libraries**",
        lambda r: _fulfill_json(
            r,
            {"libraries": [{"id": "lib-1", "name": "Movies", "type": "movie", "server_id": "jf-1"}]},
        ),
    )
    authed_page.goto(f"{app_url}/")
    authed_page.wait_for_load_state("domcontentloaded")
    return authed_page


@pytest.mark.e2e
class TestNewJobModalNonPlex:
    """Phase D regression: the New Job modal must accept Jellyfin/Emby targets
    and POST to /api/jobs *without* the Plex-only validation gate that used
    to silently zero-output the request."""

    def test_multi_plex_pin_to_second_server_sends_correct_server_id(self, authed_page: Page, app_url: str) -> None:
        """Issue #244 reproducer: two Plex servers, both have a library
        id="1" (Plex assigns ids per-server starting at 1). User picks
        the SECOND Plex's library. The submit MUST carry
        ``server_id="plex-calypso"`` — the id of the originating server,
        NOT ``"plex-kraken"`` which the library-id inference would
        wrongly pick first."""
        mock_dashboard_defaults(authed_page)
        servers = [
            {
                "id": "plex-kraken",
                "name": "Plex - Kraken",
                "type": "plex",
                "enabled": True,
                "url": "http://kraken:32400",
            },
            {
                "id": "plex-calypso",
                "name": "Plex - Calypso - 4k",
                "type": "plex",
                "enabled": True,
                "url": "http://calypso:32400",
            },
        ]
        mock_media_servers_status(authed_page, servers=servers)
        mock_servers_list(authed_page, servers=servers)
        # Both servers expose a library with id="1" — the collision is
        # the whole point of this regression.
        authed_page.route(
            "**/api/libraries**",
            lambda r: _fulfill_json(
                r,
                {
                    "libraries": [
                        {
                            "id": "1",
                            "name": "movies",
                            "type": "movie",
                            "server_id": "plex-kraken",
                            "server_name": "Plex - Kraken",
                            "server_type": "plex",
                        },
                        {
                            "id": "1",
                            "name": "4k Movies",
                            "type": "movie",
                            "server_id": "plex-calypso",
                            "server_name": "Plex - Calypso - 4k",
                            "server_type": "plex",
                        },
                    ],
                },
            ),
        )
        authed_page.goto(f"{app_url}/")
        authed_page.wait_for_load_state("domcontentloaded")

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

        authed_page.route("**/api/jobs", handler)

        authed_page.locator('button:has-text("Start New Job")').click()
        expect(authed_page.locator("#newJobForm")).to_be_visible(timeout=2000)
        authed_page.wait_for_timeout(400)

        # Precondition: the rendered DOM must actually contain the
        # second-Plex checkbox we're about to click. Without this guard
        # a future renderer change that drops one of the per-server
        # groups would leave the JS evaluate's ``cb.checked = true``
        # as a no-op on a null reference and the assertion below would
        # pass trivially (form submits with no library_ids, backend
        # returns 400, ``captured`` stays empty — the test's other
        # asserts would never run). Same defensive shape as
        # tests/e2e/test_wizard_step2_libraries.py's locator-visible
        # gates before any DOM-state-mutating script.
        expect(authed_page.locator('input[data-server-id="plex-calypso"][value="1"]')).to_be_visible(timeout=1500)

        # Pick the SECOND Plex's "4k Movies" library specifically — the
        # checkbox carries ``data-server-id="plex-calypso"``. Pre-fix the
        # UI ignored that attribute and the backend inference picked
        # Kraken's "movies" (same id="1") instead.
        authed_page.evaluate(
            "(async () => {"
            "  const all = document.getElementById('jobLibraryAll');"
            "  all.checked = false;"
            "  if (typeof toggleAllLibraries === 'function') toggleAllLibraries(all);"
            "  const cb = document.querySelector('input[data-server-id=\"plex-calypso\"]');"
            "  if (cb) { cb.checked = true; cb.dispatchEvent(new Event('change')); }"
            "  if (typeof startNewJob === 'function') { try { await startNewJob(); } catch(_){} }"
            "})()"
        )
        authed_page.wait_for_timeout(800)

        assert captured, "POST /api/jobs never fired"
        body = captured[0]
        # The bug-blind anti-pattern: only asserting library_ids was
        # exactly how D34 hid; the regression here is in WHICH server
        # gets the pin, not whether the request fires.
        assert body.get("library_ids") == ["1"], body
        assert body.get("server_id") == "plex-calypso", (
            f"#244 regression: UI must send server_id='plex-calypso' for a tick on the "
            f"second Plex's library, NOT fall back to the inference that picks Kraken. "
            f"Got body={body!r}"
        )

    def test_jellyfin_full_scan_posts_to_jobs_endpoint(self, jellyfin_dashboard_page: Page) -> None:
        """Ticking a Jellyfin-only library must submit ``library_ids``
        with the Jellyfin ids AND ``server_id`` set to the owning Jellyfin.

        Behaviour change after issue #244: pre-fix the client deliberately
        omitted ``server_id`` and let the backend infer it from
        ``library_ids``. The inference picks the first ``media_servers[]``
        entry whose ``libraries[]`` contains a matching id, which is
        wrong on multi-Plex installs (Plex assigns library ids per-server
        starting at "1", so the same id usually exists on both Plex
        servers). The fix: the UI knows which server each library tick
        belongs to (data-server-id attribute) and sends it explicitly
        when every tick resolves to one server.
        """
        captured: list[dict] = []

        def handler(route: Route) -> None:
            if route.request.method == "POST":
                try:
                    captured.append(route.request.post_data_json or {})
                except Exception:
                    captured.append({})
                _fulfill_json(route, {"id": "job-jf-1", "status": "queued"})
            else:
                route.continue_()

        jellyfin_dashboard_page.route("**/api/jobs", handler)

        jellyfin_dashboard_page.locator('button:has-text("Start New Job")').click()
        expect(jellyfin_dashboard_page.locator("#newJobForm")).to_be_visible(timeout=2000)
        jellyfin_dashboard_page.wait_for_timeout(400)

        jellyfin_dashboard_page.evaluate(
            "(async () => {"
            "  const all = document.getElementById('jobLibraryAll');"
            "  all.checked = false;"
            "  if (typeof toggleAllLibraries === 'function') toggleAllLibraries(all);"
            "  const cb = document.querySelector('.job-library-checkbox');"
            "  if (cb) { cb.checked = true; cb.dispatchEvent(new Event('change')); }"
            "  if (typeof startNewJob === 'function') { try { await startNewJob(); } catch(_){} }"
            "})()"
        )
        jellyfin_dashboard_page.wait_for_timeout(800)

        assert captured, "POST /api/jobs never fired for the Jellyfin full-scan request"
        body = captured[0]
        assert body.get("library_ids") == ["lib-1"], body
        # Single-server selection MUST carry server_id so the backend
        # doesn't fall back to library-id inference (the cause of #244).
        assert body.get("server_id") == "jf-1", (
            f"server_id must be sent when every ticked library belongs to one server. Body: {body}"
        )


@pytest.mark.e2e
class TestServerDropdownVendorBadges:
    """Phase F regression: every server <select> annotates options with a
    vendor type suffix so the user can tell Plex from Emby from Jellyfin
    at a glance — even when the server names look the same."""

    def test_new_job_modal_groups_libraries_by_server_vendor(self, authed_page: Page, app_url: str) -> None:
        """After dropping the Media Server dropdown, the library checkbox
        list is the sole scope control — it MUST group libraries under a
        per-server heading so same-named libraries on different servers
        ("Movies" on Plex vs "Movies" on Emby) stay visibly distinct.
        """
        mock_dashboard_defaults(authed_page)
        servers = [
            {"id": "p", "name": "Plex Main", "type": "plex", "enabled": True, "url": "http://p"},
            {"id": "e", "name": "Emby Alt", "type": "emby", "enabled": True, "url": "http://e"},
            {"id": "j", "name": "Jelly Alt", "type": "jellyfin", "enabled": True, "url": "http://j"},
        ]
        mock_media_servers_status(authed_page, servers=servers)
        mock_servers_list(authed_page, servers=servers)
        authed_page.route(
            "**/api/libraries**",
            lambda r: _fulfill_json(
                r,
                {
                    "libraries": [
                        {
                            "id": "p1",
                            "name": "Movies",
                            "type": "movie",
                            "server_id": "p",
                            "server_name": "Plex Main",
                            "server_type": "plex",
                        },
                        {
                            "id": "e1",
                            "name": "Movies",
                            "type": "movie",
                            "server_id": "e",
                            "server_name": "Emby Alt",
                            "server_type": "emby",
                        },
                        {
                            "id": "j1",
                            "name": "Shows",
                            "type": "tvshows",
                            "server_id": "j",
                            "server_name": "Jelly Alt",
                            "server_type": "jellyfin",
                        },
                    ]
                },
            ),
        )
        authed_page.goto(f"{app_url}/")
        authed_page.wait_for_load_state("domcontentloaded")
        authed_page.locator('button:has-text("Start New Job")').click()
        expect(authed_page.locator("#jobLibraryList")).to_be_visible(timeout=2000)
        authed_page.wait_for_timeout(500)

        list_text = authed_page.locator("#jobLibraryList").inner_text()
        assert "Plex Main" in list_text, (
            f"Plex server heading missing from library list (regression — dropdown-removal broke the group-by-server render). Got: {list_text!r}"
        )
        assert "Emby Alt" in list_text, f"Emby server heading missing from library list. Got: {list_text!r}"
        assert "Jelly Alt" in list_text, f"Jellyfin server heading missing from library list. Got: {list_text!r}"

        # And the old dropdown must not have silently come back.
        assert authed_page.locator("#jobServerScope").count() == 0, (
            "jobServerScope dropdown must not exist — library selection is the sole scope control."
        )

    def test_manual_trigger_dropdown_shows_vendor_in_option_text(self, authed_page: Page, app_url: str) -> None:
        mock_dashboard_defaults(authed_page)
        servers = [
            {"id": "p", "name": "Servers", "type": "plex", "enabled": True, "url": "http://p"},
            {"id": "j", "name": "Servers", "type": "jellyfin", "enabled": True, "url": "http://j"},
        ]
        mock_media_servers_status(authed_page, servers=servers)
        mock_servers_list(authed_page, servers=servers)
        authed_page.goto(f"{app_url}/")
        authed_page.wait_for_load_state("domcontentloaded")
        authed_page.locator('button:has-text("Manual Trigger")').click()
        expect(authed_page.locator("#manualServerScope")).to_be_visible(timeout=2000)
        authed_page.wait_for_timeout(500)

        option_texts = authed_page.locator("#manualServerScope option").all_text_contents()
        joined = " | ".join(option_texts)
        assert "(PLEX)" in joined and "(JELLYFIN)" in joined, option_texts
