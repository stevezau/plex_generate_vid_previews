"""Universal webhook router with vendor auto-detection.

Single inbound URL — ``POST /api/webhooks/incoming`` — handles every
source. The router classifies the payload by shape and (for vendor
webhooks) by the server identifier embedded in every vendor's payload,
then dispatches to :func:`process_canonical_path` for fan-out.

Detection cascade:

1. **Plex multipart**: form-encoded ``payload`` field with JSON inside.
   Identified by ``Server.uuid``.
2. **Jellyfin webhook plugin**: top-level ``NotificationType``.
   Identified by ``ServerId``.
3. **Emby Webhooks plugin**: top-level ``Event`` with a ``Server`` block
   (Plex-format-compatible). Identified by ``Server.Id``.
4. **Sonarr / Radarr**: top-level ``eventType`` plus ``movieFile`` /
   ``episodeFile`` blocks. Path-bearing — no callback needed.
5. **Path-first / templated**: a normalized ``{"path": "..."}`` body.
   Used by custom integrations and templated Jellyfin webhooks.

For any vendor identified by server id, we look the corresponding
:class:`ServerConfig` up in the registry and use its
:meth:`MediaServer.parse_webhook` to extract the item id, then call
:meth:`MediaServer.resolve_item_to_remote_path` for the file path.
The reported path is canonicalised via the server's own
``path_mappings`` before being handed to the processor.

A per-server fallback URL — ``POST /api/webhooks/server/<id>`` — is
also available for setups where auto-detection is ambiguous (rare;
typically only when two installations share a machine identifier).
"""

from __future__ import annotations

import json
from typing import Any

from flask import jsonify, request
from loguru import logger

from ..processing.multi_server import process_canonical_path
from ..servers import (
    MediaServer,
    ServerConfig,
    ServerRegistry,
    ServerType,
    WebhookEvent,
)
from ..servers.ownership import apply_path_mappings
from .settings_manager import get_settings_manager
from .webhooks import _authenticate_webhook, webhooks_bp


def _build_registry_from_settings() -> ServerRegistry:
    """Construct a fresh :class:`ServerRegistry` from current settings.

    Webhook-time construction is cheap (settings is already in memory and
    each ``MediaServer`` instance defers HTTP I/O until first use). Doing
    it per request keeps the router stateless and picks up settings
    changes (added / removed servers) without any cache invalidation.
    """
    settings = get_settings_manager()
    raw_servers = settings.get("media_servers") or []
    if not isinstance(raw_servers, list):
        raw_servers = []

    # Plex needs the legacy config; Emby/Jellyfin don't. If load_config
    # fails (no Plex configured, or running in a multi-server-only
    # deployment), continue with legacy_config=None — the registry's
    # _build_server skips Plex entries that lack one.
    legacy_config = None
    try:
        from ..config import load_config

        legacy_config = load_config()
    except Exception as exc:
        logger.debug("Webhook router: load_config failed (Plex paths disabled): {}", exc)

    return ServerRegistry.from_settings(raw_servers, legacy_config=legacy_config)


