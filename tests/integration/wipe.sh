#!/bin/bash
# Fully reset the integration test stack — STOPS containers AND wipes volumes.
#
# Use when you want a clean slate — e.g. testing the bootstrap flow itself,
# or recovering from a corrupt volume. The next ./up.sh will require a
# fresh PLEX_CLAIM token from https://plex.tv/claim (4-min validity).

set -euo pipefail
readonly HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
echo "==> docker compose down -v (this WILL erase the Plex token, Emby admin, Jellyfin API key)"
docker compose -f "${HERE}/docker-compose.test.yml" down -v
rm -f "${HERE}/servers.env"
echo "==> done. next ./up.sh will need PLEX_CLAIM=claim-XXXXXXXX"
