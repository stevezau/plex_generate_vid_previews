"""Jellyfin implementation of the :class:`MediaServer` interface.

Jellyfin forked from Emby; the REST API surface is nearly identical for
the endpoints this tool touches. Differences worth flagging:

* Authentication has an extra friendly path — Jellyfin's "Quick Connect"
  feature lets the user authorise our session by entering a 6-character
  code in their Jellyfin web UI (no password ever leaves the browser).
  When Quick Connect isn't available, we fall back to
  ``/Users/AuthenticateByName`` (same shape as Emby) or a directly
  pasted API key.
* No equivalent of Emby's ``/Library/Media/Updated`` path-based refresh
  endpoint. We use ``POST /Items/{id}/Refresh`` when an item id is
  known and ``POST /Library/Refresh`` (full library scan) as a fallback.
* The webhook plugin (``jellyfin-plugin-webhook``) emits Handlebars-
  templated JSON; the default ``ItemAdded`` template carries
  ``ItemId``/``ItemType``/``ServerId`` but **not** the file path, so
  the dispatcher follows up with :meth:`resolve_item_to_remote_path`.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import requests
import urllib3
from loguru import logger

from .base import (
    ConnectionResult,
    Library,
    MediaItem,
    MediaServer,
    ServerConfig,
    ServerType,
    WebhookEvent,
)


class JellyfinServer(MediaServer):
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

    def __init__(self, config: ServerConfig) -> None:
        super().__init__(server_id=config.id, name=config.name or "Jellyfin")
        self._config = config

    @property
    def type(self) -> ServerType:
        return ServerType.JELLYFIN

    @property
    def config(self) -> ServerConfig:
        return self._config

    # ------------------------------------------------------------------ HTTP
    def _token(self) -> str:
        auth = self._config.auth or {}
        return str(auth.get("access_token") or auth.get("api_key") or auth.get("token") or "")

    def _user_id(self) -> str | None:
        auth = self._config.auth or {}
        user_id = auth.get("user_id")
        return str(user_id) if user_id else None

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
    ) -> requests.Response:
        url = f"{self._config.url.rstrip('/')}{path}"
        headers = {
            "X-Emby-Token": self._token(),
            "Accept": "application/json",
        }
        verify = bool(self._config.verify_ssl)
        if not verify:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        return requests.request(
            method,
            url,
            headers=headers,
            params=params,
            json=json_body,
            timeout=int(self._config.timeout or 30),
            verify=verify,
        )

    # ------------------------------------------------------------ MediaServer
    def test_connection(self) -> ConnectionResult:
        """Probe ``/System/Info`` for identity and credential validation.

        ``/System/Info`` requires authentication, so a successful response
        also confirms the configured token is accepted.
        """
        if not self._config.url:
            return ConnectionResult(ok=False, message="Jellyfin URL is required")
        if not self._token():
            return ConnectionResult(ok=False, message="Jellyfin access token / API key is required")

        try:
            response = self._request("GET", "/System/Info")
            response.raise_for_status()
            data = response.json()
            return ConnectionResult(
                ok=True,
                server_id=str(data.get("Id") or "") or None,
                server_name=str(data.get("ServerName") or "") or None,
                version=str(data.get("Version") or "") or None,
                message="Connected",
            )
        except requests.exceptions.SSLError as exc:
            return ConnectionResult(
                ok=False,
                message=f"SSL certificate verification failed: {exc}",
            )
        except requests.exceptions.Timeout:
            return ConnectionResult(
                ok=False,
                message=f"Connection to {self._config.url} timed out",
            )
        except requests.exceptions.ConnectionError as exc:
            return ConnectionResult(
                ok=False,
                message=f"Could not connect to Jellyfin at {self._config.url}: {exc}",
            )
        except requests.exceptions.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            if status == 401:
                msg = "Jellyfin rejected the access token (401)"
            elif status == 403:
                msg = "Access denied by Jellyfin server (403)"
            else:
                msg = f"Jellyfin returned HTTP {status}"
            return ConnectionResult(ok=False, message=msg)
        except (ValueError, requests.RequestException) as exc:
            return ConnectionResult(ok=False, message=f"Connection test failed: {exc}")

    def list_libraries(self) -> list[Library]:
        """List Jellyfin's "Virtual Folders" with their folder paths.

        Same endpoint shape as Emby (``/Library/VirtualFolders``); the
        wrapper keeps the user's per-library ``enabled`` toggle across
        refreshes.
        """
        try:
            response = self._request("GET", "/Library/VirtualFolders")
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            logger.warning("Failed to list Jellyfin virtual folders: {}", exc)
            return []

        if not isinstance(data, list):
            logger.warning(
                "Jellyfin VirtualFolders returned unexpected shape: {}",
                type(data).__name__,
            )
            return []

        existing_enabled = {lib.id: lib.enabled for lib in self._config.libraries}
        libraries: list[Library] = []
        for raw in data:
            if not isinstance(raw, dict):
                continue
            lib_id = str(raw.get("ItemId") or raw.get("Id") or raw.get("Name") or "")
            name = str(raw.get("Name") or "")
            locations = tuple(str(loc) for loc in (raw.get("Locations") or []))
            kind = str(raw.get("CollectionType") or "") or None
            enabled = existing_enabled.get(lib_id, True)
            libraries.append(
                Library(
                    id=lib_id,
                    name=name,
                    remote_paths=locations,
                    enabled=enabled,
                    kind=kind,
                )
            )
        return libraries

    def list_items(self, library_id: str) -> Iterator[MediaItem]:
        """Yield every video :class:`MediaItem` inside the given library."""
        params = {
            "ParentId": library_id,
            "IncludeItemTypes": "Movie,Episode",
            "Recursive": "true",
            "Fields": "Path",
            "Limit": 5000,
        }
        try:
            response = self._request("GET", "/Items", params=params)
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            logger.warning("Failed to list Jellyfin items for library {}: {}", library_id, exc)
            return

        for raw in payload.get("Items", []) or []:
            if not isinstance(raw, dict):
                continue
            path = str(raw.get("Path") or "")
            if not path:
                continue
            yield MediaItem(
                id=str(raw.get("Id") or ""),
                library_id=library_id,
                title=_format_jellyfin_title(raw),
                remote_path=path,
            )

    def resolve_item_to_remote_path(self, item_id: str) -> str | None:
        """Return ``MediaSources[0].Path`` (or top-level ``Path``) for ``item_id``."""
        try:
            response = self._request(
                "GET",
                f"/Items/{item_id}",
                params={"Fields": "Path,MediaSources"},
            )
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            logger.debug("Jellyfin item lookup failed for {}: {}", item_id, exc)
            return None

        for source in data.get("MediaSources", []) or []:
            if isinstance(source, dict):
                path = str(source.get("Path") or "")
                if path:
                    return path

        path = str(data.get("Path") or "")
        return path or None

    def trigger_refresh(self, *, item_id: str | None, remote_path: str | None) -> None:
        """Notify Jellyfin to re-scan an item.

        Jellyfin has no equivalent of Emby's path-based ``/Library/Media/Updated``,
        so we prefer the per-item ``/Items/{id}/Refresh`` endpoint when the
        item id is known and fall back to a full ``/Library/Refresh``
        scan otherwise. Failures are silently swallowed — the publishing
        side already wrote the trickplay tiles next to the media; the
        scan trigger is best-effort.
        """
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


def _format_jellyfin_title(item: dict[str, Any]) -> str:
    """Build a human-readable title; identical pattern to Emby."""
    item_type = str(item.get("Type") or "")
    name = str(item.get("Name") or "")
    if item_type == "Episode":
        series = str(item.get("SeriesName") or "")
        season = item.get("ParentIndexNumber")
        episode = item.get("IndexNumber")
        if series and season is not None and episode is not None:
            return f"{series} S{int(season):02d}E{int(episode):02d}"
    return name