class _MinimalConfig:
    """Minimal stand-in for :class:`Config` when Plex isn't configured.

    The webhook dispatcher's :func:`process_canonical_path` only reads
    a handful of attrs from its config arg (``working_tmp_folder``,
    ``plex_bif_frame_interval``, plus FFmpeg settings used by
    :func:`generate_images`). When Plex isn't configured (a valid
    multi-server-only deployment, or a CI runner with no PLEX_URL),
    we synthesise this minimal shape from settings instead of failing
    the dispatch.
    """

    def __init__(self, settings) -> None:
        # working_tmp_folder; the dispatcher uses it for the frame
        # cache base_dir + per-file tmp dirs. Defaults to the system
        # tempdir when settings haven't been configured.
        import tempfile

        self.working_tmp_folder = str(
            settings.get("working_tmp_folder") or settings.get("tmp_folder") or tempfile.gettempdir()
        )
        self.tmp_folder = self.working_tmp_folder
        self.plex_bif_frame_interval = int(
            settings.get("plex_bif_frame_interval") or settings.get("thumbnail_interval") or 10
        )
        self.thumbnail_quality = int(settings.get("thumbnail_quality") or 4)
        self.tonemap_algorithm = str(settings.get("tonemap_algorithm") or "hable")
        self.ffmpeg_threads = int(settings.get("ffmpeg_threads") or 2)
        self.cpu_threads = int(settings.get("cpu_threads") or 2)
        self.gpu_threads = int(settings.get("gpu_threads") or 0)
        self.gpu_config = settings.get("gpu_config") or []
        self.regenerate_thumbnails = bool(settings.get("regenerate_thumbnails", False))
        self.path_mappings = settings.get("path_mappings") or []
        self.plex_local_videos_path_mapping = ""
        self.plex_videos_path_mapping = ""
        # Plex-only fields kept as empty strings so any incidental
        # access during the Emby/Jellyfin path doesn't AttributeError.
        self.plex_url = ""
        self.plex_token = ""
        self.plex_config_folder = ""
        self.plex_timeout = 60
        self.plex_verify_ssl = True
        self.plex_libraries = []
        self.plex_library_ids = None
        self.tmp_folder_created_by_us = False
        self.ffmpeg_path = "/usr/bin/ffmpeg"
        self.log_level = "INFO"
        self.worker_pool_timeout = 60
        self.sort_by = "newest"
        self.selected_libraries = []


def _load_config_or_minimal():
    """Return a real :class:`Config` if Plex is configured, else a minimal shim."""
    try:
        from ..config import load_config

        return load_config()
    except Exception as exc:
        logger.debug("Webhook router: load_config failed; using minimal shim: {}", exc)
        return _MinimalConfig(get_settings_manager())


def _classify_payload(req) -> tuple[str, dict[str, Any] | None, str]:
    """Classify the inbound request by payload shape.

    Returns ``(kind, parsed_payload, error_message)`` where ``kind`` is
    one of ``"plex" | "jellyfin" | "emby" | "sonarr" | "radarr" |
    "path" | "unknown"``. ``parsed_payload`` is the dict the rest of
    the router walks; ``error_message`` is non-empty only when parsing
    failed.
    """
    # Plex sends multipart form data with a JSON ``payload`` field.
    plex_payload = req.form.get("payload") if req.form else None
    if plex_payload:
        try:
            data = json.loads(plex_payload)
        except (TypeError, ValueError) as exc:
            return "plex", None, f"Plex payload was not valid JSON: {exc}"
        if isinstance(data, dict):
            return "plex", data, ""

    # Everything else is JSON.
    try:
        data = req.get_json(silent=True, force=False) if req.is_json else None
    except Exception:
        data = None
    if not isinstance(data, dict):
        # Best-effort: try parsing the raw body as JSON.
        try:
            raw = req.get_data(as_text=True) or ""
            if raw.strip().startswith("{"):
                data = json.loads(raw)
            else:
                data = None
        except (TypeError, ValueError):
            data = None

    if not isinstance(data, dict):
        return "unknown", None, "request body was neither multipart nor JSON"

    if "NotificationType" in data:
        return "jellyfin", data, ""
    if "Event" in data and isinstance(data.get("Server"), dict):
        return "emby", data, ""
    event_type = str(data.get("eventType") or "").lower()
    if event_type and ("movie" in data or "movieFile" in data):
        return "radarr", data, ""
    if event_type and ("series" in data or "episodeFile" in data or "episodes" in data):
        return "sonarr", data, ""
    if "path" in data:
        return "path", data, ""

    return "unknown", data, "no recognised vendor signature in payload"


