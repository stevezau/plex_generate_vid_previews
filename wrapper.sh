#!/bin/bash
# Wrapper script to run plex-generate-previews web server.
# Executed by s6-overlay after user/group setup.
set -euo pipefail

cd /app

# Check if init: true is preventing s6-overlay from running
if [ ! -d "/run/s6" ] && ps -p 1 -o comm= 2>/dev/null | grep -qE '(tini|docker-init)'; then
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "❌ ERROR: 'init: true' detected in your Docker configuration"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""
    echo "This container uses s6-overlay (LinuxServer.io base) which is MORE"
    echo "capable than Docker's basic init. Using 'init: true' prevents s6-overlay"
    echo "from running and you LOSE these features:"
    echo ""
    echo "  ❌ PUID/PGID support (file permissions will be wrong!)"
    echo "  ❌ Process supervision and auto-restart"
    echo "  ❌ Proper initialization scripts"
    echo "  ❌ Better signal handling and logging"
    echo ""
    echo "HOW TO FIX:"
    echo ""
    echo "  Docker Compose: Remove the 'init: true' line from your compose file"
    echo "  Docker CLI: Remove the '--init' flag from your docker run command"
    echo ""
    echo "More info: https://github.com/stevezau/plex_generate_vid_previews#troubleshooting"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    exit 1
fi

# Function to run command with proper user
run_as_user() {
    if [ "$(id -u)" = "0" ]; then
        exec gosu abc "$@"
    else
        exec "$@"
    fi
}

# Start gunicorn web server
RELOAD_FLAG=""
if [ "${DEV_RELOAD:-false}" = "true" ]; then
    RELOAD_FLAG="--reload"
    echo "Starting gunicorn web server on port ${WEB_PORT:-8080} (live reload enabled)..."
else
    echo "Starting gunicorn web server on port ${WEB_PORT:-8080}..."
fi
run_as_user gunicorn \
    --bind "0.0.0.0:${WEB_PORT:-8080}" \
    --worker-class gthread \
    --threads 8 \
    --workers 1 \
    --timeout 300 \
    --graceful-timeout 30 \
    --keep-alive 65 \
    --error-logfile - \
    --log-level info \
    $RELOAD_FLAG \
    "media_preview_generator.web.wsgi:app"
