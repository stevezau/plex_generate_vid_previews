#!/bin/bash
# Re-record vendor-API contract cassettes against the test stack.
#
# Reads credentials from ./servers.env (produced by ./up.sh) and runs
# the cassette test modules with --record-mode=once. Existing cassettes
# are preserved unless explicitly deleted; pass --clean to drop them
# all and re-record from scratch.
#
# Why use the test stack instead of the user's live servers:
# - No risk of leaking real library data into a committed cassette.
# - Deterministic synthetic media → deterministic responses → smaller diffs.
# - Re-runnable end-to-end with no manual prep.
#
# Usage:
#   ./tests/integration/up.sh                       # stand the stack up
#   ./tests/integration/record-cassettes.sh         # record only missing
#   ./tests/integration/record-cassettes.sh --clean # drop + re-record all

set -euo pipefail

readonly HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly REPO="$(cd "${HERE}/../.." && pwd)"
readonly SERVERS_ENV="${HERE}/servers.env"
readonly CASSETTE_DIR="${REPO}/tests/cassettes"
readonly TEST_FILES=(
    "${REPO}/tests/test_servers_plex_vcr.py"
    "${REPO}/tests/test_servers_emby_vcr.py"
    "${REPO}/tests/test_servers_jellyfin_vcr.py"
)

if [[ ! -f "${SERVERS_ENV}" ]]; then
    echo "ERROR: ${SERVERS_ENV} not found — run ./up.sh first" >&2
    exit 1
fi

# shellcheck disable=SC1090
set -a; source "${SERVERS_ENV}"; set +a

# pytest-recording reads PLEX_URL / PLEX_TOKEN / EMBY_URL / EMBY_TOKEN /
# EMBY_USER_ID / JELLYFIN_URL / JELLYFIN_TOKEN. servers.env uses
# *_ACCESS_TOKEN naming; rebind here so the test fixtures see the
# variables they expect.
export PLEX_URL="${PLEX_URL:-}"
export PLEX_TOKEN="${PLEX_ACCESS_TOKEN:-}"
export EMBY_URL="${EMBY_URL:-}"
export EMBY_TOKEN="${EMBY_ACCESS_TOKEN:-}"
export EMBY_USER_ID="${EMBY_USER_ID:-}"
export JELLYFIN_URL="${JELLYFIN_URL:-}"
export JELLYFIN_TOKEN="${JELLYFIN_ACCESS_TOKEN:-}"

if [[ "${1:-}" == "--clean" ]]; then
    echo "==> dropping existing cassettes under ${CASSETTE_DIR}/test_servers_*_vcr/"
    find "${CASSETTE_DIR}" -mindepth 2 -maxdepth 2 -name '*.yaml' -path '*/test_servers_*_vcr/*' -print -delete || true
fi

PYTHON="${PYTHON:-/home/data/.venv/bin/python}"
if [[ ! -x "${PYTHON}" ]]; then
    PYTHON="$(command -v python3 || command -v python)"
fi

echo
echo "==> recording cassettes (PLEX=${PLEX_URL}  EMBY=${EMBY_URL}  JELLYFIN=${JELLYFIN_URL})"
"${PYTHON}" -m pytest "${TEST_FILES[@]}" --record-mode=once --no-cov -v

echo
echo "==> done. inspect ${CASSETTE_DIR}/test_servers_*_vcr/ before committing"
echo "   verify scrub:    grep -rE '(X-Plex-Token|X-Emby-Token):' ${CASSETTE_DIR} | grep -v FAKE_ || echo OK"
