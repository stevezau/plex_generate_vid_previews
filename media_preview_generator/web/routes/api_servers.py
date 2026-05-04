"""Multi-media-server registry API.

Surfaces the persisted ``media_servers`` array as a CRUD-style REST API
so the UI can list, add, edit, and delete configured servers without
parsing ``settings.json`` directly. Vendor-specific auth flow helpers
(Emby/Jellyfin password exchange, Jellyfin Quick Connect ceremony) live
alongside in :mod:`api_server_auth`.
"""

from __future__ import annotations

import os
import re
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


def _probe_for_identity(entry: dict) -> str | None:
    """Probe the candidate config and return its self-reported identity.

    Used during create/update so the webhook router can later match
    inbound vendor payloads (Plex ``Server.uuid`` / Emby ``Server.Id`` /
    Jellyfin ``ServerId``) to the persisted entry. Returns ``None`` when
    the probe fails (offline, bad creds) — saving an unreachable server
    is deliberately allowed; the webhook router falls back to the
    "single candidate of this vendor" heuristic when identity is
    missing.
    """
    try:
        cfg = server_config_from_dict(entry)
    except UnsupportedServerTypeError:
        return None

    try:
        live = _instantiate_for_probe(cfg)
    except Exception as exc:
        logger.info(
            "Could not auto-discover the server identity for {} ({}: {}). "
            "The server still saves; webhooks coming back with this server's id will fall through to the "
            "vendor-type fallback (works fine when only one server of this vendor is configured).",
            cfg.name or cfg.id,
            type(exc).__name__,
            exc,
        )
        return None
    if live is None:
        return None

    try:
        result = live.test_connection()
    except Exception as exc:
        logger.info(
            "Could not auto-discover the server identity for {} — connection probe failed ({}: {}). "
            "Webhooks for this server will rely on the vendor-type fallback. "
            "Verify the URL + credentials in Settings → Media Servers if this matters for your setup.",
            cfg.name or cfg.id,
            type(exc).__name__,
            exc,
        )
        return None

    return result.server_id if result.ok else None


def _instantiate_for_probe(cfg) -> Any:
    """Build a transient :class:`MediaServer` for connection probing.

    Centralised here so create/update/test-connection share the same
    Plex shim and import paths.
    """
    if cfg.type is ServerType.PLEX:
        from ...servers.plex import PlexServer as _PlexServer

        # PlexServer accepts ServerConfig directly — no shim needed.
        return _PlexServer(cfg)
    if cfg.type is ServerType.EMBY:
        from ...servers.emby import EmbyServer as _EmbyServer

        return _EmbyServer(cfg)
    if cfg.type is ServerType.JELLYFIN:
        from ...servers.jellyfin import JellyfinServer as _JellyfinServer

        return _JellyfinServer(cfg)
    return None


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


def _validate_path_mappings(rows: list) -> str:
    """Save-time validation for per-server path_mappings.

    Catches three foot-guns at save rather than at job-runtime:
      * non-dict rows (UI bug),
      * missing remote_prefix or local_prefix when the row is otherwise populated,
      * local_prefix that doesn't exist on disk (silent path mapping → no previews).

    Accepts either ``remote_prefix`` (the modern multi-vendor key used
    by Emby/Jellyfin/Plex entries) or the legacy ``plex_prefix`` —
    ``ownership.py`` already coalesces both at read time, so the
    validator must too. Without this fallback, any PUT/PATCH to a
    server saved with the modern key would 400 even when the body
    is unchanged.
    """
    if not isinstance(rows, list):
        return "path_mappings must be a list"
    for idx, row in enumerate(rows):
        if not isinstance(row, dict):
            return f"path_mappings[{idx}] must be an object"
        remote_prefix = str(row.get("remote_prefix") or row.get("plex_prefix") or "").strip()
        local_prefix = str(row.get("local_prefix") or "").strip()
        if not remote_prefix and not local_prefix:
            continue  # blank row — UI tolerates these, just skip
        if not remote_prefix or not local_prefix:
            return f"path_mappings[{idx}] needs both 'remote_prefix' (or legacy 'plex_prefix') and 'local_prefix'"
        if not local_prefix.startswith("/"):
            return f"path_mappings[{idx}] local_prefix must be an absolute path (got {local_prefix!r})"
        if not os.path.isdir(local_prefix):
            return (
                f"path_mappings[{idx}] local_prefix {local_prefix!r} does not exist on this container "
                f"(file would resolve to a missing directory). Either create/mount the path or correct the value."
            )
    return ""


