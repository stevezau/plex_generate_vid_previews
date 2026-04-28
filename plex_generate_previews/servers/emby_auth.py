"""Emby username+password → access token exchange.

Pulled out of :mod:`emby` so the auth flow can be exercised by the setup
wizard (which needs to *get* a token before it can construct an
:class:`EmbyServer`) and also reused by the integration test stack
during ``setup_servers.py``.

The Emby (and Jellyfin) ``Authorization`` header for unauthenticated
endpoints follows a "scheme + comma-separated parameters" convention
that's strict — the server rejects requests with status 400 if the
``Client``, ``Device``, ``DeviceId``, and ``Version`` parameters are
absent or in the wrong shape. Compose the header in one place so callers
get it right by default.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import requests
import urllib3
from loguru import logger

# These show up in Emby's "Devices" UI; keep them stable so users can
# distinguish this tool's sessions from their own client devices.
_AUTH_CLIENT = "PlexGeneratePreviews"
_AUTH_DEVICE = "PlexGeneratePreviews"
_AUTH_VERSION = "1.0.0"
# A namespaced UUID gives every install the same DeviceId so re-auth
# lands on the same Emby session row instead of cluttering the device
# list. Tests can override via ``device_id_override``.
_AUTH_DEVICE_ID = uuid.uuid3(uuid.NAMESPACE_DNS, "PlexGeneratePreviews").hex


@dataclass(frozen=True)
class EmbyAuthResult:
    """Outcome of an Emby username+password authentication.

    Attributes:
        ok: True when the server returned a usable token.
        access_token: The ``AccessToken`` field, suitable for X-Emby-Token.
        user_id: The authenticated user's id; needed for per-user endpoints.
        server_id: The server's reported id (machine identifier).
        server_name: Friendly name for surfacing in the UI.
        message: Human-readable status / error string.
    """

    ok: bool
    access_token: str | None = None
    user_id: str | None = None
    server_id: str | None = None
    server_name: str | None = None
    message: str = ""


def _emby_authorization_header(*, device_id: str) -> str:
    """Build the strict Emby/Jellyfin ``Authorization`` header.

    Format: ``MediaBrowser Client="...", Device="...", DeviceId="...", Version="..."``.
    Both Emby and Jellyfin accept the same scheme name and parameters,
    so this helper is shared with the Jellyfin auth module that lands in
    Phase 3.
    """
    return (
        f'MediaBrowser Client="{_AUTH_CLIENT}", '
        f'Device="{_AUTH_DEVICE}", '
        f'DeviceId="{device_id}", '
        f'Version="{_AUTH_VERSION}"'
    )


def authenticate_emby_with_password(
    *,
    base_url: str,
    username: str,
    password: str,
    verify_ssl: bool = True,
    timeout: int = 30,
    device_id_override: str | None = None,
) -> EmbyAuthResult:
    """Exchange username+password for an Emby ``AccessToken``.

    Posts to ``/Users/AuthenticateByName``. The plaintext password is
    sent only once — we discard it on return; only the resulting token
    is persisted by the caller. Never raises on transport errors;
    failures are reported via ``ok=False`` with a human-readable
    ``message``.

    The device id is namespaced by default so re-authentication ends
    up on the same Emby session row; tests pass ``device_id_override``
    to keep assertions deterministic.
    """
    if not base_url:
        return EmbyAuthResult(ok=False, message="Emby URL is required")
    if not username:
        return EmbyAuthResult(ok=False, message="Emby username is required")

    if not verify_ssl:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    device_id = device_id_override or _AUTH_DEVICE_ID
    headers = {
        "Authorization": _emby_authorization_header(device_id=device_id),
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    body = {"Username": username, "Pw": password}

    try:
        response = requests.post(
            f"{base_url.rstrip('/')}/Users/AuthenticateByName",
            json=body,
            headers=headers,
            timeout=timeout,
            verify=verify_ssl,
        )
    except requests.exceptions.SSLError as exc:
        return EmbyAuthResult(ok=False, message=f"SSL verification failed: {exc}")
    except requests.exceptions.Timeout:
        return EmbyAuthResult(ok=False, message=f"Connection to {base_url} timed out")
    except requests.exceptions.ConnectionError as exc:
        return EmbyAuthResult(ok=False, message=f"Could not connect to Emby at {base_url}: {exc}")
    except requests.RequestException as exc:
        return EmbyAuthResult(ok=False, message=f"Request failed: {exc}")

    if response.status_code == 401:
        return EmbyAuthResult(ok=False, message="Emby rejected the username/password (401)")
    if response.status_code == 403:
        return EmbyAuthResult(ok=False, message="Access denied by Emby server (403)")
    if response.status_code >= 400:
        return EmbyAuthResult(
            ok=False,
            message=f"Emby returned HTTP {response.status_code}: {response.text[:200]}",
        )

    try:
        data = response.json()
    except ValueError:
        return EmbyAuthResult(ok=False, message="Emby auth response was not valid JSON")

    access_token = str(data.get("AccessToken") or "")
    user = data.get("User") or {}
    server_info = data.get("ServerId") or ""

    if not access_token:
        logger.warning("Emby auth succeeded but response did not include AccessToken")
        return EmbyAuthResult(ok=False, message="Emby auth response missing AccessToken")

    return EmbyAuthResult(
        ok=True,
        access_token=access_token,
        user_id=str(user.get("Id") or "") or None,
        server_id=str(server_info or "") or None,
        server_name=str(user.get("ServerName") or "") or None,
        message="Authenticated",
    )
