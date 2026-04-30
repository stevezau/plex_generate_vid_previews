"""Shared recently-added scanner for Emby + Jellyfin.

Both vendors expose the same ``/Items?SortBy=DateCreated`` endpoint
on the same JSON shape — the only difference between them sits at
the auth header level (handled by ``_request`` in
:mod:`media_preview_generator.servers._embyish`). This module hosts
the shared scan so neither :mod:`.emby` nor :mod:`.jellyfin` has to
repeat it.

Library enumeration + item walking + path resolution come from
:class:`._shared._MediaServerProcessor` — those work the same for any
vendor with a ``MediaServer`` adapter.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from typing import Any

from loguru import logger

from ..servers._embyish import EmbyApiClient
from ..servers.base import ServerConfig
from ._shared import _MediaServerProcessor
from .types import ProcessableItem


class _EmbyishProcessor(_MediaServerProcessor):
    """Adds the shared Emby/Jellyfin recently-added scan to the base."""

    def _make_client(self, server_config: ServerConfig) -> EmbyApiClient:  # pragma: no cover - overridden
        raise NotImplementedError

    def scan_recently_added(
        self,
        server_config: ServerConfig,
        *,
        lookback_hours: int,
        library_ids: list[str] | None = None,
    ) -> Iterator[ProcessableItem]:
        """Walk items added in the last ``lookback_hours``.

        Uses ``/Items?SortBy=DateCreated&SortOrder=Descending`` and filters
        client-side. Both vendors return ``DateCreated`` in ISO format on
        the item payload.
        """
        client: EmbyApiClient = self._make_client(server_config)
        params: dict[str, Any] = {
            "IncludeItemTypes": "Movie,Episode",
            "Recursive": "true",
            "Fields": "Path,DateCreated",
            "SortBy": "DateCreated",
            "SortOrder": "Descending",
            "Limit": 500,
        }
        if library_ids:
            params["ParentId"] = ",".join(library_ids)

        cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)

        try:
            response = client._request("GET", "/Items", params=params)  # noqa: SLF001 — sibling-module access by design
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Could not query recently-added items on {} server {!r}: {}. "
                "This scheduled run will produce no items — verify the URL "
                "and token, then try the next tick.",
                self.vendor_name,
                server_config.name or server_config.id,
                exc,
            )
            return

        for raw in payload.get("Items", []) or []:
            if not isinstance(raw, dict):
                continue
            created_str = str(raw.get("DateCreated") or "")
            if not created_str or not _within_lookback(created_str, cutoff):
                continue
            path = str(raw.get("Path") or "")
            if not path:
                continue
            for canonical in self._canonical_paths_for(path, server_config):
                yield ProcessableItem(
                    canonical_path=canonical,
                    server_id=server_config.id,
                    item_id_by_server={server_config.id: str(raw.get("Id") or "")},
                    title=_format_title(raw),
                    library_id=str(raw.get("ParentId") or "") or None,
                )


def _within_lookback(created_iso: str, cutoff: datetime) -> bool:
    """Parse Emby/Jellyfin ISO datetime + decide if it lies after ``cutoff``."""
    candidate = created_iso.strip()
    if not candidate:
        return False
    # Both vendors emit ``2026-04-30T13:35:43.1234567Z`` style strings.
    try:
        if candidate.endswith("Z"):
            candidate = candidate[:-1] + "+00:00"
        # Drop sub-microsecond precision (Python's datetime can't parse 7+ digits).
        if "." in candidate:
            head, _, rest = candidate.partition(".")
            frac, sign, tz = (rest, "", "")
            for marker in ("+", "-"):
                if marker in rest:
                    frac, sign, tz = rest.partition(marker)
                    break
            frac = frac[:6]
            candidate = f"{head}.{frac}{sign}{tz}" if sign else f"{head}.{frac}"
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed >= cutoff


def _format_title(raw: dict[str, Any]) -> str:
    """Build a display title from an Emby/Jellyfin item dict."""
    name = str(raw.get("Name") or "")
    series = str(raw.get("SeriesName") or "")
    if series:
        season = raw.get("ParentIndexNumber")
        episode = raw.get("IndexNumber")
        if season is not None and episode is not None:
            return f"{series} - S{int(season):02d}E{int(episode):02d} - {name}"
        return f"{series} - {name}"
    return name or str(raw.get("Path") or "<unknown>")
