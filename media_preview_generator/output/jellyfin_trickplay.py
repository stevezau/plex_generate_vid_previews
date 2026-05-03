"""Jellyfin native trickplay output adapter.

Produces Jellyfin 10.10+'s **native** "saved with media" trickplay layout —
the format Jellyfin's own ``Library/PathManager.GetTrickplayDirectory(item, saveWithMedia=true)``
returns, so Jellyfin's import-existing-tiles path
(``TrickplayManager.RefreshTrickplayDataAsync``) finds our output, registers
the resolution in its DB, and skips spawning ffmpeg.

Output layout under the media file's directory::

    <basename>.trickplay/
        <width> - <tileW>x<tileH>/
            0.jpg                   # sheet 0: tiles 0..(tileW*tileH - 1)
            1.jpg                   # sheet 1: next tileW*tileH tiles
            ...

That is the EXACT layout Jellyfin's ``PathManager.GetTrickplayDirectory``
emits for ``saveWithMedia=true`` plus the ``"<width> - <tileW>x<tileH>"``
sub-directory ``TrickplayManager.GetTrickplayDirectory`` constructs. No
manifest JSON is written — Jellyfin synthesises its own ``TrickplayInfo``
DB row from the on-disk file count + the tile geometry encoded in the
sub-dir name.

The required Jellyfin library options for this layout to be picked up::

    EnableTrickplayImageExtraction = true   # detection / serving gate
    SaveTrickplayWithMedia        = true    # look in <basename>.trickplay/...
    ExtractTrickplayImagesDuringLibraryScan = false  # don't burn CPU on scans

Note that ``EnableTrickplayImageExtraction = false`` is destructive in
Jellyfin — it deletes the trickplay directory on the next refresh.
``servers/jellyfin.py::set_vendor_extraction`` keeps it on for that reason.

References:
    * Jellyfin ``PathManager.GetTrickplayDirectory`` (release-10.11.z) —
      https://github.com/jellyfin/jellyfin/blob/release-10.11.z/Emby.Server.Implementations/Library/PathManager.cs
    * Jellyfin ``TrickplayManager.RefreshTrickplayDataAsync`` (release-10.11.z) —
      https://github.com/jellyfin/jellyfin/blob/release-10.11.z/Jellyfin.Server.Implementations/Trickplay/TrickplayManager.cs
    * Pre-D38 layout was ``<dir>/trickplay/<basename>-<width>/<i>.jpg`` plus
      a ``<basename>-<width>.json`` manifest. Jellyfin 10.10+ never looked
      there — the output existed but rendered nowhere.
"""

from __future__ import annotations

import math
import os
from pathlib import Path

from loguru import logger
from PIL import Image

from ..servers.base import MediaServer
from .base import BifBundle, OutputAdapter

# Jellyfin's native format groups thumbnails into 10x10 sheets. The
# server reads this geometry from the sheet sub-directory name; changing
# either dimension WITHOUT also changing the sub-directory name would
# silently desync Jellyfin's tile-picker math from our layout.
_TILE_W = 10
_TILE_H = 10
_TILES_PER_SHEET = _TILE_W * _TILE_H


