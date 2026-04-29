"""Vendor auth-flow API endpoints used by the Add Server wizard.

The wizard cannot construct an :class:`EmbyServer` / :class:`JellyfinServer`
without a token, but the user only has a username + password (or wants
to use Jellyfin Quick Connect). These endpoints bridge that gap by
calling the auth helpers in :mod:`servers.emby_auth` /
:mod:`servers.jellyfin_auth` and returning the resulting access token
ready to drop into ``POST /api/servers``'s ``auth`` field.

No persistence happens here — every endpoint is stateless. The wizard
takes the returned token, builds a full ``ServerConfig`` payload, and
POSTs to ``/api/servers`` to save.
"""

from __future__ import annotations

from flask import jsonify, request
from loguru import logger

from ...servers.emby_auth import authenticate_emby_with_password
from ...servers.jellyfin_auth import (
    authenticate_jellyfin_with_password,
    exchange_quick_connect,
    initiate_quick_connect,
    poll_quick_connect,
)
from ..auth import setup_or_auth_required
from . import api


def _required_string(payload: dict, key: str) -> tuple[str | None, str]:
    """Pull and validate a required string field. Returns ``(value, error)``."""
    value = str(payload.get(key) or "").strip()
    if not value:
        return None, f"{key} is required"
    return value, ""


@api.route("/servers/auth/emby/password", methods=["POST"])
@setup_or_auth_required
def emby_password_auth():
    """Exchange Emby username + password for an access token.

    Body:
        ``{"url": "...", "username": "...", "password": "...",
            "verify_ssl": bool (optional, default true)}``

    Returns:
        On success: ``{"ok": true, "access_token": "...",
            "user_id": "...", "server_id": "...", "server_name": "..."}``.
        On failure: ``{"ok": false, "message": "..."}``.

    The plaintext password is forwarded once to Emby and never persisted
    on this side. The wizard takes the returned ``access_token`` and
    drops it into the ``auth`` field of the subsequent
    ``POST /api/servers`` call.
    """
    payload = request.get_json(silent=True) or {}
    url, err = _required_string(payload, "url")
    if err:
        return jsonify({"ok": False, "message": err}), 400
    username, err = _required_string(payload, "username")
    if err:
        return jsonify({"ok": False, "message": err}), 400
    password = str(payload.get("password") or "")
    verify_ssl = bool(payload.get("verify_ssl", True))

    try:
        result = authenticate_emby_with_password(
            base_url=url,
            username=username,
            password=password,
            verify_ssl=verify_ssl,
        )
    except Exception as exc:
        logger.warning(
            "Emby username+password sign-in failed for {} ({}: {}). "
            "Verify the URL, username, and password — and that the Emby server is reachable.",
            url,
            type(exc).__name__,
            exc,
        )
        return jsonify({"ok": False, "message": f"unexpected error: {exc}"}), 500

    return jsonify(
        {
            "ok": result.ok,
            "access_token": result.access_token,
            "user_id": result.user_id,
            "server_id": result.server_id,
            "server_name": result.server_name,
            "message": result.message,
        }
    )


@api.route("/servers/auth/jellyfin/password", methods=["POST"])
@setup_or_auth_required
def jellyfin_password_auth():
    """Exchange Jellyfin username + password for an access token.

    Identical wire format to the Emby endpoint above; both vendors expose
    ``/Users/AuthenticateByName``. Wizard chooses one based on the
    server type the user picked.
    """
    payload = request.get_json(silent=True) or {}
    url, err = _required_string(payload, "url")
    if err:
        return jsonify({"ok": False, "message": err}), 400
    username, err = _required_string(payload, "username")
    if err:
        return jsonify({"ok": False, "message": err}), 400
    password = str(payload.get("password") or "")
    verify_ssl = bool(payload.get("verify_ssl", True))

    try:
        result = authenticate_jellyfin_with_password(
            base_url=url,
            username=username,
            password=password,
            verify_ssl=verify_ssl,
        )
    except Exception as exc:
        logger.warning(
            "Jellyfin username+password sign-in failed for {} ({}: {}). "
            "Verify the URL, username, and password — and that the Jellyfin server is reachable. "
            "If you'd rather use Quick Connect, switch the auth method on the previous step.",
            url,
            type(exc).__name__,
            exc,
        )
        return jsonify({"ok": False, "message": f"unexpected error: {exc}"}), 500

    return jsonify(
        {
            "ok": result.ok,
            "access_token": result.access_token,
            "user_id": result.user_id,
            "server_id": result.server_id,
            "server_name": result.server_name,
            "message": result.message,
        }
    )


