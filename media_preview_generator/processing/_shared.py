"""Shared :class:`VendorProcessor` logic for every vendor.

The pieces that only need :class:`MediaServer`'s public interface
(``list_libraries`` / ``list_items`` / ``resolve_item_to_remote_path``)
live here. Subclasses override exactly two methods:

* :meth:`_make_client` — vendor-specific :class:`MediaServer` factory.
* :meth:`scan_recently_added` — vendor-specific recently-added query
  (plexapi for Plex; ``/Items?SortBy=DateCreated`` for Emby/Jellyfin).

Mirrors the per-vendor pattern in ``servers/`` and ``output/`` while
keeping every byte of duplicated code out of the per-vendor modules.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

from loguru import logger

from ..servers.base import Library, MediaServer, ServerConfig
from ..servers.ownership import apply_path_mappings
from .types import ProcessableItem


class _MediaServerProcessor:
    """Reusable base for processors that wrap a :class:`MediaServer`.

    Most of :class:`VendorProcessor` is vendor-agnostic the moment a
    :class:`MediaServer` is in hand — every adapter already exposes
    ``list_libraries``, ``list_items``, and ``resolve_item_to_remote_path``
    in the same shape. Subclasses only have to identify the vendor and
    teach the recently-added scan how the vendor sorts by added-date.
    """

    vendor_name: str = "Media server"

    def _make_client(self, server_config: ServerConfig) -> MediaServer:  # pragma: no cover - overridden
        """Construct the live :class:`MediaServer` for this config."""
        raise NotImplementedError

    # --------------------------------------------------------------- helpers
    def _canonical_paths_for(self, remote_path: str, server_config: ServerConfig) -> list[str]:
        """Apply the server's path_mappings to a single remote path.

        Mirrors the dispatcher's path-resolution semantics — same helper,
        same fall-back-to-raw-path behaviour — so the publisher and the
        enumerator can never disagree on the canonical form.
        """
        return apply_path_mappings(remote_path, server_config.path_mappings or [])

    # -------------------------------------------------------- VendorProcessor
    def list_libraries(self, server_config: ServerConfig) -> list[Library]:
        client = self._make_client(server_config)
        try:
            return client.list_libraries()
        except Exception as exc:  # noqa: BLE001 — protocol contract is "empty list on failure"
            logger.warning(
                "Could not list libraries on {} server {!r}: {}. "
                "Scans will skip this server until the API call succeeds — "
                "verify the URL, token, and that the user account has access "
                "to the libraries.",
                self.vendor_name,
                server_config.name or server_config.id,
                exc,
            )
            return []

    def list_canonical_paths(
        self,
        server_config: ServerConfig,
        *,
        library_ids: list[str] | None = None,
        cancel_check: Callable[[], bool] | None = None,
        progress_callback: Callable[[int, int, str], None] | None = None,
    ) -> Iterator[ProcessableItem]:
        client = self._make_client(server_config)

        try:
            available = client.list_libraries()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Could not list libraries on {} server {!r}: {}. Full-library scan aborted for this server.",
                self.vendor_name,
                server_config.name or server_config.id,
                exc,
            )
            return

        wanted_ids: set[str] | None = set(library_ids) if library_ids else None

        def _is_target(lib: Library) -> bool:
            if not lib.enabled:
                return False
            if wanted_ids is not None and lib.id not in wanted_ids:
                return False
            return True

        targets = [lib for lib in available if _is_target(lib)]
        total_libraries = len(targets)
        if total_libraries == 0:
            logger.info(
                "No libraries to scan on {} server {!r} (library_ids={}).",
                self.vendor_name,
                server_config.name or server_config.id,
                library_ids,
            )
            return

        for lib_index, library in enumerate(targets, start=1):
            if cancel_check is not None and cancel_check():
                logger.info("Cancellation requested — stopping {} scan.", self.vendor_name)
                return
            # Announce the per-library query at INFO so both the app
            # log and the per-job log panel show user-meaningful
            # progress. Large TV libraries (10k+ items) take 30-120s
            # to enumerate; without this the UI appears frozen.
            logger.info(
                "Querying library {}/{}: {!r} (this can take a while for large libraries)",
                lib_index,
                total_libraries,
                library.name,
            )
            if progress_callback is not None:
                progress_callback(
                    lib_index,
                    total_libraries,
                    f"Querying {library.name} ({lib_index}/{total_libraries})…",
                )

            try:
                items_iter = client.list_items(library.id)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Could not list items in {} library {} ({!r}): {}. Continuing with the next library.",
                    self.vendor_name,
                    library.name,
                    library.id,
                    exc,
                )
                continue

            items_yielded = 0
            for media_item in items_iter:
                if cancel_check is not None and cancel_check():
                    logger.info("Cancellation requested — stopping {} scan.", self.vendor_name)
                    return
                for processable in self._yield_processable_for(media_item, library, server_config):
                    items_yielded += 1
                    yield processable
            # Post-library summary so the log tells a complete story:
            # "Querying TV Shows…" → "Found 12,458 items in TV Shows".
            logger.info(
                "Found {} item(s) in {!r} (library {}/{})",
                items_yielded,
                library.name,
                lib_index,
                total_libraries,
            )

    def scan_recently_added(  # pragma: no cover - overridden
        self,
        server_config: ServerConfig,
        *,
        lookback_hours: int,
        library_ids: list[str] | None = None,
    ) -> Iterator[ProcessableItem]:
        """Vendor-specific recently-added query — subclasses must override.

        Plex uses ``plex.library.recentlyAdded()``; Emby/Jellyfin both use
        ``/Items?SortBy=DateCreated``. Implementations must filter to the
        ``lookback_hours`` window themselves and yield ProcessableItems
        with canonical_path already path-mapped.
        """
        raise NotImplementedError

    def resolve_canonical_path(
        self,
        server_config: ServerConfig,
        *,
        item_id: str,
    ) -> str | None:
        """Vendor item-id → local canonical path. Used by webhook flows."""
        client = self._make_client(server_config)
        try:
            remote_path = client.resolve_item_to_remote_path(item_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Could not resolve {} item id {!r} on server {!r}: {}",
                self.vendor_name,
                item_id,
                server_config.name or server_config.id,
                exc,
            )
            return None
        if not remote_path:
            return None
        candidates = self._canonical_paths_for(remote_path, server_config)
        return candidates[0] if candidates else None

    # ----------------------------------------------------------------- internal
    def _yield_processable_for(
        self,
        media_item: Any,
        library: Library,
        server_config: ServerConfig,
    ) -> Iterator[ProcessableItem]:
        """Convert one :class:`MediaItem` into ProcessableItems.

        A single item maps to one media file but possibly several local
        canonical paths when the user has multi-mount path mappings
        (mergerfs et al.). We yield one ProcessableItem per candidate so
        the dispatcher can pick whichever the worker can actually read.
        """
        remote_path = getattr(media_item, "remote_path", "") or ""
        if not remote_path:
            return
        item_id = getattr(media_item, "id", "") or ""
        title = getattr(media_item, "title", "") or remote_path
        bundle_metadata = getattr(media_item, "bundle_metadata", ()) or ()
        bundle_meta_map: dict[str, tuple[tuple[str, str], ...]] = (
            {server_config.id: tuple(bundle_metadata)} if bundle_metadata else {}
        )
        for canonical in self._canonical_paths_for(remote_path, server_config):
            yield ProcessableItem(
                canonical_path=canonical,
                server_id=server_config.id,
                item_id_by_server={server_config.id: item_id} if item_id else {},
                title=title,
                library_id=library.id,
                bundle_metadata_by_server=bundle_meta_map,
            )
