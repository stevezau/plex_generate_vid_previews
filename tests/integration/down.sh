#!/bin/bash
# Stop the integration test stack — KEEPS volumes.
#
# Containers shut down but config volumes persist, so the next
# ./up.sh reuses the same Plex claim, the same Emby admin, and the same
# Jellyfin API key. Use ./wipe.sh for a full reset (will need a fresh
# PLEX_CLAIM).

set -euo pipefail
readonly HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec docker compose -f "${HERE}/docker-compose.test.yml" down
