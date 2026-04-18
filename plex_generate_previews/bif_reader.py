"""BIF (Base Index Frame) file reader.

Provides random-access reading of Roku BIF files used by Plex for video
preview thumbnails.  The write side lives in media_processing.generate_bif().
"""

import os
import struct
from dataclasses import dataclass, field

BIF_MAGIC = bytes([0x89, 0x42, 0x49, 0x46, 0x0D, 0x0A, 0x1A, 0x0A])
_HEADER_SIZE = 64
_SENTINEL_TIMESTAMP = 0xFFFFFFFF


@dataclass(frozen=True)
class BifMetadata:
    """Parsed BIF file header and index information."""

    path: str
    version: int
    frame_count: int
    frame_interval_ms: int
    file_size: int
    created_at: float
    frame_offsets: list[int] = field(repr=False)
    frame_sizes: list[int] = field(repr=False)


def read_bif_metadata(path: str) -> BifMetadata:
    """Parse BIF header and index table without loading image data.

    Args:
        path: Absolute path to a .bif file.

    Returns:
        BifMetadata with frame count, interval, offsets, and sizes.

    Raises:
        FileNotFoundError: If the file does not exist.
        ValueError: If the file is not a valid BIF file.

    """
    file_size = os.path.getsize(path)
    created_at = os.path.getmtime(path)

    with open(path, "rb") as f:
        magic = f.read(8)
        if magic != BIF_MAGIC:
            raise ValueError(f"Not a valid BIF file (bad magic): {path}")

        version = struct.unpack("<I", f.read(4))[0]
        frame_count = struct.unpack("<I", f.read(4))[0]
        frame_interval_ms = struct.unpack("<I", f.read(4))[0]

        f.seek(_HEADER_SIZE)

        offsets: list[int] = []
        for _ in range(frame_count):
            f.read(4)  # skip timestamp (sequential counter, unused)
            offset = struct.unpack("<I", f.read(4))[0]
            offsets.append(offset)

        sentinel_ts = struct.unpack("<I", f.read(4))[0]
        end_offset = struct.unpack("<I", f.read(4))[0]
        if sentinel_ts != _SENTINEL_TIMESTAMP:
            raise ValueError(f"Missing sentinel in BIF index table: {path}")

    all_offsets = offsets + [end_offset]
    sizes = [all_offsets[i + 1] - all_offsets[i] for i in range(len(offsets))]

    return BifMetadata(
        path=path,
        version=version,
        frame_count=frame_count,
        frame_interval_ms=frame_interval_ms,
        file_size=file_size,
        created_at=created_at,
        frame_offsets=offsets,
        frame_sizes=sizes,
    )


def read_bif_frame(path: str, index: int, metadata: BifMetadata | None = None) -> bytes:
    """Extract a single JPEG frame from a BIF file.

    Args:
        path: Absolute path to a .bif file.
        index: Zero-based frame index.
        metadata: Pre-parsed metadata (avoids re-reading the index table).

    Returns:
        Raw JPEG bytes for the requested frame.

    Raises:
        IndexError: If index is out of range.
        ValueError: If the file is not a valid BIF file.

    """
    if metadata is None:
        metadata = read_bif_metadata(path)

    if index < 0 or index >= metadata.frame_count:
        raise IndexError(f"Frame index {index} out of range (0..{metadata.frame_count - 1})")

    offset = metadata.frame_offsets[index]
    size = metadata.frame_sizes[index]

    with open(path, "rb") as f:
        f.seek(offset)
        return f.read(size)
