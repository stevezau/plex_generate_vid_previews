#!/bin/bash
# Bring up the integration test stack — idempotent.
#
# One-stop entry point for everything the cassette + integration tests
# need. Safe to re-run; skips work that's already done.
#
#   1. Generate synthetic test media (skips files that already exist).
#   2. docker compose up -d (reuses persisted volumes — Plex token survives).
#   3. Wait for each server to become healthy.
#   4. Run setup_servers.py to (re-)capture credentials into servers.env.
#
# First-time only: Plex requires a one-time claim token from
# https://plex.tv/claim. Pass via env var:
#
#     PLEX_CLAIM=claim-XXXXXXXX ./tests/integration/up.sh
#
# Subsequent runs need NO claim — the previous one is persisted in the
# plex_config volume.
#
# Tear-down:
#   ./tests/integration/down.sh   # stops containers, KEEPS volumes
#   ./tests/integration/wipe.sh   # stops containers AND wipes volumes
#                                 # (next ./up.sh will need a fresh PLEX_CLAIM)

set -euo pipefail

readonly HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly COMPOSE_FILE="${HERE}/docker-compose.test.yml"
readonly SERVERS_ENV="${HERE}/servers.env"

# 1. Synthetic media
"${HERE}/generate_test_media.sh"

# 2. docker compose up
echo
echo "==> docker compose up -d"
PLEX_CLAIM="${PLEX_CLAIM:-}" docker compose -f "${COMPOSE_FILE}" up -d

# 3. Wait for health
echo
echo "==> waiting for all three servers to become healthy"
for service in emby jellyfin plex; do
    container="previews-test-${service}"
    deadline=$(($(date +%s) + 180))
    while :; do
        # Plex's healthcheck targets /identity (200 OK once the server is
        # listening). Emby + Jellyfin target /System/Info/Public.
        status=$(docker inspect --format '{{.State.Health.Status}}' "${container}" 2>/dev/null || echo missing)
        if [[ "${status}" == "healthy" ]]; then
            echo "   ${service}: healthy"
            break
        fi
        if (( $(date +%s) > deadline )); then
            echo "ERROR: ${service} (${container}) did not become healthy in 180s — last status: ${status}" >&2
            docker logs --tail=50 "${container}" >&2 || true
            exit 1
        fi
        sleep 2
    done
done

# 4. Capture credentials
echo
echo "==> setup_servers.py"
PYTHON="${PYTHON:-/home/data/.venv/bin/python}"
if [[ ! -x "${PYTHON}" ]]; then
    PYTHON="$(command -v python3 || command -v python)"
fi
"${PYTHON}" "${HERE}/setup_servers.py"

echo
echo "==> done. credentials in ${SERVERS_ENV}"
echo "   to record cassettes:        ./tests/integration/record-cassettes.sh"
echo "   to run integration tests:   pytest -m integration --no-cov tests/integration/"
echo "   to stop (keep volumes):     ./tests/integration/down.sh"
echo "   to fully reset:             ./tests/integration/wipe.sh"
