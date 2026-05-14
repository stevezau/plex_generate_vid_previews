"""BIF viewer API routes for troubleshooting thumbnail quality."""

import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import urllib3
from flask import Response, jsonify, request
from loguru import logger

from ..auth import api_token_required
from . import api
from ._helpers import limiter

_MAX_SEARCH_RESULTS = 15

_SEASON_EP_RE = re.compile(r"\bS(\d{1,2})(?:E(\d{1,3}))?\b", re.IGNORECASE)


def _get_plex_config_folder() -> str:
    """Return the Plex config folder from user settings."""
    from ..settings_manager import get_settings_manager

    return get_settings_manager().plex_config_folder or "/plex"


def _allowed_bif_roots() -> list[str]:
    """Every directory the BIF viewer is allowed to read .bif files from.

    Multi-server installs put BIFs in two distinct places:

    * Plex bundle BIFs under ``plex_config_folder/Media/localhost/...``.
    * Emby sidecar BIFs next to the source media file (``<title>-320-5.bif``)
      — these live under each Emby/Jellyfin server's per-library
      ``remote_paths``, translated to local-filesystem paths via the
      server's ``path_mappings``.

    Returns a list of normalised absolute roots; the validator accepts
    any path that lives under at least one of them.
    """
    from ..settings_manager import get_settings_manager

    roots: set[str] = set()
    plex_root = (get_settings_manager().plex_config_folder or "").strip()
    if plex_root:
        roots.add(os.path.normpath(plex_root))

    try:
        for entry in get_settings_manager().get("media_servers") or []:
            if not isinstance(entry, dict) or entry.get("enabled") is False:
                continue
            mappings = entry.get("path_mappings") or []
            local_prefixes = {
                str(m.get("local_prefix") or "").strip()
                for m in mappings
                if isinstance(m, dict) and m.get("local_prefix")
            }
            for lib in entry.get("libraries") or []:
                if not isinstance(lib, dict) or lib.get("enabled") is False:
                    continue
                for remote in lib.get("remote_paths") or []:
                    remote = str(remote or "").strip()
                    if not remote:
                        continue
                    # Best-effort: translate via the server's path_mappings
                    # if any prefix matches; otherwise accept the remote
                    # path as-is (covers same-host installs).
                    translated = remote
                    for m in mappings:
                        if not isinstance(m, dict):
                            continue
                        # Accept either modern ``remote_prefix`` or
                        # legacy ``plex_prefix`` — ownership.py:80 does
                        # the same. Without the fallback, a Plex entry
                        # written by the legacy JS form would not get
                        # its library remote path translated, and the
                        # generated BIFs under that library would be
                        # rejected by the allow-list.
                        rp = str(m.get("remote_prefix") or m.get("plex_prefix") or "").strip()
                        lp = str(m.get("local_prefix") or "").strip()
                        if rp and lp and (remote == rp or remote.startswith(rp.rstrip("/") + "/")):
                            translated = lp.rstrip("/") + remote[len(rp.rstrip("/")) :]
                            break
                    roots.add(os.path.normpath(translated))
                    # Also add bare local prefixes so anything under them
                    # (not just the specific library subdir) is reachable.
                    for lp in local_prefixes:
                        roots.add(os.path.normpath(lp))
    except Exception as exc:
        logger.debug("BIF viewer: failed to enumerate media_server roots: {}", exc)

    return [r for r in roots if r and r != "."]


def _validate_bif_path(user_path: str) -> str | None:
    """Validate a user-provided BIF path without resolving symlinks.

    Uses ``os.path.normpath`` (not ``realpath``) so Docker bind-mounts
    and Plex symlink trees are preserved.  Path-traversal is blocked by
    verifying no ``..`` segments remain after normalisation and that the
    result lives under one of the allow-listed roots — the Plex config
    folder OR any configured media server's library local paths (so
    Emby sidecar BIFs next to media are inspectable too).

    Returns the normalised absolute path when valid, or ``None``.
    """
    if not user_path or "\x00" in user_path:
        logger.debug("BIF path rejected: empty or null bytes")
        return None

    cleaned = user_path.strip()
    if not cleaned.endswith(".bif"):
        logger.debug("BIF path rejected: does not end with .bif: {}", cleaned)
        return None

    normalized = os.path.normpath(cleaned)

    if ".." in normalized.split(os.sep):
        logger.debug("BIF path rejected: contains traversal: {}", normalized)
        return None

    allowed_roots = _allowed_bif_roots()
    if not any(normalized == root or normalized.startswith(root + os.sep) for root in allowed_roots):
        logger.debug(
            "BIF path rejected: not under any allowed root (path={}, roots={})",
            normalized,
            allowed_roots,
        )
        return None

    if not os.path.isfile(normalized):
        logger.debug("BIF path rejected: file not found: {}", normalized)
        return None

    return normalized


def _bif_path_for_hash(bundle_hash: str, plex_config_folder: str) -> str:
    """Build the expected BIF file path from a Plex bundle hash."""
    bundle_file = f"{bundle_hash[0]}/{bundle_hash[1:]}.bundle"
    return os.path.join(
        plex_config_folder,
        "Media",
        "localhost",
        bundle_file,
        "Contents",
        "Indexes",
        "index-sd.bif",
    )


