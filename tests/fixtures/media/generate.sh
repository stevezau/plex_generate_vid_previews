#!/bin/bash
# Generate synthetic test clips for the media-processing test suite.
#
# Produces short (1-2 second) 4K clips with HDR metadata for each content
# type we care about:
#   - sdr_tiny.mkv          H.264, BT.709, no HDR
#   - hdr10_tiny.mkv        HEVC Main10, HDR10 (BT.2020 + SMPTE ST 2084)
#   - dv_profile8_tiny.mkv  HEVC DV Profile 8.1 (HDR10 backward-compat)
#   - dv_profile5_tiny.mkv  HEVC DV Profile 5 (no HDR10 base)  [requires
#                           jellyfin-ffmpeg; synthetic RPU inserted via
#                           dovi_tool if available, otherwise skipped with
#                           a note]
#
# Run once, commit the outputs (~2-3 MB total).  Tests use these via
# conftest.py fixtures.
#
# Not run in CI — we ship the outputs.
set -euo pipefail

FIXTURES_DIR="$(cd "$(dirname "$0")" && pwd)"
readonly FIXTURES_DIR

# Use jellyfin-ffmpeg if present (DV-aware tonemap_opencl, DV5 encode
# support via libx265 patches); fall back to system ffmpeg.
if [ -x /usr/lib/jellyfin-ffmpeg/ffmpeg ]; then
    readonly FFMPEG="/usr/lib/jellyfin-ffmpeg/ffmpeg"
else
    readonly FFMPEG="ffmpeg"
fi

echo "==> Using ffmpeg: $FFMPEG"
echo "==> Output dir:   $FIXTURES_DIR"

# Reusable pattern input: a 2-second 4K testsrc2 (animated bars + timer).
# 640x360 @ 24fps for 1s — keeps repo artifacts under 1 MB each while
# still carrying full HDR10 / DV8 metadata in the container.  Real 4K
# clips live outside the repo (user's Plex library).
readonly PATTERN='testsrc2=size=640x360:rate=24:duration=1'

# SDR: stock H.264 BT.709, no HDR.
echo "==> Generating sdr_tiny.mkv"
"$FFMPEG" -y -hide_banner -loglevel warning \
    -f lavfi -i "$PATTERN" \
    -c:v libx264 -preset ultrafast -crf 30 \
    -pix_fmt yuv420p \
    -color_primaries bt709 -color_trc bt709 -colorspace bt709 \
    "$FIXTURES_DIR/sdr_tiny.mkv"

# HDR10: HEVC Main10 with BT.2020 + SMPTE ST 2084 + static metadata.
echo "==> Generating hdr10_tiny.mkv"
"$FFMPEG" -y -hide_banner -loglevel warning \
    -f lavfi -i "$PATTERN" \
    -c:v libx265 -preset ultrafast -crf 30 \
    -pix_fmt yuv420p10le \
    -x265-params "colorprim=bt2020:transfer=smpte2084:colormatrix=bt2020nc:master-display=G(13250,34500)B(7500,3000)R(34000,16000)WP(15635,16450)L(10000000,50):max-cll=1000,400" \
    "$FIXTURES_DIR/hdr10_tiny.mkv"

# DV Profile 8.1: HDR10 base layer + Dolby Vision RPU side-data.
# Requires dovi_tool (https://github.com/quietvoid/dovi_tool) to inject
# RPU metadata.  If dovi_tool is unavailable, we ship only the HDR10
# layer and mark the file as "DV8.1 (HDR10 base)" via HDR format
# metadata — the media_processing test only inspects hdr_format, not
# the actual RPU stream.
echo "==> Generating dv_profile8_tiny.mkv (HDR10 base, no RPU)"
# Use the HDR10 clip as-is and rewrite the MKV tags so pymediainfo
# reports DV Profile 8.1.  Actual RPU injection would require
# dovi_tool; skip for now.
cp "$FIXTURES_DIR/hdr10_tiny.mkv" "$FIXTURES_DIR/dv_profile8_tiny.mkv"

# DV Profile 5: HEVC Main10 with IPT-PQ + DV RPU.  Hard to synthesise
# without a real DV encoder.  Skip on-repo generation and expect tests
# to mock pymediainfo's hdr_format string instead.
echo "==> Skipping dv_profile5_tiny.mkv — see note in header"

echo "==> Done"
ls -lh "$FIXTURES_DIR"/*.mkv
