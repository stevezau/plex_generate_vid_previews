"""Plex direct-webhook registration API.

Hosts the four ``/settings/plex_webhook/*`` endpoints used by the
Settings page (Plex Direct Webhook panel) to register, unregister, probe
the live state, and self-test the inbound webhook URL on plex.tv.

Split out of ``api_settings.py`` because Plex-account-side webhook
registration is a self-contained concern (its own helper cluster, its
own ``plex_webhook_registration`` collaborator, its own per-server token
resolver) and was 380+ LOC sitting in the middle of the generic
settings file. Moving it into its own module makes the settings file
focus on app settings instead of vendor-account state.

The actual *receiving* end of the webhook (POST handlers that consume
the events Plex fires) lives in :mod:`media_preview_generator.web.webhooks`
and :mod:`media_preview_generator.web.webhook_router` — this module only
manages the registration metadata that tells Plex where to send them.
"""

from flask import jsonify, request
from loguru import logger

from ..auth import setup_or_auth_required
from . import api
from .api_settings import _loopback_in_docker_warning


def _default_plex_webhook_url(server_id: str | None = None) -> str:
    """Build the default webhook URL Plex should POST to.

    Uses the request's effective host/scheme so the same browser
    session that's looking at the Settings page can register a URL
    Plex Media Server is likely to be able to reach (typical
    same-host or same-LAN setups).  Users on reverse proxies / split
    networks override this manually.

    When ``server_id`` is supplied the per-server pinned route is
    returned so multi-Plex installs route unambiguously by path. The
    auth token is appended at registration time (Plex's webhook UI has
    no header field) — the UI never displays the token.
    """
    base = request.host_url.rstrip("/")
    if server_id:
        return f"{base}/api/webhooks/server/{server_id}"
    return f"{base}/api/webhooks/incoming"


def _rebuild_path_preserving_host(url: str | None, server_id: str | None) -> str:
    """Rewrite ``url``'s path to ``/api/webhooks/server/<id>`` while keeping scheme+host.

    Existing installs persisted ``output.webhook_public_url`` pointing
    at the legacy ``/api/webhooks/incoming`` route. The user's custom
    host (e.g. a reverse-proxy domain like ``previews.example.com``) is
    the load-bearing bit — Plex Media Server has to be able to reach
    it. The path is ours to migrate. So: keep the host the user
    configured, swap the path for the per-server form.

    When ``url`` is empty or unparseable, falls back to
    :func:`_default_plex_webhook_url`. When ``server_id`` is empty,
    falls back to the legacy ``/incoming`` path because we have nothing
    to pin to.
    """
    from urllib.parse import urlparse, urlunparse

    if not url:
        return _default_plex_webhook_url(server_id)
    parsed = urlparse(url.strip())
    if not parsed.scheme or not parsed.netloc:
        return _default_plex_webhook_url(server_id)
    new_path = f"/api/webhooks/server/{server_id}" if server_id else "/api/webhooks/incoming"
    return urlunparse((parsed.scheme, parsed.netloc, new_path, "", "", ""))


def _resolve_plex_server_for_webhook(server_id: str | None) -> tuple[dict | None, str | None, int | None]:
    """Look up the Plex server entry the webhook endpoint should operate on.

    Returns (server_entry, error_message, status_code). On success the second
    and third members are None. When ``server_id`` is provided we require an
    exact match in ``media_servers``; when it's omitted we fall back to the
    first Plex entry (handles the setup-wizard / single-server case).
    """
    from ..settings_manager import get_settings_manager

    settings = get_settings_manager()
    media_servers = settings.get("media_servers") or []
    if server_id:
        match = next(
            (s for s in media_servers if isinstance(s, dict) and s.get("id") == server_id),
            None,
        )
        if not match:
            return None, f"Server {server_id!r} not configured", 404
        if (match.get("type") or "").lower() != "plex":
            return None, "Plex Direct webhook is Plex-only", 400
        return match, None, None
    plex_entry = next(
        (s for s in media_servers if isinstance(s, dict) and (s.get("type") or "").lower() == "plex"),
        None,
    )
    return plex_entry, None, None


def _server_token(server_entry: dict | None) -> str:
    """Extract the Plex token from a server entry, falling back to the legacy global one.

    Server entries store the token under ``auth.token`` (matching the
    multi-server schema). A flat ``token`` key is also accepted for
    forward-compat with any future re-shape.
    """
    from ..settings_manager import get_settings_manager

    if server_entry:
        token = (server_entry.get("auth") or {}).get("token") or server_entry.get("token") or ""
        token = str(token).strip()
        if token:
            return token
    return (get_settings_manager().plex_token or "").strip()


