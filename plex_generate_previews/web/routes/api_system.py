"""System, health, config, library, and log-history API routes."""

import glob
import html as html_escape
import json as _json
import os
import threading
import time

import urllib3
from flask import jsonify, request
from loguru import logger

from ...logging_config import LEVEL_ORDER, get_app_log_path
from ..auth import api_token_required, setup_or_auth_required
from ..jobs import get_job_manager
from . import api
from ._helpers import (
    _ensure_gpu_cache,
    _gpu_cache,
    _gpu_cache_lock,
    _param_to_bool,
)


def _get_timezone_info() -> dict:
    """Detect container timezone configuration.

    Returns a dict with the current timezone name and whether the TZ env var
    is set.  Includes a ``warning`` key when the container appears to be using
    the default UTC timezone without an explicit TZ variable — a common Docker
    misconfiguration that causes schedules and timestamps to be wrong.
    """
    tz_env = os.environ.get("TZ", "")
    system_tz = time.tzname[0]

    # No explicit TZ *and* system reports UTC → likely misconfigured container
    needs_warning = not tz_env and system_tz == "UTC"

    result: dict = {"timezone": system_tz, "tz_env_set": bool(tz_env)}
    if needs_warning:
        # HTML — the dashboard injects this into an alert via innerHTML
        # and the help lines need proper breaks to be readable.  The
        # Settings page has its own static markup for the same message.
        result["warning"] = (
            "Your container timezone is UTC (default). "
            "Scheduled jobs and log timestamps may not match your local time."
            "<br><br>"
            '<span class="small">To fix, either:</span>'
            '<ul class="small mb-0 mt-1">'
            "<li>Add <code>-v /etc/localtime:/etc/localtime:ro</code> to your "
            "Docker run command <em>(recommended)</em></li>"
            "<li>Or set <code>-e TZ=America/New_York</code> "
            "(replace with your timezone)</li>"
            "</ul>"
        )
    return result


@api.route("/system/timezone")
def get_timezone():
    """Return container timezone info and warn if misconfigured.

    No authentication required — timezone is not sensitive.
    """
    return jsonify(_get_timezone_info())


def _vendor_display_name(gpus: list, vendor: str) -> str:
    """Return a display string for the first GPU matching ``vendor``.

    Falls back to a generic ``"<Vendor> GPU"`` label if no readable name
    is present in the cache, so the warning text stays grammatical even
    when the detection layer only returned the vendor code.
    """
    for g in gpus:
        if g.get("type") != vendor:
            continue
        name = (g.get("name") or "").strip()
        if name and name != vendor:
            return name
        break
    return {
        "NVIDIA": "NVIDIA GPU",
        "INTEL": "Intel GPU",
        "AMD": "AMD GPU",
    }.get(vendor, f"{vendor} GPU")


