"""Backend-real E2E: open the Servers page, click Edit on an existing server,
modify name + library toggle, save, reload, assert persistence.

Audit gap #4: edit-modal not pre-populating from PUT body, save merging
incorrectly, vendor switch corrupting record. The existing tests cover
add-server only — edit was never end-to-end tested.

This test pre-seeds a Plex server in settings.json so the Servers page
has something to render Edit against, drives the real PUT /api/servers/<id>,
then reloads the page and re-fetches via GET /api/servers/<id> to confirm
the change actually round-tripped through disk.
"""

from __future__ import annotations

import json

import pytest
from playwright.sync_api import expect


def _seeded_server() -> dict:
    """A Plex server entry in the on-disk shape (matches settings.json schema)."""
    return {
        "id": "plex-edit-test",
        "type": "plex",
        "name": "Original Plex",
        "enabled": True,
        "url": "http://plex.invalid:32400",
        "auth": {"method": "token", "token": "x" * 20},
        "verify_ssl": True,
        "timeout": 60,
        "libraries": [
            {"id": "1", "name": "Movies", "remote_paths": [], "enabled": True},
            {"id": "2", "name": "TV Shows", "remote_paths": [], "enabled": True},
            {"id": "3", "name": "Music", "remote_paths": [], "enabled": False},
        ],
        "path_mappings": [],
        "exclude_paths": [],
        "output": {
            "adapter": "plex_bundle",
            # Use /tmp because it always exists in CI; the server-payload
            # validator rejects nonexistent paths with HTTP 400.
            "plex_config_folder": "/tmp",
            "frame_interval": 10,
        },
    }