def _parse_season_episode(query: str) -> tuple[str, int | None, int | None]:
    """Extract show name, season, and optional episode from a query.

    Parses patterns like ``S01E02``, ``S1E3``, or ``S02`` from the query.

    Args:
        query: Raw search string.

    Returns:
        Tuple of (base_query, season, episode).  When no pattern is found
        returns ``(query, None, None)``.
    """
    match = _SEASON_EP_RE.search(query)
    if not match:
        return query, None, None
    base = query[: match.start()].strip()
    season = int(match.group(1))
    episode = int(match.group(2)) if match.group(2) else None
    return base or query, season, episode


def _resolve_bif_for_item(
    item_key: str,
    plex_url: str,
    plex_token: str,
    plex_config: str,
    verify_ssl: bool,
    http_mod,
) -> tuple[str, bool, dict]:
    """Resolve the BIF path and existence for a single Plex media item.

    Args:
        item_key: Plex metadata key (e.g. ``/library/metadata/12345``).
        plex_url: Base Plex server URL.
        plex_token: Plex authentication token.
        plex_config: Plex config folder path.
        verify_ssl: Whether to verify SSL certificates.
        http_mod: The ``requests`` module (avoids top-level import).

    Returns:
        Tuple of (bif_path, bif_exists, bif_info_dict).

        Returns ``("", False, {})`` on ANY failure path — network error,
        malformed XML, missing ``hash`` attribute, or filesystem error
        on the bundle path. This shape is intentional: the caller can't
        distinguish "Plex hasn't analyzed this item yet" from "the
        request failed" from the return value alone, but every branch
        leaves the user in the same state (no preview to load) so the
        downstream UX is the same. Failures are logged at DEBUG so
        they're visible when troubleshooting.

        Critical for the per-search-result loop in
        ``_resolve_previews_for_item``: a single malformed ``/tree``
        response must not poison the whole search (~15 invocations per
        search). Pre-fix only ``RequestException`` was caught, so a
        single ``ET.ParseError`` or ``OSError`` would propagate out
        and the search route's top-level catch-all would turn the
        whole search into the empty-fallback path.
    """
    bif_path = ""
    bif_exists = False
    bif_info: dict = {}

    try:
        tree_resp = http_mod.get(
            f"{plex_url.rstrip('/')}{item_key}/tree",
            headers={"X-Plex-Token": plex_token, "Accept": "application/xml"},
            timeout=10,
            verify=verify_ssl,
        )
        tree_resp.raise_for_status()
        # XML content comes from the user's own Plex server authenticated with
        # their own token — trusted origin, not untrusted upload.
        tree_data = ET.fromstring(tree_resp.content)  # nosec B314

        for media_part in tree_data.findall(".//MediaPart"):
            bundle_hash = media_part.attrib.get("hash", "")
            if not bundle_hash or len(bundle_hash) < 2:
                continue
            bif_path = _bif_path_for_hash(bundle_hash, plex_config)
            try:
                bif_exists = os.path.isfile(bif_path)
            except OSError:
                bif_exists = False
            if bif_exists:
                try:
                    stat = os.stat(bif_path)
                    bif_info = {
                        "file_size": stat.st_size,
                        "created_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
                    }
                except OSError:
                    pass
            break
    except http_mod.RequestException as e:
        logger.debug("BIF viewer: Failed to get tree for {}: {}", item_key, e)
    except ET.ParseError as e:
        # Malformed XML from Plex — observed in the wild as truncated /tree
        # bodies under heavy library-scan load. Without this catch the
        # per-item loop in _resolve_previews_for_item would 500 the whole
        # search instead of degrading just the one offending row.
        logger.debug("BIF viewer: Plex /tree returned malformed XML for {}: {}", item_key, e)
    except OSError as e:
        # Filesystem failure on the bundle path (permission, mount loss,
        # NFS hiccup). Treated like an unanalysed item rather than a
        # search-killing exception.
        logger.debug("BIF viewer: filesystem error resolving BIF for {}: {}", item_key, e)

    return bif_path, bif_exists, bif_info


def _resolve_bif_for_item_all_parts(
    item_key: str,
    plex_url: str,
    plex_token: str,
    plex_config: str,
    verify_ssl: bool,
    http_mod,
) -> list[dict]:
    """Resolve every MediaPart's BIF for a Plex item (multi-version aware).

    Same fetch-and-parse contract as ``_resolve_bif_for_item`` but emits
    one entry per ``MediaPart`` instead of breaking after the first.
    Each entry is a dict with ``part_file`` (the file path Plex reports
    for that part, used to drive per-version path mapping in the caller),
    ``bif_path``, ``bif_exists`` and optional ``file_size`` / ``created_at``.

    Returns ``[]`` on any caught failure (network, ParseError, OSError),
    same swallow-and-degrade behaviour as the single-part helper — see
    its docstring for why. The multi-version reporter on issue #231 hit
    the "only one row" symptom because the original helper used ``break``;
    this function does not, and Preview Inspector fans the results into
    one search row per part.
    """
    parts: list[dict] = []

    try:
        tree_resp = http_mod.get(
            f"{plex_url.rstrip('/')}{item_key}/tree",
            headers={"X-Plex-Token": plex_token, "Accept": "application/xml"},
            timeout=10,
            verify=verify_ssl,
        )
        tree_resp.raise_for_status()
        # XML content comes from the user's own Plex server authenticated with
        # their own token — trusted origin, not untrusted upload.
        tree_data = ET.fromstring(tree_resp.content)  # nosec B314

        for media_part in tree_data.findall(".//MediaPart"):
            bundle_hash = media_part.attrib.get("hash", "")
            if not bundle_hash or len(bundle_hash) < 2:
                continue
            part_file = media_part.attrib.get("file", "")
            bif_path = _bif_path_for_hash(bundle_hash, plex_config)
            try:
                bif_exists = os.path.isfile(bif_path)
            except OSError:
                bif_exists = False
            entry: dict = {
                "part_file": part_file,
                "bif_path": bif_path,
                "bif_exists": bif_exists,
            }
            if bif_exists:
                try:
                    stat = os.stat(bif_path)
                    entry["file_size"] = stat.st_size
                    entry["created_at"] = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()
                except OSError:
                    pass
            parts.append(entry)
    except http_mod.RequestException as e:
        logger.debug("BIF viewer: Failed to get tree for {}: {}", item_key, e)
    except ET.ParseError as e:
        logger.debug("BIF viewer: Plex /tree returned malformed XML for {}: {}", item_key, e)
    except OSError as e:
        logger.debug("BIF viewer: filesystem error resolving BIF for {}: {}", item_key, e)

    return parts


