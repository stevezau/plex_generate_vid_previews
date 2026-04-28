"""Plex bundle BIF output adapter.

Translates a frame :class:`BifBundle` into Plex's bundle-hash on-disk layout
and packs the BIF at that location. Plex's expected path structure (verified
in the existing pipeline at ``processing/orchestrator.py:_setup_bundle_paths``):

    {plex_config_folder}/Media/localhost/<h0>/<h[1:]>.bundle/Contents/Indexes/index-sd.bif

where ``<h0>`` is the first character of the per-item bundle hash returned by
``GET /library/metadata/{id}/tree``.

The adapter calls :meth:`PlexServer.get_bundle_metadata` to resolve the hash,
which is why :meth:`needs_server_metadata` returns ``True``.
"""

from __future__ import annotations

import os
from pathlib import Path

from loguru import logger

from ..servers.base import LibraryNotYetIndexedError, MediaServer
from ..utils import sanitize_path
from .base import BifBundle, OutputAdapter


class PlexBundleAdapter(OutputAdapter):
    """Publish into Plex's bundle-hash directory structure.

    Args:
        plex_config_folder: Absolute path to the Plex Media Server data root
            (the directory that contains ``Media/localhost/...``).
        frame_interval: BIF frame interval in seconds. Persisted in the BIF
            header so Plex spaces preview frames correctly during scrubbing.
    """

    def __init__(self, plex_config_folder: str, frame_interval: int) -> None:
        self._plex_config_folder = plex_config_folder
        self._frame_interval = int(frame_interval)

    @property
    def name(self) -> str:
        return "plex_bundle"

    def needs_server_metadata(self) -> bool:
        # The bundle hash comes from Plex's /tree endpoint; no hash, no path.
        return True

    def compute_output_paths(
        self,
        bundle: BifBundle,
        server: MediaServer | None,
        item_id: str | None,
    ) -> list[Path]:
        """Look up the bundle hash for ``item_id`` and return the BIF path.

        Raises:
            ValueError: When ``item_id`` or ``server`` is missing — Plex
                bundle paths require both (the bundle hash is fetched
                from Plex's ``/library/metadata/{id}/tree`` endpoint).
            LibraryNotYetIndexedError: When Plex has no MediaPart hash matching
                the bundle's ``canonical_path`` — e.g. Plex hasn't scanned the
                file yet. The dispatcher routes this into the slow-backoff
                retry queue.
            TypeError: When ``server`` is not a Plex server.
        """
        if item_id is None:
            raise ValueError("PlexBundleAdapter requires an item_id")
        if server is None:
            raise ValueError("PlexBundleAdapter requires a live PlexServer to look up the bundle hash")

        from ..servers.plex import PlexServer  # local import to avoid cycles

        if not isinstance(server, PlexServer):
            raise TypeError(f"PlexBundleAdapter expected a PlexServer, got {type(server).__name__}")

        parts = server.get_bundle_metadata(item_id)
        if not parts:
            raise LibraryNotYetIndexedError(f"Plex item {item_id} has no MediaPart with a bundle hash yet")

        target_basename = os.path.basename(bundle.canonical_path)
        bundle_hash = self._select_hash_for_path(parts, target_basename, item_id)

        return [self._bundle_bif_path(bundle_hash)]

    def publish(self, bundle: BifBundle, output_paths: list[Path], item_id: str | None = None) -> None:
        """Pack ``bundle.frame_dir`` into a BIF file at the first output path.

        Plex stores exactly one ``index-sd.bif`` per bundle, so we expect
        ``output_paths`` to have a single entry. Parent directories are
        created if missing.
        """
        if not output_paths:
            raise ValueError("PlexBundleAdapter.publish requires at least one output path")

        index_bif = output_paths[0]
        indexes_dir = index_bif.parent
        try:
            indexes_dir.mkdir(parents=True, exist_ok=True)
        except PermissionError as exc:
            logger.error(
                "Cannot create Plex bundle folder at {}: permission denied. "
                "Plex previews live under your Plex config folder — make sure that folder is "
                "mounted read-write (not :ro in Docker), the path in Settings → Media Servers "
                "matches your actual mount, and the user running this tool can write to it. "
                "Original error: {}",
                indexes_dir,
                exc,
            )
            raise

        # Delegate the actual byte-packing to the existing helper so we keep a
        # single source of truth for the BIF header layout.
        from ..processing.orchestrator import generate_bif

        generate_bif(
            str(index_bif),
            str(bundle.frame_dir),
            BifIntervalConfig(self._frame_interval),
        )
        logger.debug("Plex BIF written to {}", index_bif)

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def bundle_bif_path(plex_config_folder: str, bundle_hash: str) -> Path:
        """Compute ``{plex}/Media/localhost/<h0>/<h[1:]>.bundle/Contents/Indexes/index-sd.bif``.

        Public so callers that already hold a bundle hash (the orchestrator,
        which extracts hashes from a single Plex ``/tree`` response while
        iterating MediaParts) can compute paths without going through an
        adapter instance or paying for a duplicate API call.
        """
        bundle_file = sanitize_path(f"{bundle_hash[0]}/{bundle_hash[1:]}.bundle")
        bundle_path = sanitize_path(os.path.join(plex_config_folder, "Media", "localhost", bundle_file))
        indexes_path = sanitize_path(os.path.join(bundle_path, "Contents", "Indexes"))
        return Path(sanitize_path(os.path.join(indexes_path, "index-sd.bif")))

    def _bundle_bif_path(self, bundle_hash: str) -> Path:
        """Instance-bound shim that calls :meth:`bundle_bif_path`."""
        return self.bundle_bif_path(self._plex_config_folder, bundle_hash)

    @staticmethod
    def _select_hash_for_path(
        parts: list[tuple[str, str]],
        target_basename: str,
        item_id: str,
    ) -> str:
        """Pick the bundle hash whose MediaPart filename matches ``target_basename``.

        Plex items with multiple parts (e.g. multi-disc movies) report several
        MediaParts on ``/tree``; we want the one corresponding to the file
        we're processing. When no exact basename match exists we fall back to
        the first part with a usable hash and log a debug warning.
        """
        for bundle_hash, remote_path in parts:
            if os.path.basename(remote_path) == target_basename:
                if bundle_hash and len(bundle_hash) >= 2:
                    return bundle_hash
                raise LibraryNotYetIndexedError(f"Plex item {item_id} returned an invalid bundle hash {bundle_hash!r}")
        # Fallback: first usable hash. Plex sometimes reports paths that don't
        # exactly match the file we received via webhook (e.g. casing, mount
        # differences); the hash is still correct for the item.
        for bundle_hash, _remote_path in parts:
            if bundle_hash and len(bundle_hash) >= 2:
                logger.debug(
                    "Plex item {} has no MediaPart matching {!r}; using first hash",
                    item_id,
                    target_basename,
                )
                return bundle_hash
        raise LibraryNotYetIndexedError(f"Plex item {item_id} returned no valid bundle hashes")


class BifIntervalConfig:
    """Minimal shim exposing :attr:`plex_bif_frame_interval` for ``generate_bif``.

    ``generate_bif`` consumes only this one attribute from its config
    parameter; rather than passing a full :class:`Config` object we hand it
    this tiny adapter so the BIF packing helper can be reused without forcing
    callers to materialise an entire config.
    """

    def __init__(self, frame_interval: int) -> None:
        self.plex_bif_frame_interval = int(frame_interval)
