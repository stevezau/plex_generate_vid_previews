#!/bin/bash
# Generate synthetic test media for the multi-media-server integration stack.
#
# Uses ffmpeg's lavfi testsrc filter to produce deterministic 30-second
# clips in several codecs, exercising the same code paths the GPU pipeline
# hits for real media. Output is byte-identical run-to-run so frame-cache
# assertions in tests stay stable.
#
# Files are placed into ``tests/integration/media/`` matching Plex's
# expected library layout (Movies/<title>/<title>.mkv,
# TV Shows/<show>/Season XX/<show> - SxxExx.mkv).

set -euo pipefail

readonly HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly MEDIA_DIR="${HERE}/media"

if ! command -v ffmpeg >/dev/null 2>&1; then
    echo "error: ffmpeg not found on PATH" >&2
    exit 1
fi

mkdir -p \
    "${MEDIA_DIR}/Movies/Test Movie H264 (2024)" \
    "${MEDIA_DIR}/Movies/Test Movie HEVC (2024)" \
    "${MEDIA_DIR}/Movies/Test Movie VP9 (2024)" \
    "${MEDIA_DIR}/Movies/Test Movie AV1 (2024)" \
    "${MEDIA_DIR}/TV Shows/Test Show/Season 01"

generate() {
    local out="$1"
    local codec="$2"
    local size="$3"
    local duration="$4"

    if [[ -f "${out}" ]]; then
        echo "skipping (already exists): ${out}"
        return
    fi

    echo "generating ${out} (${codec}, ${size}, ${duration}s)..."
    ffmpeg -loglevel error -y \
        -f lavfi -i "testsrc2=size=${size}:rate=30:duration=${duration}" \
        -f lavfi -i "sine=frequency=440:duration=${duration}" \
        -c:v "${codec}" -pix_fmt yuv420p \
        -c:a aac -b:a 128k \
        -movflags +faststart \
        "${out}"
}

generate \
    "${MEDIA_DIR}/Movies/Test Movie H264 (2024)/Test Movie H264 (2024).mkv" \
    libx264 1280x720 30

generate \
    "${MEDIA_DIR}/Movies/Test Movie HEVC (2024)/Test Movie HEVC (2024).mkv" \
    libx265 1920x1080 30

generate \
    "${MEDIA_DIR}/TV Shows/Test Show/Season 01/Test Show - S01E01 - Pilot.mkv" \
    libx264 1280x720 30

generate \
    "${MEDIA_DIR}/TV Shows/Test Show/Season 01/Test Show - S01E02 - Two.mkv" \
    libx264 1280x720 30

# VP9 (libvpx-vp9) — exercises the WebM container + VP9 codec path.
# Encoded faster than typical (-deadline good) since we only need a
# valid stream, not visual quality.
if [[ ! -f "${MEDIA_DIR}/Movies/Test Movie VP9 (2024)/Test Movie VP9 (2024).mkv" ]]; then
    echo "generating Test Movie VP9 (2024).mkv (libvpx-vp9, 1280x720, 30s)..."
    ffmpeg -loglevel error -y \
        -f lavfi -i "testsrc2=size=1280x720:rate=30:duration=30" \
        -f lavfi -i "sine=frequency=440:duration=30" \
        -c:v libvpx-vp9 -deadline good -cpu-used 4 -row-mt 1 -pix_fmt yuv420p \
        -c:a libopus -b:a 128k \
        "${MEDIA_DIR}/Movies/Test Movie VP9 (2024)/Test Movie VP9 (2024).mkv"
fi

# AV1 (libsvtav1) — fast SVT-AV1 encoder, exercises the AV1 decode path.
# preset 8 is one of the fastest; quality irrelevant for fixture.
if [[ ! -f "${MEDIA_DIR}/Movies/Test Movie AV1 (2024)/Test Movie AV1 (2024).mkv" ]]; then
    echo "generating Test Movie AV1 (2024).mkv (libsvtav1, 1280x720, 30s)..."
    ffmpeg -loglevel error -y \
        -f lavfi -i "testsrc2=size=1280x720:rate=30:duration=30" \
        -f lavfi -i "sine=frequency=440:duration=30" \
        -c:v libsvtav1 -preset 8 -crf 50 -pix_fmt yuv420p \
        -c:a aac -b:a 128k \
        "${MEDIA_DIR}/Movies/Test Movie AV1 (2024)/Test Movie AV1 (2024).mkv"
fi

echo
echo "Done. Generated files under ${MEDIA_DIR}:"
find "${MEDIA_DIR}" -type f -name '*.mkv' | sort