def _build_display_title(item: dict) -> str:
    """Build a human-readable display title from a Plex metadata item.

    Args:
        item: Plex metadata dict (from JSON API).

    Returns:
        Formatted title string.
    """
    item_type = item.get("type", "")
    title = item.get("title", "Unknown")
    year = item.get("year", "")

    if item_type == "episode":
        show = item.get("grandparentTitle", "")
        season = item.get("parentIndex")
        episode = item.get("index")
        if show and season is not None and episode is not None:
            return f"{show} S{int(season):02d}E{int(episode):02d} - {title}"
        return title

    return f"{title} ({year})" if year else title


def _extract_media_file(item: dict) -> str:
    """Return the first media file path from a Plex metadata item."""
    for media in item.get("Media", []):
        for part in media.get("Part", []):
            path = part.get("file", "")
            if path:
                return path
    return ""


def _fetch_show_episodes(
    plex_url: str,
    plex_token: str,
    show_key: str,
    verify_ssl: bool,
    http_mod,
    season_filter: int | None = None,
    episode_filter: int | None = None,
) -> list[dict]:
    """Fetch episodes from a Plex show, optionally filtered by season/episode.

    Args:
        plex_url: Base Plex server URL.
        plex_token: Plex authentication token.
        show_key: Plex metadata key for the show.
        verify_ssl: Whether to verify SSL certificates.
        http_mod: The ``requests`` module.
        season_filter: If set, only return episodes from this season.
        episode_filter: If set, only return the episode with this number.

    Returns:
        List of Plex episode metadata dicts.
    """
    resp = http_mod.get(
        f"{plex_url.rstrip('/')}{show_key}/allLeaves",
        headers={"X-Plex-Token": plex_token, "Accept": "application/json"},
        timeout=10,
        verify=verify_ssl,
    )
    resp.raise_for_status()
    episodes = resp.json().get("MediaContainer", {}).get("Metadata", [])

    if season_filter is not None:
        episodes = [e for e in episodes if e.get("parentIndex") == season_filter]
    if episode_filter is not None:
        episodes = [e for e in episodes if e.get("index") == episode_filter]

    return episodes


def _item_to_result(
    item: dict,
    plex_url: str,
    plex_token: str,
    plex_config: str,
    verify_ssl: bool,
    http_mod,
) -> dict:
    """Convert a Plex metadata item dict into a BIF search result dict."""
    item_key = item.get("key", "")
    bif_path, bif_exists, bif_info = _resolve_bif_for_item(
        item_key, plex_url, plex_token, plex_config, verify_ssl, http_mod
    )
    return {
        "title": _build_display_title(item),
        "type": item.get("type", ""),
        "year": item.get("year", ""),
        "media_file": _extract_media_file(item),
        "bif_path": bif_path,
        "bif_exists": bif_exists,
        **bif_info,
    }


