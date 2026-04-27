"""Media server abstractions.

Each supported server type (Plex, Emby, Jellyfin) implements the
:class:`MediaServer` interface defined in :mod:`.base`.

Concrete implementations are added in later phases; Phase 1 only ships the
abstraction so the rest of the codebase can be refactored against it.
"""

from .base import (
    ConnectionResult,
    Library,
    LibraryNotYetIndexedError,
    MediaItem,
    MediaServer,
    ServerConfig,
    ServerType,
    WebhookEvent,
)
from .plex import PlexServer

__all__ = [
    "ConnectionResult",
    "Library",
    "LibraryNotYetIndexedError",
    "MediaItem",
    "MediaServer",
    "PlexServer",
    "ServerConfig",
    "ServerType",
    "WebhookEvent",
]
