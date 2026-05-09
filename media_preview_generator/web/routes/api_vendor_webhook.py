"""Webhook info + test endpoints for Emby and Jellyfin servers.

Pre-fix the per-server Edit modal had a "Webhook & Scanner" tab for
Plex only — Emby and Jellyfin users had no place to find the webhook
URL their plugin should POST to. The user asked: "Plex has a register
webhook and scanner section but emby and jelly does not? Why? I thought
they do support webhooks also?"

Both vendors DO support webhooks (Emby Notifier plugin, Jellyfin
Webhook plugin) and the backend already routes them through the
universal :func:`media_preview_generator.web.webhook_router.handle_incoming`
endpoint at ``/api/webhooks/incoming``. The asymmetry was UI-only.

This module adds:

* ``GET  /api/settings/vendor_webhook/info?server_id=<id>`` — returns
  the recommended webhook URL, the auth token to use, and per-vendor
  plugin install instructions.
* ``POST /api/settings/vendor_webhook/test?server_id=<id>`` — fires a
  synthetic payload at OUR own ``/api/webhooks/incoming`` endpoint via
  loopback so the user can verify the URL is reachable and the auth
  token works without configuring their plugin first.

Auto-registration via the plugin APIs is intentionally NOT included
in this pass — Jellyfin Webhook plugin's config schema differs across
versions, and Emby Notifier's API is undocumented externally. The
manual flow ships now to close the discoverability gap; auto-register
is a follow-up driven by plugin-version detection (see TODO in the
``_register`` placeholder).
"""

from __future__ import annotations

from urllib.parse import urlencode

import requests
from flask import jsonify, request
from loguru import logger

from ..auth import setup_or_auth_required
from . import api

_PLUGIN_INSTRUCTIONS = {
    "emby": {
        "plugin_name": "Emby Notifier (built-in)",
        "install_url": "https://emby.media/community/index.php?/topic/55322-notifier-plug-in/",
        "config_steps": [
            "Open Emby Settings → Notifications → Webhooks (or Notifications → Add Webhook).",
            "Set 'Url' to the address shown above.",
            "Tick the events you want to forward — for new-media triggers, enable 'New Media Added'.",
            "Save. Use the 'Test webhook' button below to confirm the round-trip works.",
        ],
    },
    "jellyfin": {
        "plugin_name": "Jellyfin Webhook plugin",
        "install_url": "https://github.com/jellyfin/jellyfin-plugin-webhook",
        "config_steps": [
            "In Jellyfin: Dashboard → Plugins → Catalog → install 'Webhook' (restart Jellyfin if prompted).",
            "Dashboard → Plugins → Webhook → 'Add Generic Destination'.",
            "Set 'Webhook Url' to the address shown above.",
            "Under 'Notification Type', tick 'Item Added' (and any other events you want).",
            "Set 'Item Type' to Movie + Episode at minimum.",
            "Save. Use the 'Test webhook' button below to confirm the round-trip works.",
        ],
    },
}


def _resolve_vendor_server(server_id: str | None, expected_type: str) -> tuple[dict | None, str | None, int | None]:
    """Resolve a configured Emby / Jellyfin server entry by id."""
    from ..settings_manager import get_settings_manager

    if not server_id:
        return None, "server_id query parameter is required", 400

    settings = get_settings_manager()
    media_servers = settings.get("media_servers") or []
    match = next(
        (s for s in media_servers if isinstance(s, dict) and s.get("id") == server_id),
        None,
    )
    if not match:
        return None, f"Server {server_id!r} not configured", 404
    actual_type = (match.get("type") or "").lower()
    if actual_type != expected_type:
        return (
            None,
            f"Server {server_id!r} is type {actual_type!r}, expected {expected_type!r}",
            400,
        )
    return match, None, None


def _build_webhook_url() -> str:
    """Return the universal webhook URL with the current host pre-filled.

    The user copies this verbatim into the plugin's destination field.
    Uses ``request.host_url`` so a session connected via http://10.0.0.5:8080
    gets a URL Emby/Jellyfin can actually reach back from its own
    network namespace.
    """
    base = request.host_url.rstrip("/")
    return f"{base}/api/webhooks/incoming"


