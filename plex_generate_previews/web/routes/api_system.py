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


# Standard locations the Vulkan loader searches for the NVIDIA ICD JSON.
# nvidia-container-toolkit mounts at /etc/vulkan/icd.d/; some loaders and
# distributions only scan /usr/share/vulkan/icd.d/. Both are checked so
# the Case A dispatch can tell "file exists somewhere" from "file missing
# everywhere" without needing to know which path a given loader prefers.
_NVIDIA_ICD_JSON_SEARCH_PATHS = (
    "/etc/vulkan/icd.d/nvidia_icd.json",
    "/usr/share/vulkan/icd.d/nvidia_icd.json",
)

# Glob patterns for libnvidia-glvkspirv.so, the sibling library the NVIDIA
# Vulkan ICD needs to `dlopen`. Checking multiple locations covers amd64
# Debian, arm64, and non-standard layouts.
_LIBNVIDIA_GLVKSPIRV_GLOBS = (
    "/usr/lib/x86_64-linux-gnu/libnvidia-glvkspirv.so*",
    "/usr/lib/aarch64-linux-gnu/libnvidia-glvkspirv.so*",
    "/usr/lib/libnvidia-glvkspirv.so*",
    "/usr/lib64/libnvidia-glvkspirv.so*",
)

# Glob patterns for libEGL_nvidia.so, the library GLVND routes to when
# NVIDIA's vendor config is present.  Strategy 2c in gpu_detection.py only
# runs when this library is present (otherwise synthesising a vendor JSON
# would be pointing at a non-existent target).  Exposed in the diagnostic
# bundle so users / upstream issue readers can see whether Strategy 2c
# could have helped.
_LIBEGL_NVIDIA_GLOBS = (
    "/usr/lib/x86_64-linux-gnu/libEGL_nvidia.so*",
    "/usr/lib/aarch64-linux-gnu/libEGL_nvidia.so*",
    "/usr/lib/libEGL_nvidia.so*",
    "/usr/lib64/libEGL_nvidia.so*",
)

# Standard locations the GLVND libEGL dispatcher searches for vendor
# configs.  nvidia-container-toolkit injects its NVIDIA config at
# /usr/share/glvnd/egl_vendor.d/10_nvidia.json when the ``graphics``
# capability is set; some setups instead drop it under /etc/glvnd.  Both
# are checked so the diagnostic bundle can show whether the file is
# present before Strategy 2c synthesises a replacement.
_NVIDIA_EGL_VENDOR_JSON_SEARCH_PATHS = (
    "/usr/share/glvnd/egl_vendor.d/10_nvidia.json",
    "/etc/glvnd/egl_vendor.d/10_nvidia.json",
)


