"""Programmatic registration of Plex Pass webhooks via plex.tv account API.

Wraps :class:`plexapi.myplex.MyPlexAccount` so the app can register and
unregister its own ``/api/webhooks/plex`` endpoint with Plex without the
user having to copy-paste the URL into the Plex Web Settings page.

The plex.tv webhook endpoint requires a Plex Pass subscription on the
server-owner account.  All helpers raise :class:`PlexWebhookError` with a
human-readable message when registration fails for any reason
(missing token, no Plex Pass, network error, etc.).
"""

from __future__ import annotations

from typing import List, Optional
from urllib.parse import urlencode, urlparse, urlunparse

from loguru import logger


class PlexWebhookError(Exception):
    """Raised when a Plex webhook registration call fails.

    The :attr:`reason` attribute carries a short, machine-readable code
    so the API layer can drive UI behaviour without re-parsing the
    message string:

    * ``"missing_token"`` — no Plex token configured
    * ``"missing_url"`` — caller passed an empty webhook URL
    * ``"plex_pass_required"`` — account does not have Plex Pass
    * ``"unauthorized"`` — token rejected by plex.tv
    * ``"network_error"`` — connection / TLS / timeout failure
    * ``"unknown"`` — anything else
    """

    def __init__(self, message: str, reason: str = "unknown") -> None:
        super().__init__(message)
        self.reason = reason


def _account(token: str):
    """Build a :class:`MyPlexAccount` from a Plex token.

    Raises:
        PlexWebhookError: When the token is missing/invalid or plex.tv
            cannot be reached.
    """
    if not token or not str(token).strip():
        raise PlexWebhookError("Plex token is not configured", reason="missing_token")

    try:
        from plexapi.exceptions import BadRequest, Unauthorized
        from plexapi.myplex import MyPlexAccount
    except ImportError as exc:
        raise PlexWebhookError(
            f"plexapi is not available: {exc}", reason="unknown"
        ) from exc

    try:
        return MyPlexAccount(token=str(token).strip())
    except Unauthorized as exc:
        raise PlexWebhookError(
            "Plex token was rejected by plex.tv. "
            "Re-authenticate via the Setup Wizard and try again.",
            reason="unauthorized",
        ) from exc
    except BadRequest as exc:
        raise PlexWebhookError(
            f"plex.tv rejected the account request: {exc}", reason="unknown"
        ) from exc
    except Exception as exc:  # network / TLS / timeout
        raise PlexWebhookError(
            f"Could not reach plex.tv: {exc}", reason="network_error"
        ) from exc


def _normalize_url(url: str) -> str:
    """Trim whitespace and a trailing slash so URL comparisons are stable."""
    if not url:
        return ""
    return str(url).strip().rstrip("/")


def _base_url(url: str) -> str:
    """Return the URL without its query string or fragment.

    Used for matching the user-facing base URL against URLs Plex has
    stored — those may have a ``?token=...`` query param appended for
    authentication.  Trailing slashes are stripped so the result is
    stable for equality comparisons.
    """
    if not url:
        return ""
    parsed = urlparse(str(url).strip())
    base = urlunparse(
        (parsed.scheme, parsed.netloc, parsed.path, parsed.params, "", "")
    )
    return base.rstrip("/")


def _build_authenticated_url(url: str, auth_token: Optional[str]) -> str:
    """Append ``?token=<auth_token>`` to ``url`` so Plex can authenticate.

    Plex's webhook UI does not allow custom headers, so the only way to
    authenticate POSTs from Plex Media Server is to embed the token in
    the URL.  When ``auth_token`` is empty the URL is returned unchanged
    (the endpoint will then 401, which is what we want).
    """
    base = _normalize_url(url)
    if not base or not auth_token:
        return base
    parsed = urlparse(base)
    existing_query = parsed.query
    new_query = urlencode({"token": auth_token})
    if existing_query:
        # Preserve any pre-existing query, but overwrite an existing token.
        existing_pairs = [
            kv for kv in existing_query.split("&") if kv and not kv.startswith("token=")
        ]
        existing_pairs.append(new_query)
        combined = "&".join(existing_pairs)
    else:
        combined = new_query
    return urlunparse(
        (parsed.scheme, parsed.netloc, parsed.path, parsed.params, combined, "")
    )


def has_plex_pass(token: str) -> bool:
    """Return ``True`` when the account has an active Plex Pass subscription.

    Returns ``False`` for any failure mode (missing token, network error,
    no subscription) so callers can use this as a quick UI gate without
    needing to handle exceptions.
    """
    try:
        account = _account(token)
    except PlexWebhookError:
        return False

    try:
        sub_active = bool(getattr(account, "subscriptionActive", False))
    except Exception:
        sub_active = False

    if sub_active:
        return True

    # Some plexapi versions expose the flag as ``hasPlexPass``; fall back
    # to that, then to a final probe of the webhooks endpoint, which
    # plex.tv only allows for Plex Pass accounts.
    try:
        if bool(getattr(account, "hasPlexPass", False)):
            return True
    except Exception:
        pass

    try:
        account.webhooks()
        return True
    except Exception:
        return False


