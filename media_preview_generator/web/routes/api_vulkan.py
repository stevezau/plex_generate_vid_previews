"""Vulkan diagnostics API routes.

Hosts the ``/system/vulkan`` and ``/system/vulkan/debug`` endpoints used by
the dashboard's "Dolby Vision Profile 5 green-overlay" warning banner and
the GitHub-issue-bundle copy button. Split out of ``api_system.py`` because
~640 lines of NVIDIA-ICD-path probing and loader-debug capture are
unrelated to the rest of the system endpoints (status / config / health /
log history) and were drowning them in a single 1.8K-LOC file.

Talks to :mod:`media_preview_generator.gpu.vulkan_probe` for the actual
Vulkan device probe; everything in this module is presentation +
diagnosis (mapping the probe result into a user-actionable warning).
"""

import glob
import html as html_escape
import os

from flask import jsonify
from loguru import logger

from . import api
from ._helpers import _ensure_gpu_cache, _gpu_cache, _gpu_cache_lock

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
        "=== media_preview_generator Vulkan diagnostic bundle ===",
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
