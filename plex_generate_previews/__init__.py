"""
Plex Video Preview Generator

A tool for generating video preview thumbnails for Plex Media Server.
Supports GPU acceleration (NVIDIA, AMD, Intel, Windows) and CPU processing.
"""

import os
import uuid

# Ensure a stable Plex client identity to prevent "new device" notifications.
# Users can override these via environment variables before import if desired.
os.environ.setdefault(
    "PLEXAPI_HEADER_IDENTIFIER",
    uuid.uuid3(uuid.NAMESPACE_DNS, "PlexGeneratePreviews").hex,
)
os.environ.setdefault("PLEXAPI_HEADER_DEVICE_NAME", "PlexGeneratePreviews")

from ._version import __version__

__author__ = "stevezau"
__description__ = "Generate video preview thumbnails for Plex Media Server"