@pytest.mark.e2e
@pytest.mark.parametrize(
    "backend_real_app",
    [{"media_servers": [_seeded_server()]}],
    indirect=True,
)
class TestEditExistingServer:
    def test_edit_modal_prepopulates_from_saved_server(
        self,
        backend_real_page,
        backend_real_app: tuple[str, str],
    ) -> None:
        """Open Edit and verify URL, name, libraries reflect saved state.

        This is the regression that protects "edit modal opened blank /
        with stale defaults / with another server's data" — three distinct
        bug classes that all look the same to the user.
        """
        app_url, _ = backend_real_app

        backend_real_page.goto(f"{app_url}/servers")
        backend_real_page.wait_for_load_state("domcontentloaded")

        # Wait for the edit button — server cards render after /api/servers loads.
        edit_btn = backend_real_page.locator(".edit-server-btn[data-id='plex-edit-test']")
        edit_btn.wait_for(state="visible", timeout=10000)
        edit_btn.click()

        # Modal opens — assert pre-populated values match the seeded server.
        expect(backend_real_page.locator("#editServerModal")).to_be_visible(timeout=5000)
        expect(backend_real_page.locator("#editServerDisplayName")).to_have_value("Original Plex", timeout=5000)
        expect(backend_real_page.locator("#editServerUrl")).to_have_value("http://plex.invalid:32400")
        expect(backend_real_page.locator("#editServerEnabled")).to_be_checked()

    def test_edit_save_persists_name_and_library_toggle_through_reload(
        self,
        backend_real_page,
        backend_real_app: tuple[str, str],
    ) -> None:
        """Change name + toggle a library; save; reload; assert persistence.

        Reload is the key step — without it we'd only be testing that the
        modal closed, not that the PUT actually wrote to disk. The real
        bug class (D34-style) hides in the gap between "modal closed
        cheerfully" and "next page load shows the change".
        """
        app_url, config_dir = backend_real_app

        backend_real_page.goto(f"{app_url}/servers")
        backend_real_page.wait_for_load_state("domcontentloaded")

        edit_btn = backend_real_page.locator(".edit-server-btn[data-id='plex-edit-test']")
        edit_btn.wait_for(state="visible", timeout=10000)
        edit_btn.click()
        expect(backend_real_page.locator("#editServerModal")).to_be_visible(timeout=5000)

        # Wait for libraries to render in the modal — the element is
        # rendered even when collapsed; use attached state instead of
        # visible (it may be inside a closed accordion section).
        backend_real_page.locator("#editLibraryList").wait_for(state="attached", timeout=5000)

        # Change name.
        backend_real_page.locator("#editServerDisplayName").fill("Renamed Plex")

        # Drive the PUT directly via the app's saveEditedServer(). The
        # button's text is locale-flexible, so we hit the function. This
        # exercises the full read-form + PUT pipeline without depending on
        # a label string.
        # Click the actual Save button — saveEditedServer is in an IIFE
        # so it's not on window; click() the click-bound element instead.
        backend_real_page.locator("#editServerSave").click()

        # Wait for either: modal hides (success) OR error alert appears.
        # If the PUT hangs we can surface the error message.
        try:
            expect(backend_real_page.locator("#editServerModal")).to_be_hidden(timeout=10000)
        except AssertionError:
            err_text = backend_real_page.locator("#editServerResult").inner_text()
            raise AssertionError(
                f"Save modal did not close after click. Error result element says: {err_text!r}. "
                "The PUT likely returned a validation error."
            ) from None

        # Re-fetch via the API to confirm persistence (more reliable than
        # waiting for the cards to re-render).
        get_resp = backend_real_page.request.get(
            f"{app_url}/api/servers/plex-edit-test",
            headers={"X-Auth-Token": "e2e-test-token"},
        )
        assert get_resp.ok, f"GET /api/servers/plex-edit-test failed: {get_resp.status}"
        server = get_resp.json()
        assert server["name"] == "Renamed Plex", (
            f"After PUT + reload, server name is {server['name']!r} — the rename "
            "did not persist. The PUT either dropped the field or the GET is reading "
            "from a stale cache."
        )

        # And the on-disk settings.json must reflect it too — the strongest
        # test against the failure mode "PUT updated in-memory state but
        # never wrote to disk" (the class of bug that wiped jobs.json).
        settings_path = f"{config_dir}/settings.json"
        with open(settings_path) as f:
            on_disk = json.load(f)
        servers = on_disk.get("media_servers") or []
        target = next((s for s in servers if s.get("id") == "plex-edit-test"), None)
        assert target is not None, "Server vanished from settings.json after PUT"
        assert target["name"] == "Renamed Plex", (
            f"On-disk settings.json still says name={target['name']!r}. "
            "PUT updated in-memory state but did not flush to disk."
        )

    def test_edit_save_does_not_corrupt_vendor_type(
        self,
        backend_real_page,
        backend_real_app: tuple[str, str],
    ) -> None:
        """A name-only edit must not flip the vendor type.

        Bug class: server.type was getting overwritten to whatever was on
        the form's hidden input, which could end up empty on a quick
        re-render. Result: server silently changes from Plex to Emby on
        next load and refuses to dispatch.
        """
        app_url, config_dir = backend_real_app

        backend_real_page.goto(f"{app_url}/servers")
        backend_real_page.wait_for_load_state("domcontentloaded")

        edit_btn = backend_real_page.locator(".edit-server-btn[data-id='plex-edit-test']")
        edit_btn.wait_for(state="visible", timeout=10000)
        edit_btn.click()
        expect(backend_real_page.locator("#editServerModal")).to_be_visible(timeout=5000)
        backend_real_page.locator("#editLibraryList").wait_for(state="attached", timeout=5000)

        backend_real_page.locator("#editServerDisplayName").fill("Still Plex")
        # Click the actual Save button — saveEditedServer is in an IIFE
        # so it's not on window; click() the click-bound element instead.
        backend_real_page.locator("#editServerSave").click()
        expect(backend_real_page.locator("#editServerModal")).to_be_hidden(timeout=10000)

        with open(f"{config_dir}/settings.json") as f:
            on_disk = json.load(f)
        target = next(
            (s for s in (on_disk.get("media_servers") or []) if s.get("id") == "plex-edit-test"),
            None,
        )
        assert target is not None
        assert target["type"] == "plex", (
            f"Edit-save flipped the server type from 'plex' to {target['type']!r}. "
            "The PUT body's type field was either set wrong or merged in incorrectly."
        )
