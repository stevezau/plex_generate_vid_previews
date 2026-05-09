"""Tests for /api/settings/{emby,jellyfin}_webhook/{info,test} routes.

Pre-fix the per-server Edit modal had a "Webhook & Scanner" tab for
Plex only. Emby and Jellyfin users had no place to find the webhook
URL their plugin should POST to. The user asked: "Plex has a register
webhook and scanner section but emby and jelly does not? Why?"

These routes return the webhook URL + plugin install instructions for
Emby and Jellyfin servers respectively. The /test endpoint POSTs a
synthetic payload through the universal /api/webhooks/incoming route
to confirm the URL is reachable AND the auth token works.

Matrix coverage per .claude/rules/testing.md:
  * vendor (emby / jellyfin)
  * info request shape (returns URL, plugin info, auto_register flag)
  * test request shape (success / unreachable / non-2xx)
  * security (server_id is required, type-mismatched server rejected)
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

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
            ("emby", "emby-1", "Emby Notifier"),
            ("jellyfin", "jelly-1", "Jellyfin Webhook"),
        ],
    )
    def test_info_returns_url_plus_plugin_instructions(self, client, vendor, server_id, expected_plugin_substring):
        resp = client.get(f"/api/settings/{vendor}_webhook/info?server_id={server_id}", headers=_hdrs())
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["vendor"] == vendor
        assert data["server_id"] == server_id
        assert data["webhook_url"].endswith("/api/webhooks/incoming"), (
            f"webhook_url should point at the universal incoming endpoint; got {data['webhook_url']!r}"
        )
        assert "?token=" in data["webhook_url_with_token"], (
            "webhook_url_with_token MUST embed the auth token so the user can paste it directly into the plugin"
        )
        assert expected_plugin_substring in data["plugin"]["plugin_name"], (
            f"plugin info should reference {expected_plugin_substring}"
        )
        assert isinstance(data["plugin"]["config_steps"], list)
        assert len(data["plugin"]["config_steps"]) >= 2, (
            "Plugin instructions should be a multi-step walk-through, not a single line."
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


class TestVendorWebhookTest:
    @pytest.mark.parametrize("vendor,server_id", [("emby", "emby-1"), ("jellyfin", "jelly-1")])
    def test_test_round_trips_through_loopback(self, client, vendor, server_id):
        with patch("media_preview_generator.web.routes.api_vendor_webhook.requests.post") as post:
            ok = MagicMock()
            ok.status_code = 200
            ok.json.return_value = {"success": True, "message": "Test"}
            post.return_value = ok

            resp = client.post(f"/api/settings/{vendor}_webhook/test?server_id={server_id}", headers=_hdrs())

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["status_code"] == 200
        # Boundary kwargs: confirm the synthetic payload + auth header
        # made it onto the loopback POST so the round-trip actually
        # tests what users will configure (per .claude/rules/testing.md).
        call = post.call_args
        url = call.args[0] if call.args else call.kwargs["url"]
        assert url.endswith("/api/webhooks/incoming"), f"target URL should be the universal endpoint; got {url!r}"
        sent_headers = call.kwargs["headers"]
        assert sent_headers.get("X-Auth-Token") == "test-token-123"
        sent_payload = call.kwargs["json"]
        assert sent_payload["eventType"] == "Test"
        assert sent_payload["server_id"] == server_id

    def test_test_unreachable_returns_502(self, client):
        import requests as _requests

        with patch(
            "media_preview_generator.web.routes.api_vendor_webhook.requests.post",
            side_effect=_requests.ConnectionError("connection refused"),
        ):
            resp = client.post("/api/settings/emby_webhook/test?server_id=emby-1", headers=_hdrs())
        assert resp.status_code == 502
        data = resp.get_json()
        assert data["success"] is False
        assert "could not reach" in data["error"].lower()
        assert "hint" in data, "A 502 should always include a hint so the user knows what to check"

    def test_test_non_2xx_response_returns_502(self, client):
        with patch("media_preview_generator.web.routes.api_vendor_webhook.requests.post") as post:
            err = MagicMock()
            err.status_code = 401
            err.json.return_value = {"error": "Authentication required"}
            err.text = '{"error":"Authentication required"}'
            post.return_value = err

            resp = client.post("/api/settings/jellyfin_webhook/test?server_id=jelly-1", headers=_hdrs())
        assert resp.status_code == 502
        data = resp.get_json()
        assert data["success"] is False
        assert data["status_code"] == 401