def _server_id_from_payload(kind: str, payload: dict[str, Any]) -> str | None:
    """Extract the source server identifier from a parsed payload.

    Plex, Emby, and Jellyfin all surface the server id in webhook
    payloads — Plex as ``Server.uuid``, Emby as ``Server.Id``, Jellyfin
    as ``ServerId``. We use whichever is present to match the
    configured registry entry.
    """
    if kind == "plex":
        return str((payload.get("Server") or {}).get("uuid") or "") or None
    if kind == "emby":
        server = payload.get("Server") or {}
        return str(server.get("Id") or server.get("uuid") or "") or None
    if kind == "jellyfin":
        return str(payload.get("ServerId") or "") or None
    return None


def _match_registry_server(
    registry: ServerRegistry,
    *,
    kind: str,
    server_id_hint: str | None,
) -> tuple[MediaServer | None, ServerConfig | None]:
    """Find the live ``MediaServer`` matching the payload's reported id.

    Returns ``(None, None)`` when no match exists. Falls back to the
    first server of the right type when the payload didn't carry an id
    (some older webhook plugins omit it) and only one such server is
    configured — that's safe because there's no ambiguity to resolve.
    """
    expected_type = {
        "plex": ServerType.PLEX,
        "emby": ServerType.EMBY,
        "jellyfin": ServerType.JELLYFIN,
    }.get(kind)

    if expected_type is None:
        return None, None

    candidates = [(registry.get(c.id), c) for c in registry.configs() if c.type is expected_type]

    if server_id_hint:
        # Match against the server's *self-reported* identity
        # captured at probe time (Plex machineIdentifier, Emby/
        # Jellyfin ServerId). The locally-generated ``cfg.id`` UUID
        # never appears in vendor payloads.
        matches = [
            (live, cfg) for live, cfg in candidates if cfg.server_identity and cfg.server_identity == server_id_hint
        ]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            # Identity collision — extremely rare in practice (cloned-VM
            # scenario where two Plex installs share the same
            # machineIdentifier). The webhook can't be confidently routed
            # so we refuse rather than silently picking one. Users hit
            # this should give one of the two servers a fresh identity
            # or use the per-server fallback URL ``/api/webhooks/server/<id>``.
            logger.warning(
                "Two or more configured {} servers share the same server identity ({!r}, {} matches). "
                "We can't tell which one this webhook came from, so it's being dropped to avoid the "
                "wrong server getting credit. Fix: either re-install one server so it has a fresh "
                "identity, or point each server at its own URL '/api/webhooks/server/<server_id>' "
                "(found on the Servers page).",
                kind,
                server_id_hint,
                len(matches),
            )
            return None, None

    # Single candidate: unambiguous — use it. Covers two cases:
    # (a) the payload omits the identity hint, (b) the configured
    # entry's ``server_identity`` hasn't been populated yet (e.g. user
    # added the server while it was offline and never re-probed).
    if len(candidates) == 1:
        return candidates[0]

    return None, None


def _path_from_path_payload(payload: dict[str, Any]) -> str | None:
    """Pull a path out of a Sonarr/Radarr/templated payload."""
    path = payload.get("path")
    if isinstance(path, str) and path.strip():
        return path.strip()
    # Sonarr / Radarr embed paths under typed sub-objects.
    movie_file = payload.get("movieFile") or {}
    if isinstance(movie_file, dict) and movie_file.get("path"):
        return str(movie_file["path"])
    episode_file = payload.get("episodeFile") or {}
    if isinstance(episode_file, dict) and episode_file.get("path"):
        return str(episode_file["path"])
    return None


def _canonicalize_path(remote_path: str, server_config: ServerConfig | None) -> str:
    """Translate a server's view of a path to the local canonical view.

    Falls back to the input path unchanged when no server is provided
    (e.g. Sonarr/Radarr payloads) — the dispatcher's ownership
    resolver will still match if the path is already local.
    """
    if not server_config or not server_config.path_mappings:
        return remote_path
    candidates = apply_path_mappings(remote_path, server_config.path_mappings)
    return candidates[0] if candidates else remote_path


