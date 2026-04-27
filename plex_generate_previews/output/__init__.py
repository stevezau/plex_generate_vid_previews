"""Output adapter abstractions.

Each supported destination format (Plex bundle BIF, Emby sidecar BIF,
Jellyfin native trickplay) implements the :class:`OutputAdapter` interface
defined in :mod:`.base`. Concrete adapters are added in later phases.
"""

from .base import BifBundle, OutputAdapter

__all__ = ["BifBundle", "OutputAdapter"]
