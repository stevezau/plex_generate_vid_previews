"""System, health, config, and library API routes."""

import os
import threading
import time

import urllib3
from flask import jsonify, request
from loguru import logger

from ..auth import api_token_required, setup_or_auth_required
from ..jobs import get_job_manager
from . import api
from ._helpers import (
    _ensure_gpu_cache,
    _gpu_cache,
    _gpu_cache_lock,
    _param_to_bool,
)


@api.route("/system/rescan-gpus", methods=["POST"])
@setup_or_auth_required
def rescan_gpus():
    """Force GPU re-detection and return updated list."""
    try:
        with _gpu_cache_lock:
            _gpu_cache["result"] = None
        _ensure_gpu_cache()
        with _gpu_cache_lock:
            gpus = _gpu_cache["result"] or []
        return jsonify({"gpus": gpus})
    except Exception as e:
        logger.error(f"Failed to rescan GPUs: {e}")
        return jsonify({"error": "GPU scan failed"}), 500


@api.route("/system/status")
@setup_or_auth_required
def get_system_status():
    """Get system status including GPU info.

    GPU detection runs lazily on first access and is cached for the lifetime
    of the process. Call clear_gpu_cache() to force a re-scan.
    """
    try:
        _ensure_gpu_cache()
        with _gpu_cache_lock:
            gpus = _gpu_cache["result"] or []

        job_manager = get_job_manager()
        running_job = job_manager.get_running_job()

        return jsonify(
            {
                "gpus": gpus,
                "gpu_stats": [],
                "running_job": running_job.to_dict() if running_job else None,
                "pending_jobs": len(job_manager.get_pending_jobs()),
            }
        )
    except Exception as e:
        logger.error(f"Failed to get system status: {e}")
        return jsonify({"error": "Failed to retrieve system status"}), 500


@api.route("/system/config")
@api_token_required
def get_config():
    """Get current configuration."""
    try:
        from ...config import get_cached_config
        from ..settings_manager import get_settings_manager

        config = get_cached_config()
        settings = get_settings_manager()
        if config is None:
            return jsonify(
                {
                    "plex_url": settings.plex_url or "",
                    "plex_token": "****" if settings.plex_token else "",
                    "plex_config_folder": settings.plex_config_folder or "",
                    "plex_verify_ssl": settings.plex_verify_ssl,
                    "config_error": "Configuration incomplete. Complete the setup wizard.",
                    "gpu_config": settings.gpu_config,
                    "gpu_threads": settings.gpu_threads,
                    "cpu_threads": settings.cpu_threads,
                    "cpu_fallback_threads": settings.cpu_fallback_threads,
                    "ffmpeg_threads": settings.get("ffmpeg_threads", 2),
                }
            )

        return jsonify(
            {
                "plex_url": config.plex_url or "",
                "plex_token": "****" if config.plex_token else "",
                "plex_config_folder": config.plex_config_folder or "",
                "plex_verify_ssl": config.plex_verify_ssl,
                "plex_local_videos_path_mapping": config.plex_local_videos_path_mapping
                or "",
                "plex_videos_path_mapping": config.plex_videos_path_mapping or "",
                "thumbnail_interval": config.plex_bif_frame_interval,
                "thumbnail_quality": config.thumbnail_quality,
                "regenerate_thumbnails": config.regenerate_thumbnails,
                "gpu_config": config.gpu_config,
                "gpu_threads": config.gpu_threads,
                "cpu_threads": config.cpu_threads,
                "cpu_fallback_threads": config.fallback_cpu_threads,
                "ffmpeg_threads": config.ffmpeg_threads,
                "log_level": config.log_level,
            }
        )
    except Exception as e:
        logger.error(f"Failed to get config: {e}")
        return jsonify({"error": "Failed to retrieve configuration"}), 500


_version_cache: dict = {"result": None, "fetched_at": 0.0}
_version_cache_lock = threading.Lock()
_VERSION_CACHE_TTL = 3600  # seconds


