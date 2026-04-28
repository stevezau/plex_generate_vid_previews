"""Multi-media-server registry API.

Surfaces the persisted ``media_servers`` array as a CRUD-style REST API
so the UI can list, add, edit, and delete configured servers without
parsing ``settings.json`` directly. Vendor-specific auth flow helpers
(Emby/Jellyfin password exchange, Jellyfin Quick Connect ceremony) live
alongside in :mod:`api_server_auth`.
"""

from __future__ import annotations

import uuid
from typing import Any

from flask import jsonify, request
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

_SECRET_AUTH_KEYS = ("token", "api_key", "password", "access_token")
_REDACTED_PLACEHOLDER = "***REDACTED***"


def _redact_auth(entry: dict) -> dict:
    """Strip secrets from an auth dict before returning it over the wire.

    Keeps the auth method (``token`` / ``api_key`` / ``password``) so the UI
    can render the right edit form, but redacts any credential fields. The
    server registry never re-uses the redacted view; this is purely for
    response shaping.
    """
    auth = dict(entry.get("auth") or {})
    for secret_key in _SECRET_AUTH_KEYS:
        if secret_key in auth and auth[secret_key]:
            auth[secret_key] = _REDACTED_PLACEHOLDER
    return auth


def _get_media_servers() -> list[dict]:
    """Read the persisted ``media_servers`` array as a list (default empty)."""
    raw = get_settings_manager().get("media_servers") or []
    return list(raw) if isinstance(raw, list) else []


def _save_media_servers(servers: list[dict]) -> None:
    """Atomically persist ``servers`` back into settings."""
    get_settings_manager().set("media_servers", servers)


def _merge_auth(existing: dict, incoming: dict | None) -> dict:
    """Apply incoming auth fields without clobbering retained secrets.

    The UI round-trips the redacted form when editing — fields that come
    back as ``"***REDACTED***"`` mean "keep what's there"; any other
    value (including empty string) overrides. This avoids the foot-gun
    where saving an edit silently wipes the user's token.
    """
    merged = dict(existing or {})
    if not isinstance(incoming, dict):
        return merged
    for key, value in incoming.items():
        if key in _SECRET_AUTH_KEYS and value == _REDACTED_PLACEHOLDER:
            continue  # keep existing secret
        merged[key] = value
    return merged


