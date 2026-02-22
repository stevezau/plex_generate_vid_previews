#!/bin/bash
# Wrapper script to run plex-generate-previews with proper user permissions
# This script is executed by s6-overlay after user/group setup
#
# Default: Start web UI on port 8080
# Use --cli flag to run in CLI mode instead

cd /app || exit 1

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
    echo "Example docker-compose.yml:"
    echo ""
    echo "  services:"
    echo "    previews:"
    echo "      image: stevezzau/plex_generate_vid_previews:latest"
    echo "      # init: true  ← REMOVE THIS LINE"
    echo "      environment:"
    echo "        - PLEX_URL=http://localhost:32400"
    echo "        ..."
    echo ""
    echo "Why? s6-overlay is already a better init system - you don't need both!"
    echo ""
    echo "More info: https://github.com/stevezau/plex_generate_vid_previews#troubleshooting"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    exit 1
fi

# Determine run mode
RUN_MODE="web"
CLI_ARGS=()

for arg in "$@"; do
    if [ "$arg" = "--cli" ]; then
        RUN_MODE="cli"
    else
        CLI_ARGS+=("$arg")
    fi
done

# Function to run command with proper user
run_as_user() {
    if [ "$(id -u)" = "0" ]; then
        # Running as root - drop privileges to abc user
        exec gosu abc "$@"
    else
        # Already running as non-root - just run directly
        exec "$@"
    fi
}

# Run in appropriate mode
if [ "$RUN_MODE" = "cli" ]; then
    # CLI mode - run the original command line tool
    echo "Running in CLI mode..."
    run_as_user plex-generate-previews "${CLI_ARGS[@]}"
else
    # Web mode - start gunicorn with threaded workers for production
    echo "Starting gunicorn web server on port ${WEB_PORT:-8080}..."
    run_as_user gunicorn \
        --bind "0.0.0.0:${WEB_PORT:-8080}" \
        --worker-class gthread \
        --threads 4 \
        --workers 1 \
        --timeout 300 \
        --graceful-timeout 30 \
        --keep-alive 65 \
        --access-logfile - \
        --error-logfile - \
        --log-level info \
        "plex_generate_previews.web.wsgi:app"
fi
