"""Read-only API for the multi-media-server registry.

Phase 1 surfaces the persisted ``media_servers`` array (synthesised from
legacy ``plex_*`` settings via the v7 schema migration) so the UI can
enumerate configured servers without parsing settings directly. Mutating
endpoints (add/edit/delete server, refresh libraries, vendor-specific
auth flows) land in later phases.
"""

from flask import jsonify
from loguru import logger

from ...servers import (
    ServerRegistry,
    ServerType,
    UnsupportedServerTypeError,
    server_config_from_dict,
    server_config_to_dict,
)
from ..auth import setup_or_auth_required
from ..settings_manager import get_settings_manager
from . import api


def _redact_auth(entry: dict) -> dict:
    """Strip secrets from an auth dict before returning it over the wire.

    Keeps the auth method (``token`` / ``api_key`` / ``password``) so the UI
    can render the right edit form, but redacts any credential fields. The
    server registry never re-uses the redacted view; this is purely for
    response shaping.
    """
    auth = dict(entry.get("auth") or {})
    for secret_key in ("token", "api_key", "password", "access_token"):
        if secret_key in auth and auth[secret_key]:
            auth[secret_key] = "***REDACTED***"
    return auth


@api.route("/servers", methods=["GET"])
@setup_or_auth_required
def list_servers():
    """List every configured media server with redacted credentials.

    Returns the persisted ``media_servers`` array verbatim except that
    auth credentials are masked. The UI uses this to render the per-server
    cards on the Servers page; the existing single-Plex Settings page
    keeps reading the legacy ``plex_*`` keys directly during the Phase 1
    transition.
    """
    settings = get_settings_manager()
    raw_servers = settings.get("media_servers") or []
    if not isinstance(raw_servers, list):
        logger.warning("media_servers in settings is not a list; returning empty")
        raw_servers = []

    response_servers: list[dict] = []
    for entry in raw_servers:
        if not isinstance(entry, dict):
            continue
        try:
            cfg = server_config_from_dict(entry)
        except UnsupportedServerTypeError as exc:
            logger.warning("Skipping server with unsupported type: {}", exc)
            continue
        response_servers.append(
            {
                **server_config_to_dict(cfg),
                "auth": _redact_auth(entry),
            }
        )

    return jsonify({"servers": response_servers})


@api.route("/servers/<server_id>", methods=["GET"])
@setup_or_auth_required
def get_server(server_id: str):
    """Return a single server's configuration with credentials redacted."""
    settings = get_settings_manager()
    raw_servers = settings.get("media_servers") or []
    if not isinstance(raw_servers, list):
        return jsonify({"error": "media_servers not configured"}), 404

    for entry in raw_servers:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("id") or "") != server_id:
            continue
        try:
            cfg = server_config_from_dict(entry)
        except UnsupportedServerTypeError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify(
            {
                **server_config_to_dict(cfg),
                "auth": _redact_auth(entry),
            }
        )

    return jsonify({"error": f"server {server_id!r} not found"}), 404


@api.route("/servers/owners", methods=["GET"])
@setup_or_auth_required
def get_path_owners():
    """Return which configured servers own a given canonical path.

    Useful for diagnostics ("why isn't Foo.mkv going to Plex?") and as the
    backbone of the future webhook router. Phase 1 only sees the single
    migrated Plex server.

    Query params:
        path: Absolute local file path to test ownership for. Required.
    """
    from flask import request

    path = (request.args.get("path") or "").strip()
    if not path:
        return jsonify({"error": "path query parameter required"}), 400

    settings = get_settings_manager()
    raw_servers = settings.get("media_servers") or []
    if not isinstance(raw_servers, list):
        raw_servers = []

    # Build a registry without instantiating live clients — we only need
    # the ownership resolver, which works off ServerConfig dataclasses.
    registry = ServerRegistry.from_settings(raw_servers, legacy_config=None)
    matches = registry.find_owning_servers(path)

    return jsonify(
        {
            "path": path,
            "owners": [
                {
                    "server_id": m.server_id,
                    "library_id": m.library_id,
                    "library_name": m.library_name,
                    "local_prefix": m.local_prefix,
                }
                for m in matches
            ],
        }
    )


