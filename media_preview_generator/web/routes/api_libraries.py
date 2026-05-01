"""Library discovery API.

Hosts the ``GET /api/libraries`` endpoint and the helpers that back it.
Split out of ``api_system.py`` so library-specific code (multi-server
aggregation, Plex-specific HTTP fast-path, in-process result cache, type
classification) lives next to the route that uses it instead of being
mixed in with status / config / health / log-history endpoints.

Public re-exports kept for backwards compatibility with callers that
imported these names from ``api_system``:

- :func:`clear_library_cache` — used by ``api_settings`` whenever a Plex
  URL/token change invalidates the cached library list.
- :func:`_fetch_libraries_via_http` — used by ``api_plex`` for the
  unauthenticated setup-wizard probe.
- :func:`classify_library_type` — used by tests and by the Plex section
  enumerator inside :func:`get_libraries` to derive a display label.
"""

import threading
import time

import urllib3
from flask import jsonify, request
from loguru import logger

from ..auth import api_token_required
from . import api
from ._helpers import _param_to_bool

_SPORTS_AGENT_PATTERNS = ("sportarr", "sportscanner")

# Plex library list is cached for 5 minutes so the dashboard, Schedules
# picker, and Start-Job modal don't each issue an HTTP request to Plex on
# every page load. Only used for the saved-credential path; setup-wizard
# overrides bypass the cache so users see immediate results when they
# change URL/token.
_library_cache: dict = {"result": None, "fetched_at": 0.0}
_library_cache_lock = threading.Lock()
_LIBRARY_CACHE_TTL = 300  # 5 minutes


def clear_library_cache() -> None:
    """Reset the Plex library cache.

    Useful for tests and when settings change (e.g. Plex URL updated).
    """
    with _library_cache_lock:
        _library_cache["result"] = None
        _library_cache["fetched_at"] = 0.0


def classify_library_type(section_type: str, agent: str) -> str:
    """Derive a display-friendly library type from Plex section type and agent.

    Args:
        section_type: Plex library type (``"movie"``, ``"show"``, etc.).
        agent: Plex metadata agent identifier string.

    Returns:
        One of ``"movie"``, ``"show"``, ``"sports"``, or ``"other_videos"``.
    """
    agent_lower = (agent or "").lower()
    if section_type == "show":
        for pattern in _SPORTS_AGENT_PATTERNS:
            if pattern in agent_lower:
                return "sports"
        return "show"
    if section_type == "movie":
        if agent_lower == "com.plexapp.agents.none":
            return "other_videos"
        return "movie"
    return section_type


def _fetch_libraries_via_http(
    plex_url: str,
    plex_token: str,
    verify_ssl: bool = True,
) -> list:
    """Fetch Plex libraries via direct HTTP request.

    Args:
        plex_url: Plex server URL
        plex_token: Plex authentication token
        verify_ssl: Whether to verify the server's TLS certificate

    Returns:
        List of library dicts with id, name, type, agent, and display_type.
    """
    import requests

    if not verify_ssl:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    response = requests.get(
        f"{plex_url.rstrip('/')}/library/sections",
        headers={"X-Plex-Token": plex_token, "Accept": "application/json"},
        timeout=10,
        verify=verify_ssl,
    )
    response.raise_for_status()
    data = response.json()

    libraries = []
    for section in data.get("MediaContainer", {}).get("Directory", []):
        section_type = section.get("type")
        if section_type not in ("movie", "show"):
            continue
        agent = section.get("agent", "")
        libraries.append(
            {
                "id": str(section.get("key")),
                "name": section.get("title"),
                "type": section_type,
                "agent": agent,
                "display_type": classify_library_type(section_type, agent),
                "server_id": None,  # setup-wizard fast path: media_servers entry doesn't exist yet
                "server_name": None,
                "server_type": "plex",
            }
        )
    return libraries