@api.route("/servers/auth/jellyfin/quick-connect/initiate", methods=["POST"])
@setup_or_auth_required
def jellyfin_quick_connect_initiate():
    """Start a Jellyfin Quick Connect handshake.

    Body: ``{"url": "...", "verify_ssl": bool (optional)}``.

    Returns ``{"ok": true, "code": "ABC123", "secret": "..."}`` on
    success — the wizard displays the ``code`` to the user and tells
    them to enter it in their Jellyfin profile menu, then begins
    polling :func:`jellyfin_quick_connect_poll` with the ``secret``.

    Quick Connect is admin-disabled by default on Jellyfin; a 401 here
    means the admin needs to enable it under Server → Quick Connect.
    The error message surfaces that hint.
    """
    payload = request.get_json(silent=True) or {}
    url, err = _required_string(payload, "url")
    if err:
        return jsonify({"ok": False, "message": err}), 400
    verify_ssl = bool(payload.get("verify_ssl", True))

    initiation, message = initiate_quick_connect(base_url=url, verify_ssl=verify_ssl)
    if initiation is None:
        return jsonify({"ok": False, "message": message}), 200

    return jsonify(
        {
            "ok": True,
            "code": initiation.code,
            "secret": initiation.secret,
            "message": message,
        }
    )


@api.route("/servers/auth/jellyfin/quick-connect/poll", methods=["POST"])
@setup_or_auth_required
def jellyfin_quick_connect_poll():
    """Poll a Quick Connect session once for approval.

    Body: ``{"url": "...", "secret": "...", "verify_ssl": bool (optional)}``.

    Returns ``{"ok": true, "authenticated": bool, "message": "..."}``.
    The wizard typically calls this on a short interval (every 2-3
    seconds) until ``authenticated`` becomes ``true``, then exchanges
    via :func:`jellyfin_quick_connect_exchange`.
    """
    payload = request.get_json(silent=True) or {}
    url, err = _required_string(payload, "url")
    if err:
        return jsonify({"ok": False, "message": err}), 400
    secret, err = _required_string(payload, "secret")
    if err:
        return jsonify({"ok": False, "message": err}), 400
    verify_ssl = bool(payload.get("verify_ssl", True))

    authenticated, message = poll_quick_connect(
        base_url=url,
        secret=secret,
        verify_ssl=verify_ssl,
    )
    return jsonify({"ok": True, "authenticated": authenticated, "message": message})


@api.route("/servers/auth/jellyfin/quick-connect/exchange", methods=["POST"])
@setup_or_auth_required
def jellyfin_quick_connect_exchange():
    """After approval, exchange the Quick Connect secret for an access token.

    Body: ``{"url": "...", "secret": "...", "verify_ssl": bool (optional)}``.

    Returns the same shape as the password-auth endpoint — wizard uses
    it identically.
    """
    payload = request.get_json(silent=True) or {}
    url, err = _required_string(payload, "url")
    if err:
        return jsonify({"ok": False, "message": err}), 400
    secret, err = _required_string(payload, "secret")
    if err:
        return jsonify({"ok": False, "message": err}), 400
    verify_ssl = bool(payload.get("verify_ssl", True))

    result = exchange_quick_connect(
        base_url=url,
        secret=secret,
        verify_ssl=verify_ssl,
    )
    return jsonify(
        {
            "ok": result.ok,
            "access_token": result.access_token,
            "user_id": result.user_id,
            "server_id": result.server_id,
            "server_name": result.server_name,
            "message": result.message,
        }
    )