def _server_webhook_url(server_entry: dict | None) -> str:
    """Stored public URL for the given Plex server, or the per-request default."""
    if server_entry:
        url = ((server_entry.get("output") or {}).get("webhook_public_url") or "").strip()
        if url:
            return url
        return _default_plex_webhook_url(server_entry.get("id"))
    return _default_plex_webhook_url()


def _persist_server_webhook_url(server_entry: dict | None, public_url: str) -> None:
    """Write the public URL back onto the server entry's ``output``."""
    from ..settings_manager import get_settings_manager

    if not server_entry:
        return
    settings = get_settings_manager()
    media_servers = list(settings.get("media_servers") or [])
    for i, s in enumerate(media_servers):
        if isinstance(s, dict) and s.get("id") == server_entry.get("id"):
            entry = dict(s)
            output = dict(entry.get("output") or {})
            output["webhook_public_url"] = public_url
            entry["output"] = output
            media_servers[i] = entry
            break
    settings.update({"media_servers": media_servers})


def _plex_webhook_auth_token() -> str:
    """Return the secret to embed in the registered Plex webhook URL.

    Plex's webhook UI offers no way to set headers or HTTP Basic
    credentials, so the only way for Plex Media Server to authenticate
    against this app's webhook endpoint is via a ``?token=`` query
    parameter. The canonical inbound URL is ``/api/webhooks/incoming``;
    the legacy ``/api/webhooks/plex`` endpoint is kept around for
    installs that registered before the unified router landed.

    Returns the global ``webhook_secret`` (or the API auth token as a
    fallback). Per-server secrets were removed — every Plex server
    in a multi-Plex install shares the same URL token, rotated by
    changing the global secret.
    """
    from ..auth import get_auth_token
    from ..settings_manager import get_settings_manager

    settings = get_settings_manager()
    secret = (settings.get("webhook_secret") or "").strip()
    if secret:
        return secret
    return get_auth_token() or ""


@api.route("/settings/plex_webhook/status")
@setup_or_auth_required
def plex_webhook_status():
    """Return the live registration state of the Plex direct webhook.

    Probes plex.tv on every call so the UI reflects reality (e.g. the
    user revoked the webhook in Plex Web Settings).  Returns Plex Pass
    detection so the UI can disable the toggle when unsupported.

    Accepts ``?server_id=<id>`` to scope the check to one specific Plex
    server (each Plex server has its own token + URL). Without server_id,
    falls back to the first configured Plex server.
    """
    from .. import plex_webhook_registration as pwh

    server_id = (request.args.get("server_id") or "").strip() or None
    server_entry, err, status = _resolve_plex_server_for_webhook(server_id)
    if err:
        return jsonify({"error": err, "error_reason": "server_not_found"}), status

    token = _server_token(server_entry)
    # The input field always pre-fills with the per-server recommended
    # URL (path rebuilt; host preserved from any stored override or
    # falling back to the request host). The badge probe also tries
    # the stored URL in case plex.tv still has the legacy ``/incoming``
    # registration — that prevents the UI from claiming "Not registered"
    # while a perfectly working legacy webhook is still firing.
    stored_url = ""
    if server_entry:
        stored_url = ((server_entry.get("output") or {}).get("webhook_public_url") or "").strip()
    recommended_id = server_entry.get("id") if server_entry else None
    public_url = _rebuild_path_preserving_host(stored_url, recommended_id)

    has_pass: bool | None
    registered = False
    error: str | None = None
    error_reason: str | None = None

    if not token:
        has_pass = None
        error = "Plex token not configured"
        error_reason = "missing_token"
    else:
        try:
            registered = pwh.is_registered(token, public_url)
            if not registered and stored_url:
                registered = pwh.is_registered(token, stored_url)
            has_pass = True
        except pwh.PlexWebhookError as exc:
            registered = False
            has_pass = False if exc.reason == "plex_pass_required" else None
            error = str(exc)
            error_reason = exc.reason
        except Exception:
            try:
                has_pass = pwh.has_plex_pass(token)
            except Exception:
                has_pass = None
            registered = False

    return jsonify(
        {
            "server_id": server_entry.get("id") if server_entry else None,
            "server_name": server_entry.get("name") if server_entry else None,
            "registered_in_plex": registered,
            "public_url": public_url,
            "default_url": _default_plex_webhook_url(server_entry.get("id") if server_entry else None),
            "has_plex_pass": has_pass,
            "error": error,
            "error_reason": error_reason,
            "warning": _loopback_in_docker_warning(public_url),
        }
    )


