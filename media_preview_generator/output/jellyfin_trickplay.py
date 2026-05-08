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

Publish is **atomic** — tiles are packed into a sibling staging directory
and only rename'd into place when every sheet is written. This closes the
race where Jellyfin's 3 AM ``TrickplayImagesTask`` (or a scan-triggered
refresh) could land mid-write, synthesise ``TrickplayInfo`` from a partial
set, and permanently poison its DB with the wrong ``ThumbnailCount``
(``TrickplayManager.cs`` L243–291 short-circuits on any existing tile count).

The required Jellyfin library options for this layout to be picked up::

    EnableTrickplayImageExtraction = true   # detection / serving gate
    SaveTrickplayWithMedia        = true    # look in <basename>.trickplay/...
    ExtractTrickplayImagesDuringLibraryScan = false  # with plugin (Mode A)
                                             # or true without (Mode B)

Note that ``EnableTrickplayImageExtraction = false`` is destructive in
Jellyfin — it deletes the trickplay directory on the next refresh
(``TrickplayManager.cs`` L118–133).

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
import shutil
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
        # Tile layout + geometry are derivable from ``canonical_path``
        # alone. ``item_id`` is an optional fast-path (the Media Preview
        # Bridge plugin's instant-activation endpoint) — the adapter
        # itself never needs it, and ``publish()`` writes successfully
        # with ``item_id=None``.
        return False

    def compute_output_paths(
        self,
        bundle: BifBundle,
        server: MediaServer | None,
        item_id: str | None,
    ) -> list[Path]:
        """Return the path of sheet 0 — used for the freshness check.

        Sheet 0 is always present whenever any trickplay output exists
        for this item, so its mtime is a safe stand-in for the whole
        trickplay output's freshness. ``item_id`` is ignored — the path
        is derived entirely from ``bundle.canonical_path``.
        """
        del server, item_id  # layout is derived purely from canonical_path
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

        The write is atomic: tiles go into a sibling ``.<basename>.trickplay.staging/``
        directory, then the three-step rename below swaps it into place.
        Jellyfin's adoption path never observes a partial tile set.

        Rename sequence (keeps the OLD complete tiles live until the NEW
        ones are ready, so a mid-swap crash leaves a valid directory)::

            1. os.rename(final, .trickplay.old)   # atomic; old survives
            2. os.rename(staging, final)          # atomic; new in place
            3. shutil.rmtree(.trickplay.old)      # cleanup (not atomic, don't care)

        Between steps 1 and 2 ``final`` is missing for microseconds. A
        Jellyfin adoption check hitting that window skips (no dir) and
        our follow-up ``trigger_refresh`` seconds later lands on the
        complete new dir.

        Falls back to the legacy in-place write on filesystems where
        directory rename isn't atomic (FUSE / SMB / some overlays).

        ``output_paths[0]`` is the sheet-0 path (computed by
        :meth:`compute_output_paths`). All sibling sheets are written
        into the same parent directory. ``item_id`` is accepted for
        interface symmetry with :class:`PlexBundleAdapter` but is not
        used — tiles are written regardless.
        """
        if not output_paths:
            raise ValueError("JellyfinTrickplayAdapter.publish requires the sheet-0 path")
        del item_id  # not used for tile writes; kept in signature for interface symmetry

        sheets_dir = output_paths[0].parent  # <basename>.trickplay/<W> - 10x10
        final_dir = sheets_dir.parent  # <basename>.trickplay
        staging_dir = final_dir.parent / f".{final_dir.name}.staging"
        old_dir = final_dir.parent / f".{final_dir.name}.old"

        staging_sheets_dir = staging_dir / sheets_dir.name  # mirror width-x-height

        frames = sorted(p for p in os.listdir(bundle.frame_dir) if p.lower().endswith(".jpg"))
        if not frames:
            raise RuntimeError(f"No JPG frames found under {bundle.frame_dir}; cannot pack Jellyfin trickplay")

        # Clean up any debris from a prior aborted run, then make the
        # staging dir fresh. Failure to rmtree is tolerable — mkdir will
        # surface the real problem if one exists.
        shutil.rmtree(staging_dir, ignore_errors=True)
        shutil.rmtree(old_dir, ignore_errors=True)
        try:
            staging_sheets_dir.mkdir(parents=True, exist_ok=True)
        except PermissionError as exc:
            logger.error(
                "Cannot create Jellyfin trickplay staging folder at {}: permission denied. "
                "The container or process running this tool needs WRITE access to the "
                "folder containing this media file. "
                "If running in Docker, make sure your media volume is mounted read-write "
                "(not :ro) and the PUID/PGID env vars match the file owner. "
                "Original error: {}",
                staging_sheets_dir,
                exc,
            )
            raise

        thumb_w, thumb_h = _measure_first_frame(Path(bundle.frame_dir) / frames[0])
        sheets_written = _pack_sheets_into_dir(
            frames=frames,
            frame_dir=Path(bundle.frame_dir),
            sheets_dir=staging_sheets_dir,
            thumb_w=thumb_w,
            thumb_h=thumb_h,
            jpeg_quality=self._jpeg_quality,
        )

        # fsync each sheet + the sheets dir + the staging root so a crash
        # after the rename leaves durable files on disk.
        _fsync_tree(staging_dir)

        # Atomic-ish directory swap. ``os.rename`` is atomic per-step on
        # POSIX; the gap between steps 1 and 2 is microseconds.
        try:
            if final_dir.exists():
                os.rename(final_dir, old_dir)
            os.rename(staging_dir, final_dir)
        except OSError as exc:
            logger.warning(
                "Atomic rename of Jellyfin trickplay dir failed at {} ({}: {}). "
                "Falling back to in-place write — Jellyfin may briefly see a "
                "partial tile set mid-write on this filesystem. Race is still "
                "bounded (sheet count is known in advance) but atomic adoption "
                "is not guaranteed here.",
                final_dir,
                type(exc).__name__,
                exc,
            )
            # If step-1 succeeded but step-2 failed (rare: disk pressure
            # between the two renames), old_dir holds the *prior*
            # complete tile set. Restore it so we never lose a valid
            # publish to a mid-swap failure. Otherwise old_dir is absent
            # (never created) and the rename below is a no-op wrapped in
            # try/except.
            if old_dir.exists() and not final_dir.exists():
                try:
                    os.rename(old_dir, final_dir)
                except OSError as restore_exc:
                    logger.warning(
                        "Could not restore prior trickplay from {} → {}: {}",
                        old_dir,
                        final_dir,
                        restore_exc,
                    )
            _inplace_fallback_write(
                frames=frames,
                frame_dir=Path(bundle.frame_dir),
                sheets_dir=sheets_dir,
                thumb_w=thumb_w,
                thumb_h=thumb_h,
                jpeg_quality=self._jpeg_quality,
            )
            shutil.rmtree(staging_dir, ignore_errors=True)
            shutil.rmtree(old_dir, ignore_errors=True)
            logger.debug(
                "Jellyfin trickplay published (fallback in-place): {} sheet(s) at {}",
                sheets_written,
                sheets_dir,
            )
            return

        # Step 3: cleanup (not atomic; doesn't matter).
        shutil.rmtree(old_dir, ignore_errors=True)

        logger.debug(
            "Jellyfin trickplay published (atomic): {} sheet(s) at {}",
            sheets_written,
            sheets_dir,
        )


def _measure_first_frame(frame_path: Path) -> tuple[int, int]:
    """Read the first frame's dimensions to size each tile in the sheet."""
    with Image.open(frame_path) as img:
        return img.size


