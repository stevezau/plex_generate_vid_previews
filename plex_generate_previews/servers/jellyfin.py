"""Jellyfin implementation of the :class:`MediaServer` interface.

Most of the REST surface is shared with Emby (Jellyfin forked from
Emby) and lives in :mod:`._embyish`. This module specialises that base
with the Jellyfin-only bits:

* :class:`ServerType` enum value.
* :meth:`trigger_refresh` — Jellyfin has no path-based equivalent of
  Emby's ``/Library/Media/Updated``; uses ``/Items/{id}/Refresh`` or
  full ``/Library/Refresh`` instead.
* :meth:`parse_webhook` — jellyfin-plugin-webhook payload shape
  (``NotificationType`` / ``ItemId`` / ``ServerId``).

The Quick Connect auth flow lives separately in
:mod:`.jellyfin_auth` because it's only used during setup, not
during normal operation.
"""

from __future__ import annotations

import json
from typing import Any

from loguru import logger

from ._embyish import EmbyApiClient
from .base import ServerType, WebhookEvent


class JellyfinServer(EmbyApiClient):
    """Wrap a single Jellyfin server in the :class:`MediaServer` interface.

    Args:
        config: Persisted configuration. ``config.auth`` accepts any of:

            - ``{"method": "quick_connect", "access_token": "...", "user_id": "..."}``
            - ``{"method": "password", "access_token": "...", "user_id": "..."}``
            - ``{"method": "api_key", "api_key": "..."}``

            The token (whichever flow produced it) goes out on the
            ``X-Emby-Token`` header — Jellyfin honours the legacy Emby
            header name alongside the modern ``Authorization`` form.
    """

    vendor_name = "Jellyfin"

    def __init__(self, config) -> None:
        super().__init__(config, default_name="Jellyfin")

    @property
    def type(self) -> ServerType:
        return ServerType.JELLYFIN

    def trigger_refresh(self, *, item_id: str | None, remote_path: str | None) -> None:
        """Notify Jellyfin to re-scan an item.

        Jellyfin has no equivalent of Emby's path-based ``/Library/Media/Updated``,
        so we prefer the per-item ``/Items/{id}/Refresh`` endpoint when the
        item id is known and fall back to a full ``/Library/Refresh``
        scan otherwise. ``remote_path`` is unused — it's accepted to
        match the abstract :meth:`MediaServer.trigger_refresh` signature.
        Failures are silently swallowed — the publishing side already
        wrote the trickplay tiles next to the media; the scan trigger
        is best-effort.
        """
        del remote_path  # Jellyfin's API doesn't expose a path-keyed refresh

        if item_id:
            try:
                response = self._request("POST", f"/Items/{item_id}/Refresh")
                response.raise_for_status()
                return
            except Exception as exc:
                logger.debug("Jellyfin per-item refresh failed for {}: {}", item_id, exc)

        # Fallback: nudge a full scan. Should rarely fire — most paths
        # arrive at the publisher with an item id from the source webhook.
        try:
            response = self._request("POST", "/Library/Refresh")
            response.raise_for_status()
        except Exception as exc:
            logger.debug("Jellyfin /Library/Refresh failed: {}", exc)

    def parse_webhook(
        self,
        payload: dict[str, Any] | bytes,
        headers: dict[str, str],
    ) -> WebhookEvent | None:
        """Normalise a jellyfin-plugin-webhook payload to a :class:`WebhookEvent`.

        The plugin's stock ``ItemAdded`` template emits:
        ``{"NotificationType": "ItemAdded", "ItemId": "...", "ItemType": "Episode", ...}``.
        We only act on ``ItemAdded`` (and Emby-flavoured ``library.new``
        if a user has copied an Emby template); anything else returns
        ``None``.
        """
        if isinstance(payload, bytes | bytearray):
            try:
                data = json.loads(payload.decode("utf-8", errors="replace"))
            except (json.JSONDecodeError, ValueError):
                return None
        elif isinstance(payload, dict):
            data = payload
        else:
            return None

        if not isinstance(data, dict):
            return None

        event_type = str(data.get("NotificationType") or data.get("Event") or "")
        if event_type.lower() not in {"itemadded", "library.new"}:
            return None

        item_id = str(data.get("ItemId") or data.get("Id") or "")
        return WebhookEvent(
            event_type=event_type,
            item_id=item_id or None,
            raw=data,
        )
