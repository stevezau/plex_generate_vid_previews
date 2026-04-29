"""Emby sidecar BIF output adapter.

Emby's "Save preview video thumbnails into media folders" feature drops
``{basename}-{width}-{interval}.bif`` next to the source video; the
client picks them up automatically on library scan. Reproducing that
naming exactly means our generated BIFs slot into Emby installations as
if Emby had produced them itself ([forum discussion](
https://emby.media/community/topic/112001-what-is-a-bif-file-and-why-do-all-have-320-10-at-end-of-filename/)).

Unlike :class:`PlexBundleAdapter`, this adapter doesn't need any
server-side metadata — the output path is derived purely from the
canonical media path plus the configured width and interval.
"""

from __future__ import annotations

from pathlib import Path

from loguru import logger

from ..servers.base import MediaServer
from .base import BifBundle, OutputAdapter


class EmbyBifAdapter(OutputAdapter):
    """Publish Emby-style sidecar BIF files alongside the media file.

    Args:
        width: BIF thumbnail width in pixels (Emby's default is 320).
            Encoded into the filename so multiple resolutions can coexist.
        frame_interval: Seconds between frames. Encoded into the filename
            (Emby uses this on its own generation runs too).
    """

    def __init__(self, *, width: int = 320, frame_interval: int = 10) -> None:
        self._width = int(width)
        self._frame_interval = int(frame_interval)

    @property
    def name(self) -> str:
        return "emby_sidecar"

    def needs_server_metadata(self) -> bool:
        # Sidecar path is derived purely from the canonical media path;
        # no API calls needed before publishing.
        return False

    def compute_output_paths(
        self,
        bundle: BifBundle,
        server: MediaServer | None,
        item_id: str | None,
    ) -> list[Path]:
        """Return ``[<media_dir>/<basename>-<width>-<interval>.bif]``."""
        media_path = Path(bundle.canonical_path)
        basename = media_path.stem  # without extension
        sidecar = media_path.parent / f"{basename}-{self._width}-{self._frame_interval}.bif"
        return [sidecar]

    def publish(self, bundle: BifBundle, output_paths: list[Path], item_id: str | None = None) -> None:
        """Pack ``bundle.frame_dir`` into a BIF at the sidecar path.

        Reuses the existing ``generate_bif`` helper so the BIF byte layout
        stays in lockstep with the Plex publisher. The media folder must
        already exist (it does — we only got here because the source file
        is present); we only create missing parent directories defensively
        for unusual mount setups.
        """
        if not output_paths:
            raise ValueError("EmbyBifAdapter.publish requires at least one output path")

        sidecar = output_paths[0]
        try:
            sidecar.parent.mkdir(parents=True, exist_ok=True)
        except PermissionError as exc:
            logger.error(
                "Cannot save Emby preview file next to media at {}: permission denied. "
                "The Emby BIF format requires writing the .bif file alongside the source video. "
                "Verify the media folder is mounted read-write (not :ro in Docker) and that "
                "the user running this tool has write permission. "
                "Original error: {}",
                sidecar.parent,
                exc,
            )
            raise

        from ..processing.orchestrator import generate_bif
        from .plex_bundle import BifIntervalConfig

        generate_bif(
            str(sidecar),
            str(bundle.frame_dir),
            BifIntervalConfig(self._frame_interval),
        )

        # Sanity: filename must follow Emby's <basename>-<w>-<i>.bif pattern.
        # If a future caller misuses compute_output_paths and passes a
        # custom path, Emby won't pick it up — log a warning so it's
        # diagnosable in the field.
        if not sidecar.name.endswith(f"-{self._width}-{self._frame_interval}.bif"):
            logger.warning(
                "Emby preview file saved with an unexpected name: {}. "
                "The file is on disk and is valid, but Emby looks for the pattern "
                "'<video-name>-<width>-<interval>.bif' to auto-discover previews — "
                "with this name Emby will likely ignore it. This is a configuration "
                "bug worth reporting; previews on other servers are unaffected.",
                sidecar.name,
            )

        logger.debug("Emby sidecar BIF written to {}", sidecar)

    @staticmethod
    def sidecar_path(canonical_path: str, *, width: int, frame_interval: int) -> Path:
        """Public helper used by tests + future read-side code (BIF viewer).

        Returns the Emby sidecar path that *would* be written for the given
        media path / width / interval, without instantiating an adapter.
        """
        media_path = Path(canonical_path)
        basename = media_path.stem
        return media_path.parent / f"{basename}-{int(width)}-{int(frame_interval)}.bif"