def _get_vulkan_info() -> dict:
    """Return Vulkan device info and warn if the DV5 green-overlay bug will hit.

    When the cached Vulkan device from ``get_vulkan_device_info()`` is a
    software rasteriser (``llvmpipe`` / ``lavapipe``), builds a
    GPU-aware HTML warning that leads with the user-visible symptom
    (green overlay on some Dolby Vision thumbnails), then branches on
    what the user can actually do about it:

    - **Pure NVIDIA** (regardless of ``/dev/dri``): upstream version
      skew between linuxserver/ffmpeg's Vulkan loader and the NVIDIA
      driver. Mounting ``/dev/dri`` on a pure-NVIDIA host *does not
      help*, because there is no Mesa ICD to fall back to — so this
      branch fires whether or not the render node is mapped.
    - **NVIDIA + Intel/AMD, no render node mapped:** mount
      ``/dev/dri`` so the container can reach the Mesa driver.
    - **Intel/AMD only, no render node mapped:** mount ``/dev/dri``.
    - **Intel/AMD (with or without NVIDIA), render node mapped but
      still llvmpipe:** host drivers or render-node permissions issue.
    - **No GPU detected at all:** no hardware visible to the container.

    The shared header avoids jargon like "Vulkan driver misconfigured"
    or "software rasterizer" and explains the mechanism in plain
    English. Per-case bodies name the user's actual GPU. Technical
    details (``VK_ERROR_INCOMPATIBLE_DRIVER``, loader versions, etc.)
    live in a muted footer so curious users can google them without
    cluttering the main message.
    """
    from ...gpu_detection import get_vulkan_device_info

    info = get_vulkan_device_info()
    device = info.get("device")
    is_software = info.get("is_software", False)

    result: dict = {"device": device}
    if not is_software:
        logger.debug(
            f"Vulkan warning: device={device!r} is_software=False; "
            "no DV5 warning will be shown."
        )
        return result

    try:
        _ensure_gpu_cache()
        with _gpu_cache_lock:
            gpus = list(_gpu_cache["result"] or [])
    except Exception as exc:
        logger.warning(
            f"Vulkan warning: GPU cache lookup raised {exc!r}; "
            "proceeding with an empty GPU list for the warning body."
        )
        gpus = []

    vendors = {g.get("type") for g in gpus if g.get("type")}
    has_nvidia = "NVIDIA" in vendors
    has_intel = "INTEL" in vendors
    has_amd = "AMD" in vendors
    has_mesa_vendor = has_intel or has_amd
    dri_render_nodes = glob.glob("/dev/dri/renderD*")
    dri_mapped = bool(dri_render_nodes)

    logger.info(
        f"Vulkan warning inputs: device={device!r} vendors={sorted(vendors)} "
        f"has_nvidia={has_nvidia} has_mesa={has_mesa_vendor} "
        f"dri_render_nodes={dri_render_nodes or '[]'}"
    )

    nvidia_name = _vendor_display_name(gpus, "NVIDIA") if has_nvidia else ""
    # Prefer AMD for the Mesa label when both AMD and Intel are present
    # (AMD is more likely to be the user's primary display GPU); either
    # works for the /dev/dri remediation text.
    mesa_vendor_code = "AMD" if has_amd else ("INTEL" if has_intel else "")
    mesa_name = _vendor_display_name(gpus, mesa_vendor_code) if mesa_vendor_code else ""
    mesa_vendor_label = {"INTEL": "Intel", "AMD": "AMD"}.get(mesa_vendor_code, "Mesa")
    all_names_escaped = ", ".join(
        html_escape.escape(g.get("name") or g.get("type") or "GPU")
        for g in gpus
        if g.get("name") or g.get("type")
    )

    header = (
        "When this app creates thumbnails for <strong>Dolby Vision "
        "Profile 5</strong> content, it relies on GPU-accelerated color "
        "conversion. Your container does not have a working GPU rendering "
        "driver for this step, so the app is falling back to software "
        "rendering — which has a known bug that paints a green rectangle "
        "onto a portion of each affected thumbnail."
        "<br><br>"
        "All other content (standard video, HDR10, Dolby Vision Profile "
        "7 and 8) is not affected."
        "<br><br>"
    )
    footer = (
        '<div class="small text-muted mt-2">You can safely dismiss this '
        "warning if you have no Dolby Vision Profile 5 content, or if a "
        "green overlay on a few thumbnails doesn't bother you.</div>"
    )

    # Pure-NVIDIA takes precedence over dri_mapped: mounting /dev/dri on
    # a host with no Mesa-capable GPU does nothing, because there is no
    # Mesa ICD to fall back to. Route these users to the version-skew
    # explanation regardless of whether they've mounted /dev/dri.
    if has_nvidia and not has_mesa_vendor:
        logger.info(
            f"Vulkan warning: selected Case A (pure NVIDIA version "
            f"mismatch) for {nvidia_name!r}; dri_mapped={dri_mapped}"
        )
        body = (
            f"<strong>Your GPU:</strong> {html_escape.escape(nvidia_name)}"
            "<br><br>"
            "Your NVIDIA card does have a GPU rendering driver, but "
            "there's a <strong>version mismatch</strong> between your "
            "NVIDIA driver and the version of the rendering toolkit "
            "built into this container. The container refuses to load "
            "your NVIDIA driver and falls back to software rendering. "
            "This is a known upstream packaging issue &mdash; there is "
            "no configuration fix you can apply from your side."
            "<br><br>"
            '<span class="small"><strong>Workarounds:</strong></span>'
            '<ul class="small mb-0 mt-1">'
            "<li><strong>Dual-GPU hosts:</strong> if your host also has "
            "an Intel iGPU or AMD GPU (even an unused one), forward its "
            "render node with "
            "<code>--device /dev/dri:/dev/dri</code> (docker run) or "
            "<code>devices: [&quot;/dev/dri:/dev/dri&quot;]</code> "
            "(docker-compose). The container will use that GPU for "
            "rendering instead. Your NVIDIA card keeps handling the "
            "video decoding — the two paths are independent.</li>"
            "<li><strong>Pure NVIDIA hosts:</strong> wait for a "
            "container base image update, or skip Dolby Vision Profile "
            "5 content until then.</li>"
            "</ul>"
            '<div class="small text-muted mt-2">Technical details: the '
            "container's Vulkan loader (linuxserver/ffmpeg) rejects the "
            "NVIDIA ICD with <code>VK_ERROR_INCOMPATIBLE_DRIVER</code> "
            "because the loader API version is newer than the NVIDIA "
            "driver's reported Vulkan API version.</div>"
        )
    elif has_nvidia and has_mesa_vendor and not dri_mapped:
        # NVIDIA + Intel/AMD but /dev/dri not forwarded: mounting the
        # render node lets libplacebo use Mesa alongside NVIDIA decoding.
        logger.info(
            f"Vulkan warning: selected Case B (NVIDIA + Mesa, /dev/dri "
            f"not mapped) for NVIDIA={nvidia_name!r} Mesa={mesa_name!r}"
        )
        body = (
            f"<strong>Your GPUs:</strong> "
            f"{html_escape.escape(nvidia_name)} and "
            f"{html_escape.escape(mesa_name)}"
            "<br><br>"
            f"Your {mesa_vendor_label} GPU can handle the GPU rendering "
            "step, but the container can't reach it because the "
            "<code>/dev/dri</code> render node isn't forwarded. NVIDIA's "
            "own rendering driver can't be used due to a separate "
            "version-mismatch issue, so the app falls back to software "
            "rendering."
            "<br><br>"
            '<span class="small"><strong>Fix</strong> — add this to '
            "your Docker configuration and restart the container:</span>"
            '<ul class="small mb-0 mt-1">'
            "<li><strong>Docker run:</strong> add "
            "<code>--device /dev/dri:/dev/dri</code></li>"
            "<li><strong>Docker Compose:</strong> add "
            "<code>devices: [&quot;/dev/dri:/dev/dri&quot;]</code> "
            "under the service</li>"
            "</ul>"
            '<div class="small mt-2">After the restart, the green '
            f"overlay will disappear. Your NVIDIA card keeps handling "
            "video decoding &mdash; the two paths are independent.</div>"
        )
    elif has_mesa_vendor and not has_nvidia and not dri_mapped:
        # Intel/AMD only, no render node: straight mount fix.
        logger.info(
            f"Vulkan warning: selected Case C (Mesa only, /dev/dri "
            f"not mapped) for {mesa_name!r}"
        )
        body = (
            f"<strong>Your GPU:</strong> {html_escape.escape(mesa_name)}"
            "<br><br>"
            "Your GPU can handle the rendering step, but the container "
            "can't reach it because the <code>/dev/dri</code> render "
            "node isn't forwarded."
            "<br><br>"
            '<span class="small"><strong>Fix</strong> — add this to '
            "your Docker configuration and restart the container:</span>"
            '<ul class="small mb-0 mt-1">'
            "<li><strong>Docker run:</strong> add "
            "<code>--device /dev/dri:/dev/dri</code></li>"
            "<li><strong>Docker Compose:</strong> add "
            "<code>devices: [&quot;/dev/dri:/dev/dri&quot;]</code> "
            "under the service</li>"
            "</ul>"
            '<div class="small mt-2">After the restart, the green '
            "overlay will disappear.</div>"
        )
    elif has_mesa_vendor and dri_mapped:
        # Intel/AMD (with or without NVIDIA) already has /dev/dri but
        # rendering still fell back to software. Usually host-side.
        logger.info(
            f"Vulkan warning: selected Case D (Mesa with /dev/dri "
            f"mapped but rendering still fell back) for "
            f"{mesa_name!r}; dri_nodes={dri_render_nodes}"
        )
        detected = all_names_escaped or "a GPU"
        body = (
            f"<strong>Your GPUs:</strong> {detected}"
            "<br><br>"
            "The <code>/dev/dri</code> render node is already forwarded "
            "to the container, but the GPU rendering check still "
            "failed. Usually this means one of two things:"
            '<ul class="small mb-0 mt-1">'
            "<li><strong>Your host's GPU drivers are missing or "
            "broken.</strong> Run <code>vainfo</code> on the host "
            "(outside the container) &mdash; if it does not list your "
            "GPU, install or fix the host's Mesa drivers.</li>"
            "<li><strong>Render node permissions don't match the "
            "container user.</strong> Run "
            "<code>ls -la /dev/dri/renderD*</code> on the host. The "
            "container runs as <code>PUID:PGID</code>, and the render "
            "node's group (usually <code>render</code> or "
            "<code>video</code>) needs to be readable by that user.</li>"
            "</ul>"
        )
    else:
        # No GPU detected at all.
        logger.info("Vulkan warning: selected Case E (no GPU detected)")
        body = (
            "<strong>No GPU detected in this container.</strong>"
            "<br><br>"
            "The container has no GPU visible to it, so GPU rendering "
            "isn't possible at all. Make sure your host has a GPU with "
            "drivers installed, and that the GPU is forwarded to the "
            "container:"
            '<ul class="small mb-0 mt-1">'
            "<li><strong>Intel or AMD:</strong> "
            "<code>--device /dev/dri:/dev/dri</code></li>"
            "<li><strong>NVIDIA:</strong> "
            "<code>--runtime=nvidia --gpus all</code></li>"
            "</ul>"
        )

    result["warning"] = header + body + footer
    return result


