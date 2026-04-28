"""Jellyfin auth helpers: Quick Connect + username/password.

Two flows are exposed because they have very different UX:

* **Quick Connect** is the friendliest path for users on a desktop browser
  — we ask Jellyfin for a 6-character code; the user opens their
  Jellyfin web UI and types the code in their profile menu; once
  approved we fetch an ``AccessToken``. The user's password never
  leaves their browser.
* **Username + password** mirrors the Emby helper: ``POST
  /Users/AuthenticateByName`` with the same strict
  ``MediaBrowser`` ``Authorization`` header (Jellyfin forked the
  scheme from Emby).

Both flows surface :class:`JellyfinAuthResult` with the same shape so
the setup wizard can branch on ``method`` without duplicate plumbing.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import requests
import urllib3
from loguru import logger

from .emby_auth import _emby_authorization_header  # same Authorization scheme


@dataclass(frozen=True)
class JellyfinAuthResult:
    """Outcome of a Jellyfin authentication attempt.

    Attributes:
        ok: True when the server returned a usable token.
        access_token: The ``AccessToken`` field, suitable for X-Emby-Token.
        user_id: The authenticated user's id.
        server_id: The server's reported id.
        server_name: Friendly name for surfacing in the UI.
        message: Human-readable status / error string.
    """

    ok: bool
    access_token: str | None = None
    user_id: str | None = None
    server_id: str | None = None
    server_name: str | None = None
    message: str = ""


@dataclass(frozen=True)
class QuickConnectInitiation:
    """The state of an in-progress Quick Connect handshake.

    The setup wizard receives this from :func:`initiate_quick_connect`,
    displays ``code`` to the user, and polls
    :func:`poll_quick_connect` with ``secret`` until it succeeds.
    """

    code: str
    secret: str


def _device_id() -> str:
    """Return the namespaced device id used by both Emby and Jellyfin."""
    # Reuse the Emby module's namespaced device id so a Jellyfin user
    # who later switches to Emby (or vice versa) ends up on the same
    # session row in either server's "Devices" list.
    from .emby_auth import _AUTH_DEVICE_ID

    return _AUTH_DEVICE_ID


def authenticate_jellyfin_with_password(
    *,
    base_url: str,
    username: str,
    password: str,
    verify_ssl: bool = True,
    timeout: int = 30,
    device_id_override: str | None = None,
) -> JellyfinAuthResult:
    """Exchange username+password for a Jellyfin ``AccessToken``.

    Identical wire format to Emby's ``/Users/AuthenticateByName``;
    Jellyfin retained the inherited Emby endpoint.
    """
    if not base_url:
        return JellyfinAuthResult(ok=False, message="Jellyfin URL is required")
    if not username:
        return JellyfinAuthResult(ok=False, message="Jellyfin username is required")

    if not verify_ssl:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    headers = {
        "Authorization": _emby_authorization_header(device_id=device_id_override or _device_id()),
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    try:
        response = requests.post(
            f"{base_url.rstrip('/')}/Users/AuthenticateByName",
            json={"Username": username, "Pw": password},
            headers=headers,
            timeout=timeout,
            verify=verify_ssl,
        )
    except requests.exceptions.SSLError as exc:
        return JellyfinAuthResult(ok=False, message=f"SSL verification failed: {exc}")
    except requests.exceptions.Timeout:
        return JellyfinAuthResult(ok=False, message=f"Connection to {base_url} timed out")
    except requests.exceptions.ConnectionError as exc:
        return JellyfinAuthResult(ok=False, message=f"Could not connect to Jellyfin at {base_url}: {exc}")
    except requests.RequestException as exc:
        return JellyfinAuthResult(ok=False, message=f"Request failed: {exc}")

    if response.status_code == 401:
        return JellyfinAuthResult(ok=False, message="Jellyfin rejected the username/password (401)")
    if response.status_code == 403:
        return JellyfinAuthResult(ok=False, message="Access denied by Jellyfin server (403)")
    if response.status_code >= 400:
        return JellyfinAuthResult(
            ok=False,
            message=f"Jellyfin returned HTTP {response.status_code}: {response.text[:200]}",
        )

    try:
        data = response.json()
    except ValueError:
        return JellyfinAuthResult(ok=False, message="Jellyfin auth response was not valid JSON")

    access_token = str(data.get("AccessToken") or "")
    user = data.get("User") or {}

    if not access_token:
        return JellyfinAuthResult(ok=False, message="Jellyfin auth response missing AccessToken")

    return JellyfinAuthResult(
        ok=True,
        access_token=access_token,
        user_id=str(user.get("Id") or "") or None,
        server_id=str(data.get("ServerId") or "") or None,
        server_name=str(user.get("ServerName") or "") or None,
        message="Authenticated",
    )


def initiate_quick_connect(
    *,
    base_url: str,
    verify_ssl: bool = True,
    timeout: int = 30,
    device_id_override: str | None = None,
) -> tuple[QuickConnectInitiation | None, str]:
    """Ask Jellyfin to start a Quick Connect handshake.

    Returns a ``(initiation, message)`` tuple — ``initiation`` is
    ``None`` on failure with ``message`` describing why. On success,
    show ``initiation.code`` to the user and poll
    :func:`poll_quick_connect` with ``initiation.secret``.

    Quick Connect is admin-disabled by default on Jellyfin; a 401/403
    here usually means the admin needs to enable it under Server →
    Quick Connect, which the wizard surfaces in the error message.
    """
    if not base_url:
        return None, "Jellyfin URL is required"

    if not verify_ssl:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    headers = {
        "Authorization": _emby_authorization_header(device_id=device_id_override or _device_id()),
        "Accept": "application/json",
    }

    try:
        response = requests.post(
            f"{base_url.rstrip('/')}/QuickConnect/Initiate",
            headers=headers,
            timeout=timeout,
            verify=verify_ssl,
        )
    except requests.RequestException as exc:
        return None, f"Could not contact Jellyfin: {exc}"

    if response.status_code == 401:
        return None, "Jellyfin rejected the request (401) — Quick Connect may not be enabled by the admin"
    if response.status_code >= 400:
        return None, f"Jellyfin returned HTTP {response.status_code}"

    try:
        data = response.json()
    except ValueError:
        return None, "Quick Connect response was not valid JSON"

    code = str(data.get("Code") or "")
    secret = str(data.get("Secret") or "")
    if not code or not secret:
        return None, "Quick Connect response missing Code or Secret"

    return QuickConnectInitiation(code=code, secret=secret), "Initiated"


def poll_quick_connect(
    *,
    base_url: str,
    secret: str,
    verify_ssl: bool = True,
    timeout: int = 30,
    device_id_override: str | None = None,
) -> tuple[bool, str]:
    """Poll Jellyfin once for a Quick Connect approval.

    Returns ``(authenticated, message)``. The wizard typically calls
    this in a loop with a small sleep until ``authenticated`` becomes
    True, then exchanges the secret via :func:`exchange_quick_connect`.
    """
    if not base_url or not secret:
        return False, "URL and secret are required"

    if not verify_ssl:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    headers = {
        "Authorization": _emby_authorization_header(device_id=device_id_override or _device_id()),
        "Accept": "application/json",
    }

    try:
        response = requests.get(
            f"{base_url.rstrip('/')}/QuickConnect/Connect",
            params={"secret": secret},
            headers=headers,
            timeout=timeout,
            verify=verify_ssl,
        )
    except requests.RequestException as exc:
        return False, f"Quick Connect poll failed: {exc}"

    if response.status_code == 401:
        return False, "Jellyfin rejected the poll (401)"
    if response.status_code == 404:
        return False, "Quick Connect session not found — the secret may have expired"
    if response.status_code >= 400:
        return False, f"Jellyfin returned HTTP {response.status_code}"

    try:
        data = response.json()
    except ValueError:
        return False, "Quick Connect poll response was not valid JSON"

    return bool(data.get("Authenticated")), "Pending" if not data.get("Authenticated") else "Approved"


def exchange_quick_connect(
    *,
    base_url: str,
    secret: str,
    verify_ssl: bool = True,
    timeout: int = 30,
    device_id_override: str | None = None,
) -> JellyfinAuthResult:
    """After approval, exchange the secret for a real ``AccessToken``.

    Wraps ``POST /Users/AuthenticateWithQuickConnect`` with the same
    ``Authorization`` header used during initiation; the server checks
    that the same device id is exchanging the secret it issued.
    """
    if not base_url or not secret:
        return JellyfinAuthResult(ok=False, message="URL and secret are required")

    if not verify_ssl:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    headers = {
        "Authorization": _emby_authorization_header(device_id=device_id_override or _device_id()),
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    try:
        response = requests.post(
            f"{base_url.rstrip('/')}/Users/AuthenticateWithQuickConnect",
            json={"Secret": secret},
            headers=headers,
            timeout=timeout,
            verify=verify_ssl,
        )
    except requests.RequestException as exc:
        return JellyfinAuthResult(ok=False, message=f"Quick Connect exchange failed: {exc}")

    if response.status_code == 401:
        return JellyfinAuthResult(
            ok=False,
            message="Jellyfin rejected the secret (401) — code may not have been approved yet",
        )
    if response.status_code >= 400:
        return JellyfinAuthResult(
            ok=False,
            message=f"Jellyfin returned HTTP {response.status_code}: {response.text[:200]}",
        )

    try:
        data = response.json()
    except ValueError:
        return JellyfinAuthResult(ok=False, message="Quick Connect exchange response was not valid JSON")

    access_token = str(data.get("AccessToken") or "")
    user = data.get("User") or {}

    if not access_token:
        return JellyfinAuthResult(ok=False, message="Quick Connect exchange response missing AccessToken")

    return JellyfinAuthResult(
        ok=True,
        access_token=access_token,
        user_id=str(user.get("Id") or "") or None,
        server_id=str(data.get("ServerId") or "") or None,
        server_name=str(user.get("ServerName") or "") or None,
        message="Authenticated via Quick Connect",
    )


def quick_connect_blocking(
    *,
    base_url: str,
    secret: str,
    verify_ssl: bool = True,
    timeout: int = 30,
    poll_interval: float = 2.0,
    deadline_seconds: int = 300,
    device_id_override: str | None = None,
) -> JellyfinAuthResult:
    """Synchronous helper: poll Quick Connect until approved or deadline.

    Useful for non-UI callers (tests, the integration setup script).
    The web wizard normally orchestrates :func:`poll_quick_connect`
    itself so the user can see "still waiting" UI state.
    """
    deadline = time.time() + deadline_seconds
    while time.time() < deadline:
        approved, _ = poll_quick_connect(
            base_url=base_url,
            secret=secret,
            verify_ssl=verify_ssl,
            timeout=timeout,
            device_id_override=device_id_override,
        )
        if approved:
            return exchange_quick_connect(
                base_url=base_url,
                secret=secret,
                verify_ssl=verify_ssl,
                timeout=timeout,
                device_id_override=device_id_override,
            )
        time.sleep(poll_interval)

    logger.info("Quick Connect deadline reached after {}s without approval", deadline_seconds)
    return JellyfinAuthResult(ok=False, message="Quick Connect deadline reached without approval")