def _diagnose_vulkan_environment() -> dict:
    """Gather facts about the container's Vulkan configuration.

    Used by :func:`_get_vulkan_info` to dispatch the pure-NVIDIA warning
    branch into one of four sub-cases (A1 / A2 / A3 / A4) naming the
    specific misconfiguration rather than a generic "upstream packaging
    issue" — and by the ``GET /api/system/vulkan/debug`` endpoint to
    produce a plain-text diagnostic bundle users can paste into GitHub
    issues.

    All facts are derived from ``os.environ`` and the filesystem, so
    this is safe to call from any thread and does not perform any
    expensive subprocess work.

    Returns:
        dict with the following keys:

        - ``nvidia_capabilities`` (str or None) — the raw value of the
          ``NVIDIA_DRIVER_CAPABILITIES`` env var, or None if unset.
        - ``nvidia_capabilities_has_graphics`` (bool) — True if the
          capability string contains ``graphics`` (or is ``all``).
        - ``nvidia_icd_json_path`` (str or None) — the first path in
          :data:`_NVIDIA_ICD_JSON_SEARCH_PATHS` where
          ``nvidia_icd.json`` exists, or None.
        - ``libnvidia_glvkspirv_found`` (bool) — True if a
          ``libnvidia-glvkspirv.so*`` file is present in any of the
          standard library paths.
        - ``nvidia_drm_loaded`` (bool) — True if ``/proc/driver/nvidia``
          exists, meaning the host's NVIDIA kernel module is loaded and
          exposed to the container.
        - ``nvidia_driver_version`` (str or None) — parsed from
          ``/proc/driver/nvidia/version`` if available. Surfaced in the
          debug bundle and in the Case A2 warning that blames a specific
          driver version range.
    """
    caps = os.environ.get("NVIDIA_DRIVER_CAPABILITIES")
    caps_lower = (caps or "").lower()
    caps_has_graphics = "graphics" in caps_lower or caps_lower == "all"

    nvidia_icd_json_path: str | None = None
    for path in _NVIDIA_ICD_JSON_SEARCH_PATHS:
        if os.path.exists(path):
            nvidia_icd_json_path = path
            break

    libnvidia_glvkspirv_found = False
    for pattern in _LIBNVIDIA_GLVKSPIRV_GLOBS:
        if glob.glob(pattern):
            libnvidia_glvkspirv_found = True
            break

    libegl_nvidia_found = False
    for pattern in _LIBEGL_NVIDIA_GLOBS:
        if glob.glob(pattern):
            libegl_nvidia_found = True
            break

    nvidia_egl_vendor_json_path: str | None = None
    for path in _NVIDIA_EGL_VENDOR_JSON_SEARCH_PATHS:
        if os.path.exists(path):
            nvidia_egl_vendor_json_path = path
            break

    nvidia_drm_loaded = os.path.exists("/proc/driver/nvidia")
    nvidia_driver_version: str | None = None
    version_path = "/proc/driver/nvidia/version"
    if os.path.exists(version_path):
        try:
            with open(version_path, encoding="utf-8", errors="replace") as fh:
                content = fh.read()
            # Typical line: "NVRM version: NVIDIA UNIX x86_64 Kernel Module  570.133.07  Thu Mar 20 14:50:40 UTC 2025"
            for line in content.splitlines():
                if "NVRM version" in line:
                    parts = line.split()
                    for part in parts:
                        # Match e.g. "570.133.07" or "550.54.15"
                        if "." in part and part.replace(".", "").isdigit():
                            nvidia_driver_version = part
                            break
                    if nvidia_driver_version:
                        break
        except OSError as exc:
            logger.debug("Could not read {}: {}", version_path, exc)

    return {
        "nvidia_capabilities": caps,
        "nvidia_capabilities_has_graphics": caps_has_graphics,
        "nvidia_icd_json_path": nvidia_icd_json_path,
        "libnvidia_glvkspirv_found": libnvidia_glvkspirv_found,
        "libegl_nvidia_found": libegl_nvidia_found,
        "nvidia_egl_vendor_json_path": nvidia_egl_vendor_json_path,
        "nvidia_drm_loaded": nvidia_drm_loaded,
        "nvidia_driver_version": nvidia_driver_version,
    }


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
    from ...gpu.vulkan_probe import get_vulkan_device_info

    info = get_vulkan_device_info()
    device = info.device
    is_software = info.is_software

    result: dict = {"device": device}
    if not is_software:
        logger.debug("Vulkan warning: device={!r} is_software=False; no DV5 warning will be shown.", device)
        return result

    try:
        _ensure_gpu_cache()
        with _gpu_cache_lock:
            gpus = list(_gpu_cache["result"] or [])
    except Exception as exc:
        logger.warning(
            "Dolby Vision warning: could not list detected GPUs while building the diagnostic message "
            "({}: {}). The warning will still be shown but won't include your GPU model name. "
            "This is cosmetic only — your GPU detection itself isn't affected.",
            type(exc).__name__,
            exc,
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
        "Vulkan warning inputs: device={!r} vendors={} has_nvidia={} has_mesa={} dri_render_nodes={}",
        device,
        sorted(vendors),
        has_nvidia,
        has_mesa_vendor,
        dri_render_nodes or "[]",
    )

    nvidia_name = _vendor_display_name(gpus, "NVIDIA") if has_nvidia else ""
    # Prefer AMD for the Mesa label when both AMD and Intel are present
    # (AMD is more likely to be the user's primary display GPU); either
    # works for the /dev/dri remediation text.
    mesa_vendor_code = "AMD" if has_amd else ("INTEL" if has_intel else "")
    mesa_name = _vendor_display_name(gpus, mesa_vendor_code) if mesa_vendor_code else ""
    mesa_vendor_label = {"INTEL": "Intel", "AMD": "AMD"}.get(mesa_vendor_code, "Mesa")
    all_names_escaped = ", ".join(
        html_escape.escape(g.get("name") or g.get("type") or "GPU") for g in gpus if g.get("name") or g.get("type")
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
    # a host with no Mesa-capable GPU does nothing on its own, so we
    # never send these users down the /dev/dri path. Instead, dispatch
    # into one of four sub-cases (A1/A2/A3/A4) based on what
    # _diagnose_vulkan_environment() tells us is actually broken, so the
    # warning names the specific fix and the user can act on it.
    if has_nvidia and not has_mesa_vendor:
        diag = _diagnose_vulkan_environment()
        nvidia_name_esc = html_escape.escape(nvidia_name)

        if not diag["nvidia_capabilities_has_graphics"]:
            # Case A1: NVIDIA_DRIVER_CAPABILITIES is missing 'graphics'.
            # nvidia-container-toolkit didn't inject the Vulkan ICD at all.
            # This is ~80% of real pure-NVIDIA reports.
            current_caps = diag["nvidia_capabilities"] or "(unset)"
            logger.info(
                "Vulkan warning: selected Case A1 (missing 'graphics' capability) for {!r}; NVIDIA_DRIVER_CAPABILITIES={!r}",
                nvidia_name,
                current_caps,
            )
            body = (
                f"<strong>Your GPU:</strong> {nvidia_name_esc}"
                "<br><br>"
                "Your NVIDIA card is working for video decoding, but "
                "the container was started <strong>without the "
                "<code>graphics</code> NVIDIA driver capability</strong>. "
                "The NVIDIA Container Toolkit only injects the NVIDIA "
                "Vulkan driver into the container when <code>graphics</code> "
                "(or <code>all</code>) is declared; the "
                "<code>compute,video,utility</code> trio that people "
                "usually start with only covers CUDA, NVDEC/NVENC, and "
                "<code>nvidia-smi</code>."
                "<br><br>"
                f'<div class="small">Your container\'s current value: '
                f"<code>NVIDIA_DRIVER_CAPABILITIES={html_escape.escape(current_caps)}</code>"
                "</div>"
                "<br>"
                '<span class="small"><strong>Fix</strong> — change '
                "<code>NVIDIA_DRIVER_CAPABILITIES</code> to "
                "<code>all</code> and restart the container:</span>"
                '<ul class="small mb-0 mt-1">'
                "<li><strong>Docker run:</strong> add "
                "<code>-e NVIDIA_DRIVER_CAPABILITIES=all</code></li>"
                "<li><strong>Docker Compose:</strong> under "
                "<code>environment:</code>, add "
                "<code>- NVIDIA_DRIVER_CAPABILITIES=all</code></li>"
                "<li><strong>Unraid:</strong> set "
                "<em>NVIDIA Driver Capabilities</em> to <code>all</code> "
                "in the template</li>"
                "</ul>"
                '<div class="small mt-2">This is the single most common '
                "cause of this warning on pure-NVIDIA hosts and will "
                "almost certainly fix it. After the restart the green "
                "overlay will disappear.</div>"
            )
        elif diag["nvidia_icd_json_path"] is None:
            # Case A2: graphics capability is set but the ICD JSON is
            # missing. Usually the driver 570-579 regression that's
            # fixed in 580 (nvidia-container-toolkit#1041), or a CDI
            # manifest bug (#1559).
            driver_version = diag["nvidia_driver_version"]
            logger.info(
                "Vulkan warning: selected Case A2 (graphics cap set but nvidia_icd.json missing) for {!r}; driver_version={!r}",
                nvidia_name,
                driver_version,
            )
            driver_line = ""
            if driver_version:
                driver_line = (
                    f'<div class="small mt-2">Detected host NVIDIA driver: '
                    f"<code>{html_escape.escape(driver_version)}</code>. "
                    "If that version is in the 570.x–579.x range, you're "
                    "almost certainly hitting this specific regression.</div>"
                )
            body = (
                f"<strong>Your GPU:</strong> {nvidia_name_esc}"
                "<br><br>"
                "<code>NVIDIA_DRIVER_CAPABILITIES</code> looks correct, "
                "but the NVIDIA Vulkan ICD file "
                "(<code>nvidia_icd.json</code>) is not present inside "
                "the container. The NVIDIA Container Toolkit should "
                "have injected it and didn't."
                "<br><br>"
                '<span class="small"><strong>Most likely cause:</strong> '
                "a known regression on NVIDIA driver versions "
                "<strong>570.x – 579.x</strong> where the ICD is not "
                "injected into containers "
                '(<a href="https://github.com/NVIDIA/nvidia-container-toolkit/issues/1041" '
                'target="_blank" rel="noopener">'
                "nvidia-container-toolkit#1041</a>). "
                "<strong>Fix:</strong> upgrade your host NVIDIA driver "
                "to <strong>580 or newer</strong>, or downgrade to "
                "<strong>550</strong>.</span>"
                '<br><br><span class="small"><strong>Less likely cause:</strong> '
                "if your NVIDIA runtime is using the CDI mode, its CDI "
                "manifest may be missing Vulkan libraries "
                '(<a href="https://github.com/NVIDIA/nvidia-container-toolkit/issues/1559" '
                'target="_blank" rel="noopener">'
                "nvidia-container-toolkit#1559</a>). Switch to legacy "
                'mode by setting <code>mode = "legacy"</code> in '
                "<code>/etc/nvidia-container-runtime/config.toml</code> "
                "on the host.</span>"
                f"{driver_line}"
            )
        elif not diag["libnvidia_glvkspirv_found"]:
            # Case A3: ICD JSON exists but the supporting library is
            # missing. Almost always the CDI manifest bug (#1559).
            logger.info(
                "Vulkan warning: selected Case A3 (nvidia_icd.json at {} but libnvidia-glvkspirv not found) for {!r}",
                diag["nvidia_icd_json_path"],
                nvidia_name,
            )
            body = (
                f"<strong>Your GPU:</strong> {nvidia_name_esc}"
                "<br><br>"
                "Your NVIDIA Vulkan driver file is present (found at "
                f"<code>{html_escape.escape(diag['nvidia_icd_json_path'])}</code>) "
                "but a required supporting library — "
                "<code>libnvidia-glvkspirv.so</code> — is not. The ICD "
                "loads and then immediately fails to resolve its own "
                "dependencies, so the loader discards it and falls back "
                "to software rendering."
                "<br><br>"
                '<span class="small"><strong>Root cause:</strong> a '
                "known NVIDIA Container Toolkit bug where the CDI "
                "manifest only lists the ICD JSON, not the library it "
                "depends on "
                '(<a href="https://github.com/NVIDIA/nvidia-container-toolkit/issues/1559" '
                'target="_blank" rel="noopener">'
                "nvidia-container-toolkit#1559</a>).</span>"
                "<br><br>"
                '<span class="small"><strong>Fix:</strong></span>'
                '<ul class="small mb-0 mt-1">'
                "<li><strong>Quick fix:</strong> switch your NVIDIA "
                "runtime to legacy mode by editing "
                "<code>/etc/nvidia-container-runtime/config.toml</code> "
                'on the host and setting <code>mode = "legacy"</code>, '
                "then restart the container.</li>"
                "<li><strong>Or:</strong> regenerate the CDI manifest "
                "with <code>sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml</code> "
                "after upgrading to a toolkit version that includes the "
                "fix.</li>"
                "</ul>"
            )
        else:
            # Case A4: everything on the checklist is correct but the
            # loader still rejected the ICD. This is the "please file
            # an issue with diagnostics" path.
            logger.warning(
                "Dolby Vision Profile 5 warning: your NVIDIA driver looks correctly installed in this "
                "container ({!r}, all toolkit checks pass), but the Vulkan loader still rejected it. "
                "This is rare — please open a GitHub issue and include the diagnostic bundle from "
                "the 'Copy diagnostic bundle' button on the Settings page (or GET /api/system/vulkan/debug). "
                "Software fallback is in use, which can cause green overlays on DV5 thumbnails only; "
                "all other thumbnails are unaffected.",
                nvidia_name,
            )
            body = (
                f"<strong>Your GPU:</strong> {nvidia_name_esc}"
                "<br><br>"
                "All the usual NVIDIA container requirements are "
                "satisfied &mdash; the <code>graphics</code> capability "
                "is set, the NVIDIA Vulkan ICD file is present at "
                f"<code>{html_escape.escape(diag['nvidia_icd_json_path'] or '(unknown)')}</code>, "
                "and <code>libnvidia-glvkspirv.so</code> is reachable. "
                "Despite all that, the Vulkan loader still rejected the "
                "NVIDIA driver. This is uncommon."
                "<br><br>"
                '<span class="small"><strong>Please file a GitHub '
                "issue</strong> and include the diagnostic bundle from "
                "<code>GET /api/system/vulkan/debug</code> (there's a "
                "<em>Copy diagnostic bundle</em> button just below this "
                "message). The bundle contains the full "
                "<code>VK_LOADER_DEBUG=all</code> trace, which will "
                "usually show exactly why each ICD was rejected.</span>"
            )
    elif has_nvidia and has_mesa_vendor and not dri_mapped:
        # NVIDIA + Intel/AMD but /dev/dri not forwarded: mounting the
        # render node lets libplacebo use Mesa alongside NVIDIA decoding.
        logger.info(
            "Vulkan warning: selected Case B (NVIDIA + Mesa, /dev/dri not mapped) for NVIDIA={!r} Mesa={!r}",
            nvidia_name,
            mesa_name,
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
        logger.info("Vulkan warning: selected Case C (Mesa only, /dev/dri not mapped) for {!r}", mesa_name)
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
            "Vulkan warning: selected Case D (Mesa with /dev/dri mapped but rendering still fell back) for {!r}; dri_nodes={}",
            mesa_name,
            dri_render_nodes,
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


@api.route("/system/vulkan/debug")
def get_vulkan_debug():
    """Return a plain-text Vulkan diagnostic bundle for GitHub issue reports.

    Combines the environment diagnosis, the probe result, and the
    captured ``VK_LOADER_DEBUG=all`` stderr (when the probe exhausted
    all strategies) into a single pasteable block. The dashboard and
    settings banners expose a "Copy diagnostic bundle" button that
    fetches this endpoint and writes the response to
    ``navigator.clipboard``.

    No authentication required — the bundle contains environment facts
    and loader traces but no secrets.
    """
    from ...gpu.vulkan_probe import (
        get_vulkan_debug_buffer,
        get_vulkan_device_info,
        get_vulkan_env_overrides,
    )

    device_info = get_vulkan_device_info()
    diag = _diagnose_vulkan_environment()
    env_overrides = get_vulkan_env_overrides()
    debug_buffer = get_vulkan_debug_buffer()

    with _gpu_cache_lock:
        gpus = list(_gpu_cache["result"] or [])

    gpu_lines = [
        f"  - type={g.get('type', '?')} name={g.get('name', '?')} device={g.get('device', '?')}" for g in gpus
    ] or ["  (none detected)"]

    bundle_lines = [
        "=== plex_generate_vid_previews Vulkan diagnostic bundle ===",
        "",
        "Use this block when reporting a Dolby Vision Profile 5 green-overlay",
        "issue. It captures the app's view of your container's Vulkan state,",
        "plus the full VK_LOADER_DEBUG=all trace (if one was captured).",
        "",
        "--- Probe result ---",
        f"device:      {device_info.device}",
        f"is_software: {device_info.is_software}",
        "",
        "--- Detected GPUs (from gpu_detection cache) ---",
        *gpu_lines,
        "",
        "--- Environment diagnosis ---",
        f"NVIDIA_DRIVER_CAPABILITIES: {diag['nvidia_capabilities']!r}",
        f"  has 'graphics':            {diag['nvidia_capabilities_has_graphics']}",
        f"nvidia_icd_json_path:        {diag['nvidia_icd_json_path']!r}",
        f"libnvidia_glvkspirv_found:   {diag['libnvidia_glvkspirv_found']}",
        f"libegl_nvidia_found:         {diag['libegl_nvidia_found']}",
        f"nvidia_egl_vendor_json_path: {diag['nvidia_egl_vendor_json_path']!r}",
        f"nvidia_drm_loaded:           {diag['nvidia_drm_loaded']}",
        f"nvidia_driver_version:       {diag['nvidia_driver_version']!r}",
        "",
        "--- Render nodes ---",
        f"/dev/dri/renderD*: {glob.glob('/dev/dri/renderD*') or '[]'}",
        "",
        "--- Active Vulkan env overrides ---",
    ]
    if env_overrides:
        bundle_lines.append(
            "  (populated by the Strategy-2 VK_DRIVER_FILES retry; these"
            " env vars are injected into the FFmpeg libplacebo subprocess)"
        )
        for k, v in env_overrides.items():
            bundle_lines.append(f"  {k}={v}")
    else:
        bundle_lines.append("  (none — the default probe found a working Vulkan device, or the retry did not succeed)")
    bundle_lines.append("")

    bundle_lines.append("--- VK_LOADER_DEBUG=all capture ---")
    if debug_buffer:
        bundle_lines.append(f"(last {len(debug_buffer)} bytes of ffmpeg stderr from the Strategy-3 diagnostic probe)")
        bundle_lines.append("")
        bundle_lines.append(debug_buffer)
    else:
        bundle_lines.append(
            "(empty — no diagnostic probe was run; Vulkan either worked"
            " on the default or retry strategy, or Vulkan is unavailable"
            " in this FFmpeg build)"
        )

    bundle_lines.append("")
    bundle_lines.append("=== end bundle ===")

    response_body = "\n".join(bundle_lines)
    return response_body, 200, {"Content-Type": "text/plain; charset=utf-8"}


@api.route("/system/notifications")
def list_notifications():
    """Return active system notifications for the bell-icon dropdown.

    Filters out notifications the user has permanently dismissed (stored
    in ``settings.json``) and those dismissed for this process session.
    No authentication required — notifications contain environment
    diagnostics, not secrets.
    """
    from ..notifications import build_active_notifications
    from ..settings_manager import get_settings_manager

    try:
        dismissed = get_settings_manager().dismissed_notifications
    except Exception as exc:
        logger.warning(
            "Notifications: could not read the list of notifications you've previously dismissed "
            "({}: {}). For now, every active notification will be shown — including any you'd "
            "previously hidden. They'll hide again automatically once the settings file becomes "
            "readable. Check the recent log lines for any settings-load errors.",
            type(exc).__name__,
            exc,
        )
        dismissed = []

    notifications = build_active_notifications(dismissed_permanent=dismissed)
    return jsonify({"notifications": notifications})


@api.route("/system/notifications/<notification_id>/dismiss", methods=["POST"])
def dismiss_notification_session(notification_id: str):
    """Dismiss a notification for the current process session only.

    Cleared on container restart.  No authentication required.
    """
    from ..notifications import dismiss_session

    dismiss_session(notification_id)
    return jsonify({"ok": True, "id": notification_id, "persisted": False})


@api.route("/system/notifications/<notification_id>/dismiss-permanent", methods=["POST"])
def dismiss_notification_permanent(notification_id: str):
    """Dismiss a notification permanently (persist to ``settings.json``).

    Survives container restarts.  No authentication required.
    """
    from ..settings_manager import get_settings_manager

    try:
        get_settings_manager().dismiss_notification_permanent(notification_id)
    except Exception as exc:
        logger.error(
            "Notifications: could not save your dismissal of notification {!r} ({}: {}). "
            "The notification will reappear on the next page reload. "
            "Check the config directory is writable (Docker: confirm volume mount permissions and PUID/PGID).",
            notification_id,
            type(exc).__name__,
            exc,
        )
        return (
            jsonify({"ok": False, "error": "Failed to persist dismissal"}),
            500,
        )
    return jsonify({"ok": True, "id": notification_id, "persisted": True})


@api.route("/system/notifications/reset-dismissed", methods=["POST"])
@setup_or_auth_required
def reset_dismissed_notifications():
    """Clear all permanently-dismissed notifications.

    Exposed as a settings-page action so users who accidentally
    dismissed a notification can bring them back.  Requires auth because
    it modifies persistent settings state.
    """
    from ..notifications import reset_session
    from ..settings_manager import get_settings_manager

    try:
        get_settings_manager().reset_dismissed_notifications()
    except Exception as exc:
        logger.error(
            "Notifications: could not reset your list of dismissed notifications ({}: {}). "
            "Your dismissals are unchanged and the previously-hidden notifications will remain hidden. "
            "Check the config directory is writable (Docker: confirm volume mount permissions and PUID/PGID).",
            type(exc).__name__,
            exc,
        )
        return jsonify({"ok": False, "error": "Failed to reset"}), 500
    reset_session()
    return jsonify({"ok": True})


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
        logger.error(
            "GPU re-scan failed ({}: {}). "
            "The GPU list shown in Settings won't refresh — the previous list is still in effect. "
            "Check the recent log lines above; if your GPU isn't visible to the container, "
            "verify the device is forwarded (Docker: --runtime=nvidia or --device /dev/dri:/dev/dri).",
            type(e).__name__,
            e,
        )
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

        resp = {
            "gpus": gpus,
            "gpu_stats": [],
            "running_job": running_job.to_dict() if running_job else None,
            "pending_jobs": len(job_manager.get_pending_jobs()),
        }
        return jsonify(resp)
    except Exception as e:
        logger.error(
            "Could not load the system status panel for the dashboard ({}: {}). "
            "GPU info and running-job summary won't load until this is resolved — "
            "actual job processing is unaffected. "
            "Check the recent log lines above for the underlying cause.",
            type(e).__name__,
            e,
        )
        return jsonify({"error": "Failed to retrieve system status"}), 500


_media_server_status_cache: dict = {"result": None, "fetched_at": 0.0}
_media_server_status_lock = threading.Lock()
_MEDIA_SERVER_STATUS_TTL = 30  # seconds


def _probe_media_server_entry(entry: dict) -> dict:
    """Probe a single media-server registry entry for the dashboard.

    Returns a wire-friendly summary: id, name, type, enabled flag, url, and
    a coarse ``status`` ("connected" | "unreachable" | "unauthorised" |
    "disabled" | "misconfigured"). Errors are caught and surfaced via
    ``status`` + ``error`` so a single bad server can't break the dashboard.
    """
    from ...servers import server_config_from_dict
    from .api_servers import _instantiate_for_probe

    summary = {
        "id": str(entry.get("id") or ""),
        "name": str(entry.get("name") or ""),
        "type": str(entry.get("type") or "").lower(),
        "enabled": bool(entry.get("enabled", True)),
        "url": str(entry.get("url") or ""),
    }

    if not summary["enabled"]:
        summary["status"] = "disabled"
        return summary

    try:
        cfg = server_config_from_dict(entry)
    except Exception as exc:
        summary["status"] = "misconfigured"
        summary["error"] = str(exc)
        return summary

    try:
        live = _instantiate_for_probe(cfg)
    except Exception as exc:
        summary["status"] = "misconfigured"
        summary["error"] = str(exc)
        return summary
    if live is None:
        summary["status"] = "misconfigured"
        summary["error"] = "no probe client available for this server type"
        return summary

    try:
        result = live.test_connection()
    except Exception as exc:
        summary["status"] = "unreachable"
        summary["error"] = str(exc)
        return summary

    if result.ok:
        summary["status"] = "connected"
        if result.server_id:
            summary["server_id"] = result.server_id
        return summary

    err = (getattr(result, "error", "") or "").lower()
    if "401" in err or "403" in err or "unauth" in err or "forbid" in err:
        summary["status"] = "unauthorised"
    else:
        summary["status"] = "unreachable"
    if getattr(result, "error", ""):
        summary["error"] = result.error
    return summary


@api.route("/system/media-servers")
@setup_or_auth_required
def get_media_servers_status():
    """Per-server reachability summary for the dashboard.

    Returns one row per configured ``media_servers`` entry, each tagged
    with a status string the UI maps to a coloured badge. Cached for 30s
    so a busy dashboard doesn't open a TCP connection per refresh; the
    settings UI can call ``/api/servers/<id>/test-connection`` for an
    immediate probe.
    """
    from ..settings_manager import get_settings_manager

    now = time.time()
    with _media_server_status_lock:
        cached = _media_server_status_cache["result"]
        fetched = _media_server_status_cache["fetched_at"]
        if cached is not None and (now - fetched) < _MEDIA_SERVER_STATUS_TTL:
            return jsonify({"servers": cached, "cached": True, "ttl": _MEDIA_SERVER_STATUS_TTL})

    raw = get_settings_manager().get("media_servers") or []
    entries = list(raw) if isinstance(raw, list) else []

    summaries = [_probe_media_server_entry(e) for e in entries if isinstance(e, dict)]

    with _media_server_status_lock:
        _media_server_status_cache["result"] = summaries
        _media_server_status_cache["fetched_at"] = time.time()

    return jsonify({"servers": summaries, "cached": False, "ttl": _MEDIA_SERVER_STATUS_TTL})


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
                    "ffmpeg_threads": settings.get("ffmpeg_threads", 2),
                }
            )

        resp = {
            "plex_url": config.plex_url or "",
            "plex_token": "****" if config.plex_token else "",
            "plex_config_folder": config.plex_config_folder or "",
            "plex_verify_ssl": config.plex_verify_ssl,
            "plex_local_videos_path_mapping": config.plex_local_videos_path_mapping or "",
            "plex_videos_path_mapping": config.plex_videos_path_mapping or "",
            "thumbnail_interval": config.plex_bif_frame_interval,
            "thumbnail_quality": config.thumbnail_quality,
            "regenerate_thumbnails": config.regenerate_thumbnails,
            "gpu_config": config.gpu_config,
            "gpu_threads": config.gpu_threads,
            "cpu_threads": config.cpu_threads,
            "ffmpeg_threads": config.ffmpeg_threads,
            "log_level": config.log_level,
        }
        if config.gpu_threads == 0 and config.cpu_threads == 0:
            resp["config_warning"] = (
                "No workers configured — jobs will remain pending until GPU or CPU workers are added."
            )
        return jsonify(resp)
    except Exception as e:
        logger.error(
            "Could not load the runtime config for the API ({}: {}). "
            "The /api/system/config endpoint will return an error until this is resolved. "
            "Check the recent log lines above for the underlying cause; "
            "verify settings.json is readable and valid JSON.",
            type(e).__name__,
            e,
        )
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

    elif git_branch.lower().startswith("pr-") and git_sha:
        # PR CI build -- show "PR-123", reference the latest release, no update banner
        install_type = "pr_build"
        pr_num = git_branch.split("-", 1)[1]
        current_version = f"PR-{pr_num}"
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
                    update_available = parse_version(latest_version) > parse_version(current_version)
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
                update_available = parse_version(latest_version) > parse_version(current_version)
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


