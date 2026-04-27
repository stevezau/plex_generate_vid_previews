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
from .ownership import OwnershipMatch, find_owning_servers, server_owns_path
from .plex import PlexServer

__all__ = [
    "ConnectionResult",
    "Library",
    "LibraryNotYetIndexedError",
    "MediaItem",
    "MediaServer",
    "OwnershipMatch",
    "PlexServer",
    "ServerConfig",
    "ServerType",
    "WebhookEvent",
    "find_owning_servers",
    "server_owns_path",
]