def _resolve_to_canonical_path(
    *,
    kind: str,
    payload: dict[str, Any],
    registry: ServerRegistry,
    explicit_server_id: str | None = None,
) -> tuple[str | None, dict[str, str], str]:
    """Convert any classified payload to a canonical local file path.

    Returns ``(canonical_path, item_id_by_server, error_message)``.

    ``item_id_by_server`` is forwarded to :func:`process_canonical_path`
    so per-server adapters that need a server-specific item id (Plex's
    bundle hash; Jellyfin's manifest key) can use the dispatcher's
    hint instead of paying for a second API roundtrip.
    """
    if kind in ("sonarr", "radarr", "path"):
        path = _path_from_path_payload(payload)
        if not path:
            return None, {}, "payload did not carry a usable file path"
        return path, {}, ""

    if kind in ("plex", "emby", "jellyfin"):
        server_id_hint = explicit_server_id or _server_id_from_payload(kind, payload)
        live_server, server_cfg = _match_registry_server(
            registry,
            kind=kind,
            server_id_hint=server_id_hint,
        )
        if live_server is None or server_cfg is None:
            return None, {}, f"could not match {kind} webhook to a configured server"

        try:
            event: WebhookEvent | None = live_server.parse_webhook(payload, headers=dict(request.headers))
        except Exception as exc:
            logger.debug("parse_webhook raised for {}: {}", live_server.name, exc)
            return None, {}, f"{kind} webhook parsing failed: {exc}"
        if event is None:
            return None, {}, f"{kind} payload not relevant (e.g. playback event)"

        # Path-bearing webhook (templated Jellyfin) — short-circuit.
        if event.remote_path:
            canonical = _canonicalize_path(event.remote_path, server_cfg)
            return canonical, ({live_server.id: event.item_id} if event.item_id else {}), ""

        if not event.item_id:
            return None, {}, f"{kind} webhook had neither item id nor path"

        try:
            remote_path = live_server.resolve_item_to_remote_path(event.item_id)
        except Exception as exc:
            return None, {}, f"resolve_item_to_remote_path failed: {exc}"
        if not remote_path:
            return (
                None,
                {},
                f"{kind} item {event.item_id!r} could not be resolved to a path (it may not be indexed yet)",
            )

        canonical = _canonicalize_path(remote_path, server_cfg)
        return canonical, {live_server.id: event.item_id}, ""

    return None, {}, f"unsupported webhook kind: {kind}"


@webhooks_bp.route("/incoming", methods=["POST"])
@_authenticate_webhook
def webhook_incoming():
    """Universal webhook entry point with auto-detected vendor routing.

    Single URL accepts payloads from Plex, Emby, Jellyfin, Sonarr,
    Radarr, and any custom integration that posts ``{"path": "..."}``.
    The router resolves the payload to a canonical local path and calls
    :func:`process_canonical_path` to fan out to every owning server.

    Auth: same shared-secret token semantics as the existing
    Sonarr/Radarr webhooks (token in ``Authorization`` header or
    ``token`` query param). Plex's native webhook UI doesn't support
    custom headers, so query-param auth is the only option for that
    source.
    """
    kind, payload, parse_error = _classify_payload(request)
    # Single line capturing the classification — useful for "what came
    # in?" debugging without enabling DEBUG-level logs in production.
    logger.info(
        "Webhook arrived: kind={} remote={} content_type={} content_length={}",
        kind,
        request.remote_addr,
        request.content_type,
        request.content_length,
    )
    if payload is None:
        logger.warning(
            "Webhook from {} rejected: {}. "
            "The body couldn't be parsed — make sure the source is sending JSON (Content-Type: application/json). "
            "Plex sends multipart/form-data with a 'payload' field; that's also accepted.",
            request.remote_addr,
            parse_error or "unrecognised payload",
        )
        return jsonify({"status": "ignored", "reason": parse_error or "unrecognised"}), 400

    if kind == "unknown":
        # Body parsed but didn't match any vendor signature — caller error.
        logger.warning(
            "Webhook from {} (Content-Type={}) parsed OK but didn't match any known vendor shape "
            '(Plex / Emby / Jellyfin / Sonarr / Radarr / generic \'{{"path": "..."}}\'). '
            "If you're using a custom integration, post a JSON body with a top-level 'path' field.",
            request.remote_addr,
            request.content_type,
        )
        return (
            jsonify({"status": "ignored", "kind": kind, "reason": parse_error or "unrecognised"}),
            400,
        )

    registry = _build_registry_from_settings()
    canonical, item_id_by_server, error = _resolve_to_canonical_path(
        kind=kind,
        payload=payload,
        registry=registry,
    )

    if not canonical:
        logger.info(
            "Webhook router: ignoring {} payload from {} — {}",
            kind,
            request.remote_addr,
            error,
        )
        return jsonify({"status": "ignored", "kind": kind, "reason": error}), 202

    logger.info(
        "Webhook router: routing {} payload to canonical_path={} (item hints: {})",
        kind,
        canonical,
        item_id_by_server or "{}",
    )
    return _dispatch_canonical_path(canonical, registry, item_id_by_server, kind=kind)