def _validate_server_payload(
    data: dict[str, Any],
    *,
    is_update: bool = False,
    existing: dict | None = None,
) -> tuple[dict | None, str]:
    """Coerce a request body into a ``media_servers`` entry.

    Returns ``(entry, error)`` — ``entry`` is the dict ready to persist
    when validation passes; ``error`` is a non-empty message otherwise.

    For updates, ``existing`` holds the previously persisted entry so
    we can fill in fields the client didn't send (typical "patch"
    semantics) and merge auth without dropping secrets.
    """
    if not isinstance(data, dict):
        return None, "request body must be a JSON object"

    base = dict(existing) if existing else {}

    type_value = str(data.get("type") or base.get("type") or "").strip().lower()
    if not type_value:
        return None, "type is required (one of: plex, emby, jellyfin)"
    try:
        ServerType(type_value)
    except ValueError:
        return None, f"unknown type {type_value!r}; must be plex, emby, or jellyfin"

    name = str(data.get("name") or base.get("name") or "").strip()
    if not name:
        return None, "name is required"

    url = str(data.get("url") or base.get("url") or "").strip()
    if not url:
        return None, "url is required"

    auth_in = data.get("auth")
    if is_update and auth_in is not None:
        auth = _merge_auth(base.get("auth") or {}, auth_in)
    else:
        auth = dict(auth_in) if isinstance(auth_in, dict) else dict(base.get("auth") or {})

    libraries = data.get("libraries") if "libraries" in data else base.get("libraries", [])
    path_mappings = data.get("path_mappings") if "path_mappings" in data else base.get("path_mappings", [])
    output = data.get("output") if "output" in data else base.get("output", {})

    enabled = bool(data.get("enabled", base.get("enabled", True)))
    verify_ssl = bool(data.get("verify_ssl", base.get("verify_ssl", True)))
    timeout = int(data.get("timeout") or base.get("timeout") or 30)

    entry = {
        "id": str(data.get("id") or base.get("id") or ""),
        "type": type_value,
        "name": name,
        "enabled": enabled,
        "url": url,
        "auth": auth,
        "verify_ssl": verify_ssl,
        "timeout": timeout,
        "libraries": list(libraries or []),
        "path_mappings": list(path_mappings or []),
        "output": dict(output or {}),
    }

    # Sanity-check the result: server_config_from_dict applies its own
    # validation rules (type, library shapes). Catch unsupported types
    # here too even though we filtered above — defensive.
    try:
        server_config_from_dict(entry)
    except UnsupportedServerTypeError as exc:
        return None, str(exc)

    return entry, ""


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

    if cfg.type not in (ServerType.PLEX, ServerType.EMBY, ServerType.JELLYFIN):
        return (
            jsonify({"error": f"Refresh for {cfg.type.value} servers is not implemented"}),
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


# ---------------------------------------------------------------------------
# CRUD endpoints
# ---------------------------------------------------------------------------


@api.route("/servers", methods=["POST"])
@setup_or_auth_required
def create_server():
    """Persist a new media server entry.

    Body: a partial ``ServerConfig`` JSON object — ``type``, ``name``,
    ``url`` are required; ``auth``/``libraries``/``path_mappings``/``output``
    optional. The ``id`` field is generated server-side as a UUID and
    surfaces in the response so the caller can show it in the URL row /
    use it for follow-up calls.

    The endpoint does **not** validate the server is reachable — that's
    the dedicated ``/api/servers/test-connection`` endpoint's job, which
    the wizard typically calls first. Saving an unreachable server is
    deliberately allowed so users can prepare config offline.
    """
    payload = request.get_json(silent=True) or {}
    entry, error = _validate_server_payload(payload, is_update=False)
    if entry is None:
        return jsonify({"error": error}), 400

    # Generate id when the client didn't supply one (typical), or keep
    # the supplied id (useful for migrations / scripted deploys) provided
    # it doesn't collide.
    servers = _get_media_servers()
    if not entry["id"]:
        entry["id"] = uuid.uuid4().hex
    if any(isinstance(s, dict) and s.get("id") == entry["id"] for s in servers):
        return jsonify({"error": f"server id {entry['id']!r} already exists"}), 409

    servers.append(entry)
    _save_media_servers(servers)
    logger.info("Added media server {!r} (id={})", entry["name"], entry["id"])

    return (
        jsonify({**server_config_to_dict(server_config_from_dict(entry)), "auth": _redact_auth(entry)}),
        201,
    )


@api.route("/servers/<server_id>", methods=["PUT", "PATCH"])
@setup_or_auth_required
def update_server(server_id: str):
    """Update fields on an existing server.

    Both PUT and PATCH accept a partial body — fields the client
    omits keep their current value. Auth fields are merged: any
    secret coming back as ``"***REDACTED***"`` is treated as
    "leave alone" so the UI can safely send back the form payload
    after a redacted GET.
    """
    payload = request.get_json(silent=True) or {}

    servers = _get_media_servers()
    for i, entry in enumerate(servers):
        if isinstance(entry, dict) and entry.get("id") == server_id:
            updated, error = _validate_server_payload(payload, is_update=True, existing=entry)
            if updated is None:
                return jsonify({"error": error}), 400
            updated["id"] = server_id  # never let id be changed via update
            servers[i] = updated
            _save_media_servers(servers)
            logger.info("Updated media server {!r} (id={})", updated["name"], server_id)
            return jsonify(
                {
                    **server_config_to_dict(server_config_from_dict(updated)),
                    "auth": _redact_auth(updated),
                }
            )

    return jsonify({"error": f"server {server_id!r} not found"}), 404


@api.route("/servers/<server_id>", methods=["DELETE"])
@setup_or_auth_required
def delete_server(server_id: str):
    """Remove a server entry.

    No tombstone, no "soft delete" — once gone the entry is gone.
    Returning the deleted entry's id lets the caller confirm what was
    removed; we don't return the auth body since the entry is gone.
    """
    servers = _get_media_servers()
    new_servers = [s for s in servers if not (isinstance(s, dict) and s.get("id") == server_id)]
    if len(new_servers) == len(servers):
        return jsonify({"error": f"server {server_id!r} not found"}), 404

    _save_media_servers(new_servers)
    logger.info("Removed media server id={}", server_id)
    return jsonify({"deleted": server_id})


@api.route("/servers/test-connection", methods=["POST"])
@setup_or_auth_required
def test_server_connection():
    """Probe a server using a candidate config without saving it.

    Used by the "Test connection" button in the Add Server wizard
    before the user commits to creating the entry. The body has the
    same shape as :func:`create_server`'s but no entry is persisted —
    we instantiate a transient :class:`MediaServer` and call its
    :meth:`test_connection`. Always returns 200 with a JSON
    ``ConnectionResult``; the actual probe outcome lives in ``ok``.
    """
    payload = request.get_json(silent=True) or {}
    entry, error = _validate_server_payload(payload, is_update=False)
    if entry is None:
        return jsonify({"ok": False, "message": error}), 400

    # We don't need a stable id for a transient probe.
    if not entry.get("id"):
        entry["id"] = "test-connection"

    cfg = server_config_from_dict(entry)
    if cfg.type is ServerType.PLEX:
        # Plex's wrapper still needs a Config-shaped object during the
        # transition. The connection probe only reads URL/token/SSL/timeout
        # so a tiny shim is enough.
        plex_shim = _PlexProbeConfig(
            plex_url=cfg.url,
            plex_token=str((cfg.auth or {}).get("token") or ""),
            plex_verify_ssl=cfg.verify_ssl,
            plex_timeout=cfg.timeout,
        )
        from ...servers.plex import PlexServer as _PlexServer

        live = _PlexServer(plex_shim, server_id=cfg.id, name=cfg.name)
    elif cfg.type is ServerType.EMBY:
        from ...servers.emby import EmbyServer as _EmbyServer

        live = _EmbyServer(cfg)
    elif cfg.type is ServerType.JELLYFIN:
        from ...servers.jellyfin import JellyfinServer as _JellyfinServer

        live = _JellyfinServer(cfg)
    else:
        return jsonify({"ok": False, "message": f"unsupported type {cfg.type.value!r}"}), 400

    try:
        result = live.test_connection()
    except Exception as exc:
        logger.warning("test_connection raised: {}", exc)
        return jsonify({"ok": False, "message": f"unexpected error: {exc}"}), 200

    return jsonify(
        {
            "ok": result.ok,
            "server_id": result.server_id,
            "server_name": result.server_name,
            "version": result.version,
            "message": result.message,
        }
    )


class _PlexProbeConfig:
    """Minimal config shim used only by the test-connection probe.

    The :class:`PlexServer` wrapper currently requires a legacy
    :class:`Config` to read its URL / token / SSL / timeout. Building
    one for a single throwaway probe is wasteful — this tiny adapter
    surfaces only the four fields ``test_connection`` reads.
    """

    def __init__(self, *, plex_url: str, plex_token: str, plex_verify_ssl: bool, plex_timeout: int) -> None:
        self.plex_url = plex_url
        self.plex_token = plex_token
        self.plex_verify_ssl = plex_verify_ssl
        self.plex_timeout = plex_timeout
        # Optional fields that the wider Plex code path reads; safe defaults
        # so any incidental attribute access during the probe doesn't AttributeError.
        self.path_mappings: list[dict] = []
        self.exclude_paths: list[dict] = []