def _libraries_for_configured_server(server_id: str) -> tuple[list[dict] | None, str | None, int]:
    """List libraries for one configured media server via the registry.

    Returns ``(libraries, error_message, http_status)``. ``libraries`` is None
    when ``error_message`` is set. Used by the multi-server library picker so
    the same endpoint serves Plex, Emby, and Jellyfin uniformly — each row is
    tagged with the originating ``server_id`` / ``server_name`` / ``server_type``
    so the Schedules picker can disambiguate same-named libraries.
    """
    from ...servers import ServerRegistry
    from ..settings_manager import get_settings_manager

    settings = get_settings_manager()
    raw_servers = settings.get("media_servers") or []
    if not isinstance(raw_servers, list):
        raw_servers = []

    target = next((s for s in raw_servers if isinstance(s, dict) and s.get("id") == server_id), None)
    if target is None:
        return None, f"server {server_id!r} not configured", 404
    if not target.get("enabled", True):
        return [], None, 200

    try:
        registry = ServerRegistry.from_settings(raw_servers, legacy_config=None)
    except Exception as exc:
        logger.warning(
            "Could not build server registry to list libraries for {} ({}: {}). "
            "Verify the server's configuration on the Servers page.",
            server_id,
            type(exc).__name__,
            exc,
        )
        return None, f"server registry unavailable: {exc}", 500

    server = registry.get(server_id)
    if server is None:
        return None, f"server {server_id!r} not instantiable", 500

    rows: list[dict] = []
    try:
        for lib in server.list_libraries():
            rows.append(
                {
                    "id": str(lib.id),
                    "name": lib.name,
                    "type": lib.kind or "",
                    "agent": "",
                    "display_type": (lib.kind or "").lower() or "library",
                    "server_id": server_id,
                    "server_name": target.get("name") or "",
                    "server_type": (target.get("type") or "").lower(),
                }
            )
    except Exception as exc:
        logger.warning(
            "Could not list libraries for {} ({}: {}). The schedules library picker will show no entries for this server. "
            "Verify the server is reachable on the Servers page (Test Connection).",
            target.get("name") or server_id,
            type(exc).__name__,
            exc,
        )
        return None, f"failed to list libraries: {exc}", 502
    return rows, None, 200


def _libraries_for_all_configured_servers() -> list[dict]:
    """Aggregate libraries across every configured + enabled media server.

    Each row is tagged with ``server_id`` / ``server_name`` / ``server_type``
    so a single picker can disambiguate "Movies (Home Plex)" from
    "Movies (Living Room Emby)". Servers that fail to enumerate are skipped
    silently after a warning — one bad server can't block the picker.
    """
    from ..settings_manager import get_settings_manager

    settings = get_settings_manager()
    raw_servers = settings.get("media_servers") or []
    if not isinstance(raw_servers, list):
        return []

    rows: list[dict] = []
    for entry in raw_servers:
        if not isinstance(entry, dict) or not entry.get("enabled", True):
            continue
        sid = entry.get("id")
        if not sid:
            continue
        libs, err, _status = _libraries_for_configured_server(sid)
        if err:
            continue
        rows.extend(libs or [])
    return rows


