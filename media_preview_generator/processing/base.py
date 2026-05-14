"""VendorProcessor protocol — the per-vendor processing interface.

Each supported server type (Plex, Emby, Jellyfin) ships a concrete
implementation in ``processing/{vendor}.py``. The orchestrator never
branches on server type; it asks
:func:`processing.registry.get_processor_for` for the right
implementation and calls these methods.

Mirrors the shape of :mod:`servers.base` (MediaServer interface) and
:mod:`output.base` (OutputAdapter interface) — the codebase already
uses one-file-per-vendor behind a small base interface for both
inbound (servers) and outbound (output) concerns; this module adds
the same pattern for processing.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Protocol

from ..servers.base import Library, ServerConfig
from .types import ProcessableItem


class VendorProcessor(Protocol):
    """Per-vendor processing operations.

    All methods take the persisted :class:`ServerConfig` and instantiate
    the live server adapter internally. This keeps callers free of
    vendor-specific construction logic — the orchestrator only sees
    ``ServerConfig`` and ``ProcessableItem``.
    """

    def list_libraries(self, server_config: ServerConfig) -> list[Library]:
        """Return the library snapshot the vendor exposes.

        Should round-trip every library the configured credentials can see,
        with the per-library ``enabled`` flag respecting the user's settings.
        Empty list on transport / auth failure (logged by the implementation).
        """
        ...

    def list_canonical_paths(
        self,
        server_config: ServerConfig,
        *,
        library_ids: list[str] | None = None,
        cancel_check: Callable[[], bool] | None = None,
        progress_callback: Callable[[int, int, str], None] | None = None,
    ) -> Iterator[ProcessableItem]:
        """Walk every video in the chosen libraries; yield ProcessableItems.

        Args:
            server_config: Persisted server config (auth, URL, path mappings).
            library_ids: Optional whitelist; ``None`` means every enabled library.
            cancel_check: Optional callable returning True when the caller wants
                the iteration to stop. Implementations should poll this between
                items so user-cancelled jobs don't drag.
            progress_callback: Optional ``(processed, total, message)`` callback
                forwarded to the UI's progress widget.

        Yields:
            One :class:`ProcessableItem` per video file found, with
            ``canonical_path`` already path-mapped to the local filesystem.
        """
        ...

    def scan_recently_added(
        self,
        server_config: ServerConfig,
        *,
        lookback_hours: int,
        library_ids: list[str] | None = None,
    ) -> Iterator[ProcessableItem]:
        """Yield items added within the lookback window.

        Used by the recently-added scheduler. Implementations query the
        vendor's "sort by date added" endpoint (Plex: ``/library/recentlyAdded``,
        Emby/Jellyfin: ``/Users/{userId}/Items?SortBy=DateCreated``) and filter
        client-side to the lookback window.
        """
        ...

    def resolve_canonical_path(
        self,
        server_config: ServerConfig,
        *,
        item_id: str,
    ) -> str | None:
        """Vendor item-id → local canonical path. Used by webhooks.

        Returns ``None`` when the item is not yet indexed by the vendor
        (a typical race for webhook-driven flows where the source system
        publishes before the media server has finished its own scan).
        """
        ...
