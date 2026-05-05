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

from loguru import logger


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
class HealthCheckIssue:
    """A single mis-configured (or sub-optimally configured) setting on a server.

    Returned by :meth:`MediaServer.check_settings_health` so the UI can render
    a per-server "what needs fixing" panel and offer one-click remediation.

    Attributes:
        library_id: Owning library id, or ``None`` when the issue is
            server-wide (e.g. a Plex instance flag with no per-library
            equivalent). Used by the UI to group rows.
        library_name: Human-readable library name. May be empty for
            server-wide issues.
        flag: API-side flag name (e.g. ``"EnableRealtimeMonitor"``).
            Authoritative identifier the apply-fix path uses to know
            which setting to flip.
        label: Plain-English label for the setting. Goes on the row
            heading in the UI.
        rationale: One-sentence explanation of *why* the user should care.
            Rendered as the ⓘ tooltip body next to the label.
        current: The value the server currently reports. Stringified for
            display; could be a bool, int, str, or None.
        recommended: The value this app would set for an ideal preview
            workflow. Same type as ``current``.
        severity: ``"critical"`` (will break previews if left alone) or
            ``"recommended"`` (works without it but UX suffers). Drives
            the badge colour in the UI.
        fixable: Whether :meth:`MediaServer.apply_recommended_settings`
            can flip this flag programmatically. Some settings (e.g. Plex
            "Generate video preview thumbnails", set per-library via the
            Plex web UI) are read-only via API; we surface them with
            ``fixable=False`` and explain what the user must do manually.
    """

    library_id: str | None
    library_name: str
    flag: str
    label: str
    rationale: str
    current: Any
    recommended: Any
    severity: str
    fixable: bool


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

    @property
    def path_mappings(self) -> list[dict[str, Any]]:
        """Per-server path mappings used to translate canonical paths
        into server-view paths during resolve / refresh fan-out.

        Subclasses backed by a :class:`ServerConfig` inherit the
        default, which reads ``self._config.path_mappings``. Stub
        servers used in tests can override directly.
        """
        cfg = getattr(self, "_config", None)
        return getattr(cfg, "path_mappings", None) or []

    def resolve_remote_path_to_item_id(self, remote_path: str) -> str | None:
        """Inverse of :meth:`resolve_item_to_remote_path`.

        Given a canonical absolute path, walk every server-view
        candidate produced by ``expand_path_mapping_candidates`` (which
        bidirectionally expands the path through the server's
        ``path_mappings``) and return the first non-None hit from
        :meth:`_resolve_one_path`. Returns ``None`` when no candidate
        resolves — the dispatcher then skips publishers that need an
        item id.

        This loop is the single source of truth for path-mapping
        translation during reverse lookup; subclasses implement only
        the per-path API call in :meth:`_resolve_one_path`.
        """
        if not remote_path:
            return None
        from ..config.paths import expand_path_mapping_candidates

        for candidate in expand_path_mapping_candidates(remote_path, self.path_mappings):
            item_id = self._resolve_one_path(candidate)
            if item_id is not None:
                return item_id
        return None

    def _resolve_one_path(self, server_view_path: str) -> str | None:
        """Subclass hook: server-view path → item id, or ``None`` on miss.

        Default returns ``None``. Subclasses override with their
        vendor-specific per-path lookup (Plex section walk by
        basename, Emby ``Path=<exact>`` filter, Jellyfin
        ``MediaPreviewBridge/ResolvePath``) — the base class loops
        candidates so each subclass only has to handle a single
        already-translated path.
        """
        del server_view_path
        return None

    def trigger_refresh(self, *, item_id: str | None, remote_path: str | None) -> None:
        """Notify the server about media or sidecar changes.

        For path-based scan-nudges, fires :meth:`_trigger_path_refresh`
        once per mapped candidate. Multi-disk installs that map a
        single canonical root onto multiple server-view mounts get one
        nudge per mount — Plex has done this since the targeted-scan
        feature shipped, and the unified path here means Emby and
        Jellyfin inherit the same behaviour.

        For item-based metadata refresh, fires
        :meth:`_trigger_item_refresh` once. Both hooks are best-effort:
        per-candidate exceptions are swallowed and logged so a single
        transient HTTP failure can't suppress the rest of the fan-out.
        """
        if remote_path:
            from ..config.paths import expand_path_mapping_candidates

            for candidate in expand_path_mapping_candidates(remote_path, self.path_mappings):
                try:
                    self._trigger_path_refresh(candidate)
                except Exception as exc:
                    logger.debug(
                        "Scan-nudge failed on {} for {}: {}",
                        self.name,
                        candidate,
                        exc,
                    )
        if item_id:
            try:
                self._trigger_item_refresh(item_id)
            except Exception as exc:
                logger.debug(
                    "Item refresh failed on {} for item {}: {}",
                    self.name,
                    item_id,
                    exc,
                )

    def _trigger_path_refresh(self, server_view_path: str) -> None:
        """Subclass hook: nudge the server to scan a single path.

        Default is a no-op. Subclasses override with their
        vendor-specific scan-nudge call (Plex
        ``/library/sections/{key}/refresh?path=…``, Emby/Jellyfin
        ``/Library/Media/Updated``).
        """
        del server_view_path

    def _trigger_item_refresh(self, item_id: str) -> None:
        """Subclass hook: refresh metadata for a single item id.

        Default is a no-op. Subclasses override with the vendor's
        per-item refresh call (Emby/Jellyfin ``/Items/{id}/Refresh``;
        Plex has no equivalent and inherits the default).
        """
        del item_id

    @abstractmethod
    def parse_webhook(self, payload: dict[str, Any] | bytes, headers: dict[str, str]) -> WebhookEvent | None:
        """Normalise a vendor-specific webhook payload to a :class:`WebhookEvent`.

        Returns ``None`` when the payload is not relevant to this tool
        (e.g. playback events). Concrete implementations are responsible for
        format detection details (multipart vs JSON, header conventions).
        """

    def check_settings_health(self) -> list[HealthCheckIssue]:
        """Return a list of mis-configured settings on this server.

        Used by the Edit-Server modal's health-check panel to surface
        per-library settings the user should flip for the preview
        pipeline to work optimally — e.g. Jellyfin's
        ``EnableTrickplayImageExtraction`` (must be true or our sidecar
        trickplay is invisible) or ``EnableRealtimeMonitor`` (off →
        new files require a manual scan-nudge to be discovered).

        Empty list means "all good". A non-empty list is rendered with
        a per-issue severity badge and a single "Fix all" button that
        calls :meth:`apply_recommended_settings`.

        Default returns an empty list — concrete server clients
        override when they have settings worth checking.
        """
        return []

    def get_vendor_extraction_status(self) -> dict[str, int]:
        """Report current per-library state of vendor-side preview generation.

        Drives the "Stop this server from generating previews itself"
        panel — without this probe the UI has to render both Disable and
        Re-enable buttons regardless of state, which is noisy when one
        of them would be a no-op. Returns:

        .. code-block:: python

            {
                "extracting_count": int,   # libraries where the server IS generating
                "stopped_count":  int,     # libraries where it ISN'T (recommended state)
                "skipped_count":  int,     # libraries we couldn't audit (custom agents, etc.)
                "total":          int,
            }

        Default returns zeros — concrete server clients override.
        """
        return {"extracting_count": 0, "stopped_count": 0, "skipped_count": 0, "total": 0}

    def apply_recommended_settings(self, flags: list[str] | None = None) -> dict[str, str]:
        """Flip the ``flag``s named in ``check_settings_health`` to their recommended values.

        Args:
            flags: Restrict to the named flag list, or ``None`` for "every
                fixable issue currently surfaced by ``check_settings_health``".

        Returns:
            Dict mapping ``"<library_id>:<flag>"`` (or ``":<flag>"`` for
            server-wide settings) to ``"ok"`` on success or an error
            message string on failure. Same envelope shape as the existing
            ``set_vendor_extraction`` family of helpers so the UI can
            render a per-row outcome.
        """
        del flags  # unused in base; override
        return {}


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
