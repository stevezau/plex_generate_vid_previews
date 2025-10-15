#!/bin/bash
# Wrapper script to run plex-generate-previews with proper user permissions
# This script is executed by s6-overlay after user/group setup

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

# Check if we're running as root (UID 0)
if [ "$(id -u)" = "0" ]; then
    # Running as root - drop privileges to abc user
    # gosu preserves environment variables
    exec gosu abc plex-generate-previews "$@"
else
    # Already running as non-root - just run directly
    exec plex-generate-previews "$@"
fi

