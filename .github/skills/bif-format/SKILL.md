# BIF File Format Skill

Expertise in Roku's BIF (Base Index Frame) format used by Plex for video preview thumbnails.

## When to Use

- Debugging BIF generation failures
- Modifying thumbnail extraction
- Troubleshooting Plex preview display issues
- Understanding the file format internals

## BIF Binary Structure

```
Offset  Size  Field
──────────────────────────────────────
0x00    8     Magic: 0x89 0x42 0x49 0x46 0x0d 0x0a 0x1a 0x0a
0x08    4     Version (uint32 LE, always 0)
0x0C    4     Image count (uint32 LE)
0x10    4     Frame interval in ms (uint32 LE, default 5000)
0x14    44    Reserved (zeros)
0x40    ...   Index table (8 bytes per image)
              └── timestamp (uint32) + offset (uint32)
              └── Terminator: 0xffffffff + final offset
...     ...   Concatenated JPEG image data
```

## Implementation

See [generate_bif()](../../../plex_generate_previews/media_processing.py#L701):

```python
magic = [0x89, 0x42, 0x49, 0x46, 0x0d, 0x0a, 0x1a, 0x0a]
f.write(struct.pack("<I", version))           # Little-endian uint32
f.write(struct.pack("<I", len(images)))
f.write(struct.pack("<I", 1000 * frame_interval))
```

## Generation Pipeline

1. **FFmpeg extraction**: Extract frames at `1/interval` fps
2. **JPEG files**: Save as numbered `00001.jpg`, `00002.jpg`, etc.
3. **BIF packing**: Pack header + index + images into single `.bif`
4. **Cleanup**: Remove temporary JPEGs

## Output Location

```
{PLEX_LOCAL_DATA_PATH}/Media/localhost/{hash}/Indexes/index-sd.bif
```

The hash is derived from the media file's metadata key.

## Debugging BIF Issues

**Empty BIF**: Check FFmpeg extracted frames (look in tmp directory)

**Plex not showing previews**: Verify file at correct path, correct permissions

**Corrupted BIF**: Validate magic bytes, check image count matches index entries

## Configuration

- `PLEX_BIF_FRAME_INTERVAL`: Seconds between frames (default: 5)
- Lower = more granular previews but larger files
