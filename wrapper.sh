#!/bin/bash
# Wrapper script to run plex-generate-previews with proper user permissions
# This script is executed by s6-overlay after user/group setup

cd /app || exit 1

# Check if we're running as root (UID 0)
if [ "$(id -u)" = "0" ]; then
    # Running as root - drop privileges to abc user
    # gosu preserves environment variables
    exec gosu abc plex-generate-previews "$@"
else
    # Already running as non-root - just run directly
    exec plex-generate-previews "$@"
fi