def _get_version_info() -> dict:
    """Build version info, using a 1-hour TTL cache for the GitHub API call.

    The installed version and install_type are cheap to compute and never
    change at runtime, but the latest-release lookup hits the GitHub API,
    so we cache the full result for ``_VERSION_CACHE_TTL`` seconds.

    Returns:
        Dict with current_version, latest_version, update_available,
        and install_type.
    """
    with _version_cache_lock:
        if (
            _version_cache["result"] is not None
            and (time.monotonic() - _version_cache["fetched_at"]) < _VERSION_CACHE_TTL
        ):
            return _version_cache["result"]

    from ...utils import is_docker_environment
    from ...version_check import (
        get_branch_head_sha,
        get_current_version,
        get_latest_github_release,
        parse_version,
    )

    git_branch_raw = (os.environ.get("GIT_BRANCH") or "").strip()
    git_sha_raw = (os.environ.get("GIT_SHA") or "").strip()

    # Dockerfile ARG defaults are the literal string "unknown".
    is_local_docker = git_branch_raw == "unknown" and git_sha_raw == "unknown"
    git_branch = "" if git_branch_raw == "unknown" else git_branch_raw
    git_sha = "" if git_sha_raw == "unknown" else git_sha_raw

    update_available = False
    latest_version = None

    if is_local_docker:
        # Local Docker build (Dockerfile defaults, not CI)
        install_type = "local_docker"
        current_version = "local build"
        latest_version = get_latest_github_release()

    elif git_branch and git_sha:
        # CI Docker build -- distinguish release tags from dev branches
        try:
            parse_version(git_branch)
            # GIT_BRANCH is a version tag (e.g. 3.4.1) -- release image
            install_type = "docker"
            current_version = git_branch.lstrip("v")
            latest_version = get_latest_github_release()
            if latest_version:
                try:
                    update_available = parse_version(latest_version) > parse_version(
                        current_version
                    )
                except ValueError:
                    logger.debug("Could not compare versions for update check")
        except ValueError:
            # GIT_BRANCH is a branch name (e.g. dev) -- dev image
            install_type = "dev_docker"
            current_version = f"{git_branch}@{git_sha[:7]}"
            head_sha = get_branch_head_sha(git_branch)
            if head_sha and not head_sha.startswith(git_sha):
                update_available = True
            latest_version = f"{git_branch}@{head_sha[:7]}" if head_sha else None

    else:
        # Non-Docker: source checkout or pip install
        install_type = "source" if not is_docker_environment() else "docker"
        current_version = get_current_version()
        latest_version = get_latest_github_release()
        if latest_version:
            try:
                update_available = parse_version(latest_version) > parse_version(
                    current_version
                )
            except ValueError:
                logger.debug("Could not compare versions for update check")

    result = {
        "current_version": current_version,
        "latest_version": latest_version,
        "update_available": update_available,
        "install_type": install_type,
    }

    with _version_cache_lock:
        _version_cache["result"] = result
        _version_cache["fetched_at"] = time.monotonic()

    return result


@api.route("/system/version")
@setup_or_auth_required
def get_version_info():
    """Get installed version and latest available version.

    Returns:
        JSON with current_version, latest_version, update_available,
        and install_type fields. latest_version may be null if the
        GitHub API is unreachable. Results are cached for 1 hour.
    """
    return jsonify(_get_version_info())


@api.route("/health")
def health_check():
    """Health check endpoint (no auth required)."""
    return jsonify({"status": "healthy"})


_GITHUB_RELEASES_URL = (
    "https://api.github.com/repos/stevezau/plex_generate_vid_previews/releases"
)
_RELEASES_CACHE: dict = {"result": None, "fetched_at": 0.0}
_RELEASES_CACHE_TTL = 3600


def _fetch_github_releases(limit: int = 10) -> list:
    """Fetch recent GitHub releases with TTL caching.

    Args:
        limit: Max releases to return.

    Returns:
        List of dicts with version, date, and body (markdown).
    """
    now = time.monotonic()
    if (
        _RELEASES_CACHE["result"] is not None
        and (now - _RELEASES_CACHE["fetched_at"]) < _RELEASES_CACHE_TTL
    ):
        return _RELEASES_CACHE["result"][:limit]

    import requests as req

    try:
        resp = req.get(
            _GITHUB_RELEASES_URL,
            headers={"User-Agent": "plex-generate-previews"},
            params={"per_page": limit},
            timeout=5,
        )
        resp.raise_for_status()
        entries = []
        for rel in resp.json():
            if rel.get("draft"):
                continue
            entries.append(
                {
                    "version": (rel.get("tag_name") or "").lstrip("v"),
                    "name": rel.get("name") or rel.get("tag_name") or "",
                    "date": rel.get("published_at") or "",
                    "body": rel.get("body") or "",
                    "url": rel.get("html_url") or "",
                }
            )
        _RELEASES_CACHE["result"] = entries
        _RELEASES_CACHE["fetched_at"] = time.monotonic()
        return entries[:limit]
    except Exception as e:
        logger.debug(f"Failed to fetch GitHub releases: {e}")
        return []