@api.route("/bif/search")
@api_token_required
@limiter.limit("10 per minute")
def bif_search():
    """Search Plex for media items and return BIF availability.

    Supports plain title queries (``Inception``) as well as season/episode
    patterns (``Rooster Fighter S01E02``, ``Breaking Bad S03``).  When a
    pattern is detected the show name is searched and its episodes are
    fetched via ``/allLeaves`` with appropriate season/episode filters.

    Query params:
        q: Title search string (min 2 characters).

    Returns:
        JSON list of matching media items with BIF path and status.
    """
    import requests as req

    query = (request.args.get("q") or "").strip()
    if not query or len(query) < 2:
        return jsonify({"error": "Query must be at least 2 characters", "results": []}), 400

    from ..settings_manager import get_settings_manager

    settings = get_settings_manager()
    plex_url = settings.plex_url
    plex_token = settings.plex_token
    plex_config = settings.plex_config_folder or "/plex"
    verify_ssl = settings.plex_verify_ssl

    if not plex_url or not plex_token:
        return jsonify({"error": "Plex not configured", "results": []}), 400

    if not verify_ssl:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    base_query, season_filter, episode_filter = _parse_season_episode(query)

    try:
        resp = req.get(
            f"{plex_url.rstrip('/')}/hubs/search",
            headers={"X-Plex-Token": plex_token, "Accept": "application/json"},
            params={
                "query": base_query,
                "includeCollections": "0",
                "includeExternalMedia": "0",
            },
            timeout=10,
            verify=verify_ssl,
        )
        resp.raise_for_status()
        hubs = resp.json().get("MediaContainer", {}).get("Hub", [])
    except req.RequestException as e:
        logger.error(
            "BIF Viewer: Plex search request failed ({}: {}). "
            "The viewer's search box can't return results until this is resolved — "
            "verify the Plex URL and token in Settings, and that Plex is reachable from this app.",
            type(e).__name__,
            e,
        )
        return jsonify({"error": f"Plex search failed: {e}", "results": []}), 502

    results: list[dict] = []
    seen_keys: set[str] = set()

    def _add_item(item: dict) -> bool:
        """Resolve and append one item if not already seen. Returns False when full."""
        if len(results) >= _MAX_SEARCH_RESULTS:
            return False
        item_key = item.get("key", "")
        if not item_key or item_key in seen_keys:
            return True
        seen_keys.add(item_key)
        results.append(_item_to_result(item, plex_url, plex_token, plex_config, verify_ssl, req))
        return True

    # --- Phase 1: expand "show" hubs into individual episodes -----------
    for hub in hubs:
        if hub.get("type") != "show":
            continue
        for show_item in hub.get("Metadata", []):
            if len(results) >= _MAX_SEARCH_RESULTS:
                break
            rating_key = show_item.get("ratingKey", "")
            if not rating_key:
                continue
            show_key = f"/library/metadata/{rating_key}"
            try:
                episodes = _fetch_show_episodes(
                    plex_url,
                    plex_token,
                    show_key,
                    verify_ssl,
                    req,
                    season_filter=season_filter,
                    episode_filter=episode_filter,
                )
                for ep in episodes:
                    if not _add_item(ep):
                        break
            except req.RequestException as e:
                logger.debug("BIF viewer: Failed to fetch episodes for {}: {}", show_key, e)

    # --- Phase 2: direct movie/episode hub hits (no SxxExx filter) ------
    if season_filter is None:
        for hub in hubs:
            if hub.get("type") not in ("movie", "episode"):
                continue
            for item in hub.get("Metadata", []):
                if not _add_item(item):
                    break

    return jsonify({"results": results})


@api.route("/bif/info")
@api_token_required
def bif_info():
    """Return detailed BIF metadata for troubleshooting.

    Query params:
        path: Absolute path to a .bif file.
    """
    from ...bif_reader import read_bif_metadata

    path = request.args.get("path", "")
    resolved = _validate_bif_path(path)
    if resolved is None:
        return jsonify({"error": "Invalid or missing BIF file path"}), 400

    try:
        meta = read_bif_metadata(resolved)
    except (ValueError, OSError) as e:
        return jsonify({"error": f"Failed to read BIF: {e}"}), 400

    sizes = meta.frame_sizes
    avg_size = sum(sizes) / len(sizes) if sizes else 0
    min_size = min(sizes) if sizes else 0
    max_size = max(sizes) if sizes else 0

    # Frames under 500 bytes are likely blank or corrupt
    suspect_indices = [i for i, s in enumerate(sizes) if s < 500]

    return jsonify(
        {
            "path": meta.path,
            "version": meta.version,
            "frame_count": meta.frame_count,
            "frame_interval_ms": meta.frame_interval_ms,
            "file_size": meta.file_size,
            "created_at": datetime.fromtimestamp(meta.created_at, tz=timezone.utc).isoformat(),
            "avg_frame_size": round(avg_size),
            "min_frame_size": min_size,
            "max_frame_size": max_size,
            "suspect_frame_count": len(suspect_indices),
            "suspect_frame_indices": suspect_indices[:50],
        }
    )


@api.route("/bif/frame")
@api_token_required
def bif_frame():
    """Serve a single JPEG frame extracted from a BIF file.

    Query params:
        path: Absolute path to a .bif file.
        index: Zero-based frame index.
    """
    from ...bif_reader import read_bif_frame

    path = request.args.get("path", "")
    resolved = _validate_bif_path(path)
    if resolved is None:
        return jsonify({"error": "Invalid or missing BIF file path"}), 400

    try:
        index = int(request.args.get("index", "0"))
    except (ValueError, TypeError):
        return jsonify({"error": "Invalid frame index"}), 400

    try:
        jpeg_data = read_bif_frame(resolved, index)
    except (IndexError, ValueError, OSError) as e:
        return jsonify({"error": str(e)}), 400

    return Response(
        jpeg_data,
        mimetype="image/jpeg",
        headers={
            "Cache-Control": "public, max-age=3600",
            "Content-Length": str(len(jpeg_data)),
        },
    )


# ============================================================================
# Multi-server preview routes — used by the new server-aware BIF Viewer.
# These work for all three vendors:
#
#   * Plex  → existing bundle BIFs at the hash-keyed plex_config_folder path
#   * Emby  → sidecar BIFs next to the source media
#   * Jellyfin → tile-grid trickplay JPGs + manifest.json next to the source
#
# Each endpoint takes a ``server_id`` query param, looks the server up in the
# registry, and dispatches per vendor. Output paths are validated against the
# server's own media roots (Plex: plex_config_folder; Emby/Jellyfin: media
# directory derived from path_mappings).
# ============================================================================