@webhooks_bp.route("/server/<server_id>", methods=["POST"])
@_authenticate_webhook
def webhook_per_server(server_id: str):
    """Per-server URL — disambiguates the source AND pins dispatch.

    Two distinct uses:

    1. **Disambiguation:** auto-detection can't tell two servers apart
       (e.g. two Plex installs share a machine identifier — rare but real
       with cloned VMs). The URL's ``server_id`` overrides any server-id
       hint in the payload during resolution.
    2. **Dispatch pinning:** when the user explicitly POSTs to this URL
       they're saying "this webhook is for *this* server". We forward
       ``server_id`` as a ``server_id_filter`` to ``process_canonical_path``
       so previews are generated only for that server, even if a sibling
       server in the registry also owns the canonical path. Without
       this, the same Plex webhook on a Plex+Jellyfin install would
       publish to both publishers — surprising given the URL's intent.
    """
    kind, payload, parse_error = _classify_payload(request)
    if payload is None:
        return jsonify({"status": "ignored", "reason": parse_error or "unrecognised"}), 400

    registry = _build_registry_from_settings()
    if registry.get_config(server_id) is None:
        return jsonify({"status": "ignored", "reason": f"server {server_id!r} not configured"}), 404

    canonical, item_id_by_server, error = _resolve_to_canonical_path(
        kind=kind,
        payload=payload,
        registry=registry,
        explicit_server_id=server_id,
    )
    if not canonical:
        return jsonify({"status": "ignored", "kind": kind, "reason": error}), 202

    return _dispatch_canonical_path(
        canonical,
        registry,
        item_id_by_server,
        kind=kind,
        server_id_filter=server_id,
    )


def _dispatch_canonical_path(
    canonical_path: str,
    registry: ServerRegistry,
    item_id_by_server: dict[str, str],
    *,
    kind: str,
    server_id_filter: str | None = None,
):
    """Hand the canonical path to :func:`process_canonical_path` and shape the response."""
    config = _load_config_or_minimal()

    result = process_canonical_path(
        canonical_path=canonical_path,
        registry=registry,
        config=config,
        item_id_by_server=item_id_by_server,
        server_id_filter=server_id_filter,
    )

    body = {
        "status": result.status.value,
        "kind": kind,
        "canonical_path": result.canonical_path,
        "frame_count": result.frame_count,
        "publishers": [
            {
                "server_id": p.server_id,
                "server_name": p.server_name,
                "adapter": p.adapter_name,
                "status": p.status.value,
                "message": p.message,
            }
            for p in result.publishers
        ],
        "message": result.message,
    }
    # 202 Accepted for async-style responses; 200 for completed.
    return jsonify(body), 200