def _validate_exclude_paths(rows: list) -> str:
    """Save-time validation for per-server exclude_paths.

    Compiles ``regex`` rows immediately so users discover bad patterns at save time
    rather than at job-runtime, and rejects empty rows / unknown match types.
    """
    if not isinstance(rows, list):
        return "exclude_paths must be a list"
    for idx, row in enumerate(rows):
        if not isinstance(row, dict):
            return f"exclude_paths[{idx}] must be an object"
        value = str(row.get("value") or "").strip()
        if not value:
            continue  # blank row — skip
        match_type = str(row.get("type") or "path").strip().lower()
        if match_type not in ("path", "regex"):
            return f"exclude_paths[{idx}] type must be 'path' or 'regex' (got {match_type!r})"
        if match_type == "regex":
            try:
                re.compile(value)
            except re.error as exc:
                return f"exclude_paths[{idx}] regex {value!r} is not valid: {exc}"
    return ""


def _validate_plex_output(output: dict) -> str:
    """Save-time validation for Plex servers' ``output`` dict.

    The plex_config_folder must exist on disk for the BIF publisher to write to
    it; catch typos / missing mounts here instead of failing every job.
    """
    if not isinstance(output, dict):
        return "output must be an object"
    folder = str(output.get("plex_config_folder") or "").strip()
    if not folder:
        return ""  # caller may save without populating output yet
    if not folder.startswith("/"):
        return f"output.plex_config_folder must be an absolute path (got {folder!r})"
    if not os.path.isdir(folder):
        return (
            f"output.plex_config_folder {folder!r} does not exist on this container. "
            f"Verify the path is correct and that the volume is mounted."
        )
    return ""


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
    exclude_paths = data.get("exclude_paths") if "exclude_paths" in data else base.get("exclude_paths", [])
    output = data.get("output") if "output" in data else base.get("output", {})

    err = _validate_path_mappings(path_mappings or [])
    if err:
        return None, err
    err = _validate_exclude_paths(exclude_paths or [])
    if err:
        return None, err
    if type_value == "plex":
        err = _validate_plex_output(output or {})
        if err:
            return None, err

    enabled = bool(data.get("enabled", base.get("enabled", True)))
    verify_ssl = bool(data.get("verify_ssl", base.get("verify_ssl", True)))
    timeout = int(data.get("timeout") or base.get("timeout") or 30)

    server_identity = data.get("server_identity", base.get("server_identity"))
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
        "exclude_paths": list(exclude_paths or []),
        "output": dict(output or {}),
        "server_identity": str(server_identity) if server_identity else None,
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
    cards on the Servers page; the legacy Settings page still reads the
    flat ``plex_*`` keys derived from ``media_servers[0]`` for back-compat.
    """
    settings = get_settings_manager()
    raw_servers = settings.get("media_servers") or []
    if not isinstance(raw_servers, list):
        logger.warning(
            "The 'media_servers' entry in settings.json isn't in the expected format "
            "(should be a list of servers, found a single value of type {!r}). "
            "Treating it as empty — your previously-added servers will not appear on the Servers page "
            "until the setting is fixed. Easiest fix: re-add the servers via the UI; "
            "advanced fix: edit settings.json so 'media_servers' is a JSON array and restart the app.",
            type(raw_servers).__name__,
        )
        raw_servers = []

    response_servers: list[dict] = []
    for entry in raw_servers:
        if not isinstance(entry, dict):
            continue
        try:
            cfg = server_config_from_dict(entry)
        except UnsupportedServerTypeError as exc:
            logger.warning(
                "Hiding one configured media server because its type isn't recognised ({}). "
                "Other configured servers still appear normally. "
                "Open Settings → Media Servers and either correct the type "
                "(must be one of: plex, emby, jellyfin) or delete the entry.",
                exc,
            )
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
    backbone of the universal webhook router. Resolves against every
    configured server (Plex, Emby, Jellyfin), not just the first one.

    Query params:
        path: Absolute local file path to test ownership for. Required.
    """
    from ...utils import sanitize_path

    raw_path = (request.args.get("path") or "").strip()
    if not raw_path:
        return jsonify({"error": "path query parameter required"}), 400
    path = sanitize_path(raw_path)

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

    Supports Plex, Emby, and Jellyfin via :func:`_instantiate_for_probe`.
    Unrecognised server types raise :class:`UnsupportedServerTypeError` and
    surface as a 400.
    """
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
        target_cfg = server_config_from_dict(target_entry)
    except UnsupportedServerTypeError as exc:
        return jsonify({"error": str(exc)}), 400

    # Build the live server directly from the per-server ServerConfig
    # rather than going through the global registry + load_config(). This
    # lets the endpoint work in multi-server-only deployments (no Plex
    # configured) and avoids a hard dependency on PLEX_URL/PLEX_TOKEN
    # being set in the env when refreshing an Emby/Jellyfin server.
    try:
        server = _instantiate_for_probe(target_cfg)
    except Exception as exc:
        logger.warning(
            "Refresh Libraries: could not build a client for media server {!r} ({}: {}). "
            "Other servers still refresh normally. "
            "Verify the URL, type, and credentials in Settings → Media Servers; "
            "use 'Test Connection' there to confirm the server is reachable.",
            target_cfg.name or server_id,
            type(exc).__name__,
            exc,
        )
        return jsonify({"error": f"could not instantiate server {server_id!r}: {exc}"}), 500
    if server is None:
        return jsonify({"error": f"could not instantiate server {server_id!r}"}), 500

    try:
        new_libraries = server.list_libraries()
    except Exception as exc:
        logger.warning(
            "Refresh Libraries: media server {!r} returned an error when asked for its library list "
            "({}: {}). The cached library list isn't being updated this time — the existing list "
            "stays in place, so jobs and the Servers page keep working. "
            "Verify the server is reachable and credentials haven't expired; "
            "use 'Test Connection' under Settings → Media Servers to confirm.",
            target_cfg.name or server_id,
            type(exc).__name__,
            exc,
        )
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
    # Refresh server_identity opportunistically — list_libraries having
    # succeeded means the connection works; capturing the identity here
    # closes the gap for users who added their server while it was
    # offline (or pre-server_identity).
    if not updated_entry.get("server_identity"):
        try:
            probe = server.test_connection()
            if probe.ok and probe.server_id:
                updated_entry["server_identity"] = probe.server_id
        except Exception as exc:
            logger.debug("Refresh libraries: identity probe raised: {}", exc)
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

    # Best-effort: probe the new server so the webhook router can match
    # inbound vendor payloads by identity. Skipped silently when the
    # client supplied an explicit identity or the probe fails.
    if not entry.get("server_identity"):
        identity = _probe_for_identity(entry)
        if identity:
            entry["server_identity"] = identity

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
            # Re-probe identity when URL or auth changed; otherwise keep
            # the existing identity so we don't lose it if the probe is
            # transiently flaky.
            url_changed = updated.get("url") != entry.get("url")
            auth_changed = updated.get("auth") != entry.get("auth")
            if url_changed or auth_changed:
                fresh_identity = _probe_for_identity(updated)
                if fresh_identity:
                    updated["server_identity"] = fresh_identity
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
    target = next((s for s in servers if isinstance(s, dict) and s.get("id") == server_id), None)
    if target is None:
        return jsonify({"error": f"server {server_id!r} not found"}), 404
    new_servers = [s for s in servers if not (isinstance(s, dict) and s.get("id") == server_id)]

    # Best-effort: unregister this server's webhook from plex.tv before
    # we drop the entry. Otherwise Plex keeps POSTing to a URL that's no
    # longer associated with the deleted server. Failures here don't
    # block the delete — the user's intent (remove from this app) is
    # what we honour.
    if (target.get("type") or "").lower() == "plex":
        try:
            from .. import plex_webhook_registration as pwh

            token = ((target.get("auth") or {}).get("token") or "").strip()
            url = ((target.get("output") or {}).get("webhook_public_url") or "").strip()
            if token and url:
                pwh.unregister(token, url)
                logger.info(
                    "Removed Plex webhook registration for deleted server {} (url={})",
                    target.get("name") or server_id,
                    url,
                )
        except Exception as exc:
            logger.warning(
                "Could not unregister the Plex webhook for deleted server {} ({}: {}). "
                "You may want to remove it manually from plex.tv → Account → Webhooks.",
                target.get("name") or server_id,
                type(exc).__name__,
                exc,
            )

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
    live = _instantiate_for_probe(cfg)
    if live is None:
        return jsonify({"ok": False, "message": f"unsupported type {cfg.type.value!r}"}), 400

    try:
        result = live.test_connection()
    except Exception as exc:
        logger.warning(
            "Test Connection: unexpected error contacting media server at {} ({}: {}). "
            "The Test Connection dialog will show this error message verbatim. "
            "Check the URL and credentials, and that the server is reachable from this app's container.",
            cfg.url,
            type(exc).__name__,
            exc,
        )
        return jsonify({"ok": False, "message": f"unexpected error: {exc}"}), 200

    response_body = {
        "ok": result.ok,
        "server_id": result.server_id,
        "server_name": result.server_name,
        "version": result.version,
        "message": result.message,
    }

    # Jellyfin-only setup gotcha: libraries default
    # ``EnableTrickplayImageExtraction`` to false, so Jellyfin won't
    # see our sidecar trickplay even when the files are correct. Surface
    # the misconfiguration here so the wizard can show a "Fix it for me"
    # button before the user saves the server.
    if result.ok and cfg.type is ServerType.JELLYFIN and hasattr(live, "check_trickplay_extraction_status"):
        try:
            statuses = live.check_trickplay_extraction_status()
        except Exception as exc:
            logger.debug("Trickplay status probe raised: {}", exc)
            statuses = []

        misconfigured = [s for s in statuses if not s.get("extraction_enabled")]
        if misconfigured:
            response_body["warnings"] = [
                {
                    "code": "jellyfin_trickplay_disabled",
                    "message": (
                        "Some Jellyfin libraries have trickplay extraction disabled — "
                        "without it Jellyfin won't display preview thumbnails for files "
                        "we publish. Enable it via the 'Fix it for me' button or in "
                        "Jellyfin's library settings."
                    ),
                    "libraries": [{"id": s["id"], "name": s["name"]} for s in misconfigured],
                }
            ]

    return jsonify(response_body)


@api.route("/servers/<server_id>/test-connection", methods=["POST"])
@setup_or_auth_required
def test_existing_server_connection(server_id: str):
    """Probe a saved server's connection on demand.

    Drives the "Test Connection" button on the /servers page +
    Edit Server modal so users can verify after credential changes
    without having to wait for the next webhook to surface a failure.
    Returns the same shape as :func:`test_server_connection`.
    """
    raw_servers = _get_media_servers()
    target = next((s for s in raw_servers if isinstance(s, dict) and s.get("id") == server_id), None)
    if target is None:
        return jsonify({"ok": False, "message": f"server {server_id!r} not found"}), 404

    try:
        cfg = server_config_from_dict(target)
    except Exception as exc:
        return jsonify({"ok": False, "message": f"invalid server config: {exc}"}), 400

    live = _instantiate_for_probe(cfg)
    if live is None:
        return jsonify({"ok": False, "message": f"unsupported type {cfg.type.value!r}"}), 400

    try:
        result = live.test_connection()
    except Exception as exc:
        logger.warning(
            "Test Connection: unexpected error contacting saved server {!r} ({}: {})",
            cfg.name or cfg.id,
            type(exc).__name__,
            exc,
        )
        return jsonify({"ok": False, "message": f"unexpected error: {exc}"}), 200

    response_payload: dict = {
        "ok": result.ok,
        "server_id": result.server_id,
        "server_name": result.server_name,
        "version": result.version,
        "message": result.message,
    }

    # For Jellyfin, also probe the Media Preview Bridge plugin so the
    # UI can show a Plugin: installed/missing badge from a single Test
    # Connection click. Only fires when the connection itself succeeded
    # (no point probing a server we can't reach).
    if result.ok and cfg.type is ServerType.JELLYFIN and hasattr(live, "check_plugin_installed"):
        try:
            response_payload["plugin"] = live.check_plugin_installed()
        except Exception as exc:
            logger.debug("Plugin probe failed for {!r}: {}", cfg.name, exc)
            response_payload["plugin"] = {"installed": False, "version": "", "error": str(exc)[:200]}

    return jsonify(response_payload)


@api.route("/servers/<server_id>/install-plugin", methods=["POST"])
@setup_or_auth_required
def install_jellyfin_plugin(server_id: str):
    """One-click install Media Preview Bridge plugin on a saved Jellyfin server.

    Drives the "Install plugin in Jellyfin" button on the Edit Server
    modal (only visible for Jellyfin servers when the plugin probe
    reports missing). Calls
    :meth:`JellyfinServer.install_plugin` which adds our manifest URL
    to Jellyfin's plugin repositories, queues the package install, and
    requests a Jellyfin restart. Caller polls
    ``/test-connection`` afterwards for the plugin badge to flip.
    """
    raw_servers = _get_media_servers()
    target = next((s for s in raw_servers if isinstance(s, dict) and s.get("id") == server_id), None)
    if target is None:
        return jsonify({"ok": False, "error": f"server {server_id!r} not found"}), 404

    try:
        cfg = server_config_from_dict(target)
    except Exception as exc:
        return jsonify({"ok": False, "error": f"invalid server config: {exc}"}), 400

    if cfg.type is not ServerType.JELLYFIN:
        return jsonify({"ok": False, "error": "plugin install is Jellyfin-only"}), 400

    live = _instantiate_for_probe(cfg)
    if live is None or not hasattr(live, "install_plugin"):
        return jsonify({"ok": False, "error": "this Jellyfin client doesn't support plugin install"}), 400

    try:
        result = live.install_plugin()
    except Exception as exc:
        logger.warning("Plugin install on {!r} raised: {}", cfg.name or cfg.id, exc)
        return jsonify({"ok": False, "error": str(exc)}), 200

    return jsonify(result)


@api.route("/servers/<server_id>/enabled", methods=["PATCH"])
@setup_or_auth_required
def set_server_enabled(server_id: str):
    """Toggle the saved server's enabled flag without a full PUT.

    Drives the on/off switch on the /servers page server cards.
    Body: ``{"enabled": bool}``.
    """
    payload = request.get_json(silent=True) or {}
    if "enabled" not in payload or not isinstance(payload["enabled"], bool):
        return jsonify({"error": "body must be {enabled: bool}"}), 400

    settings = get_settings_manager()
    raw_servers = _get_media_servers()
    target_index = next(
        (i for i, s in enumerate(raw_servers) if isinstance(s, dict) and s.get("id") == server_id),
        None,
    )
    if target_index is None:
        return jsonify({"error": f"server {server_id!r} not found"}), 404

    updated = list(raw_servers)
    entry = dict(updated[target_index])
    entry["enabled"] = payload["enabled"]
    updated[target_index] = entry
    settings.set("media_servers", updated)
    logger.info("Server {!r} enabled={}", entry.get("name") or server_id, payload["enabled"])

    return jsonify({"server_id": server_id, "enabled": payload["enabled"]})


@api.route("/servers/<server_id>/vendor-extraction", methods=["POST"])
@setup_or_auth_required
def set_vendor_extraction(server_id: str):
    """Disable (or re-enable) the vendor's own scan-time preview generation.

    Drives the "Vendor-side preview generation" panel on the Edit
    Server modal. When this app handles preview generation, the
    vendor's own scanner-thumbnail step is wasted CPU. Body:
    ``{"scan_extraction": bool}``.

    Behaviour per vendor:
      * Plex: flips ``scannerThumbnailVideoFiles`` per library section.
      * Emby: flips ``Extract*ImagesDuringLibraryScan`` per library.
      * Jellyfin: flips ``ExtractTrickplayImagesDuringLibraryScan`` AND
        ``SaveTrickplayWithMedia`` per library. Always KEEPS
        ``EnableTrickplayImageExtraction = True`` (D38: that flag is
        destructive — Jellyfin deletes our published trickplay when
        it's False). The daily "Refresh Trickplay Images" task is
        deliberately LEFT at its default 3 AM trigger because that
        task is also Jellyfin's import path for our published files;
        clearing it makes our trickplay sit on disk forever invisible.
    """
    payload = request.get_json(silent=True) or {}
    if "scan_extraction" not in payload or not isinstance(payload["scan_extraction"], bool):
        return jsonify({"error": "body must be {scan_extraction: bool}"}), 400

    raw_servers = _get_media_servers()
    target = next((s for s in raw_servers if isinstance(s, dict) and s.get("id") == server_id), None)
    if target is None:
        return jsonify({"error": f"server {server_id!r} not found"}), 404

    try:
        cfg = server_config_from_dict(target)
    except Exception as exc:
        return jsonify({"error": f"invalid server config: {exc}"}), 400

    live = _instantiate_for_probe(cfg)
    if live is None or not hasattr(live, "set_vendor_extraction"):
        return jsonify({"error": f"vendor {cfg.type.value!r} doesn't support extraction toggle yet"}), 400

    try:
        results = live.set_vendor_extraction(scan_extraction=payload["scan_extraction"])
    except Exception as exc:
        logger.warning(
            "Could not toggle vendor extraction on {!r}: {}",
            cfg.name or cfg.id,
            exc,
        )
        return jsonify({"ok": False, "error": str(exc)}), 200

    ok_count = sum(1 for v in results.values() if v == "ok")
    skipped_count = sum(1 for v in results.values() if v.startswith("skipped"))
    error_count = sum(1 for v in results.values() if v.startswith("error"))
    total = len(results)
    return jsonify(
        {
            "ok": error_count == 0,
            "results": results,
            "ok_count": ok_count,
            "skipped_count": skipped_count,
            "error_count": error_count,
            "total": total,
            "scan_extraction": payload["scan_extraction"],
        }
    )


@api.route("/servers/<server_id>/jellyfin/trickplay-status", methods=["GET"])
@setup_or_auth_required
def get_jellyfin_trickplay_status(server_id: str):
    """Per-library trickplay-extraction status for a saved Jellyfin server.

    The ``/servers`` page calls this once per Jellyfin card after the
    list renders, so the "Fix trickplay" button only appears when at
    least one library actually needs fixing — without it, the button
    showed up on every Jellyfin card forever even after a successful
    fix.

    Returns ``{"libraries": [{id, name, extraction_enabled, ...}]}``
    on success or ``{"error": "..."}`` with a 4xx/5xx status. Callers
    are expected to derive ``needs_fix = any(not l.extraction_enabled
    for l in libraries)``.
    """
    raw_servers = _get_media_servers()
    target = next((s for s in raw_servers if isinstance(s, dict) and s.get("id") == server_id), None)
    if target is None:
        return jsonify({"error": f"server {server_id!r} not found"}), 404

    try:
        cfg = server_config_from_dict(target)
    except Exception as exc:
        return jsonify({"error": f"invalid server config: {exc}"}), 400

    if cfg.type is not ServerType.JELLYFIN:
        return jsonify({"error": f"server {server_id} is not a Jellyfin server"}), 400

    live = _instantiate_for_probe(cfg)
    if live is None or not hasattr(live, "check_trickplay_extraction_status"):
        return jsonify({"error": "could not instantiate Jellyfin client"}), 500

    try:
        statuses = live.check_trickplay_extraction_status()
    except Exception as exc:
        # Upstream Jellyfin failure — surface as 502 so monitoring/clients
        # see "bad gateway" rather than a 200 with an error body (the rest
        # of this file uses 502 for the same shape; was 200 by oversight).
        logger.warning(
            "Trickplay status probe failed for Jellyfin server {!r}: {}: {}",
            cfg.name or server_id,
            type(exc).__name__,
            exc,
        )
        return jsonify({"error": str(exc)}), 502

    return jsonify({"libraries": statuses})


@api.route("/servers/<server_id>/jellyfin/fix-trickplay", methods=["POST"])
@setup_or_auth_required
def fix_jellyfin_trickplay(server_id: str):
    """One-click fix for the ``EnableTrickplayImageExtraction`` gotcha.

    Body: optional ``{"library_ids": ["<id>", ...]}`` to scope the fix
    to specific libraries; absent body flips every library on the
    server. Calls :meth:`JellyfinServer.enable_trickplay_extraction`
    which POSTs the updated ``LibraryOptions`` to Jellyfin.

    Returns ``{"ok": true|false, "results": {<lib_id>: "ok"|<error>}}``.
    The endpoint reports 200 even on partial failure — the per-library
    results dict carries the actual story so the UI can show a row-by-row
    summary.
    """
    raw_servers = _get_media_servers()
    target = next((s for s in raw_servers if isinstance(s, dict) and s.get("id") == server_id), None)
    if target is None:
        return jsonify({"error": f"server {server_id!r} not found"}), 404

    try:
        cfg = server_config_from_dict(target)
    except Exception as exc:
        return jsonify({"error": f"invalid server config: {exc}"}), 400

    if cfg.type is not ServerType.JELLYFIN:
        return jsonify({"error": f"server {server_id} is not a Jellyfin server"}), 400

    live = _instantiate_for_probe(cfg)
    if live is None or not hasattr(live, "enable_trickplay_extraction"):
        return jsonify({"error": "could not instantiate Jellyfin client"}), 500

    body = request.get_json(silent=True) or {}
    library_ids = body.get("library_ids") if isinstance(body, dict) else None
    if library_ids is not None and not isinstance(library_ids, list):
        return jsonify({"error": "library_ids must be a list"}), 400

    try:
        results = live.enable_trickplay_extraction(library_ids=library_ids)
    except Exception as exc:
        logger.warning(
            "Fix-trickplay: could not enable Jellyfin's trickplay-extraction setting on server {!r} "
            "({}: {}). No Jellyfin settings were changed. "
            "As a manual fallback, enable it in Jellyfin's web UI: "
            "Dashboard → Libraries → edit each library → tick 'Trickplay image extraction'.",
            cfg.name or server_id,
            type(exc).__name__,
            exc,
        )
        # Total upstream failure (network/auth) — return 502 so the JS path
        # `r.ok === false` triggers and the user sees the toast. Per-library
        # partial failure still returns 200 (handled below) because the
        # response carries `ok: bool` + per-library results.
        return jsonify({"ok": False, "error": str(exc)}), 502

    all_ok = all(v == "ok" for v in results.values())
    return jsonify({"ok": all_ok, "results": results})


@api.route("/servers/<server_id>/health-check", methods=["GET"])
@setup_or_auth_required
def get_server_health_check(server_id: str):
    """Generic per-server settings audit.

    Replaces the Jellyfin-specific ``trickplay-status`` endpoint
    over time — same shape, all vendors. Returns:

    .. code-block:: json

        {
          "issues": [
            {
              "library_id": "...", "library_name": "Movies",
              "flag": "EnableRealtimeMonitor", "label": "Auto-detect new files",
              "current": false, "recommended": true,
              "severity": "recommended", "fixable": true,
              "rationale": "Without this, ..."
            },
            ...
          ],
          "issue_count": <int>, "fixable_count": <int>,
          "vendor": "jellyfin"
        }

    Empty ``issues`` means "all good — render the green checkmark".
    Vendors that don't yet implement ``check_settings_health`` return
    an empty list (the default in :class:`MediaServer`).
    """
    raw_servers = _get_media_servers()
    target = next((s for s in raw_servers if isinstance(s, dict) and s.get("id") == server_id), None)
    if target is None:
        return jsonify({"error": f"server {server_id!r} not found"}), 404

    try:
        cfg = server_config_from_dict(target)
    except Exception as exc:
        return jsonify({"error": f"invalid server config: {exc}"}), 400

    live = _instantiate_for_probe(cfg)
    if live is None:
        return jsonify({"error": "could not instantiate server client"}), 500

    try:
        issues = live.check_settings_health()
    except Exception as exc:
        logger.warning(
            "Health check failed for server {!r}: {}: {}",
            cfg.name or server_id,
            type(exc).__name__,
            exc,
        )
        return jsonify({"error": str(exc)}), 502

    payload = [
        {
            "library_id": i.library_id,
            "library_name": i.library_name,
            "flag": i.flag,
            "label": i.label,
            "rationale": i.rationale,
            "current": i.current,
            "recommended": i.recommended,
            "severity": i.severity,
            "fixable": i.fixable,
        }
        for i in issues
    ]
    return jsonify(
        {
            "vendor": cfg.type.value,
            "issues": payload,
            "issue_count": len(payload),
            "fixable_count": sum(1 for i in payload if i["fixable"]),
        }
    )


@api.route("/servers/<server_id>/health-check/apply", methods=["POST"])
@setup_or_auth_required
def apply_server_health_fixes(server_id: str):
    """Apply recommended settings to one or more flags.

    Body (optional): ``{"flags": ["EnableRealtimeMonitor", ...]}`` to
    restrict the fix to specific flags. Absent body = "fix every issue
    currently surfaced". Returns:

    .. code-block:: json

        {
          "ok": true|false,
          "results": {"<lib_id>:<flag>": "ok"|"<error>", ...}
        }
    """
    raw_servers = _get_media_servers()
    target = next((s for s in raw_servers if isinstance(s, dict) and s.get("id") == server_id), None)
    if target is None:
        return jsonify({"error": f"server {server_id!r} not found"}), 404

    try:
        cfg = server_config_from_dict(target)
    except Exception as exc:
        return jsonify({"error": f"invalid server config: {exc}"}), 400

    live = _instantiate_for_probe(cfg)
    if live is None:
        return jsonify({"error": "could not instantiate server client"}), 500

    body = request.get_json(silent=True) or {}
    flags = body.get("flags") if isinstance(body, dict) else None
    if flags is not None and not isinstance(flags, list):
        return jsonify({"error": "flags must be a list of strings"}), 400

    try:
        results = live.apply_recommended_settings(flags=flags)
    except Exception as exc:
        logger.warning(
            "Health-check apply failed for server {!r}: {}: {}",
            cfg.name or server_id,
            type(exc).__name__,
            exc,
        )
        return jsonify({"ok": False, "error": str(exc)}), 502

    # Empty results = nothing needed fixing → success (not ok=False).
    # Non-empty + every entry "ok" = success. Anything else = partial.
    all_ok = all(v == "ok" for v in results.values()) if results else True
    return jsonify({"ok": all_ok, "results": results})


@api.route("/servers/<server_id>/output-status", methods=["GET"])
@setup_or_auth_required
def get_output_status(server_id: str):
    """Report whether output files exist for a given canonical path on this server.

    Diagnostic endpoint used by the multi-server BIF viewer + by users who
    want to verify "did the publisher actually write something for this
    file?" without shelling into the container.

    Query params:
        path: Absolute local file path. Required.

    Returns:
        ``{server_id, server_type, adapter, paths, exists, missing_paths}``.
        ``exists`` is True only when *every* path the adapter would write
        is present on disk; ``missing_paths`` lists any that aren't.
        Multi-file formats like Jellyfin trickplay therefore correctly
        report ``exists=False`` until the manifest *and* the sheets are
        all there.
    """
    from pathlib import Path as _Path

    from ...processing.multi_server import _adapter_for_server
    from ...utils import sanitize_path

    raw_path = (request.args.get("path") or "").strip()
    if not raw_path:
        return jsonify({"error": "path query parameter required"}), 400
    canonical_path = sanitize_path(raw_path)

    raw_servers = _get_media_servers()
    target = next((s for s in raw_servers if isinstance(s, dict) and s.get("id") == server_id), None)
    if target is None:
        return jsonify({"error": f"server {server_id!r} not found"}), 404

    try:
        cfg = server_config_from_dict(target)
    except UnsupportedServerTypeError as exc:
        return jsonify({"error": str(exc)}), 400

    adapter = _adapter_for_server(cfg)
    if adapter is None:
        return jsonify({"error": f"no adapter wired for server type {cfg.type.value}"}), 400

    # Most adapters need only the canonical path to compute outputs;
    # Plex needs an item_id (bundle hash). The endpoint accepts an
    # optional item_id query param for that case.
    from ...output import BifBundle

    bundle = BifBundle(
        canonical_path=canonical_path,
        frame_dir=_Path("."),  # unused — only canonical_path is read
        bif_path=None,
        frame_interval=int((cfg.output or {}).get("frame_interval") or 10),
        width=int((cfg.output or {}).get("width") or 320),
        height=180,
        frame_count=0,
    )
    item_id = request.args.get("item_id") or None

    if cfg.type is ServerType.PLEX:
        # Plex: we need the live server to compute the bundle path.
        # Without an item_id we can't go further; report the limitation.
        if not item_id:
            return jsonify(
                {
                    "server_id": server_id,
                    "server_type": cfg.type.value,
                    "adapter": adapter.name,
                    "paths": [],
                    "exists": False,
                    "missing_paths": [],
                    "needs_item_id": True,
                    "message": (
                        "Plex bundle adapter requires an item_id query param to look up the per-item bundle hash."
                    ),
                }
            )
        # Build a live PlexServer to query the bundle hash.
        try:
            from ...config import load_config

            registry = ServerRegistry.from_settings(raw_servers, legacy_config=load_config())
            live = registry.get(server_id)
            if live is None:
                return jsonify({"error": "could not instantiate Plex server"}), 500
            paths = adapter.compute_output_paths(bundle, live, item_id)
        except Exception as exc:
            return jsonify({"error": f"compute_output_paths failed: {exc}"}), 502
    else:
        # Emby / Jellyfin: pure path computation. Jellyfin's adapter
        # advertises needs_server_metadata=True because it requires an
        # item_id, but it doesn't need a live server — the item_id
        # comes from the query string. Pass server=None.
        try:
            paths = adapter.compute_output_paths(bundle, server=None, item_id=item_id)
        except ValueError as exc:
            # Adapter rejected — typically means item_id was missing for
            # an adapter that requires it. Surface the reason in the
            # response shape so the UI can prompt the user.
            return jsonify(
                {
                    "server_id": server_id,
                    "server_type": cfg.type.value,
                    "adapter": adapter.name,
                    "paths": [],
                    "exists": False,
                    "missing_paths": [],
                    "needs_item_id": "item_id" in str(exc).lower(),
                    "message": str(exc),
                }
            )
        except Exception as exc:
            return jsonify({"error": f"compute_output_paths failed: {exc}"}), 400

    str_paths = [str(p) for p in paths]
    missing = [str(p) for p in paths if not p.exists()]
    # Jellyfin manifests reference a sibling directory of tile sheets that
    # also must exist; check the convention.
    if cfg.type is ServerType.JELLYFIN and paths:
        sheets_dir = paths[0].with_suffix("")
        if not sheets_dir.exists():
            missing.append(str(sheets_dir))
        else:
            # Surface that the directory exists for callers who want it.
            str_paths.append(str(sheets_dir) + "/")

    return jsonify(
        {
            "server_id": server_id,
            "server_type": cfg.type.value,
            "adapter": adapter.name,
            "paths": str_paths,
            "exists": len(missing) == 0 and bool(paths),
            "missing_paths": missing,
        }
    )
