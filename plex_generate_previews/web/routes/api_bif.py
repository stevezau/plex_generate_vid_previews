"""BIF viewer API routes for troubleshooting thumbnail quality."""

import os
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import urllib3
from flask import Response, jsonify, request
from loguru import logger

from ..auth import api_token_required
from . import api
from ._helpers import limiter

_MAX_SEARCH_RESULTS = 15


def _get_plex_config_folder() -> str:
    """Return the Plex config folder from user settings."""
    from ..settings_manager import get_settings_manager

    return get_settings_manager().plex_config_folder or "/plex"


def _validate_bif_path(user_path: str) -> str | None:
    """Validate a user-provided BIF path without resolving symlinks.

    Uses ``os.path.normpath`` (not ``realpath``) so Docker bind-mounts
    and Plex symlink trees are preserved.  Path-traversal is blocked by
    verifying no ``..`` segments remain after normalisation and that the
    result starts with the configured Plex config folder.

    Returns the normalised absolute path when valid, or ``None``.
    """
    if not user_path or "\x00" in user_path:
        logger.debug("BIF path rejected: empty or null bytes")
        return None

    cleaned = user_path.strip()
    if not cleaned.endswith(".bif"):
        logger.debug(f"BIF path rejected: does not end with .bif: {cleaned}")
        return None

    normalized = os.path.normpath(cleaned)

    if ".." in normalized.split(os.sep):
        logger.debug(f"BIF path rejected: contains traversal: {normalized}")
        return None

    allowed_root = os.path.normpath(_get_plex_config_folder())
    if not (normalized == allowed_root or normalized.startswith(allowed_root + os.sep)):
        logger.debug(
            f"BIF path rejected: not under plex config folder "
            f"(path={normalized}, root={allowed_root})"
        )
        return None

    if not os.path.isfile(normalized):
        logger.debug(f"BIF path rejected: file not found: {normalized}")
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


@api.route("/bif/search")
@api_token_required
@limiter.limit("10 per minute")
def bif_search():
    """Search Plex for media items and return BIF availability.

    Query params:
        q: Title search string (min 2 characters).

    Returns:
        JSON list of matching media items with BIF path and status.
    """
    import requests as req

    query = (request.args.get("q") or "").strip()
    if not query or len(query) < 2:
        return jsonify(
            {"error": "Query must be at least 2 characters", "results": []}
        ), 400

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

    try:
        resp = req.get(
            f"{plex_url.rstrip('/')}/hubs/search",
            headers={"X-Plex-Token": plex_token, "Accept": "application/json"},
            params={
                "query": query,
                "includeCollections": "0",
                "includeExternalMedia": "0",
            },
            timeout=10,
            verify=verify_ssl,
        )
        resp.raise_for_status()
        hubs = resp.json().get("MediaContainer", {}).get("Hub", [])
    except req.RequestException as e:
        logger.error(f"BIF viewer: Plex search failed: {e}")
        return jsonify({"error": f"Plex search failed: {e}", "results": []}), 502

    results = []
    seen_keys: set[str] = set()

    for hub in hubs:
        if hub.get("type") not in ("movie", "episode"):
            continue
        for item in hub.get("Metadata", []):
            if len(results) >= _MAX_SEARCH_RESULTS:
                break

            item_key = item.get("key", "")
            if not item_key or item_key in seen_keys:
                continue
            seen_keys.add(item_key)

            item_type = item.get("type", "")
            title = item.get("title", "Unknown")
            year = item.get("year", "")

            if item_type == "episode":
                show = item.get("grandparentTitle", "")
                season = item.get("parentIndex")
                episode = item.get("index")
                if show and season is not None and episode is not None:
                    display_title = (
                        f"{show} S{int(season):02d}E{int(episode):02d} - {title}"
                    )
                else:
                    display_title = title
            else:
                display_title = f"{title} ({year})" if year else title

            media_file = ""
            for media in item.get("Media", []):
                for part in media.get("Part", []):
                    media_file = part.get("file", "")
                    if media_file:
                        break
                if media_file:
                    break

            # Resolve bundle hash via Plex /tree endpoint
            bif_path = ""
            bif_exists = False
            bif_info: dict = {}

            try:
                tree_resp = req.get(
                    f"{plex_url.rstrip('/')}{item_key}/tree",
                    headers={"X-Plex-Token": plex_token, "Accept": "application/xml"},
                    timeout=10,
                    verify=verify_ssl,
                )
                tree_resp.raise_for_status()
                tree_data = ET.fromstring(tree_resp.content)

                for media_part in tree_data.findall(".//MediaPart"):
                    bundle_hash = media_part.attrib.get("hash", "")
                    if not bundle_hash or len(bundle_hash) < 2:
                        continue
                    bif_path = _bif_path_for_hash(bundle_hash, plex_config)
                    bif_exists = os.path.isfile(bif_path)
                    if bif_exists:
                        try:
                            stat = os.stat(bif_path)
                            bif_info = {
                                "file_size": stat.st_size,
                                "created_at": datetime.fromtimestamp(
                                    stat.st_mtime, tz=timezone.utc
                                ).isoformat(),
                            }
                        except OSError:
                            pass
                    break  # first media part is enough
            except req.RequestException as e:
                logger.debug(f"BIF viewer: Failed to get tree for {item_key}: {e}")

            results.append(
                {
                    "title": display_title,
                    "type": item_type,
                    "year": year,
                    "media_file": media_file,
                    "bif_path": bif_path,
                    "bif_exists": bif_exists,
                    **bif_info,
                }
            )

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
            "created_at": datetime.fromtimestamp(
                meta.created_at, tz=timezone.utc
            ).isoformat(),
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
