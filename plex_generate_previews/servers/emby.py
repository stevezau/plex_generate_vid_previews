"""Emby implementation of the :class:`MediaServer` interface.

Emby exposes a REST API broadly compatible with Jellyfin's (Jellyfin forked
from Emby). For this tool's purposes the surface is small: a handful of
endpoints for authentication, library enumeration, item lookup, and a scan
trigger. We use plain ``requests`` rather than a generated client to keep
the dependency surface minimal.

Authentication:
    The wrapper accepts either a paste-in API key or a method-tagged
    ``access_token``/``user_id`` pair captured from a ``/Users/AuthenticateByName``
    flow. Either is sent via the ``X-Emby-Token`` header.

Webhooks:
    The Emby Webhooks plugin emits Plex-format-compatible JSON. The
    payload only carries an ``ItemId`` (no path), so the dispatcher
    follows up with :meth:`resolve_item_to_remote_path` before publishing.
"""

from __future__ import annotations

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


class EmbyServer(MediaServer):
    """Wrap a single Emby Server in the :class:`MediaServer` interface.

    Constructed from a :class:`ServerConfig` (auth, URL, libraries, etc.).
    Unlike :class:`PlexServer`, no ``plexapi``-equivalent library exists
    that's worth a dependency, so all endpoints go through ``requests``.

    Args:
        config: The persisted server configuration. ``config.auth`` is
            interpreted as ``{"method": "api_key", "api_key": ...}`` or
            ``{"method": "password", "access_token": ..., "user_id": ...}``;
            either token shape is accepted via ``X-Emby-Token``.
    """

    def __init__(self, config: ServerConfig) -> None:
        super().__init__(server_id=config.id, name=config.name or "Emby")
        self._config = config

    @property
    def type(self) -> ServerType:
        return ServerType.EMBY

    @property
    def config(self) -> ServerConfig:
        return self._config

    # ------------------------------------------------------------------ HTTP
    def _token(self) -> str:
        """Extract the X-Emby-Token value from the persisted auth dict."""
        auth = self._config.auth or {}
        return str(auth.get("access_token") or auth.get("api_key") or auth.get("token") or "")

    def _user_id(self) -> str | None:
        """Optional user id used by per-user endpoints."""
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
        """Issue an authenticated request against the Emby HTTP API."""
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
        """Probe ``/System/Info`` for the server identity.

        ``/System/Info`` requires an authenticated token; this also
        validates that the credential the user supplied is actually
        accepted by the server. ``/System/Info/Public`` is reachable
        anonymously but doesn't tell us whether auth works.
        """
        if not self._config.url:
            return ConnectionResult(ok=False, message="Emby URL is required")
        if not self._token():
            return ConnectionResult(ok=False, message="Emby access token / API key is required")

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
                message=f"Could not connect to Emby at {self._config.url}: {exc}",
            )
        except requests.exceptions.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            if status == 401:
                msg = "Emby rejected the access token (401)"
            elif status == 403:
                msg = "Access denied by Emby server (403)"
            else:
                msg = f"Emby returned HTTP {status}"
            return ConnectionResult(ok=False, message=msg)
        except (ValueError, requests.RequestException) as exc:
            return ConnectionResult(ok=False, message=f"Connection test failed: {exc}")

    def list_libraries(self) -> list[Library]:
        """List Emby's libraries (called "Virtual Folders") with their folder paths.

        Uses ``/Library/VirtualFolders`` which returns each library's name,
        id (``ItemId``), and one or more ``Locations`` (server-side paths).
        Per-library ``enabled`` is sourced from the existing snapshot in
        ``self._config.libraries`` so the user's toggle survives a refresh.
        """
        try:
            response = self._request("GET", "/Library/VirtualFolders")
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            logger.warning("Failed to list Emby virtual folders: {}", exc)
            return []

        if not isinstance(data, list):
            logger.warning("Emby VirtualFolders endpoint returned unexpected shape: {}", type(data).__name__)
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
        """Yield every video :class:`MediaItem` inside the given library.

        Uses ``GET /Items?ParentId=<lib>&IncludeItemTypes=Movie,Episode&Recursive=true&Fields=Path``
        — Emby paginates by default but the page size is large enough that
        a single call covers most installations. Iteration is implemented
        for completeness; multi-page support arrives if real-world tests
        prove it necessary.
        """
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
            logger.warning("Failed to list Emby items for library {}: {}", library_id, exc)
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
                title=_format_emby_title(raw),
                remote_path=path,
            )

    def resolve_item_to_remote_path(self, item_id: str) -> str | None:
        """Return ``item.MediaSources[0].Path`` for ``item_id`` or ``None``.

        ``GET /Items/{id}?Fields=Path,MediaSources`` exposes the full media
        path. We prefer ``MediaSources[0].Path`` over the top-level ``Path``
        when both exist because some Emby item types only populate the
        media source.
        """
        try:
            response = self._request("GET", f"/Items/{item_id}", params={"Fields": "Path,MediaSources"})
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            logger.debug("Emby item lookup failed for {}: {}", item_id, exc)
            return None

        for source in data.get("MediaSources", []) or []:
            if isinstance(source, dict):
                path = str(source.get("Path") or "")
                if path:
                    return path

        path = str(data.get("Path") or "")
        return path or None

    def resolve_remote_path_to_item_id(self, remote_path: str) -> str | None:
        """Search Emby for an item whose stored ``Path`` matches ``remote_path``.

        Used by the secondary-publisher fan-out from the legacy single-Plex
        scan path: when Plex finishes processing a file at the canonical
        local path, we ask Emby (and Jellyfin) for their item id so the
        manifest-keyed adapters (Jellyfin) can publish without waiting for
        a vendor-native webhook.

        The lookup is by **basename** (we don't know whether ``remote_path``
        is canonical-local or server-view), then we verify the match by
        comparing the trailing two path components — accurate enough for
        the typical ``/library/Show/Season X/EpisodeName.mkv`` layout.
        """
        import os as _os

        basename = _os.path.basename(remote_path or "")
        if not basename:
            return None

        stem = _os.path.splitext(basename)[0]
        params = {
            "searchTerm": stem,
            "Recursive": "true",
            "IncludeItemTypes": "Movie,Episode",
            "Fields": "Path",
            "Limit": 50,
        }
        try:
            response = self._request("GET", "/Items", params=params)
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            logger.debug("Emby reverse-lookup search failed for {}: {}", remote_path, exc)
            return None

        # Compare the final two components (parent dir + basename) so we
        # don't false-match on episode names that recur across shows.
        target_tail = "/".join(remote_path.rstrip("/").split("/")[-2:])
        for raw in data.get("Items", []) or []:
            if not isinstance(raw, dict):
                continue
            path = str(raw.get("Path") or "")
            if not path:
                continue
            if _os.path.basename(path) == basename and path.replace("\\", "/").endswith(target_tail):
                item_id = str(raw.get("Id") or "")
                if item_id:
                    return item_id
        return None

    def trigger_refresh(self, *, item_id: str | None, remote_path: str | None) -> None:
        """Notify Emby that a media path changed.

        Prefers ``POST /Library/Media/Updated`` (path-based; matches our
        path-centric dispatcher). Falls back to a per-item refresh when
        only an item id is available.
        """
        if remote_path:
            try:
                response = self._request(
                    "POST",
                    "/Library/Media/Updated",
                    json_body={"Updates": [{"Path": remote_path, "UpdateType": "Modified"}]},
                )
                response.raise_for_status()
                return
            except Exception as exc:
                logger.debug("Emby /Library/Media/Updated failed for {}: {}", remote_path, exc)

        if item_id:
            try:
                response = self._request("POST", f"/Items/{item_id}/Refresh")
                response.raise_for_status()
            except Exception as exc:
                logger.debug("Emby item refresh failed for {}: {}", item_id, exc)

    def parse_webhook(
        self,
        payload: dict[str, Any] | bytes,
        headers: dict[str, str],
    ) -> WebhookEvent | None:
        """Normalise an Emby Webhooks plugin payload to a :class:`WebhookEvent`.

        The plugin emits a Plex-format-compatible JSON envelope:
        ``{"Event": "library.new", "Item": {"Id": "..."}, "Server": {...}}``.
        We only act on ``library.new`` (or the equivalent ``ItemAdded``
        event some plugin builds emit); playback events are ignored.
        """
        import json

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

        event_type = str(data.get("Event") or data.get("event") or data.get("NotificationType") or "")
        if event_type.lower() not in {"library.new", "itemadded"}:
            return None

        item = data.get("Item") or data.get("Metadata") or {}
        item_id = str(item.get("Id") or item.get("guid") or "") if isinstance(item, dict) else ""

        return WebhookEvent(
            event_type=event_type,
            item_id=item_id or None,
            raw=data,
        )


def _format_emby_title(item: dict[str, Any]) -> str:
    """Build a human-readable title for an Emby item, mirroring Plex's flow."""
    item_type = str(item.get("Type") or "")
    name = str(item.get("Name") or "")
    if item_type == "Episode":
        series = str(item.get("SeriesName") or "")
        season = item.get("ParentIndexNumber")
        episode = item.get("IndexNumber")
        if series and season is not None and episode is not None:
            return f"{series} S{int(season):02d}E{int(episode):02d}"
    return name
