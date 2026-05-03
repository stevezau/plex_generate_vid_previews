"""OutputAdapter interface and shared dataclasses.

An :class:`OutputAdapter` knows how to turn the JPG frames produced by the
worker pool into the file layout a particular media server expects, and where
to write that output on disk. The processing pipeline runs FFmpeg once per
canonical file and fans the result out to every adapter whose owning server
should receive the output.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from ..servers.base import MediaServer


@dataclass(frozen=True)
class BifBundle:
    """Shared output of the frame-extraction stage.

    Attributes:
        canonical_path: Absolute local path to the source media file. Used by
            adapters to derive sidecar locations next to the media.
        frame_dir: Directory containing the extracted JPG frames (numbered
            ``00001.jpg``, ``00002.jpg``, ...). Adapters that pack BIFs read
            from here; tile-grid adapters compose sheets from these JPGs.
        bif_path: Absolute path to the packed BIF file when one has already
            been generated, or ``None`` if only frames are available.
        frame_interval: Frame interval in seconds used during extraction.
        width: Pixel width of the extracted frames.
        height: Pixel height of the extracted frames.
        frame_count: Total number of frames extracted.
    """

    canonical_path: str
    frame_dir: Path
    bif_path: Path | None
    frame_interval: int
    width: int
    height: int
    frame_count: int
    # Vendor-specific pre-fetched ``(hash, file)`` pairs for the publisher.
    # Plex populates this from ``ProcessableItem.bundle_metadata_by_server``
    # (captured during enumeration via plexapi's ``section.search()`` which
    # already returns ``item.media[*].parts[*].(hash, file)``). When set,
    # PlexBundleAdapter skips its per-item ``/library/metadata/{id}/tree``
    # round-trip — a 9981-item full-library scan previously paid 9981
    # sequential round-trips for hashes the enumeration already had.
    # Empty tuple for non-Plex adapters and for paths that didn't come
    # from a fresh enumeration (e.g. Sonarr/Radarr webhooks).
    prefetched_bundle_metadata: tuple[tuple[str, str], ...] = ()


class OutputAdapter(ABC):
    """Vendor-specific publisher.

    An adapter is paired with a :class:`MediaServer` at construction; the
    server provides any metadata the adapter needs (bundle hash, item id,
    etc.) and the adapter writes files to the locations that server expects.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Short identifier for logging (e.g. ``"plex_bundle"``)."""

    @abstractmethod
    def needs_server_metadata(self) -> bool:
        """Whether ``compute_output_paths`` requires a live API call.

        Plex returns ``True`` because publishing depends on the per-item
        bundle hash. Sidecar adapters return ``False`` since the path is
        derived purely from the canonical media path.
        """

    @abstractmethod
    def compute_output_paths(
        self,
        bundle: BifBundle,
        server: MediaServer | None,
        item_id: str | None,
    ) -> list[Path]:
        """Return the absolute paths this adapter will write.

        Implementations may return more than one path when the format is
        multi-file (e.g. Jellyfin tile sheets plus a manifest).

        ``server`` is ``None`` when the caller (e.g. the diagnostics
        ``output-status`` endpoint) only needs path computation and
        hasn't built a live client. Adapters that don't actually need
        the server (Emby sidecar, Jellyfin trickplay) accept ``None``
        unconditionally; adapters that do (Plex bundle, which queries
        the bundle hash from the API) raise ``ValueError`` when called
        without one.

        Implementations may raise ``LibraryNotYetIndexedError`` when the
        server has not yet ingested the item the adapter needs metadata for;
        the dispatcher routes such failures into the slow-backoff retry queue.
        """

    @abstractmethod
    def publish(self, bundle: BifBundle, output_paths: list[Path], item_id: str | None = None) -> None:
        """Write the bundle's data to ``output_paths``.

        ``output_paths`` is the result of a previous call to
        :meth:`compute_output_paths` and must contain absolute paths. All
        intermediate directories must be created by the implementation.
        ``item_id`` is the same value that was passed to
        :meth:`compute_output_paths` — adapters whose output format
        embeds the id (e.g. Jellyfin's manifest, keyed by item id)
        read it here.
        """