@api.route("/system/whats-new")
@setup_or_auth_required
def get_whats_new():
    """Return changelog entries the user hasn't seen yet.

    Compares the current running version against ``last_seen_version``
    stored in settings.  On first install (no ``last_seen_version``),
    silently sets it to the current version and returns nothing.
    """
    from ..settings_manager import get_settings_manager

    settings = get_settings_manager()
    version_info = _get_version_info()
    current = version_info.get("current_version", "")

    if not current or current in ("0.0.0", "0.0.0.dev0", "local build"):
        return jsonify({"has_new": False, "entries": []})

    last_seen = settings.get("last_seen_version", "")

    if not last_seen:
        settings.update({"last_seen_version": current})
        return jsonify({"has_new": False, "entries": []})

    if last_seen == current:
        return jsonify({"has_new": False, "entries": []})

    from ...version_check import parse_version

    releases = _fetch_github_releases(limit=10)
    unseen = []
    for entry in releases:
        v = entry["version"]
        if not v:
            continue
        try:
            if parse_version(v) > parse_version(last_seen):
                unseen.append(entry)
        except ValueError:
            continue

    return jsonify({"has_new": len(unseen) > 0, "entries": unseen})


@api.route("/system/whats-new/dismiss", methods=["POST"])
@setup_or_auth_required
def dismiss_whats_new():
    """Mark the current version's changelog as seen."""
    from ..settings_manager import get_settings_manager

    settings = get_settings_manager()
    version_info = _get_version_info()
    current = version_info.get("current_version", "")
    if current and current not in ("0.0.0", "0.0.0.dev0", "local build"):
        settings.update({"last_seen_version": current})
    return jsonify({"ok": True})


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
        List of library dicts with id, name, type

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
        if section.get("type") in ("movie", "show"):
            libraries.append(
                {
                    "id": str(section.get("key")),
                    "name": section.get("title"),
                    "type": section.get("type"),
                }
            )
    return libraries


@api.route("/libraries")
@api_token_required
def get_libraries():
    """Get available Plex libraries.

    Accepts optional query params 'url' and 'token' to override saved
    settings (used during setup wizard before config is persisted).
    """
    try:
        import requests as req_lib

        from ..settings_manager import get_settings_manager

        settings = get_settings_manager()

        plex_url = request.args.get("url")
        plex_token = request.args.get("token")
        verify_ssl = _param_to_bool(
            request.args.get("verify_ssl"), settings.plex_verify_ssl
        )

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

                libraries = []
                for section in plex.library.sections():
                    if section.type in ("movie", "show"):
                        libraries.append(
                            {
                                "id": str(section.key),
                                "name": section.title,
                                "type": section.type,
                            }
                        )

                return jsonify({"libraries": libraries})
            except Exception as e:
                logger.error(f"Failed to get libraries via config: {e}")
                return jsonify(
                    {
                        "error": "Plex not configured. Complete setup in Settings.",
                        "libraries": [],
                    }
                ), 400

        libraries = _fetch_libraries_via_http(
            plex_url,
            plex_token,
            verify_ssl=verify_ssl,
        )

        return jsonify({"libraries": libraries})

    except req_lib.ConnectionError:
        detail = f"Could not connect to Plex at {plex_url}"
        logger.error(f"Failed to get libraries: {detail}")
        return jsonify(
            {
                "error": f"{detail}. Check the server URL and ensure Plex is running and reachable from this host.",
                "libraries": [],
            }
        ), 502
    except req_lib.Timeout:
        detail = f"Connection to Plex at {plex_url} timed out"
        logger.error(f"Failed to get libraries: {detail}")
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
        logger.error(f"Failed to get libraries: {detail} (HTTP {status})")
        return jsonify({"error": f"{detail}. {hint}", "libraries": []}), 502
    except Exception as e:
        logger.error(f"Failed to get libraries: {e}")
        return jsonify(
            {"error": f"Failed to retrieve libraries: {e}", "libraries": []}
        ), 500
