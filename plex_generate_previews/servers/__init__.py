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
from .registry import (
    ServerRegistry,
    UnsupportedServerTypeError,
    server_config_from_dict,
    server_config_to_dict,
)

__all__ = [
    "ConnectionResult",
    "Library",
    "LibraryNotYetIndexedError",
    "MediaItem",
    "MediaServer",
    "OwnershipMatch",
    "PlexServer",
    "ServerConfig",
    "ServerRegistry",
    "ServerType",
    "UnsupportedServerTypeError",
    "WebhookEvent",
    "find_owning_servers",
    "server_config_from_dict",
    "server_config_to_dict",
    "server_owns_path",
]
