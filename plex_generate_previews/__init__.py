"""
Plex Video Preview Generator

A tool for generating video preview thumbnails for Plex Media Server.
Supports GPU acceleration (NVIDIA, AMD, Intel, WSL2) and CPU processing.
"""

try:
    from ._version import version as __version__
except ImportError:
    # Fallback for development without installation
    __version__ = "0.0.0.dev0"

__author__ = "stevezau"
__description__ = "Generate video preview thumbnails for Plex Media Server"