_GITHUB_RELEASES_URL = "https://api.github.com/repos/stevezau/plex_generate_vid_previews/releases"
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
    if _RELEASES_CACHE["result"] is not None and (now - _RELEASES_CACHE["fetched_at"]) < _RELEASES_CACHE_TTL:
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
        logger.debug("Failed to fetch GitHub releases: {}", e)
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
        verify_ssl = _param_to_bool(request.args.get("verify_ssl"), settings.plex_verify_ssl)

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
                                "display_type": classify_library_type(section.type, agent),
                            }
                        )

                return jsonify({"libraries": libraries})
            except Exception as e:
                logger.error(
                    "Could not load Plex libraries using the saved configuration ({}: {}). "
                    "The library picker will show 'Plex not configured. Complete setup in Settings.' "
                    "Verify the Plex URL and token in Settings, and that Plex is reachable from this app.",
                    type(e).__name__,
                    e,
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
        logger.error(
            "Plex libraries: could not retrieve the library list ({}: {}). "
            "The library picker will show an error until this is fixed. "
            "Check the recent log lines for the underlying cause; "
            "verify the Plex URL/token in Settings and that Plex is reachable.",
            type(e).__name__,
            e,
        )
        return jsonify({"error": f"Failed to retrieve libraries: {e}", "libraries": []}), 500