def _validate_path_under_any_server(file_path: str, allowed_roots: list[str]) -> str | None:
    """Validate ``file_path`` exists under one of ``allowed_roots``.

    Multi-server analogue of ``_validate_bif_path`` that accepts any of
    several roots (Plex bundle dir, Emby/Jellyfin media folders).
    Accepts either a file (Emby BIF sidecar, Plex bundle BIF) OR a
    directory (Jellyfin's saved-with-media trickplay sheet folder
    ``<basename>.trickplay/<width> - <tileW>x<tileH>``). The caller is
    responsible for re-checking the kind it actually expected.

    Returns the normalised path or ``None``.
    """
    if not file_path or "\x00" in file_path:
        return None
    normalized = os.path.normpath(file_path.strip())
    if ".." in normalized.split(os.sep):
        return None
    for root in allowed_roots:
        root_n = os.path.normpath(root)
        if not root_n:
            continue
        if normalized == root_n or normalized.startswith(root_n + os.sep):
            if os.path.isfile(normalized) or os.path.isdir(normalized):
                return normalized
    return None


def _allowed_roots_for_server(server_cfg) -> list[str]:
    """Return on-disk roots the BIF viewer is allowed to read for a server.

    For Plex: the configured plex_config_folder (where bundle BIFs live).
    For Emby/Jellyfin: every local_prefix from the server's path_mappings
    (where sidecar BIFs / trickplay tiles live).
    """
    from ...servers.base import ServerType

    if server_cfg is None:
        return []
    roots: list[str] = []
    if server_cfg.type is ServerType.PLEX:
        plex_cfg_folder = (server_cfg.output or {}).get("plex_config_folder") or ""
        if plex_cfg_folder:
            roots.append(plex_cfg_folder)
    else:
        for mapping in server_cfg.path_mappings or []:
            local = (mapping.get("local_prefix") or "").strip()
            if local:
                roots.append(local)
    return roots


@api.route("/bif/servers/<server_id>/search")
@api_token_required
@limiter.limit("10 per minute")
def bif_servers_search(server_id: str):
    """Search a configured media server for items + report preview availability.

    Per-vendor behaviour:

    * **Plex**: ``/hubs/search`` → resolve bundle hash → check sidecar BIF.
    * **Emby/Jellyfin**: ``/Items?searchTerm=`` → check format-specific
      sidecar (``.bif`` for Emby, ``trickplay/<basename>-<width>.json``
      for Jellyfin).

    Query params:
        q: Search string (min 2 chars).

    Returns:
        ``{"server_id", "server_type", "results": [{title, type, year,
        media_file, preview_path, preview_kind, preview_exists}]}``
        where ``preview_kind`` is one of ``"bif"`` or ``"trickplay"``.
    """
    from ...servers import ServerRegistry
    from ..settings_manager import get_settings_manager

    query = (request.args.get("q") or "").strip()
    if len(query) < 2:
        return jsonify({"error": "Query must be at least 2 characters", "results": []}), 400

    settings = get_settings_manager()
    raw_servers = settings.get("media_servers") or []
    if not isinstance(raw_servers, list):
        raw_servers = []

    target = next((s for s in raw_servers if isinstance(s, dict) and s.get("id") == server_id), None)
    if target is None:
        return jsonify({"error": f"server {server_id!r} not found", "results": []}), 404

    registry = ServerRegistry.from_settings(raw_servers, legacy_config=None)
    server = registry.get(server_id)
    server_cfg = registry.get_config(server_id)
    if server is None or server_cfg is None:
        return jsonify({"error": "server not instantiable", "results": []}), 500

    results: list[dict] = []
    try:
        # Use the vendor's native search API via MediaServer.search_items.
        # Pre-fix this loop walked every library and every item client-side
        # (D4 — 13.6s for a single-word query against a 119k-item Plex
        # install). Each vendor adapter overrides search_items to use its
        # own server-side index — Plex /hubs/search via library.search(),
        # Emby/Jellyfin /Items?searchTerm=…
        #
        # The vendor search API doesn't know which libraries the user
        # has disabled in this app's settings, so post-filter the hits
        # to drop results from libraries the user explicitly turned off.
        #
        # Filter strategy: REMOTE_PATH prefix first, then library_id as
        # a fallback. Pre-fix this matched only on item.library_id, but
        # Emby/Jellyfin search responses carry the season/show ParentId
        # (not the library's VirtualFolder ItemId) on episode rows, so
        # the filter dropped every legitimate episode result. Path-
        # prefix matching against each library's ``remote_paths`` is
        # what ingestion uses for the same purpose and works for every
        # vendor whose libraries have remote_paths configured.
        #
        # Some Plex/Emby/Jellyfin libraries may have an empty
        # ``remote_paths`` (the user hasn't configured local mounts
        # yet). We fall back to library_id matching for those — Plex
        # populates Episode/Movie ``librarySectionID`` natively so the
        # match works there even when the path filter can't.
        disabled_remote_prefixes: list[str] = []
        disabled_lib_ids_without_paths: set[str] = set()
        for lib in server_cfg.libraries or []:
            if getattr(lib, "enabled", True):
                continue
            paths = [p for p in (getattr(lib, "remote_paths", ()) or ()) if str(p or "").strip()]
            if paths:
                for rp in paths:
                    disabled_remote_prefixes.append(str(rp).rstrip("/") + "/")
            else:
                lid = str(getattr(lib, "id", "") or "")
                if lid:
                    disabled_lib_ids_without_paths.add(lid)

        def _is_under_disabled_library(item) -> bool:
            remote_path = item.remote_path or ""
            if remote_path and disabled_remote_prefixes:
                normalised = remote_path.rstrip("/") + "/"
                if any(normalised.startswith(p) for p in disabled_remote_prefixes):
                    return True
            if disabled_lib_ids_without_paths:
                lib_id = str(getattr(item, "library_id", "") or "")
                if lib_id and lib_id in disabled_lib_ids_without_paths:
                    return True
            return False

        items = server.search_items(query, limit=_MAX_SEARCH_RESULTS)
        for item in items:
            if len(results) >= _MAX_SEARCH_RESULTS:
                break
            if _is_under_disabled_library(item):
                continue
            for row in _resolve_previews_for_item(item, server_cfg):
                if len(results) >= _MAX_SEARCH_RESULTS:
                    break
                results.append(row)
    except Exception as exc:
        logger.warning(
            "BIF Viewer: search on media server {!r} failed ({}: {}). "
            "Searches against other configured servers still work. "
            "Verify the server's URL, credentials, and reachability under Settings → Media Servers; "
            "use 'Test Connection' to confirm it's healthy.",
            server_id,
            type(exc).__name__,
            exc,
        )
        return jsonify({"error": f"search failed: {exc}", "results": []}), 502

    return jsonify(
        {
            "server_id": server_id,
            "server_type": server_cfg.type.value,
            "results": results,
        }
    )


