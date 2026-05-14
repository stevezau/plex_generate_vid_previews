"""Tests for /api/settings/{emby,jellyfin}_webhook/info routes.

Pre-fix the per-server Edit modal had a "Webhook & Scanner" tab for
Plex only. Emby and Jellyfin users had no place to find the webhook
URL their plugin should POST to.

These routes return the per-server webhook URL + plugin install
instructions for Emby and Jellyfin. The auth token value is never
returned; the UI renders a [THIS APP'S TOKEN] placeholder and points
the user at Settings → Authentication to look it up themselves.

A companion ``/test`` endpoint that loopback-POSTed synthetic payloads
was removed — it only proved auth + route worked locally, which is
trivially true when the user is already authenticated to the UI. It
did NOT test reachability from the external media server. See
``TestVendorWebhookTestEndpointRemoved`` for the regression guard.

Matrix coverage per .claude/rules/testing.md:
  * vendor (emby / jellyfin)
  * info request shape (per-server URL, plugin info, token-not-leaked)
  * security (server_id required, type-mismatched server rejected)
  * test endpoint removal (404 for both vendors)
"""

from __future__ import annotations

import pytest

from media_preview_generator.web.app import create_app


@pytest.fixture
def client(tmp_path):
    """Flask test client with a configured Emby + Jellyfin server."""
    import os

    os.environ["WEB_AUTH_TOKEN"] = "test-token-123"
    app = create_app(config_dir=str(tmp_path))
    app.config["TESTING"] = True

    # Seed two servers so info requests have something to resolve.
    from media_preview_generator.web.settings_manager import get_settings_manager

    sm = get_settings_manager()
    sm.set(
        "media_servers",
        [
            {
                "id": "emby-1",
                "type": "emby",
                "name": "Test Emby",
                "enabled": True,
                "url": "http://emby:8096",
                "auth": {"method": "api_key", "api_key": "k"},
            },
            {
                "id": "jelly-1",
                "type": "jellyfin",
                "name": "Test Jellyfin",
                "enabled": True,
                "url": "http://jelly:8096",
                "auth": {"method": "api_key", "api_key": "k"},
            },
            {
                "id": "plex-1",
                "type": "plex",
                "name": "Test Plex",
                "enabled": True,
                "url": "http://plex:32400",
                "auth": {"token": "t"},
            },
        ],
    )

    with app.test_client() as c:
        yield c


def _hdrs():
    return {"X-Auth-Token": "test-token-123"}


class TestVendorWebhookInfo:
    @pytest.mark.parametrize(
        "vendor,server_id,expected_plugin_substring",
        [
            ("emby", "emby-1", "Emby Webhooks"),
            ("jellyfin", "jelly-1", "Jellyfin Webhook"),
        ],
    )
    def test_info_returns_url_plus_plugin_instructions(self, client, vendor, server_id, expected_plugin_substring):
        resp = client.get(f"/api/settings/{vendor}_webhook/info?server_id={server_id}", headers=_hdrs())
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["vendor"] == vendor
        assert data["server_id"] == server_id
        # Per-server URL pins dispatch to this server's id so multi-server
        # installs (two Jellyfins) don't rely on payload-ServerId matching
        # the probed server_identity. webhook_url is the per-server form.
        assert data["webhook_url"].endswith(f"/api/webhooks/server/{server_id}"), (
            f"webhook_url should be the per-server pinned endpoint; got {data['webhook_url']!r}"
        )
        assert data["webhook_url_per_server"] == data["webhook_url"]
        # Header name is fine to expose (it's a constant). Value MUST NOT
        # be exposed: it would land in JS state, screen-shares, and any
        # screenshot of the modal. The user looks up the token via
        # Settings → Authentication. Keep this assertion as a tripwire —
        # any future field that smuggles the token back in will fail it.
        assert data["auth_header_name"] == "X-Auth-Token"
        assert data["auth_token_placeholder"] == "[THIS APP'S TOKEN]", (
            "UI renders the placeholder in place of the real token so the value never appears on screen"
        )
        assert data["auth_token_present"] is True, "auth_token_present is a boolean — never the token itself"
        assert "auth_header_value" not in data, "auth_header_value MUST NOT be returned (token would leak via JS state)"
        assert "webhook_url_with_token" not in data, (
            "?token= URL fallback removed — both plugins support custom headers"
        )
        # Hard fail if the actual token value appears anywhere in the
        # JSON response. Defensive: catches any future field whose
        # value collides with the configured token.
        import json as _json

        body_text = _json.dumps(data)
        assert "test-token-123" not in body_text, (
            "The actual auth token must NOT appear anywhere in /info; only a placeholder + header name are surfaced"
        )

        assert expected_plugin_substring in data["plugin"]["plugin_name"], (
            f"plugin info should reference {expected_plugin_substring}"
        )
        assert isinstance(data["plugin"]["config_steps"], list)
        assert len(data["plugin"]["config_steps"]) >= 2, (
            "Plugin instructions should be a multi-step walk-through, not a single line."
        )
        assert any("X-Auth-Token" in step for step in data["plugin"]["config_steps"]), (
            "Plugin steps MUST instruct the user to add the X-Auth-Token header"
        )
        assert any("[THIS APP'S TOKEN]" in step for step in data["plugin"]["config_steps"]), (
            "Plugin steps reference the token via placeholder so the live value never appears in surfaced text"
        )
        assert data["plugin"].get("supports_custom_headers") is True, (
            "Both Emby's built-in Webhooks (4.6+) and the Jellyfin Webhook plugin support custom headers — "
            "this flag tells the UI not to render a fallback path"
        )
        # Auto-register is NOT yet supported (deferred follow-up).
        # Surface it in the response so the UI doesn't render a dead button.
        assert data["auto_register_supported"] is False

    def test_info_requires_server_id(self, client):
        resp = client.get("/api/settings/emby_webhook/info", headers=_hdrs())
        assert resp.status_code == 400
        assert "server_id" in resp.get_json()["error"]

    def test_info_rejects_unknown_server_id(self, client):
        resp = client.get("/api/settings/emby_webhook/info?server_id=does-not-exist", headers=_hdrs())
        assert resp.status_code == 404

    def test_info_rejects_type_mismatched_server(self, client):
        # Asking about Plex via the Emby endpoint is a programming error;
        # surface 400 so the UI can report it instead of silently returning
        # Emby plugin info for a Plex server.
        resp = client.get("/api/settings/emby_webhook/info?server_id=plex-1", headers=_hdrs())
        assert resp.status_code == 400
        assert "expected" in resp.get_json()["error"]


class TestVendorWebhookTestEndpointRemoved:
    """The ``/api/settings/{vendor}_webhook/test`` endpoint was removed.

    It only verified loopback + auth token round-trip — both trivially
    true when the user is already authenticated to the UI. It did NOT
    test whether the external media server could reach this app, which
    is the only thing worth knowing. The button was misleading: users
    saw "Round-trip OK" and assumed real webhooks would work, then
    spent hours debugging why they didn't.

    This test class is a regression guard so the endpoint stays gone.
    """

    def test_emby_test_endpoint_404s(self, client):
        resp = client.post("/api/settings/emby_webhook/test?server_id=emby-1", headers=_hdrs())
        assert resp.status_code == 404, (
            "The Emby self-test endpoint was removed because it only proved loopback worked. "
            "Re-introducing it would re-introduce the 'webhook test passes but real events fail' "
            "support pattern."
        )

    def test_jellyfin_test_endpoint_404s(self, client):
        resp = client.post("/api/settings/jellyfin_webhook/test?server_id=jelly-1", headers=_hdrs())
        assert resp.status_code == 404
