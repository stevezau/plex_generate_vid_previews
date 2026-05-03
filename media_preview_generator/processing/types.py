"""Shared dataclasses for the processing pipeline.

These types are the *processor's* view of work to do — they carry the
local canonical path (already path-mapped from the server's
remote_path), an optional vendor item-id hint per server (so the
publisher can target the right item without re-resolving), and just
enough metadata for logs and progress reporting.

Distinct from :class:`media_preview_generator.servers.base.MediaItem`
which is the *server-side* view (vendor item-id + remote_path).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ProcessableItem:
    """A single video item ready to be processed by the per-item worker.

    Attributes:
        canonical_path: Absolute LOCAL filesystem path of the source media file
            (already resolved from the server's remote_path via path_mappings).
            This is what FFmpeg reads from disk.
        server_id: Identifier of the server this item was enumerated from.
            Used to scope publishing to the originating server when relevant
            (e.g. a Plex full-scan should publish to that Plex server first,
            then fan out to other owners).
        item_id_by_server: Optional ``{server_id: vendor_item_id}`` hint that
            avoids the per-publisher item-id lookup. Always contains at least
            ``{server_id: <originating server's item id>}`` when available.
        title: Display string for logs / progress (e.g. "Show - S01E01" or
            "Movie Title").
        library_id: Source library identifier on the originating server, when
            known. Optional; primarily used for log breadcrumbs.
    """

    canonical_path: str
    server_id: str
    item_id_by_server: dict[str, str] = field(default_factory=dict)
    title: str = ""
    library_id: str | None = None
    # ``{server_id: ((hash, file), …)}`` — pre-fetched bundle metadata
    # captured during enumeration so the publisher can skip per-item
    # network round-trips. Currently populated only by Plex (where
    # ``/library/metadata/{id}/tree`` is otherwise paid per item to
    # learn the bundle hash); other vendors leave this empty. Empty
    # also for paths that didn't come from a fresh enumeration (e.g.
    # Sonarr/Radarr webhooks carrying only a path).
    bundle_metadata_by_server: dict[str, tuple[tuple[str, str], ...]] = field(default_factory=dict)


@dataclass
class ScanOutcome:
    """Aggregate counters returned by a vendor processor's scan operation.

    Used by the orchestrator for progress reporting and outcome logging.
    The actual ProcessableItems flow through an Iterator; this type holds
    the *summary* of what the iterator produced once it is exhausted.

    Attributes:
        items_yielded: Total ProcessableItems the scan produced.
        libraries_walked: How many libraries were visited.
        skipped_reason: Free-text explanation when a scan ended without
            walking any libraries (e.g. "library list was empty",
            "all libraries disabled in settings").
    """

    items_yielded: int = 0
    libraries_walked: int = 0
    skipped_reason: str | None = None
