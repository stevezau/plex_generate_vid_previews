"""Emby username+password → access token exchange.

Thin wrapper over :mod:`._mediabrowser_auth` (Emby and Jellyfin share
the same ``/Users/AuthenticateByName`` endpoint and ``MediaBrowser``
Authorization header). Pulled out of :mod:`emby` so the auth flow can
be exercised by the setup wizard before an :class:`EmbyServer` exists.
"""

from __future__ import annotations

from ._mediabrowser_auth import (
    _AUTH_DEVICE_ID,
    MediaBrowserAuthResult,
    authenticate_with_password,
    mediabrowser_authorization_header,
)

# Backwards-compatible aliases — callers and tests use the per-vendor
# names. ``EmbyAuthResult`` is a strict alias of the shared dataclass.
EmbyAuthResult = MediaBrowserAuthResult

# The private name lives on for any internal callers that imported it
# from this module. Prefer ``mediabrowser_authorization_header`` in
# new code.
_emby_authorization_header = mediabrowser_authorization_header

__all__ = [
    "EmbyAuthResult",
    "_AUTH_DEVICE_ID",
    "_emby_authorization_header",
    "authenticate_emby_with_password",
]


def authenticate_emby_with_password(
    *,
    base_url: str,
    username: str,
    password: str,
    verify_ssl: bool = True,
    timeout: int = 30,
    device_id_override: str | None = None,
) -> EmbyAuthResult:
    """Exchange username+password for an Emby ``AccessToken``."""
    return authenticate_with_password(
        vendor="Emby",
        base_url=base_url,
        username=username,
        password=password,
        verify_ssl=verify_ssl,
        timeout=timeout,
        device_id_override=device_id_override,
    )
