"""Plex bundle BIF output adapter.

Translates a frame :class:`BifBundle` into Plex's bundle-hash on-disk layout
and packs the BIF at that location. Plex's expected path structure:

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

        # Pre-fetched metadata from enumeration short-circuits the per-item
        # /library/metadata/{id}/tree call. plexapi's section.search()
        # already returned item.media[*].parts[*].(hash, file); the
        # enumerator captured them and the dispatcher threaded them
        # through bundle.prefetched_bundle_metadata. The /tree fallback
        # below stays in place for paths that didn't come from a fresh
        # enumeration (Sonarr/Radarr webhooks).
        parts = list(bundle.prefetched_bundle_metadata) if bundle.prefetched_bundle_metadata else []
        if not parts:
            parts = server.get_bundle_metadata(item_id)
        if not parts:
            raise LibraryNotYetIndexedError(
                f"File not yet scanned in Plex (item {item_id}): "
                "Plex hasn't completed its media analysis pass for this file, so the bundle "
                "hash we need to write the BIF doesn't exist yet. We'll auto-retry; if it "
                "keeps happening, run an Analyze on the library in Plex Web (Settings → "
                "Library → Run a partial scan / Analyze) and check that 'Run analysis tasks "
                "during maintenance' is enabled."
            )

        bundle_hash = self._select_hash_for_path(parts, bundle.canonical_path, item_id)

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
        from ..processing.generator import generate_bif

        generate_bif(
            str(index_bif),
            str(bundle.frame_dir),
            BifIntervalConfig(self._frame_interval, server_display_name=bundle.server_display_name),
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
        target_path: str,
        item_id: str,
    ) -> str:
        """Pick the bundle hash whose MediaPart matches ``target_path``.

        Plex items with multiple parts report several MediaParts on
        ``/tree``; we want the one corresponding to the file we're
        processing. Match priority — each tier returns IMMEDIATELY on
        a unique hit and falls through to the next tier only when the
        match is ambiguous (multiple parts tie) or absent:

        1. **Longest common suffix** — compare trailing path segments
           (``/Dune (2021)/Dune (2021).mkv``). Handles both the
           single-version case with path-mapping drift between Plex's
           server-view path and our local canonical path, AND the
           multi-version case from issue #231 where a user has
           ``/mnt/4k/Movies/Dune (2021)/Dune (2021).mkv`` and
           ``/mnt/1080p/Movies/Dune (2021)/Dune (2021).mkv`` merged
           into one Plex ratingKey. Both share the basename but the
           parent directory (``4k`` vs ``1080p``) differs; matching
           on parent+basename (or longer) disambiguates.
        2. **Basename-only match** — last-resort for when even the
           parent directory differs (Plex rewrote the path
           post-import via Sonarr-provided instructions, etc.). Only
           fires when no part had a longer suffix match.
        3. **Ambiguous-tie or no-match fallback** — two cells:
           a. tier 1 had multiple parts tied at the deepest suffix
              (best_is_unique = False) → return the first hash that
              achieved that suffix length. Guaranteed to be one of
              the tied parts; fixes a pre-2026-05 wrong-bundle bug
              where iterating the whole parts list could return an
              unrelated head-of-list hash.
           b. no part matched at any suffix → fall back to the first
              usable hash in the parts list. The hash still belongs
              to the same Plex item, so the BIF ends up where Plex
              looks, even if we can't confirm the exact MediaPart.

        Raises :class:`LibraryNotYetIndexedError` when the best
        matching MediaPart has no usable hash (analysis still in
        progress) or when every returned MediaPart lacks a hash.
        """
        target_norm = target_path.replace("\\", "/").rstrip("/")
        target_parts = [p for p in target_norm.split("/") if p]
        target_basename = target_parts[-1] if target_parts else ""

        # Tier 1: longest common suffix. Walk the suffix from full to
        # basename and, at each length, look for a UNIQUE hit. This
        # way an exact full-path match wins over a basename-only match
        # without being defeated by path-mapping prefix drift.
        best_match: tuple[int, str] | None = None  # (suffix_len, hash)
        best_is_unique = False
        for bundle_hash, remote_path in parts:
            rnorm = remote_path.replace("\\", "/").rstrip("/")
            rparts = [p for p in rnorm.split("/") if p]
            # Count matching trailing segments.
            suffix_len = 0
            for a, b in zip(reversed(target_parts), reversed(rparts), strict=False):
                if a == b:
                    suffix_len += 1
                else:
                    break
            if suffix_len == 0:
                continue
            if best_match is None or suffix_len > best_match[0]:
                best_match = (suffix_len, bundle_hash)
                best_is_unique = True
            elif suffix_len == best_match[0]:
                # Another part ties at this depth — ambiguous, keep
                # walking to see if anything ties higher.
                best_is_unique = False

        if best_match is not None and best_is_unique:
            bundle_hash = best_match[1]
            if bundle_hash and len(bundle_hash) >= 2:
                return bundle_hash
            raise LibraryNotYetIndexedError(
                f"File not yet scanned in Plex (item {item_id}): the matching MediaPart "
                f"has an invalid bundle hash ({bundle_hash!r}). Plex's media analysis "
                "didn't finish writing the bundle. We'll auto-retry."
            )

        # Tier 2: basename-only when the suffix-walk was ambiguous and
        # only one candidate matches on basename. (For the single-
        # version case this is redundant with tier 1; kept so legacy
        # tests that exercise this specific branch still pass.)
        if target_basename:
            basename_matches = [(h, p) for (h, p) in parts if os.path.basename(p) == target_basename]
            if len(basename_matches) == 1:
                bundle_hash = basename_matches[0][0]
                if bundle_hash and len(bundle_hash) >= 2:
                    return bundle_hash
                raise LibraryNotYetIndexedError(
                    f"File not yet scanned in Plex (item {item_id}): the matching MediaPart "
                    f"has an invalid bundle hash ({bundle_hash!r}). Plex's media analysis "
                    "didn't finish writing the bundle. We'll auto-retry."
                )

        # Tier 3: ambiguous-tie or no-match fallback. Two distinct cells:
        #
        # * ``best_match`` is set + ``not best_is_unique`` → multiple parts
        #   tied at the deepest suffix. Use ``best_match[1]`` — the FIRST
        #   hash that achieved that suffix length — so the BIF lands in
        #   one of the TIED bundles, not in some unrelated part that
        #   happens to sit ahead in iteration order. Pre-fix this branch
        #   iterated ``parts`` from index 0 and returned the first usable
        #   hash, which on a multi-disc + multi-version mix could pick
        #   the unrelated disc-1 hash for a Dune (2021).mkv lookup.
        #   This still doesn't solve the "which tied part is the right
        #   one" problem (Plex doesn't tell us); writing to all tied
        #   bundles would, but that's a multi-output-path change
        #   beyond this fix's scope.
        # * ``best_match`` is None → no part matched at any suffix length.
        #   Fall back to "first usable hash from parts" (legacy behaviour).
        if best_match is not None and not best_is_unique:
            best_hash = best_match[1]
            if best_hash and len(best_hash) >= 2:
                logger.warning(
                    "Plex item {} has multiple MediaParts tied at the deepest path "
                    "suffix for {!r}; picking the first tied hash. This can "
                    "happen when multi-version directory structures are identical "
                    "after the version-indicating prefix.",
                    item_id,
                    target_path,
                )
                return best_hash
        for bundle_hash, _remote_path in parts:
            if bundle_hash and len(bundle_hash) >= 2:
                logger.debug(
                    "Plex item {} has no MediaPart matching {!r}; using first hash",
                    item_id,
                    target_path,
                )
                return bundle_hash
        raise LibraryNotYetIndexedError(
            f"File not yet scanned in Plex (item {item_id}): every MediaPart returned by "
            "Plex had an invalid/missing bundle hash. We'll auto-retry."
        )


class BifIntervalConfig:
    """Minimal shim exposing :attr:`plex_bif_frame_interval` for ``generate_bif``.

    ``generate_bif`` consumes only ``plex_bif_frame_interval`` and (optionally)
    ``server_display_name`` from its config parameter; rather than passing a
    full :class:`Config` object we hand it this tiny adapter so the BIF
    packing helper can be reused without forcing callers to materialise an
    entire config.
    """

    def __init__(self, frame_interval: int, *, server_display_name: str | None = None) -> None:
        self.plex_bif_frame_interval = int(frame_interval)
        self.server_display_name = server_display_name
