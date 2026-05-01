"""Jellyfin native trickplay output adapter.

Produces Jellyfin 10.9+'s **native** trickplay format (NOT BIF — the BIF
format only works in Jellyfin via the third-party Jellyscrub plugin). The
native format is a sequence of JPG "sheets" — each sheet is a 10×10 grid
of thumbnails — accompanied by a ``manifest.json`` that catalogues the
geometry and frame interval. Together these files let Jellyfin's web
client render scrubbing previews without any plugin installed.

Output layout under the media file's directory::

    trickplay/
        <basename>-<width>.json     # manifest, keyed by Jellyfin item id
        <basename>-<width>/
            0.jpg                   # sheet 0: thumbnails 0..99 in a 10x10 grid
            1.jpg                   # sheet 1: thumbnails 100..199
            ...

The frames our existing FFmpeg pipeline extracts are repacked into sheets
via Pillow; no second FFmpeg pass needed.

References:
    * Jellyfin 10.9 release notes (introduced the native format).
    * `@jellyfin/sdk` ``TrickplayOptions`` / ``TrickplayInfo`` typings.
    * Jellyfin issue #11747 / #12887 for the next-to-media layout.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path

from loguru import logger
from PIL import Image

from ..servers.base import MediaServer
from .base import BifBundle, OutputAdapter

# Jellyfin's native format groups thumbnails into 10x10 sheets.
_TILE_W = 10
_TILE_H = 10
_TILES_PER_SHEET = _TILE_W * _TILE_H


class JellyfinTrickplayAdapter(OutputAdapter):
    """Publish Jellyfin's native JPG-tile trickplay layout next to the media file.

    Args:
        width: Pixel width of each thumbnail. Must match the width the
            FFmpeg frame-extraction stage produced — sheets are composed
            by reading frame dimensions from the on-disk JPGs, but
            packing into the manifest needs the canonical width too so
            Jellyfin's player picks the right sheet for the requested
            resolution.
        frame_interval: Seconds between successive frames; persisted in
            the manifest as milliseconds.
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
        # The manifest is keyed by Jellyfin's item id; the dispatcher
        # must surface that before publish.
        return True

    def compute_output_paths(
        self,
        bundle: BifBundle,
        server: MediaServer | None,
        item_id: str | None,
    ) -> list[Path]:
        """Return the manifest path; tile-sheet paths are derived during publish.

        Raises:
            ValueError: When ``item_id`` is missing — the manifest is
                keyed by item id and Jellyfin's web client looks the
                trickplay data up by item, so without it the output
                would be invalid.
        """
        if item_id is None:
            raise ValueError(
                "JellyfinTrickplayAdapter requires an item_id (the manifest is keyed by Jellyfin's item id)"
            )
        # We deliberately do NOT touch the server here — the item id is
        # already known from the source webhook or library scan.
        del server, item_id  # unused; the dispatcher passes item_id again at publish time
        return [self.manifest_path(bundle.canonical_path, width=self._width)]

    @staticmethod
    def manifest_path(canonical_path: str, *, width: int) -> Path:
        """Compute the manifest JSON path for a media file at the given width.

        Public helper mirroring :meth:`EmbyBifAdapter.sidecar_path` so
        callers (BIF Viewer, output-status diagnostics) can compute the
        path without instantiating an adapter.
        """
        media_path = Path(canonical_path)
        basename = media_path.stem
        return media_path.parent / "trickplay" / f"{basename}-{int(width)}.json"

    def publish(self, bundle: BifBundle, output_paths: list[Path], item_id: str | None = None) -> None:
        """Pack ``bundle.frame_dir`` JPG frames into Jellyfin tile sheets + manifest.

        ``output_paths[0]`` is the manifest path (computed by
        :meth:`compute_output_paths`). Sheet directory and contents are
        derived from it. ``item_id`` is the Jellyfin item id this
        manifest indexes — the same value that was passed to
        :meth:`compute_output_paths`.
        """
        if not output_paths:
            raise ValueError("JellyfinTrickplayAdapter.publish requires the manifest path")
        if not item_id:
            raise ValueError("JellyfinTrickplayAdapter.publish requires the Jellyfin item_id")

        manifest_path = output_paths[0]
        sheets_dir = manifest_path.with_suffix("")  # strip .json → trickplay/<basename>-<width>/
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

        manifest_payload = self._build_manifest(
            item_id=item_id,
            thumbnail_count=len(frames),
            thumb_w=thumb_w,
            thumb_h=thumb_h,
        )
        with manifest_path.open("w", encoding="utf-8") as f:
            json.dump(manifest_payload, f, indent=2)

        logger.debug(
            "Jellyfin trickplay published: {} sheet(s) + manifest at {}",
            sheets_written,
            manifest_path,
        )

    def _build_manifest(
        self,
        *,
        item_id: str,
        thumbnail_count: int,
        thumb_w: int,
        thumb_h: int,
    ) -> dict:
        """Build the manifest dict; structure verified against @jellyfin/sdk typings."""
        bandwidth = thumbnail_count * thumb_w * thumb_h * 3  # rough; Jellyfin is permissive
        return {
            "Trickplay": {
                item_id: {
                    str(self._width): {
                        "Width": int(thumb_w),
                        "Height": int(thumb_h),
                        "TileWidth": _TILE_W,
                        "TileHeight": _TILE_H,
                        "ThumbnailCount": int(thumbnail_count),
                        "Interval": int(self._frame_interval) * 1000,
                        "Bandwidth": int(bandwidth),
                    }
                }
            }
        }


def _measure_first_frame(frame_path: Path) -> tuple[int, int]:
    """Read the first frame's dimensions to size each tile in the sheet."""
    with Image.open(frame_path) as img:
        return img.size