@api.route("/settings/plex_webhook/register", methods=["POST"])
@setup_or_auth_required
def plex_webhook_register():
    """Register the Plex direct webhook with the user's plex.tv account.

    The auth secret is embedded in the URL Plex stores (as a ``?token=``
    query parameter) because Plex's webhook UI doesn't allow custom
    headers or credentials — that's the only way for Plex Media Server
    to authenticate against the receiving endpoint.
    """
    from .. import plex_webhook_registration as pwh

    data = request.get_json() or {}
    server_id = (data.get("server_id") or request.args.get("server_id") or "").strip() or None
    server_entry, err, status = _resolve_plex_server_for_webhook(server_id)
    if err:
        return jsonify({"success": False, "error": err, "reason": "server_not_found"}), status

    token = _server_token(server_entry)
    if not token:
        return (
            jsonify(
                {
                    "success": False,
                    "error": "Plex token not configured for this server. Re-authenticate from the Edit modal.",
                    "reason": "missing_token",
                }
            ),
            400,
        )

    auth_token = _plex_webhook_auth_token()
    if not auth_token:
        return (
            jsonify(
                {
                    "success": False,
                    "error": (
                        "No webhook secret or API token available to embed in the "
                        "Plex webhook URL.  Generate a webhook secret on this page "
                        "or set an API token, then try again."
                    ),
                    "reason": "missing_auth_token",
                }
            ),
            400,
        )

    registered_server_id = server_entry.get("id") if server_entry else None
    # The user's input can override the *host* (reverse proxy DNS,
    # different LAN address, etc.) — the *path* is ours and always
    # gets rebuilt to the per-server form. This self-heals legacy
    # ``/api/webhooks/incoming`` registrations without losing the
    # user's reverse-proxy host. Source preference: typed input →
    # stored override → request host.
    raw_url = (data.get("public_url") or "").strip()
    stored_url = ""
    if server_entry:
        stored_url = ((server_entry.get("output") or {}).get("webhook_public_url") or "").strip()
    source_url = raw_url or stored_url
    public_url = _rebuild_path_preserving_host(source_url, registered_server_id)

    try:
        # No ``server_id=`` kwarg: the per-server path already encodes
        # it (``/api/webhooks/server/<id>``). Passing it would land a
        # redundant ``&server_id=<id>`` on the URL plex.tv stores, and
        # nothing on the receiving side reads that query param —
        # routing matches on the Plex payload's ``Server.uuid``.
        pwh.register(token, public_url, auth_token=auth_token)
    except pwh.PlexWebhookError as exc:
        status_code = 400 if exc.reason in ("missing_url", "missing_token") else 502
        if exc.reason == "plex_pass_required":
            status_code = 403
        return (
            jsonify({"success": False, "error": str(exc), "reason": exc.reason}),
            status_code,
        )

    _persist_server_webhook_url(server_entry, public_url)

    return jsonify(
        {
            "success": True,
            "server_id": server_entry.get("id") if server_entry else None,
            "registered_in_plex": True,
            "public_url": public_url,
        }
    )


@api.route("/settings/plex_webhook/unregister", methods=["POST"])
@setup_or_auth_required
def plex_webhook_unregister():
    """Remove the Plex direct webhook from the user's plex.tv account."""
    from .. import plex_webhook_registration as pwh

    data = request.get_json() or {}
    server_id = (data.get("server_id") or request.args.get("server_id") or "").strip() or None
    server_entry, err, status = _resolve_plex_server_for_webhook(server_id)
    if err:
        return jsonify({"success": False, "error": err, "reason": "server_not_found"}), status

    token = _server_token(server_entry)
    public_url = _server_webhook_url(server_entry)

    if token:
        try:
            pwh.unregister(token, public_url)
        except pwh.PlexWebhookError as exc:
            logger.warning(
                "Could not remove the Plex webhook registration on plex.tv ({}). "
                "Plex may keep firing webhooks at us until you remove the entry manually at "
                "https://app.plex.tv/desktop#!/account → Webhooks. "
                "Check your Plex token is still valid for this server.",
                exc,
            )
            return (
                jsonify({"success": False, "error": str(exc), "reason": exc.reason}),
                502,
            )

    return jsonify(
        {
            "success": True,
            "server_id": server_entry.get("id") if server_entry else None,
            "registered_in_plex": False,
        }
    )