class JellyfinTrickplayAdapter(OutputAdapter):
    """Publish Jellyfin's native saved-with-media tile layout next to the media file.

    Args:
        width: Pixel width of each thumbnail. Encoded in the sheet
            sub-directory name (Jellyfin's tile-picker reads it from
            there). Must match the width the FFmpeg frame-extraction
            stage produced.
        frame_interval: Seconds between successive frames. Not encoded
            into the layout at all (Jellyfin tracks interval per-item
            in its DB and recomputes from the file count + duration if
            unset). Kept on the adapter for symmetry with EmbyBifAdapter.
        jpeg_quality: Output JPEG quality for the assembled sheets
            (Jellyfin's own default is 90; we mirror it).
    """

    def __init__(
        self,
        *,
        width: int = 320,
        frame_interval: int = 10,
        jpeg_quality: int = 90,
    ) -> None:
        self._width = int(width)
        self._frame_interval = int(frame_interval)
        self._jpeg_quality = int(jpeg_quality)

    @property
    def name(self) -> str:
        return "jellyfin_trickplay"

    def needs_server_metadata(self) -> bool:
        # Item id is required for the publish-time write_meta call so we
        # must surface that before publish. (No server API call needed —
        # the sheet directory is fully derivable from canonical_path.)
        return True

    def compute_output_paths(
        self,
        bundle: BifBundle,
        server: MediaServer | None,
        item_id: str | None,
    ) -> list[Path]:
        """Return the path of sheet 0 — used for the freshness check.

        Sheet 0 is always present whenever any trickplay output exists
        for this item, so its mtime is a safe stand-in for the whole
        trickplay output's freshness.

        Raises:
            ValueError: When ``item_id`` is missing.
        """
        if item_id is None:
            raise ValueError("JellyfinTrickplayAdapter requires an item_id for publish-time bookkeeping")
        del server, item_id  # unused; layout is derived purely from canonical_path
        return [self.sheet_dir(bundle.canonical_path, width=self._width) / "0.jpg"]

    @staticmethod
    def trickplay_dir(canonical_path: str) -> Path:
        """Compute ``<media_dir>/<basename>.trickplay/`` for ``canonical_path``.

        Mirrors Jellyfin's ``PathManager.GetTrickplayDirectory(item, saveWithMedia=true)``:
        ``Path.Combine(item.ContainingFolderPath, Path.ChangeExtension(item.Path, ".trickplay"))``.
        """
        media_path = Path(canonical_path)
        return media_path.parent / f"{media_path.stem}.trickplay"

    @staticmethod
    def sheet_dir(
        canonical_path: str,
        *,
        width: int,
        tile_w: int = _TILE_W,
        tile_h: int = _TILE_H,
    ) -> Path:
        """Compute the per-resolution sheet directory.

        Mirrors Jellyfin's ``TrickplayManager.GetTrickplayDirectory`` which
        appends ``"{width} - {tileW}x{tileH}"`` to the trickplay directory.
        Static + parameterised so the BIF Viewer + diagnostics can compute
        the path without instantiating an adapter.
        """
        return JellyfinTrickplayAdapter.trickplay_dir(canonical_path) / f"{int(width)} - {int(tile_w)}x{int(tile_h)}"

    def publish(self, bundle: BifBundle, output_paths: list[Path], item_id: str | None = None) -> None:
        """Pack ``bundle.frame_dir`` JPG frames into Jellyfin tile sheets.

        ``output_paths[0]`` is the sheet-0 path (computed by
        :meth:`compute_output_paths`). All sibling sheets are written
        into the same parent directory.
        """
        if not output_paths:
            raise ValueError("JellyfinTrickplayAdapter.publish requires the sheet-0 path")
        if not item_id:
            raise ValueError("JellyfinTrickplayAdapter.publish requires the Jellyfin item_id")

        sheets_dir = output_paths[0].parent
        try:
            sheets_dir.mkdir(parents=True, exist_ok=True)
        except PermissionError as exc:
            logger.error(
                "Cannot create Jellyfin trickplay folder at {}: permission denied. "
                "The container or process running this tool needs WRITE access to the "
                "folder containing this media file. "
                "If running in Docker, make sure your media volume is mounted read-write "
                "(not :ro) and the PUID/PGID env vars match the file owner. "
                "Original error: {}",
                sheets_dir,
                exc,
            )
            raise

        # Clear any stale tiles from a prior run with a different frame
        # count — Jellyfin imports the directory contents wholesale, so a
        # leftover sheet-N.jpg from before would inflate the manifest's
        # ThumbnailCount and crash the player on a missing tile.
        for stale in sheets_dir.glob("*.jpg"):
            try:
                stale.unlink()
            except OSError as exc:
                logger.debug("Could not remove stale tile {}: {}", stale, exc)

        frames = sorted(p for p in os.listdir(bundle.frame_dir) if p.lower().endswith(".jpg"))
        if not frames:
            raise RuntimeError(f"No JPG frames found under {bundle.frame_dir}; cannot pack Jellyfin trickplay")

        # Compose sheets in 10x10 batches.
        thumb_w, thumb_h = _measure_first_frame(Path(bundle.frame_dir) / frames[0])
        sheets_written = 0
        for sheet_index in range(math.ceil(len(frames) / _TILES_PER_SHEET)):
            start = sheet_index * _TILES_PER_SHEET
            end = min(start + _TILES_PER_SHEET, len(frames))
            sheet_frames = frames[start:end]

            sheet_image = Image.new("RGB", (thumb_w * _TILE_W, thumb_h * _TILE_H), (0, 0, 0))
            for i, frame_name in enumerate(sheet_frames):
                with Image.open(Path(bundle.frame_dir) / frame_name) as src:
                    # PIL.Image.resize() returns a NEW Image whose lifetime
                    # extends past the `with` block; rebinding `src` would
                    # leak it. Bind to a separate name and close explicitly.
                    if src.size != (thumb_w, thumb_h):
                        tile = src.resize((thumb_w, thumb_h))
                        try:
                            col = i % _TILE_W
                            row = i // _TILE_W
                            sheet_image.paste(tile, (col * thumb_w, row * thumb_h))
                        finally:
                            tile.close()
                    else:
                        col = i % _TILE_W
                        row = i // _TILE_W
                        sheet_image.paste(src, (col * thumb_w, row * thumb_h))

            sheet_path = sheets_dir / f"{sheet_index}.jpg"
            sheet_image.save(sheet_path, "JPEG", quality=self._jpeg_quality)
            sheets_written += 1

        logger.debug(
            "Jellyfin trickplay published: {} sheet(s) at {}",
            sheets_written,
            sheets_dir,
        )


def _measure_first_frame(frame_path: Path) -> tuple[int, int]:
    """Read the first frame's dimensions to size each tile in the sheet."""
    with Image.open(frame_path) as img:
        return img.size
