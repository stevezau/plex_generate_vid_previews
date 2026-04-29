"""Shared password-auth implementation for Emby and Jellyfin.

The two vendors share the ``/Users/AuthenticateByName`` endpoint and
the strict ``MediaBrowser`` ``Authorization`` header that gates it
(Jellyfin forked the scheme from Emby and never replaced it). The
public per-vendor wrappers in :mod:`emby_auth` and :mod:`jellyfin_auth`
delegate here so the only per-vendor difference is the brand name in
log/error strings.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import requests
import urllib3

# These show up in the server's "Devices" UI; keep them stable so users
# can distinguish this tool's sessions from their own client devices.
_AUTH_CLIENT = "PlexGeneratePreviews"
_AUTH_DEVICE = "PlexGeneratePreviews"
_AUTH_VERSION = "1.0.0"

# A namespaced UUID gives every install the same DeviceId so re-auth
# lands on the same session row instead of cluttering the device list.
# Tests can override via ``device_id_override``.
_AUTH_DEVICE_ID = uuid.uuid3(uuid.NAMESPACE_DNS, "PlexGeneratePreviews").hex


@dataclass(frozen=True)
class MediaBrowserAuthResult:
    """Outcome of a username+password authentication for either vendor.

    Same shape for Emby and Jellyfin; the auth_helper module aliases
    this under the per-vendor name (``EmbyAuthResult`` /
    ``JellyfinAuthResult``) for backwards compatibility.

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


def mediabrowser_authorization_header(*, device_id: str) -> str:
    """Build the strict Emby/Jellyfin ``Authorization`` header.

    Format: ``MediaBrowser Client="...", Device="...", DeviceId="...", Version="..."``.
    Both Emby and Jellyfin require this exact form on unauthenticated
    endpoints and reject malformed variants with HTTP 400.
    """
    return (
        f'MediaBrowser Client="{_AUTH_CLIENT}", '
        f'Device="{_AUTH_DEVICE}", '
        f'DeviceId="{device_id}", '
        f'Version="{_AUTH_VERSION}"'
    )


def authenticate_with_password(
    *,
    vendor: str,
    base_url: str,
    username: str,
    password: str,
    verify_ssl: bool = True,
    timeout: int = 30,
    device_id_override: str | None = None,
) -> MediaBrowserAuthResult:
    """Exchange username+password for an ``AccessToken``.

    Posts to ``/Users/AuthenticateByName``. The plaintext password is
    sent only once — we discard it on return; only the resulting token
    is persisted by the caller. Never raises on transport errors;
    failures are reported via ``ok=False`` with a human-readable
    ``message`` keyed off ``vendor`` for clarity in the UI.
    """
    if not base_url:
        return MediaBrowserAuthResult(ok=False, message=f"{vendor} URL is required")
    if not username:
        return MediaBrowserAuthResult(ok=False, message=f"{vendor} username is required")

    if not verify_ssl:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    device_id = device_id_override or _AUTH_DEVICE_ID
    headers = {
        "Authorization": mediabrowser_authorization_header(device_id=device_id),
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
        return MediaBrowserAuthResult(ok=False, message=f"SSL verification failed: {exc}")
    except requests.exceptions.Timeout:
        return MediaBrowserAuthResult(ok=False, message=f"Connection to {base_url} timed out")
    except requests.exceptions.ConnectionError as exc:
        return MediaBrowserAuthResult(
            ok=False,
            message=f"Could not connect to {vendor} at {base_url}: {exc}",
        )
    except requests.RequestException as exc:
        return MediaBrowserAuthResult(ok=False, message=f"Request failed: {exc}")

    if response.status_code == 401:
        return MediaBrowserAuthResult(ok=False, message=f"{vendor} rejected the username/password (401)")
    if response.status_code == 403:
        return MediaBrowserAuthResult(ok=False, message=f"Access denied by {vendor} server (403)")
    if response.status_code >= 400:
        return MediaBrowserAuthResult(
            ok=False,
            message=f"{vendor} returned HTTP {response.status_code}: {response.text[:200]}",
        )

    try:
        data = response.json()
    except ValueError:
        return MediaBrowserAuthResult(ok=False, message=f"{vendor} auth response was not valid JSON")

    access_token = str(data.get("AccessToken") or "")
    user = data.get("User") or {}
    server_id = data.get("ServerId") or ""

    if not access_token:
        return MediaBrowserAuthResult(ok=False, message=f"{vendor} auth response missing AccessToken")

    return MediaBrowserAuthResult(
        ok=True,
        access_token=access_token,
        user_id=str(user.get("Id") or "") or None,
        server_id=str(server_id or "") or None,
        server_name=str(user.get("ServerName") or "") or None,
        message="Authenticated",
    )