@api.route("/libraries")
@api_token_required
def get_libraries():
    """Get available libraries from one or all configured media servers.

    Modes:
      * ``?server_id=<id>`` — list libraries from that configured server
        (Plex / Emby / Jellyfin). Each row is tagged with its server identity.
      * ``?url=&token=`` — Setup-wizard fast path: hit Plex directly with
        credentials that haven't been persisted yet (Plex-only).
      * (no params) — list libraries across every enabled configured server,
        tagged with ``server_id`` / ``server_name`` / ``server_type`` so a
        single picker can disambiguate same-named libraries.

    Results from saved credentials are cached for 5 minutes to avoid hitting
    the server on every page load.
    """
    server_id_arg = (request.args.get("server_id") or "").strip()
    if server_id_arg:
        libs, err, status = _libraries_for_configured_server(server_id_arg)
        if err:
            return jsonify({"error": err, "libraries": []}), status
        return jsonify({"libraries": libs})

    try:
        import requests as req_lib

        from ..settings_manager import get_settings_manager

        settings = get_settings_manager()

        plex_url = request.args.get("url")
        plex_token = request.args.get("token")
        verify_ssl = _param_to_bool(request.args.get("verify_ssl"), settings.plex_verify_ssl)
        # No explicit overrides → aggregate across every configured server
        # (Plex + Emby + Jellyfin). The dashboard and Start-Job modal both
        # call /api/libraries with no params and expect the full list. The
        # old "Plex-only when Plex is configured" path silently dropped
        # Emby/Jellyfin libraries.
        #
        # The legacy single-Plex install (``plex_url``/``plex_token`` set
        # but ``media_servers`` empty) falls through to the Plex-only
        # branch below so existing behaviour is preserved.
        if not plex_url and not plex_token:
            raw_servers = settings.get("media_servers") or []
            if isinstance(raw_servers, list) and raw_servers:
                return jsonify({"libraries": _libraries_for_all_configured_servers()})
            if not settings.plex_url:
                return jsonify({"libraries": _libraries_for_all_configured_servers()})

        # Track whether explicit overrides were provided (setup wizard)
        has_overrides = bool(plex_url or plex_token)

        if not plex_url or not plex_token:
            plex_url = plex_url or settings.plex_url
            plex_token = plex_token or settings.plex_token

        if not plex_url or not plex_token:
            try:
                from ...config import get_cached_config
                from ...plex_client import plex_server

                config = get_cached_config()
                if config is None:
                    return jsonify(
                        {
                            "error": "Plex not configured. Complete setup in Settings.",
                            "libraries": [],
                        }
                    ), 400

                plex = plex_server(config)

                # Tag rows with server_id when the Plex entry exists in
                # media_servers (it almost always does post-migration); falls
                # back to None for the rare legacy-globals-only install.
                plex_entry = next(
                    (
                        e
                        for e in (settings.get("media_servers") or [])
                        if isinstance(e, dict) and (e.get("type") or "").lower() == "plex" and e.get("enabled", True)
                    ),
                    None,
                )
                plex_sid = (plex_entry or {}).get("id") or None
                plex_sname = (plex_entry or {}).get("name") or None

                libraries = []
                for section in plex.library.sections():
                    if section.type in ("movie", "show"):
                        agent = getattr(section, "agent", "") or ""
                        libraries.append(
                            {
                                "id": str(section.key),
                                "name": section.title,
                                "type": section.type,
                                "agent": agent,
                                "display_type": classify_library_type(section.type, agent),
                                "server_id": plex_sid,
                                "server_name": plex_sname,
                                "server_type": "plex",
                            }
                        )

                return jsonify({"libraries": libraries})
            except Exception:
                logger.exception(
                    "Could not load Plex libraries using the saved configuration. "
                    "The library picker will show 'Plex not configured. Complete setup in Settings.' "
                    "Verify the Plex URL and token in Settings, and that Plex is reachable from this app."
                )
                return jsonify(
                    {
                        "error": "Plex not configured. Complete setup in Settings.",
                        "libraries": [],
                    }
                ), 400

        # Use cached result when loading with saved credentials (not
        # during setup wizard where explicit overrides are provided).
        if not has_overrides:
            with _library_cache_lock:
                cached = _library_cache["result"]
                age = time.monotonic() - _library_cache["fetched_at"]
            if cached is not None and age < _LIBRARY_CACHE_TTL:
                return jsonify({"libraries": cached})

        libraries = _fetch_libraries_via_http(
            plex_url,
            plex_token,
            verify_ssl=verify_ssl,
        )

        if not has_overrides:
            with _library_cache_lock:
                _library_cache["result"] = libraries
                _library_cache["fetched_at"] = time.monotonic()

        return jsonify({"libraries": libraries})

    except req_lib.ConnectionError:
        detail = f"Could not connect to Plex at {plex_url}"
        logger.error(
            "Plex libraries: could not connect to Plex at {} (network unreachable / refused). "
            "The library picker will fail until Plex is reachable. "
            "Verify the URL is correct and that Plex is running and reachable from this app.",
            plex_url,
        )
        return jsonify(
            {
                "error": f"{detail}. Check the server URL and ensure Plex is running and reachable from this host.",
                "libraries": [],
            }
        ), 502
    except req_lib.Timeout:
        detail = f"Connection to Plex at {plex_url} timed out"
        logger.error(
            "Plex libraries: connection to Plex at {} timed out. "
            "The library picker will fail until Plex responds. "
            "Plex may be overloaded or unreachable — try again in a minute.",
            plex_url,
        )
        return jsonify(
            {
                "error": f"{detail}. The server may be overloaded or unreachable.",
                "libraries": [],
            }
        ), 504
    except req_lib.HTTPError as e:
        status = e.response.status_code if e.response is not None else "unknown"
        if status == 401:
            detail = "Plex rejected the authentication token"
            hint = "Re-authenticate with Plex or check your token."
        elif status == 403:
            detail = "Access denied by Plex server"
            hint = "Ensure your account has access to this server."
        else:
            detail = f"Plex returned HTTP {status}"
            hint = "Check Plex server logs for details."
        logger.error(
            "Plex libraries: Plex returned HTTP {} — {}. The library picker will fail until this is resolved. {}",
            status,
            detail,
            hint,
        )
        return jsonify({"error": f"{detail}. {hint}", "libraries": []}), 502
    except Exception as e:
        logger.exception(
            "Plex libraries: could not retrieve the library list. "
            "The library picker will show an error until this is fixed. "
            "The traceback above identifies the cause; verify the Plex URL/token in Settings "
            "and that Plex is reachable."
        )
        return jsonify({"error": f"Failed to retrieve libraries: {e}", "libraries": []}), 500
