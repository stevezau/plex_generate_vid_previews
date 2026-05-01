"""Output adapter abstractions.

Each supported destination format (Plex bundle BIF, Emby sidecar BIF,
Jellyfin native trickplay) implements the :class:`OutputAdapter` interface
defined in :mod:`.base`.
"""

from .base import BifBundle, OutputAdapter
from .emby_sidecar import EmbyBifAdapter
from .jellyfin_trickplay import JellyfinTrickplayAdapter
from .plex_bundle import PlexBundleAdapter

__all__ = [
    "BifBundle",
    "EmbyBifAdapter",
    "JellyfinTrickplayAdapter",
    "OutputAdapter",
    "PlexBundleAdapter",
]