def list_webhooks(token: str) -> List[str]:
    """Return all webhook URLs currently registered on the account.

    Raises:
        PlexWebhookError: On any failure (missing token, no Plex Pass,
            network error).
    """
    account = _account(token)
    try:
        urls = account.webhooks()
    except Exception as exc:
        message = str(exc).lower()
        if "401" in message or "unauthor" in message or "forbidden" in message:
            raise PlexWebhookError(
                "Webhooks require an active Plex Pass subscription on the "
                "server-owner account.",
                reason="plex_pass_required",
            ) from exc
        raise PlexWebhookError(
            f"Failed to list Plex webhooks: {exc}", reason="unknown"
        ) from exc
    return [_normalize_url(u) for u in (urls or []) if u]


def is_registered(token: str, url: str) -> bool:
    """Return ``True`` when a webhook with this base URL is registered.

    Match is performed on the base URL only — query strings (the
    ``?token=...`` we attach for auth) are stripped before comparison so
    the UI status probe works regardless of whether the registered URL
    has the token embedded.

    Returns ``False`` for any failure mode so this can be used as a
    cheap UI status probe.
    """
    target_base = _base_url(url)
    if not target_base:
        return False
    try:
        return any(_base_url(u) == target_base for u in list_webhooks(token))
    except PlexWebhookError:
        return False


def register(token: str, url: str, auth_token: Optional[str] = None) -> List[str]:
    """Register ``url`` as a Plex webhook (idempotent).

    Args:
        token: Plex token (account-level) used to talk to plex.tv.
        url: Fully-qualified webhook URL Plex will POST to.  May be the
            user-facing base URL — the auth token is appended below.
        auth_token: Webhook authentication secret to embed in the
            registered URL as a ``?token=`` query parameter.  Plex's
            webhook UI does not support custom headers, so this is the
            only way for Plex to authenticate against the receiving
            endpoint.

    Returns:
        The list of registered webhook URLs after the call.

    Raises:
        PlexWebhookError: On any failure.
    """
    target_base = _base_url(url)
    if not target_base:
        raise PlexWebhookError("Webhook URL is empty", reason="missing_url")

    target_with_auth = _build_authenticated_url(target_base, auth_token)

    account = _account(token)

    try:
        current = [_normalize_url(u) for u in (account.webhooks() or []) if u]
    except Exception as exc:
        message = str(exc).lower()
        if "401" in message or "unauthor" in message or "forbidden" in message:
            raise PlexWebhookError(
                "Webhooks require an active Plex Pass subscription on the "
                "server-owner account.",
                reason="plex_pass_required",
            ) from exc
        raise PlexWebhookError(
            f"Failed to read existing Plex webhooks: {exc}", reason="unknown"
        ) from exc

    # Remove any stale registrations matching the same base URL — they
    # may have a different (or missing) token query param.  This makes
    # "Re-register with Plex" idempotent and safe after rotating the
    # webhook secret.
    stale = [
        u for u in current if _base_url(u) == target_base and u != target_with_auth
    ]
    for stale_url in stale:
        try:
            account.deleteWebhook(stale_url)
            logger.info("Removed stale Plex webhook with old auth: {}", stale_url)
        except Exception as exc:
            logger.warning("Failed to remove stale Plex webhook {}: {}", stale_url, exc)

    if target_with_auth in current and not stale:
        logger.debug("Plex webhook already registered: {}", target_with_auth)
        return current

    try:
        account.addWebhook(target_with_auth)
    except Exception as exc:
        message = str(exc).lower()
        if "401" in message or "unauthor" in message or "forbidden" in message:
            raise PlexWebhookError(
                "Webhooks require an active Plex Pass subscription on the "
                "server-owner account.",
                reason="plex_pass_required",
            ) from exc
        raise PlexWebhookError(
            f"Failed to register Plex webhook: {exc}", reason="unknown"
        ) from exc

    logger.info("Registered Plex webhook (token embedded in URL): {}", target_base)
    try:
        return [_normalize_url(u) for u in (account.webhooks() or []) if u]
    except Exception:
        return current + [target_with_auth]


def unregister(token: str, url: str) -> List[str]:
    """Remove the webhook for ``url`` from the account.

    Matches by base URL so that any registered variants — with or
    without the ``?token=...`` query param — are all removed in a
    single call.

    Args:
        token: Plex authentication token.
        url: Webhook URL to remove (base URL is fine).

    Returns:
        The list of registered webhook URLs after the call.

    Raises:
        PlexWebhookError: On any failure other than "not registered",
            which is treated as a no-op success.
    """
    target_base = _base_url(url)
    if not target_base:
        raise PlexWebhookError("Webhook URL is empty", reason="missing_url")

    account = _account(token)

    try:
        current = [_normalize_url(u) for u in (account.webhooks() or []) if u]
    except Exception as exc:
        raise PlexWebhookError(
            f"Failed to read existing Plex webhooks: {exc}", reason="unknown"
        ) from exc

    matches = [u for u in current if _base_url(u) == target_base]
    if not matches:
        logger.debug("Plex webhook not registered (no-op): {}", target_base)
        return current

    for match in matches:
        try:
            account.deleteWebhook(match)
        except Exception as exc:
            raise PlexWebhookError(
                f"Failed to remove Plex webhook: {exc}", reason="unknown"
            ) from exc
        logger.info("Removed Plex webhook: {}", match)

    try:
        return [_normalize_url(u) for u in (account.webhooks() or []) if u]
    except Exception:
        return [u for u in current if _base_url(u) != target_base]
