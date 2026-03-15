"""System, health, config, and library API routes."""

import os

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
        if config is None:
            settings = get_settings_manager()
            return jsonify(
                {
                    "plex_url": os.environ.get("PLEX_URL", ""),
                    "plex_token": "****" if os.environ.get("PLEX_TOKEN") else "",
                    "plex_config_folder": os.environ.get("PLEX_CONFIG_FOLDER", ""),
                    "plex_verify_ssl": settings.plex_verify_ssl,
                    "config_error": "Configuration incomplete. Check required environment variables.",
                    "gpu_threads": int(os.environ.get("GPU_THREADS", 1)),
                    "cpu_threads": int(os.environ.get("CPU_THREADS", 1)),
                    "cpu_fallback_threads": int(
                        os.environ.get("FALLBACK_CPU_THREADS", 0)
                    ),
                    "ffmpeg_threads": int(os.environ.get("FFMPEG_THREADS", 2)),
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


@api.route("/health")
def health_check():
    """Health check endpoint (no auth required)."""
    return jsonify({"status": "healthy"})


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
