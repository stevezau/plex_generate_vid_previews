"""MediaServer interface and supporting dataclasses.

The :class:`MediaServer` abstract base class defines the operations every
supported media server (Plex, Emby, Jellyfin) must implement. The processing
pipeline interacts with servers exclusively through this interface; vendor
specifics live in concrete subclasses under this package.
"""

from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ServerType(str, Enum):
    """Enumeration of supported media server types."""

    PLEX = "plex"
    EMBY = "emby"
    JELLYFIN = "jellyfin"


class LibraryNotYetIndexedError(Exception):
    """Raised when a server has not yet ingested an item the publisher needs.

    The dispatcher catches this exception and routes the affected
    (server, file) pair into the slow-backoff retry queue. Distinguished from
    transport errors (5xx, timeouts) so the two are retried on different
    cadences: this one waits minutes for the server to catch up; transport
    failures retry within seconds.
    """


@dataclass(frozen=True)
class Library:
    """A library/section exposed by a media server.

    Attributes:
        id: Server-side library identifier (e.g. Plex section key, Jellyfin item id).
        name: Human-readable library name.
        remote_paths: Folder paths from the server's perspective. After applying
            the server's ``path_mappings`` these resolve to canonical local paths.
        enabled: Whether the user has opted to process this library with the tool.
            Disabled libraries are skipped during ownership resolution; see
            ``should_publish`` in the dispatcher.
        kind: Optional server-specific media type marker (e.g. ``"movie"``,
            ``"show"``). Treated as opaque metadata.
    """

    id: str
    name: str
    remote_paths: tuple[str, ...]
    enabled: bool = True
    kind: str | None = None


@dataclass(frozen=True)
class MediaItem:
    """A single video item discovered via library enumeration or webhook.

    Attributes:
        id: Server-side item identifier.
        library_id: Identifier of the owning :class:`Library`.
        title: Display title (e.g. movie title or ``"Show - S01E01"``).
        remote_path: Absolute path to the underlying media file from the server's
            perspective. Apply server path mappings to obtain a canonical local
            path before reading from disk.
        bundle_metadata: Vendor-specific pre-fetched ``(hash, file)`` pairs
            captured during enumeration. Plex populates this from
            ``item.media[*].parts[*].(hash, file)`` so :class:`PlexBundleAdapter`
            can compute the BIF output path without re-issuing
            ``/library/metadata/{id}/tree`` per item — a 9981-item scan
            previously paid 9981 sequential round-trips for hashes that
            ``section.search()`` already returned. Empty for vendors that
            don't have an analogous concept (Emby, Jellyfin) and for paths
            that didn't come from a fresh enumeration (Sonarr/Radarr
            webhook payloads carrying only a path).
    """

    id: str
    library_id: str
    title: str
    remote_path: str
    bundle_metadata: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class WebhookEvent:
    """Normalised representation of an inbound webhook event.

    Attributes:
        event_type: Free-text classifier (e.g. ``"library.new"``,
            ``"ItemAdded"``). Used for logging only.
        item_id: Server-side item identifier when the payload references one.
        remote_path: Absolute media path when the payload exposes one directly.
            Path-bearing webhooks (Sonarr/Radarr/templated) avoid the API
            callback that item-id-only webhooks require.
        raw: Original parsed payload, retained for diagnostics.
    """

    event_type: str
    item_id: str | None = None
    remote_path: str | None = None
    raw: dict[str, Any] | None = None


@dataclass(frozen=True)
class ConnectionResult:
    """Outcome of a server connection probe.

    Attributes:
        ok: True when the probe succeeded and the server identified itself.
        server_id: Server-reported unique identifier (Plex ``machineIdentifier``,
            Emby/Jellyfin ``ServerId``). Used for webhook auto-routing.
        server_name: Server-reported friendly name.
        version: Server software version string.
        message: Human-readable status (success summary or error text).
    """

    ok: bool
    server_id: str | None = None
    server_name: str | None = None
    version: str | None = None
    message: str = ""


