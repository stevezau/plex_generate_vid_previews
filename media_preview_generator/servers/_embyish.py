"""Shared base for Emby + Jellyfin clients.

Jellyfin forked from Emby; for the endpoints this tool touches the
REST surface is nearly identical (``/System/Info``, ``/Library/VirtualFolders``,
``/Items``, ``/Items/{id}``). The two concrete clients share ~90% of
their implementation. This base class hosts the common logic so each
subclass needs to specify only:

* :attr:`type` — the :class:`ServerType` enum value.
* :attr:`vendor_name` — the vendor brand for log strings.
* :meth:`trigger_refresh` — Emby has a path-based endpoint Jellyfin doesn't.
* :meth:`parse_webhook` — payload shapes differ.
"""

from __future__ import annotations

import os
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
)


class EmbyApiClient(MediaServer):
    """Base class for Emby and Jellyfin clients.

    Concrete subclasses set ``vendor_name`` (used in log strings) and
    override the bits that genuinely differ between the two vendors —
    ``trigger_refresh`` and ``parse_webhook``.
    """

    #: Display brand used in log lines (e.g. "Emby", "Jellyfin").
    vendor_name: str = "Media server"

    def __init__(self, config: ServerConfig, *, default_name: str | None = None) -> None:
        super().__init__(server_id=config.id, name=config.name or (default_name or self.vendor_name))
        self._config = config

    @property
    def config(self) -> ServerConfig:
        return self._config

    # ------------------------------------------------------------------ HTTP
    def _token(self) -> str:
        """Extract the X-Emby-Token value from the persisted auth dict.

        Both vendors accept either ``access_token`` (from the
        password / Quick Connect flow) or ``api_key`` (paste-in) on
        the legacy ``X-Emby-Token`` header.
        """
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
        """Issue an authenticated request against the server's HTTP API."""
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

    # ------------------------------------------------ Public query helpers
    def query_items(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        """Run a parameterised ``GET /Items`` query and return the ``Items`` list.

        Public counterpart to the private :meth:`_request` so callers in the
        :mod:`processing` package don't need to reach across module boundaries
        for the recently-added scan (``SortBy=DateCreated``) or any other
        ad-hoc Items-endpoint query both Emby and Jellyfin share.

        Returns the parsed ``Items`` array (empty list on transport failure;
        the failure is logged so the caller can stay quiet on routine
        outages without losing diagnostic signal).
        """
        try:
            response = self._request("GET", "/Items", params=params)
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:  # noqa: BLE001 — protocol contract is "empty list on failure"
            logger.warning(
                "Could not query /Items on {} server {!r} ({}: {}). "
                "Returning empty list — verify the server is reachable and the token is valid.",
                self.vendor_name,
                self._config.name or self._config.id,
                type(exc).__name__,
                exc,
            )
            return []
        items = payload.get("Items") if isinstance(payload, dict) else None
        return items if isinstance(items, list) else []

    # ------------------------------------------------------------ MediaServer
    def test_connection(self) -> ConnectionResult:
        """Probe ``/System/Info`` for identity and credential validation."""
        if not self._config.url:
            return ConnectionResult(ok=False, message=f"{self.vendor_name} URL is required")
        if not self._token():
            return ConnectionResult(ok=False, message=f"{self.vendor_name} access token / API key is required")

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
            return ConnectionResult(ok=False, message=f"SSL certificate verification failed: {exc}")
        except requests.exceptions.Timeout:
            return ConnectionResult(
                ok=False,
                message=f"Connection to {self._config.url} timed out",
            )
        except requests.exceptions.ConnectionError as exc:
            return ConnectionResult(
                ok=False,
                message=f"Could not connect to {self.vendor_name} at {self._config.url}: {exc}",
            )
        except requests.exceptions.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            if status == 401:
                msg = f"{self.vendor_name} rejected the access token (401)"
            elif status == 403:
                msg = f"Access denied by {self.vendor_name} server (403)"
            else:
                msg = f"{self.vendor_name} returned HTTP {status}"
            return ConnectionResult(ok=False, message=msg)
        except (ValueError, requests.RequestException) as exc:
            return ConnectionResult(ok=False, message=f"Connection test failed: {exc}")

    def list_libraries(self) -> list[Library]:
        """List "Virtual Folders" with their folder paths.

        Both vendors expose ``/Library/VirtualFolders`` returning each
        library's name, id, and one or more ``Locations`` (server-side
        paths). Per-library ``enabled`` is sourced from the existing
        snapshot in ``self._config.libraries`` so the user's toggle
        survives a refresh.
        """
        try:
            response = self._request("GET", "/Library/VirtualFolders")
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            logger.warning(
                "Could not fetch the library list from {} server {!r}: {}. "
                "Verify the server is running and your access token / API key in "
                "Settings → Media Servers is still valid.",
                self.vendor_name,
                self.name,
                exc,
            )
            return []

        if not isinstance(data, list):
            logger.warning(
                "{} server {!r} returned an unexpected library list — the response wasn't in the format we expected. "
                "This usually means the server is misconfigured or running a version this app doesn't support. "
                "Library scanning is paused for this server; other servers continue normally. "
                "Check the server's version, then restart it and try again.",
                self.vendor_name,
                self.name,
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
            logger.warning(
                "Could not list items in {} library {} ({}: {}). "
                "This library will be skipped for this run — verify the server is reachable, the API key / token "
                "is still valid, and that the library hasn't been deleted on the server side.",
                self.vendor_name,
                library_id,
                type(exc).__name__,
                exc,
            )
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
        """Return ``MediaSources[0].Path`` (or top-level ``Path``) for ``item_id``.

        Three vendor-specific quirks need accommodating:

        * Emby's bare ``/Items/{id}`` returns 404 when no user context is
          attached → use the per-user endpoint
          ``/Users/{userId}/Items/{id}`` whenever a ``user_id`` was
          captured (password auth flow).
        * Jellyfin's bare ``/Items/{id}`` returns **400** under any auth
          shape (the route signature changed across versions).
        * The plural ``/Items?Ids={id}`` works for both vendors and both
          auth shapes; we use it as the universal fallback.

        Prefers ``MediaSources[0].Path`` over the top-level ``Path``
        because some item types only populate the media source.
        """
        user_id = self._user_id()
        if user_id:
            primary_path = f"/Users/{user_id}/Items/{item_id}"
            primary_params = {"Fields": "Path,MediaSources"}
            primary_unwrap = lambda data: data  # noqa: E731 — single-item endpoint returns the item directly
        else:
            primary_path = "/Items"
            primary_params = {"Ids": item_id, "Fields": "Path,MediaSources"}

            def primary_unwrap(data):
                items = data.get("Items") or []
                return items[0] if items else {}

        try:
            response = self._request("GET", primary_path, params=primary_params)
            response.raise_for_status()
            data = primary_unwrap(response.json())
        except Exception as exc:
            logger.debug("{} item lookup failed for {}: {}", self.vendor_name, item_id, exc)
            return None

        if not isinstance(data, dict):
            return None

        for source in data.get("MediaSources", []) or []:
            if isinstance(source, dict):
                path = str(source.get("Path") or "")
                if path:
                    return path

        path = str(data.get("Path") or "")
        return path or None

    def resolve_remote_path_to_item_id(self, remote_path: str) -> str | None:
        """Search for an item whose stored ``Path`` matches ``remote_path``.

        Best-effort: matches on basename and verifies via the trailing
        two path components (parent dir + basename). Accurate enough
        for the typical ``/library/Show/Season X/Episode.mkv`` layout
        but **does not** translate canonical-local paths through the
        server's path mappings — callers that already have the
        server-view path get an exact match; callers that pass a
        canonical-local path rely on the basename match working.
        """
        basename = os.path.basename(remote_path or "")
        if not basename:
            return None

        stem = os.path.splitext(basename)[0]
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
            logger.debug("{} reverse-lookup search failed for {}: {}", self.vendor_name, remote_path, exc)
            return None

        target_tail = "/".join(remote_path.rstrip("/").split("/")[-2:])
        for raw in data.get("Items", []) or []:
            if not isinstance(raw, dict):
                continue
            path = str(raw.get("Path") or "")
            if not path:
                continue
            if os.path.basename(path) == basename and path.replace("\\", "/").endswith(target_tail):
                item_id = str(raw.get("Id") or "")
                if item_id:
                    return item_id
        return None


def _format_emby_title(item: dict[str, Any]) -> str:
    """Build a human-readable title for an Emby/Jellyfin item.

    For episodes, returns ``"<Series> S01E02"``; for movies and other
    item types, returns the raw ``Name``. The two vendors share this
    convention (Jellyfin forked from Emby), so a single helper covers
    both.
    """
    item_type = str(item.get("Type") or "")
    name = str(item.get("Name") or "")
    if item_type == "Episode":
        series = str(item.get("SeriesName") or "")
        season = item.get("ParentIndexNumber")
        episode = item.get("IndexNumber")
        if series and season is not None and episode is not None:
            return f"{series} S{int(season):02d}E{int(episode):02d}"
    return name
