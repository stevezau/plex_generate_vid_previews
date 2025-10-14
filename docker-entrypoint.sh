#!/bin/bash
set -e

# Set user/group based on PUID/PGID environment variables
PUID=${PUID:-1000}
PGID=${PGID:-1000}

# Modify plex user to match PUID/PGID
groupmod -o -g "$PGID" plex
usermod -o -u "$PUID" plex

echo "────────────────────────────────────────"
echo "User uid:    $PUID"
echo "User gid:    $PGID"
echo "────────────────────────────────────────"

# Fix permissions and run as plex user
chown plex:plex /app
exec gosu plex "$@"