@api.route("/system/vulkan")
def get_vulkan():
    """Return container Vulkan device info and warn if misconfigured for DV5.

    No authentication required — Vulkan device info is not sensitive.
    """
    return jsonify(_get_vulkan_info())


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

        tz_info = _get_timezone_info()
        vk_info = _get_vulkan_info()
        resp = {
            "gpus": gpus,
            "gpu_stats": [],
            "running_job": running_job.to_dict() if running_job else None,
            "pending_jobs": len(job_manager.get_pending_jobs()),
        }
        if "warning" in tz_info:
            resp["timezone_warning"] = tz_info["warning"]
        if "warning" in vk_info:
            resp["vulkan_warning"] = vk_info["warning"]

        return jsonify(resp)
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

        resp = {
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
        if config.gpu_threads == 0 and config.cpu_threads == 0:
            resp["config_warning"] = (
                "No workers configured — jobs will remain pending "
                "until GPU or CPU workers are added."
            )
        return jsonify(resp)
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


# ---------------------------------------------------------------------------
# Log history (reads from the JSONL app.log file)
# ---------------------------------------------------------------------------

_MAX_HISTORY_LINES = 2000
_READ_CHUNK = 64 * 1024  # 64 KB chunks for reverse reading


def _read_tail_lines(path: str, max_lines: int) -> list[str]:
    """Read the last *max_lines* lines from *path* efficiently.

    Reads backwards in fixed-size chunks to avoid loading the entire file.
    Returns lines in chronological (oldest-first) order.
    """
    try:
        size = os.path.getsize(path)
    except OSError:
        return []

    lines: list[str] = []
    with open(path, "rb") as f:
        offset = size
        partial = b""
        while offset > 0 and len(lines) < max_lines:
            read_size = min(_READ_CHUNK, offset)
            offset -= read_size
            f.seek(offset)
            chunk = f.read(read_size) + partial
            chunk_lines = chunk.split(b"\n")
            partial = chunk_lines[0]
            for raw in reversed(chunk_lines[1:]):
                if raw:
                    lines.append(raw.decode("utf-8", errors="replace"))
                if len(lines) >= max_lines:
                    break
        if partial and len(lines) < max_lines:
            lines.append(partial.decode("utf-8", errors="replace"))

    lines.reverse()
    return lines


@api.route("/logs/history")
@setup_or_auth_required
def get_log_history():
    """Return recent log entries from the persistent app.log file.

    Query params:
        limit: Max lines to return (default 500, max 2000).
        level: Minimum log level filter (default: configured log_level).
        before: ISO timestamp cursor — only return entries older than this.
    """
    try:
        limit = min(int(request.args.get("limit", 500)), _MAX_HISTORY_LINES)
    except (ValueError, TypeError):
        limit = 500
    min_level = (request.args.get("level") or "").upper()
    before = request.args.get("before", "")

    if min_level not in LEVEL_ORDER:
        min_level = ""
    min_level_val = LEVEL_ORDER.get(min_level, 0)

    log_path = get_app_log_path()
    raw_lines = _read_tail_lines(log_path, max_lines=limit * 3)

    result: list[dict] = []
    for raw in raw_lines:
        try:
            entry = _json.loads(raw)
        except (ValueError, TypeError):
            continue
        entry_level = entry.get("level", "")
        if min_level_val and LEVEL_ORDER.get(entry_level, 0) < min_level_val:
            continue
        if before and entry.get("ts", "") >= before:
            continue
        result.append(entry)

    # Trim to the requested limit (keep the newest entries)
    if len(result) > limit:
        result = result[-limit:]

    oldest_ts = result[0]["ts"] if result else ""
    return jsonify(
        {
            "lines": result,
            "has_more": len(raw_lines) >= limit * 3,
            "oldest_ts": oldest_ts,
        }
    )


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


_SPORTS_AGENT_PATTERNS = ("sportarr", "sportscanner")


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
            }
        )
    return libraries


@api.route("/libraries")
@api_token_required
def get_libraries():
    """Get available Plex libraries.

    Accepts optional query params 'url' and 'token' to override saved
    settings (used during setup wizard before config is persisted).

    Results are cached for 5 minutes when using saved credentials to
    avoid hitting the Plex server on every settings page load.
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
                                "display_type": classify_library_type(
                                    section.type, agent
                                ),
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