class MediaServer(ABC):
    """Common operations every supported media server must provide.

    Concrete subclasses (``PlexServer``, ``EmbyServer``, ``JellyfinServer``)
    encapsulate the vendor-specific HTTP, auth, and payload handling. The rest
    of the codebase interacts only with this interface.

    Attributes:
        id: Stable identifier from settings (UUID generated at server-add).
        type: Vendor type, one of :class:`ServerType`.
        name: User-supplied label.
    """

    def __init__(self, server_id: str, name: str) -> None:
        self.id = server_id
        self.name = name

    @property
    @abstractmethod
    def type(self) -> ServerType:
        """Vendor type identifier."""

    @abstractmethod
    def test_connection(self) -> ConnectionResult:
        """Probe the server and return a :class:`ConnectionResult`.

        Implementations must not raise on transport errors; the failure is
        reported via ``ConnectionResult.ok=False`` and ``message``.
        """

    @abstractmethod
    def list_libraries(self) -> list[Library]:
        """Return every library the configured credentials can see.

        The returned list is the *cached snapshot* the rest of the system uses
        for ownership resolution. Each library's ``enabled`` flag reflects the
        user's per-library toggle from settings.
        """

    @abstractmethod
    def list_items(self, library_id: str) -> Iterator[MediaItem]:
        """Yield every item in the named library.

        Implementations may stream results; callers are expected to iterate.
        """

    def search_items(self, query: str, limit: int = 50) -> list[MediaItem]:
        """Return up to ``limit`` items whose title contains ``query``.

        The default implementation walks every library and every item via
        :meth:`list_items`, filtering client-side. That's correct but
        catastrophically slow for large libraries (D4 — Preview Inspector
        search took 13 seconds against a 119k-item Plex install).
        Concrete subclasses MUST override to use the vendor's native
        search API:

          * Plex: ``/hubs/search?query=…``
          * Emby/Jellyfin: ``/Items?searchTerm=…&Recursive=true``

        The default is kept as a safety net so the API endpoint never
        crashes on a vendor that hasn't been overridden yet — but the
        per-vendor override is the actual correctness fix.
        """
        results: list[MediaItem] = []
        needle = (query or "").strip().lower()
        if not needle:
            return results
        for library in self.list_libraries():
            for item in self.list_items(library.id):
                if needle in (item.title or "").lower():
                    results.append(item)
                    if len(results) >= limit:
                        return results
        return results

    @abstractmethod
    def resolve_item_to_remote_path(self, item_id: str) -> str | None:
        """Return the server-side absolute path for ``item_id`` or ``None``.

        Used to convert webhook events that carry only an item id into a path
        the dispatcher can canonicalise.
        """

    def resolve_remote_path_to_item_id(self, remote_path: str) -> str | None:
        """Inverse of :meth:`resolve_item_to_remote_path`.

        Given a server-side absolute path, return that server's item id
        (Plex ratingKey, Emby/Jellyfin ItemId), or ``None`` when no
        matching item exists. Used by the secondary-publisher fan-out
        from the legacy single-Plex scan path: when a scheduled scan
        produces a file at ``/data/movies/Foo.mkv``, this helper lets
        us populate the ``item_id_by_server`` hint for the Jellyfin
        publisher (whose manifest is keyed by item id) without waiting
        for a Jellyfin webhook to fire.

        Default implementation returns ``None`` — the dispatcher then
        skips publishers that need an item id. Concrete subclasses
        override when their API supports a reverse lookup.
        """
        del remote_path  # unused in base; override
        return None

    @abstractmethod
    def trigger_refresh(self, *, item_id: str | None, remote_path: str | None) -> None:
        """Notify the server that media or sidecar files changed.

        Implementations should accept either an item id or a path (whichever
        the vendor's API supports more naturally) and silently no-op when the
        target isn't yet known to the server.
        """

    @abstractmethod
    def parse_webhook(self, payload: dict[str, Any] | bytes, headers: dict[str, str]) -> WebhookEvent | None:
        """Normalise a vendor-specific webhook payload to a :class:`WebhookEvent`.

        Returns ``None`` when the payload is not relevant to this tool
        (e.g. playback events). Concrete implementations are responsible for
        format detection details (multipart vs JSON, header conventions).
        """


@dataclass
class ServerConfig:
    """Persisted configuration for a single media server.

    This is the JSON-serialisable shape stored under ``media_servers`` in
    ``settings.json``. Concrete server clients are constructed from this by
    the server registry; the dataclass itself contains no live HTTP state.

    Attributes:
        id: Locally generated UUID — stable identifier for this entry,
            used in URLs and per-server fan-out routing.
        server_identity: Server-reported unique identifier captured at
            test-connection time (Plex ``machineIdentifier``,
            Emby/Jellyfin ``ServerId``). Populated when the server probe
            succeeds; the universal webhook router compares it against
            the identifier embedded in inbound vendor payloads to route
            to the right configured server when more than one of the
            same vendor is configured.
    """

    id: str
    type: ServerType
    name: str
    enabled: bool
    url: str
    auth: dict[str, Any]
    verify_ssl: bool = True
    timeout: int = 30
    libraries: list[Library] = field(default_factory=list)
    path_mappings: list[dict[str, Any]] = field(default_factory=list)
    # Per-server exclusion rules — same shape as the legacy global
    # ``exclude_paths`` setting (list of ``{"value": str, "type": "path"|"regex"}``).
    # Phase 2 of the multi-server refactor migrates the global list into
    # the first Plex entry's ``exclude_paths`` so users can have different
    # rules per server (with an "Apply to all servers" UI button to copy
    # one server's list to the others when they don't want the granularity).
    exclude_paths: list[dict[str, Any]] = field(default_factory=list)
    output: dict[str, Any] = field(default_factory=dict)
    server_identity: str | None = None