def _resolve_previews_for_item(item, server_cfg) -> list[dict]:
    """Compute the on-disk preview rows for a media item.

    Returns a list because a single Plex item can have multiple MediaParts
    (e.g. a 4K + 1080p version of the same movie), each with its own
    bundle hash, BIF path and file. Emby / Jellyfin / unknown branches
    return a one-element list so the caller can iterate uniformly. See
    issue #231: pre-fix Preview Inspector collapsed all versions into the
    single "first MediaPart" row, which made multi-version libraries
    appear broken even when both BIFs existed on disk.
    """
    from ...output.emby_sidecar import EmbyBifAdapter
    from ...output.jellyfin_trickplay import JellyfinTrickplayAdapter
    from ...servers.base import ServerType
    from ...servers.ownership import apply_path_mappings

    # Translate the server-side remote_path into a local canonical path.
    local_candidates = apply_path_mappings(
        item.remote_path or "",
        list(server_cfg.path_mappings or []),
    )
    canonical_local = local_candidates[0] if local_candidates else (item.remote_path or "")

    output_cfg = server_cfg.output or {}
    width = int(output_cfg.get("width") or 320)
    interval = int(output_cfg.get("frame_interval") or 10)

    # Episode-vs-movie classification: the MediaItem dataclass has no
    # explicit ``kind`` field, so fall back to a title-shape check. Match on
    # the actual SxxEyy/SxxEyyy pattern rather than "any S and E in the
    # title", which previously misclassified e.g. "Star Wars Episode IV"
    # as an episode.
    looks_like_episode = bool(_SEASON_EP_RE.search(item.title or ""))
    base = {
        "title": item.title,
        "type": "episode" if looks_like_episode else "movie",
        "year": None,
        "media_file": canonical_local,
        "item_id": item.id,
    }

    if server_cfg.type is ServerType.PLEX:
        # Plex's bundle-hash → BIF path lookup needs a per-item /tree call.
        # Each MediaPart in the response is a distinct version (4K, 1080p,
        # etc.) with its own hash and file. Fanning out into one row per
        # part is what fixes the "only shows one version" half of #231.
        import requests as _req

        plex_url = server_cfg.url or ""
        plex_token = str((server_cfg.auth or {}).get("token") or "")
        plex_config = _get_plex_config_folder()
        parts: list[dict] = []
        try:
            parts = _resolve_bif_for_item_all_parts(
                f"/library/metadata/{item.id}",
                plex_url,
                plex_token,
                plex_config,
                bool(server_cfg.verify_ssl),
                _req,
            )
        except Exception as exc:
            # The helper swallows the documented failure modes; this is
            # the last-resort guard so one row's unknown-unknown can't
            # poison the other ~14 search results.
            logger.debug(
                "BIF viewer: unexpected error resolving Plex BIF for item {}: {}",
                item.id,
                exc,
            )

        if not parts:
            row = dict(base)
            row.update(
                preview_kind="bif",
                preview_path="",
                preview_exists=False,
                note="Plex hasn't analyzed this item yet — no bundle hash returned.",
            )
            return [row]

        rows: list[dict] = []
        mappings = list(server_cfg.path_mappings or [])
        for part in parts:
            part_file = part.get("part_file") or ""
            # Per-part path mapping: each MediaPart has its own remote
            # path (different mount point for 4K vs 1080p, typically),
            # so the mapping must apply per-part. When a part has no
            # file= attribute we emit an empty media_file rather than
            # falling back to the item-level path — otherwise on a
            # multi-part response, the no-file part's row would display
            # a different part's media path while pointing at its own
            # BIF, which is the inverse of the bug this fix targets.
            if part_file:
                local = apply_path_mappings(part_file, mappings)
                row_media = local[0] if local else part_file
            else:
                row_media = ""
            row = dict(base)
            row["media_file"] = row_media
            row["preview_kind"] = "bif"
            row["preview_path"] = part.get("bif_path") or ""
            row["preview_exists"] = bool(part.get("bif_exists"))
            if "file_size" in part:
                row["file_size"] = part["file_size"]
            if "created_at" in part:
                row["created_at"] = part["created_at"]
            rows.append(row)
        return rows
    elif server_cfg.type is ServerType.EMBY:
        bif = EmbyBifAdapter.sidecar_path(canonical_local, width=width, frame_interval=interval)
        base.update(
            preview_kind="bif",
            preview_path=str(bif),
            preview_exists=bif.exists(),
        )
        return [base]
    elif server_cfg.type is ServerType.JELLYFIN:
        # Sheet directory is the on-disk root for Jellyfin's saved-with-media
        # layout (D38). The viewer sends this path back to /trickplay/info,
        # which synthesises the manifest from the directory listing —
        # Jellyfin doesn't write a manifest itself, so we don't either.
        sheet_dir = JellyfinTrickplayAdapter.sheet_dir(canonical_local, width=width)
        base.update(
            preview_kind="trickplay",
            preview_path=str(sheet_dir),
            preview_exists=sheet_dir.is_dir() and any(sheet_dir.glob("*.jpg")),
        )
        return [base]
    else:
        base.update(preview_kind="unknown", preview_path="", preview_exists=False)
        return [base]


