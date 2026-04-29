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

    def check_trickplay_extraction_status(self) -> list[dict[str, Any]]:
        """Return per-library trickplay-extraction flags.

        Why this exists: Jellyfin libraries default ``EnableTrickplayImageExtraction``
        to ``False``. With that flag off, Jellyfin **ignores sidecar
        trickplay files** in the media folder even if our publisher wrote
        them perfectly. The user sees no scrubbing thumbnails and reports
        the tool as broken, when actually a single library setting needs
        to be flipped.

        We surface this in the connection test so the UI can display a
        prominent warning and offer a one-click fix
        (:meth:`enable_trickplay_extraction`).

        Returns a list of ``{id, name, locations, extraction_enabled,
        scan_extraction_enabled}`` dicts — one per virtual folder. Empty
        list on failure (logged as a warning so we don't false-alarm
        the UI when the server's just unreachable).
        """
        try:
            response = self._request("GET", "/Library/VirtualFolders")
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            logger.warning(
                "Could not check Jellyfin trickplay settings on server {!r}: {}. "
                "The 'Fix trickplay' diagnostic is unavailable until Jellyfin is reachable — "
                "verify the server is running and your API key / token is valid.",
                self.name,
                exc,
            )
            return []

        if not isinstance(data, list):
            return []

        out: list[dict[str, Any]] = []
        for raw in data:
            if not isinstance(raw, dict):
                continue
            options = raw.get("LibraryOptions") or {}
            out.append(
                {
                    "id": str(raw.get("ItemId") or raw.get("Id") or raw.get("Name") or ""),
                    "name": str(raw.get("Name") or ""),
                    "locations": list(raw.get("Locations") or []),
                    # The runtime-enable flag is what gates Jellyfin's
                    # detection of *our* sidecar trickplay; the scan
                    # variant gates Jellyfin's own generation. Both
                    # need to be on for a smooth experience.
                    "extraction_enabled": bool(options.get("EnableTrickplayImageExtraction", False)),
                    "scan_extraction_enabled": bool(options.get("ExtractTrickplayImagesDuringLibraryScan", False)),
                }
            )
        return out

    def enable_trickplay_extraction(self, library_ids: list[str] | None = None) -> dict[str, str]:
        """Flip ``EnableTrickplayImageExtraction`` on for the given libraries.

        Called from the per-server "Fix it for me" UI button. ``library_ids``
        restricts which libraries to update; ``None`` means every library.

        For each target library we POST the **full existing**
        ``LibraryOptions`` dict back with the two trickplay flags
        flipped to true — Jellyfin's update endpoint is a wholesale
        replacement, not a diff, so any field we omit reverts to its
        default.

        Returns ``{library_id: "ok"|"<error message>"}`` so the UI can
        report partial success when one library succeeds and another
        fails.
        """
        try:
            response = self._request("GET", "/Library/VirtualFolders")
            response.raise_for_status()
            folders = response.json()
        except Exception as exc:
            return {"_global": f"failed to fetch libraries: {exc}"}

        results: dict[str, str] = {}
        if not isinstance(folders, list):
            return {"_global": "unexpected VirtualFolders response shape"}

        target_ids = set(library_ids) if library_ids else None
        for raw in folders:
            if not isinstance(raw, dict):
                continue
            lib_id = str(raw.get("ItemId") or raw.get("Id") or raw.get("Name") or "")
            if target_ids is not None and lib_id not in target_ids:
                continue

            options = dict(raw.get("LibraryOptions") or {})
            options["EnableTrickplayImageExtraction"] = True
            options["ExtractTrickplayImagesDuringLibraryScan"] = True

            try:
                update = self._request(
                    "POST",
                    "/Library/VirtualFolders/LibraryOptions",
                    json_body={"Id": lib_id, "LibraryOptions": options},
                )
                update.raise_for_status()
                results[lib_id] = "ok"
            except Exception as exc:
                logger.warning(
                    "Could not enable trickplay extraction on Jellyfin library {} (server {!r}): {}. "
                    "Other libraries may still be fixed — check the per-library results dict. "
                    "If this keeps happening, enable the flag manually in Jellyfin's web UI: "
                    "Dashboard → Libraries → edit library → 'Trickplay image extraction'.",
                    lib_id,
                    self.name,
                    exc,
                )
                results[lib_id] = f"error: {exc}"
        return results

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