@api.route("/servers/<server_id>/refresh-libraries", methods=["POST"])
@setup_or_auth_required
def refresh_server_libraries(server_id: str):
    """Re-fetch a server's library list from its API and persist the snapshot.

    Calls :meth:`MediaServer.list_libraries` on the live client and writes
    the result back into the persisted ``media_servers`` array. Existing
    per-library ``enabled`` toggles for libraries that survive the refresh
    are preserved; new libraries default to enabled.

    Phase 1 only supports the Plex client path; for other server types the
    endpoint returns 501 with a clear message.
    """
    from ...config import load_config

    settings = get_settings_manager()
    raw_servers = settings.get("media_servers") or []
    if not isinstance(raw_servers, list):
        return jsonify({"error": "media_servers not configured"}), 404

    target_index: int | None = None
    target_entry: dict | None = None
    for i, entry in enumerate(raw_servers):
        if isinstance(entry, dict) and str(entry.get("id") or "") == server_id:
            target_index = i
            target_entry = entry
            break

    if target_entry is None or target_index is None:
        return jsonify({"error": f"server {server_id!r} not found"}), 404

    try:
        cfg = server_config_from_dict(target_entry)
    except UnsupportedServerTypeError as exc:
        return jsonify({"error": str(exc)}), 400

    if cfg.type not in (ServerType.PLEX, ServerType.EMBY):
        return (
            jsonify(
                {
                    "error": (
                        f"Refresh for {cfg.type.value} servers is not yet implemented; "
                        "Jellyfin support arrives in Phase 3."
                    )
                }
            ),
            501,
        )

    # Plex's live client still needs a legacy Config until the wrapper is
    # updated to take a ServerConfig. load_config() reads the same settings
    # we just inspected, so the URL/token/verify_ssl agree. Emby builds
    # purely from the persisted ServerConfig and doesn't need the legacy
    # config; calling load_config() is a no-op cost for that path.
    try:
        legacy_config = load_config()
    except Exception as exc:
        logger.warning("Refresh libraries: load_config failed: {}", exc)
        return jsonify({"error": f"failed to load config: {exc}"}), 500

    registry = ServerRegistry.from_settings(raw_servers, legacy_config=legacy_config)
    server = registry.get(server_id)
    if server is None:
        return jsonify({"error": f"could not instantiate server {server_id!r}"}), 500

    try:
        new_libraries = server.list_libraries()
    except Exception as exc:
        logger.warning("Refresh libraries: list_libraries raised: {}", exc)
        return jsonify({"error": f"server query failed: {exc}"}), 502

    # Preserve the user's per-library 'enabled' toggle for any library that
    # also appears in the previous snapshot (matched by id).
    existing_enabled: dict[str, bool] = {}
    for raw_lib in target_entry.get("libraries", []) or []:
        if isinstance(raw_lib, dict):
            lib_id = str(raw_lib.get("id") or "")
            if lib_id:
                existing_enabled[lib_id] = bool(raw_lib.get("enabled", True))

    serialised_libraries: list[dict] = []
    for lib in new_libraries:
        enabled = existing_enabled.get(lib.id, lib.enabled)
        serialised_libraries.append(
            {
                "id": lib.id,
                "name": lib.name,
                "remote_paths": list(lib.remote_paths),
                "enabled": enabled,
                "kind": lib.kind,
            }
        )

    # Write the updated entry back into media_servers preserving order.
    updated_servers = list(raw_servers)
    updated_entry = dict(target_entry)
    updated_entry["libraries"] = serialised_libraries
    updated_servers[target_index] = updated_entry
    settings.set("media_servers", updated_servers)

    return jsonify(
        {
            "server_id": server_id,
            "libraries": serialised_libraries,
            "count": len(serialised_libraries),
        }
    )