@api.route("/bif/trickplay/info")
@api_token_required
def trickplay_info():
    """Return Jellyfin trickplay sheet metadata for the viewer.

    Query params:
        server_id: Configured Jellyfin server id (for path-validation).
        path: Absolute path to the sheet directory
            (``<media_dir>/<basename>.trickplay/<width> - <tileW>x<tileH>``).

    Synthesises ``thumbnail_count`` + ``thumb_width/height`` + per-sheet
    info from the on-disk listing — Jellyfin's 10.10+ saved-with-media
    layout doesn't write a manifest, so neither do we (D38).

    Tile geometry is parsed from the directory name (Jellyfin's own
    convention: ``"<width> - <tileW>x<tileH>"``); the resolution width
    comes from the same string so a single Jellyfin-format directory
    is enough for the viewer to render any frame.
    """
    import re

    from ...servers import ServerRegistry
    from ..settings_manager import get_settings_manager

    server_id = request.args.get("server_id", "")
    path = request.args.get("path", "")

    settings = get_settings_manager()
    raw_servers = settings.get("media_servers") or []
    target = next((s for s in raw_servers if isinstance(s, dict) and s.get("id") == server_id), None)
    if target is None:
        return jsonify({"error": "server not found"}), 404

    registry = ServerRegistry.from_settings(raw_servers, legacy_config=None)
    server_cfg = registry.get_config(server_id)
    if server_cfg is None:
        return jsonify({"error": "server config missing"}), 500
    allowed_roots = _allowed_roots_for_server(server_cfg)
    resolved = _validate_path_under_any_server(path, allowed_roots)
    if resolved is None:
        return jsonify({"error": "Invalid path"}), 400
    if not os.path.isdir(resolved):
        return jsonify({"error": "Sheet directory does not exist"}), 400

    # Parse "<width> - <tileW>x<tileH>" from the directory name (the same
    # convention Jellyfin's TrickplayManager.GetTrickplayDirectory uses).
    dir_name = os.path.basename(resolved.rstrip("/"))
    match = re.match(r"^\s*(\d+)\s*-\s*(\d+)x(\d+)\s*$", dir_name)
    if not match:
        return jsonify({"error": f"Sheet directory name doesn't match '<width> - <tileW>x<tileH>': {dir_name!r}"}), 400
    # match.group(1) is the resolution width — currently unused by the
    # response (the viewer reads thumb_w/thumb_h measured off the sheet)
    # but the regex still has to match it to validate the sub-dir name.
    tile_w = int(match.group(2))
    tile_h = int(match.group(3))
    if tile_w < 1 or tile_h < 1:
        # /trickplay/frame divides pos_in_sheet by frames_per_sheet; guard
        # against a 0 in either dimension producing a ZeroDivisionError.
        return jsonify({"error": f"Invalid tile geometry {tile_w}x{tile_h}"}), 400
    frames_per_sheet = tile_w * tile_h

    sheet_files = sorted(
        (f for f in os.listdir(resolved) if f.endswith(".jpg") and f.split(".")[0].isdigit()),
        key=lambda f: int(f.split(".")[0]),
    )
    sheet_count = len(sheet_files)
    if sheet_count == 0:
        return jsonify({"error": "No tile sheets found in directory"}), 400

    # Measure the LAST sheet to count tiles in it (the last sheet may be
    # partial); all earlier sheets are full at frames_per_sheet.
    last_sheet_path = os.path.join(resolved, sheet_files[-1])
    try:
        from PIL import Image as _Image

        with _Image.open(last_sheet_path) as img:
            sheet_pixel_w, sheet_pixel_h = img.size
    except Exception as exc:
        return jsonify({"error": f"Could not measure last sheet: {exc}"}), 400

    thumb_w = sheet_pixel_w // tile_w if tile_w else 0
    thumb_h = sheet_pixel_h // tile_h if tile_h else 0
    # Last sheet's filled-tile count: scan top-to-bottom for the first
    # row that's all-black (sentinel rows in our packing). Fall back to
    # frames_per_sheet for a fully-packed sheet.
    last_sheet_tiles = frames_per_sheet
    try:
        from PIL import Image as _Image

        with _Image.open(last_sheet_path) as img:
            for i in range(frames_per_sheet - 1, -1, -1):
                row = i // tile_w
                col = i % tile_w
                tile_box = (col * thumb_w, row * thumb_h, (col + 1) * thumb_w, (row + 1) * thumb_h)
                tile = img.crop(tile_box)
                # All-black tile = empty slot.
                if tile.getbbox() is not None:
                    last_sheet_tiles = i + 1
                    break
            else:
                last_sheet_tiles = 0
    except Exception:
        # Fall back to assuming the last sheet is full.
        pass
    thumb_count = (sheet_count - 1) * frames_per_sheet + last_sheet_tiles

    sheets = []
    for n in range(sheet_count):
        sheet_path = os.path.join(resolved, f"{n}.jpg")
        sheets.append(
            {
                "index": n,
                "path": sheet_path,
                "exists": os.path.isfile(sheet_path),
                "size_bytes": os.path.getsize(sheet_path) if os.path.isfile(sheet_path) else 0,
            }
        )

    # The viewer reads frame_interval_ms from this response. We don't
    # know it from disk (Jellyfin tracks it in its DB) so we default to
    # 10000 — matches our generator's default and is overridable by the
    # caller adding ``?interval_ms=`` to the request.
    interval_ms = int(request.args.get("interval_ms") or 10000)

    return jsonify(
        {
            "manifest_path": resolved,  # kept for API compat; this is now the sheet-dir path
            "tile_width": tile_w,
            "tile_height": tile_h,
            "thumb_width": thumb_w,
            "thumb_height": thumb_h,
            "thumbnail_count": thumb_count,
            "interval_ms": interval_ms,
            "frames_per_sheet": frames_per_sheet,
            "sheet_count": sheet_count,
            "sheets_dir": resolved,
            "sheets": sheets,
        }
    )


