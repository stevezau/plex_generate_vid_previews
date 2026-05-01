"""Server registry: loads ``media_servers`` from settings into live clients.

Translates the JSON-shaped entries in ``settings.json`` to:

- :class:`ServerConfig` dataclasses (the persisted-shape view), and
- live :class:`MediaServer` instances (Plex/Emby/Jellyfin clients).

The registry is the single bridge between persistent settings and the
processing pipeline. The dispatcher consumes
:meth:`ServerRegistry.find_owning_servers` to fan out a single FFmpeg pass
to every server that owns a canonical path.

Plex, Emby, and Jellyfin are all wired up. Entries with an unrecognised
``type`` raise :class:`UnsupportedServerTypeError`, which the registry
surfaces as warnings rather than aborting load.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from loguru import logger

from .base import Library, MediaServer, ServerConfig, ServerType
from .ownership import OwnershipMatch, find_owning_servers

if TYPE_CHECKING:
    from ..config import Config


class UnsupportedServerTypeError(RuntimeError):
    """Raised when ``ServerConfig.type`` has no concrete implementation.

    Plex, Emby, and Jellyfin are all wired up; this fires only for an unknown
    or unsupported ``type`` value. Such entries are skipped with a warning
    rather than crashing the whole registry load.
    """


def server_config_from_dict(data: dict[str, Any]) -> ServerConfig:
    """Hydrate a :class:`ServerConfig` from its persisted JSON shape.

    Tolerates missing keys (legacy/incomplete entries) by falling back to
    the dataclass defaults. ``libraries`` entries are normalised into
    :class:`Library` instances; unknown library shapes are skipped.
    """
    libs: list[Library] = []
    for raw in data.get("libraries", []) or []:
        if not isinstance(raw, dict):
            continue
        try:
            libs.append(
                Library(
                    id=str(raw.get("id", "")),
                    name=str(raw.get("name", "")),
                    remote_paths=tuple(str(p) for p in (raw.get("remote_paths") or [])),
                    enabled=bool(raw.get("enabled", True)),
                    kind=raw.get("kind"),
                )
            )
        except Exception as exc:
            logger.warning(
                "Library configuration is invalid and will be ignored: {}. "
                "Open Settings → Media Servers and re-add or fix this library entry. "
                "Raw entry: {!r}",
                exc,
                raw,
            )

    type_str = str(data.get("type") or "").strip().lower()
    try:
        server_type = ServerType(type_str)
    except ValueError as exc:
        raise UnsupportedServerTypeError(f"Unknown media server type {type_str!r} in settings") from exc

    identity_raw = data.get("server_identity")
    server_identity = str(identity_raw) if identity_raw else None

    return ServerConfig(
        id=str(data.get("id") or ""),
        type=server_type,
        name=str(data.get("name") or ""),
        enabled=bool(data.get("enabled", True)),
        url=str(data.get("url") or ""),
        auth=dict(data.get("auth") or {}),
        verify_ssl=bool(data.get("verify_ssl", True)),
        timeout=int(data.get("timeout") or 30),
        libraries=libs,
        path_mappings=list(data.get("path_mappings") or []),
        exclude_paths=list(data.get("exclude_paths") or []),
        output=dict(data.get("output") or {}),
        server_identity=server_identity,
    )


def server_config_to_dict(config: ServerConfig) -> dict[str, Any]:
    """Inverse of :func:`server_config_from_dict` for persistence."""
    raw = asdict(config)
    raw["type"] = config.type.value
    raw["libraries"] = [
        {
            "id": lib.id,
            "name": lib.name,
            "remote_paths": list(lib.remote_paths),
            "enabled": lib.enabled,
            "kind": lib.kind,
        }
        for lib in config.libraries
    ]
    return raw


class ServerRegistry:
    """In-memory map of ``server_id`` → live :class:`MediaServer`.

    Constructed from the ``media_servers`` array in settings, or synthesised
    from a legacy single-Plex :class:`Config` via :meth:`from_legacy_config`
    for the few remaining call sites that haven't been routed through
    ``media_servers`` yet.
    """

    def __init__(self) -> None:
        self._configs: dict[str, ServerConfig] = {}
        self._servers: dict[str, MediaServer] = {}

    # ----------------------------------------------------------- factories
    @classmethod
    def from_settings(
        cls,
        media_servers: list[dict[str, Any]],
        *,
        legacy_config: Config | None = None,
    ) -> ServerRegistry:
        """Build a registry from the persisted ``media_servers`` array.

        Args:
            media_servers: Raw ``settings.json`` ``media_servers`` list.
            legacy_config: Existing :class:`Config` instance whose ``plex_*``
                fields the :class:`PlexServer` wrapper still consults when
                constructed from a duck-typed legacy config. Optional — the
                modern path passes ``ServerConfig`` directly and ignores it.
        """
        registry = cls()
        for raw in media_servers or []:
            try:
                cfg = server_config_from_dict(raw)
            except UnsupportedServerTypeError as exc:
                logger.warning(
                    "Skipping a media server because its configuration is invalid: {}. "
                    "Open Settings → Media Servers, remove or fix this entry, then restart.",
                    exc,
                )
                continue

            registry._configs[cfg.id] = cfg
            try:
                server = registry._build_server(cfg, legacy_config=legacy_config)
            except UnsupportedServerTypeError as exc:
                logger.warning(
                    "Skipping media server {!r} (id={}) — could not initialise: {}. "
                    "Verify the server URL, credentials, and type in Settings → Media Servers.",
                    cfg.name or "(no name)",
                    cfg.id,
                    exc,
                )
                continue
            registry._servers[cfg.id] = server
        return registry

    @classmethod
    def from_legacy_config(cls, config: Config) -> ServerRegistry:
        """Synthesize a single-Plex registry from a legacy :class:`Config`.

        Used by the few remaining code paths that haven't been routed through
        the persisted ``media_servers`` array yet.
        """
        from ..upgrade import _legacy_plex_to_media_server

        # Reuse the migration helper by adapting the Config to a SettingsManager
        # protocol — only ``.get(key, default)`` is needed.
        snapshot = _ConfigGetterShim(config)
        entry = _legacy_plex_to_media_server(snapshot)
        if entry is None:
            return cls()
        return cls.from_settings([entry], legacy_config=config)

    # ----------------------------------------------------------- accessors
    def configs(self) -> list[ServerConfig]:
        """Return all server configs (including disabled) in registration order."""
        return list(self._configs.values())

    def servers(self) -> list[MediaServer]:
        """Return all live :class:`MediaServer` clients (excludes unsupported types)."""
        return list(self._servers.values())

    def get(self, server_id: str) -> MediaServer | None:
        """Return the live client for ``server_id`` or ``None`` if absent."""
        return self._servers.get(server_id)

    def get_config(self, server_id: str) -> ServerConfig | None:
        return self._configs.get(server_id)

    def find_owning_servers(self, canonical_path: str) -> list[OwnershipMatch]:
        """Resolve which configured servers should publish for ``canonical_path``.

        Returns matches in the order servers were added — same order callers
        see when iterating the publisher list, which keeps telemetry stable.
        """
        return find_owning_servers(canonical_path, self.configs())

    # ----------------------------------------------------------- helpers
    @staticmethod
    def _build_server(
        config: ServerConfig,
        *,
        legacy_config: Config | None,
    ) -> MediaServer:
        """Construct the right :class:`MediaServer` subclass for ``config.type``."""
        if config.type is ServerType.PLEX:
            from .plex import PlexServer

            # PlexServer now accepts ServerConfig directly (it synthesizes a
            # legacy Config-shape internally for plex_client). The
            # legacy_config parameter on this method is retained only for
            # backwards compatibility with from_legacy_config callers.
            return PlexServer(config)

        if config.type is ServerType.EMBY:
            from .emby import EmbyServer

            return EmbyServer(config)

        if config.type is ServerType.JELLYFIN:
            from .jellyfin import JellyfinServer

            return JellyfinServer(config)

        raise UnsupportedServerTypeError(f"Server type {config.type.value!r} is not yet supported")


class _ConfigGetterShim:
    """Adapt a :class:`Config` instance to the SettingsManager ``.get(key)`` API.

    The migration helper :func:`_legacy_plex_to_media_server` reads its input
    via ``sm.get(key, default)``; legacy callers that already hold a
    :class:`Config` shouldn't have to re-instantiate a ``SettingsManager`` to
    reuse that helper. This shim provides just enough surface for that one
    call.
    """

    def __init__(self, config: Config) -> None:
        self._config = config

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self._config, key, default)