def _pack_sheets_into_dir(
    *,
    frames: list[str],
    frame_dir: Path,
    sheets_dir: Path,
    thumb_w: int,
    thumb_h: int,
    jpeg_quality: int,
) -> int:
    """Pack ``frames`` into 10x10 JPG sheets under ``sheets_dir``.

    Returns the number of sheets written. Extracted so the atomic path
    and the fallback-in-place path share one implementation.
    """
    sheets_written = 0
    for sheet_index in range(math.ceil(len(frames) / _TILES_PER_SHEET)):
        start = sheet_index * _TILES_PER_SHEET
        end = min(start + _TILES_PER_SHEET, len(frames))
        sheet_frames = frames[start:end]

        sheet_image = Image.new("RGB", (thumb_w * _TILE_W, thumb_h * _TILE_H), (0, 0, 0))
        for i, frame_name in enumerate(sheet_frames):
            with Image.open(frame_dir / frame_name) as src:
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
        sheet_image.save(sheet_path, "JPEG", quality=jpeg_quality)
        sheets_written += 1
    return sheets_written


def _inplace_fallback_write(
    *,
    frames: list[str],
    frame_dir: Path,
    sheets_dir: Path,
    thumb_w: int,
    thumb_h: int,
    jpeg_quality: int,
) -> None:
    """Legacy in-place write for filesystems where directory rename fails.

    Purges stale sheets and re-packs into ``sheets_dir`` directly. Not
    atomic — Jellyfin can briefly see a partial tile set during the
    write. Only reached when the staging-rename path raised ``OSError``.
    """
    sheets_dir.mkdir(parents=True, exist_ok=True)
    for stale in sheets_dir.glob("*.jpg"):
        try:
            stale.unlink()
        except OSError as exc:
            logger.debug("Could not remove stale tile {}: {}", stale, exc)
    _pack_sheets_into_dir(
        frames=frames,
        frame_dir=frame_dir,
        sheets_dir=sheets_dir,
        thumb_w=thumb_w,
        thumb_h=thumb_h,
        jpeg_quality=jpeg_quality,
    )


def _fsync_tree(root: Path) -> None:
    """Best-effort fsync of every file + directory under ``root``.

    Ensures a crash after the rename leaves durable files on disk.
    Non-fatal on filesystems without fsync support (e.g. Windows, some
    FUSE mounts) — a warning gets logged and publish proceeds.
    """
    try:
        for dirpath, _dirnames, filenames in os.walk(root):
            for fname in filenames:
                fp = os.path.join(dirpath, fname)
                try:
                    fd = os.open(fp, os.O_RDONLY)
                    try:
                        os.fsync(fd)
                    finally:
                        os.close(fd)
                except OSError as exc:
                    logger.debug("fsync failed for {}: {}", fp, exc)
            try:
                fd = os.open(dirpath, os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            except OSError as exc:
                logger.debug("fsync of dir {} failed: {}", dirpath, exc)
    except OSError as exc:
        logger.debug("fsync tree walk failed under {}: {}", root, exc)