@api.route("/bif/trickplay/frame")
@api_token_required
def trickplay_frame():
    """Serve a single JPEG slice from a Jellyfin trickplay tile sheet.

    Query params:
        server_id: Configured Jellyfin server id (for path validation).
        sheets_dir: Absolute path to the trickplay sheets directory.
        index: Zero-based frame index across all sheets.
        tile_width: Tiles per row.
        tile_height: Tiles per column.

    Slices the appropriate tile out of the right sheet via Pillow.
    Cheap operation (in-memory crop + JPEG re-encode at quality 85).
    """
    from io import BytesIO

    from PIL import Image

    from ...servers import ServerRegistry
    from ..settings_manager import get_settings_manager

    server_id = request.args.get("server_id", "")
    sheets_dir = request.args.get("sheets_dir", "")
    try:
        index = int(request.args.get("index", "0"))
        tile_w = int(request.args.get("tile_width", "10"))
        tile_h = int(request.args.get("tile_height", "10"))
    except (ValueError, TypeError):
        return jsonify({"error": "Invalid integer parameter"}), 400

    if index < 0 or tile_w < 1 or tile_h < 1:
        return jsonify({"error": "Out-of-range parameter"}), 400

    settings = get_settings_manager()
    raw_servers = settings.get("media_servers") or []
    registry = ServerRegistry.from_settings(raw_servers, legacy_config=None)
    server_cfg = registry.get_config(server_id)
    if server_cfg is None:
        return jsonify({"error": "server not found"}), 404

    allowed_roots = _allowed_roots_for_server(server_cfg)
    sheets_dir_n = os.path.normpath(sheets_dir)
    if ".." in sheets_dir_n.split(os.sep):
        return jsonify({"error": "Invalid sheets_dir"}), 400
    if not any(
        sheets_dir_n == os.path.normpath(r) or sheets_dir_n.startswith(os.path.normpath(r) + os.sep)
        for r in allowed_roots
    ):
        return jsonify({"error": "sheets_dir not under any configured server root"}), 403

    frames_per_sheet = tile_w * tile_h
    sheet_n = index // frames_per_sheet
    pos_in_sheet = index % frames_per_sheet
    row = pos_in_sheet // tile_w
    col = pos_in_sheet % tile_w

    sheet_path = os.path.join(sheets_dir_n, f"{sheet_n}.jpg")
    if not os.path.isfile(sheet_path):
        return jsonify({"error": f"Sheet {sheet_n} missing on disk"}), 404

    try:
        with Image.open(sheet_path) as sheet:
            sheet_w, sheet_h = sheet.size
            thumb_w = sheet_w // tile_w
            thumb_h = sheet_h // tile_h
            box = (col * thumb_w, row * thumb_h, (col + 1) * thumb_w, (row + 1) * thumb_h)
            tile = sheet.crop(box)
            buf = BytesIO()
            tile.save(buf, format="JPEG", quality=85)
            jpeg_bytes = buf.getvalue()
    except (OSError, ValueError) as exc:
        return jsonify({"error": f"Could not slice tile: {exc}"}), 500

    return Response(
        jpeg_bytes,
        mimetype="image/jpeg",
        headers={
            "Cache-Control": "public, max-age=3600",
            "Content-Length": str(len(jpeg_bytes)),
        },
    )
