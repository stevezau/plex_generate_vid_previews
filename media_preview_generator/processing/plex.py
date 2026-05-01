"""Plex :class:`VendorProcessor` implementation.

Library enumeration + path resolution come from the shared
:class:`._shared._MediaServerProcessor` (via the existing
``PlexServer.list_libraries()`` / ``list_items()`` adapter methods).

Plex's recently-added scan uses ``plex.library.recentlyAdded()``
through the existing plexapi-flavoured client, which differs from
Emby/Jellyfin's REST endpoint enough that it lives directly in this
module rather than the shared helper.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone

from loguru import logger

from ..plex_client import _build_episode_title, _extract_item_locations, retry_plex_call
from ..servers.base import MediaServer, ServerConfig, ServerType
from ..servers.plex import PlexServer, _plex_item_id
from ._shared import _MediaServerProcessor
from .registry import register_processor
from .types import ProcessableItem


class PlexProcessor(_MediaServerProcessor):
    vendor_name = "Plex"

    def _make_client(self, server_config: ServerConfig) -> MediaServer:
        """Return a :class:`PlexServer` instance bound to ``server_config``."""
        return PlexServer(server_config)

    def scan_recently_added(
        self,
        server_config: ServerConfig,
        *,
        lookback_hours: int,
        library_ids: list[str] | None = None,
    ) -> Iterator[ProcessableItem]:
        """Walk Plex sections for items added in the last ``lookback_hours``.

        Uses the existing plexapi-flavoured ``section.search(filters=...)``
        path with the same ``addedAt>>`` filter the historical Plex-only
        scanner used; falls back to a sort-by-addedAt scan + client-side
        filter when the filter syntax isn't supported (some plexapi/PMS
        combinations reject it for certain section types).
        """
        client = PlexServer(server_config)
        try:
            plex = client._connect()  # noqa: SLF001 — sibling-module access by design
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Could not connect to Plex server {!r} for recently-added scan: {}. "
                "This scheduled run will produce no items.",
                server_config.name or server_config.id,
                exc,
            )
            return

        try:
            sections = retry_plex_call(plex.library.sections)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Could not list Plex sections on server {!r}: {}.",
                server_config.name or server_config.id,
                exc,
            )
            return

        wanted_ids: set[str] | None = set(library_ids) if library_ids else None
        cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
        cutoff_naive = cutoff.replace(tzinfo=None)

        for section in sections:
            section_id = str(getattr(section, "key", "") or "")
            if wanted_ids is not None and section_id not in wanted_ids:
                continue

            metadata_type = getattr(section, "METADATA_TYPE", "")
            if metadata_type not in {"movie", "episode"}:
                continue
            libtype = "movie" if metadata_type == "movie" else "episode"

            try:
                results = retry_plex_call(
                    section.search,
                    libtype=libtype,
                    filters={"addedAt>>": cutoff},
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "Plex addedAt filter failed on {!r} ({}); falling back to sort-by-addedAt scan",
                    getattr(section, "title", "?"),
                    exc,
                )
                try:
                    results = retry_plex_call(
                        section.search,
                        libtype=libtype,
                        sort="addedAt:desc",
                    )
                except Exception as exc2:  # noqa: BLE001
                    logger.warning(
                        "Recently-added scan: could not search Plex library {!r} ({}: {}). "
                        "Skipping this library for this run.",
                        getattr(section, "title", "?"),
                        type(exc2).__name__,
                        exc2,
                    )
                    continue

            for item in results or []:
                added_at = getattr(item, "addedAt", None)
                if isinstance(added_at, datetime):
                    added_naive = added_at.replace(tzinfo=None) if added_at.tzinfo else added_at
                    if added_naive < cutoff_naive:
                        # Newest-first iteration; below the window means stop.
                        break
                locations = _extract_item_locations(item)
                if not locations:
                    continue
                title = _build_episode_title(item) if libtype == "episode" else str(getattr(item, "title", "") or "")
                # Use the bare ratingKey, not item.key. PlexAPI's m.key is the
                # URL "/library/metadata/<id>"; passing that downstream would
                # double the prefix when PlexBundleAdapter builds
                # /library/metadata/{item_id}/tree, which silently 404s and
                # marks every recently-added item as SKIPPED_NOT_INDEXED.
                # See servers/plex.py::_plex_item_id for the same gotcha on
                # the full-library scan path.
                item_id = _plex_item_id(item)
                for canonical in self._canonical_paths_for(str(locations[0]), server_config):
                    yield ProcessableItem(
                        canonical_path=canonical,
                        server_id=server_config.id,
                        item_id_by_server={server_config.id: item_id} if item_id else {},
                        title=title,
                        library_id=section_id or None,
                    )


register_processor(ServerType.PLEX, PlexProcessor())