def _info_handler(vendor: str):
    server_id = request.args.get("server_id")
    server_entry, err, status = _resolve_vendor_server(server_id, vendor)
    if err:
        return jsonify({"error": err}), status
    instructions = _PLUGIN_INSTRUCTIONS.get(vendor, {})
    webhook_url = _build_webhook_url()

    # The plugin needs an auth token to POST through (the universal
    # /incoming endpoint enforces the same X-Auth-Token / ?token= as
    # every other webhook). Surface the active app token so the user
    # can paste a query-param-token URL directly.
    from ..auth import get_auth_token

    try:
        token = get_auth_token() or ""
    except Exception:
        token = ""
    webhook_url_with_token = f"{webhook_url}?{urlencode({'token': token})}" if token else webhook_url

    return jsonify(
        {
            "vendor": vendor,
            "server_id": server_id,
            "server_name": server_entry.get("name") or server_id,
            "webhook_url": webhook_url,
            "webhook_url_with_token": webhook_url_with_token,
            "auth_token_required": True,
            "auth_token_present": bool(token),
            "plugin": instructions,
            # Surfaces the auto-register deferral so the UI can render a
            # "Coming soon" hint instead of pretending the button does
            # something it doesn't.
            "auto_register_supported": False,
        }
    )


def _test_handler(vendor: str):
    server_id = request.args.get("server_id")
    server_entry, err, status = _resolve_vendor_server(server_id, vendor)
    if err:
        return jsonify({"error": err}), status

    webhook_url = _build_webhook_url()
    from ..auth import get_auth_token

    try:
        token = get_auth_token() or ""
    except Exception:
        token = ""
    if not token:
        return jsonify(
            {
                "success": False,
                "error": "App auth token is not configured — cannot self-test the webhook URL.",
            }
        ), 500

    # Synthetic payload the universal router will route as a "Test"
    # event (no actual processing kicks off). The router's vendor
    # detection cascade matches on Plex's multipart shape first,
    # Jellyfin's NotificationType, Emby's Event+Server, etc. Use a
    # custom-shape "Test" so we don't accidentally trigger ingestion
    # on a path we don't actually have.
    payload = {
        "eventType": "Test",
        "source": f"{vendor}_self_test",
        "server_id": server_id,
        "server_name": server_entry.get("name") or server_id,
    }
    headers = {"X-Auth-Token": token, "Content-Type": "application/json"}
    target = f"{webhook_url}"

    try:
        resp = requests.post(target, json=payload, headers=headers, timeout=10)
    except requests.RequestException as exc:
        logger.warning(
            "Vendor webhook self-test against {} failed: {}: {}",
            target,
            type(exc).__name__,
            exc,
        )
        return (
            jsonify(
                {
                    "success": False,
                    "error": f"Could not reach {target}: {exc}",
                    "hint": (
                        "Confirm the host is reachable from inside the container the plugin "
                        "runs in. If you're behind a reverse proxy, set the proxied URL on "
                        "the plugin instead of the loopback address."
                    ),
                }
            ),
            502,
        )

    body_preview: object
    try:
        body_preview = resp.json()
    except ValueError:
        body_preview = (resp.text or "")[:500]

    if 200 <= resp.status_code < 300:
        return jsonify(
            {
                "success": True,
                "status_code": resp.status_code,
                "body": body_preview,
                "message": (
                    "Round-trip OK. Configure your plugin with this URL and the "
                    "real events will land on the same endpoint."
                ),
            }
        )
    return (
        jsonify(
            {
                "success": False,
                "status_code": resp.status_code,
                "body": body_preview,
                "error": f"Webhook endpoint returned HTTP {resp.status_code}.",
            }
        ),
        502,
    )


# ---------------------------------------------------------------------------
# Emby
# ---------------------------------------------------------------------------


@api.route("/settings/emby_webhook/info", methods=["GET"])
@setup_or_auth_required
def emby_webhook_info():
    return _info_handler("emby")


@api.route("/settings/emby_webhook/test", methods=["POST"])
@setup_or_auth_required
def emby_webhook_test():
    return _test_handler("emby")


# ---------------------------------------------------------------------------
# Jellyfin
# ---------------------------------------------------------------------------


@api.route("/settings/jellyfin_webhook/info", methods=["GET"])
@setup_or_auth_required
def jellyfin_webhook_info():
    return _info_handler("jellyfin")


@api.route("/settings/jellyfin_webhook/test", methods=["POST"])
@setup_or_auth_required
def jellyfin_webhook_test():
    return _test_handler("jellyfin")
