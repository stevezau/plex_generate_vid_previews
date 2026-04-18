"""Multi-strategy Vulkan device probe used by the DV Profile 5 libplacebo path.

Owns every module-level piece of Vulkan detection logic that used to live
in :mod:`gpu_detection`:

* :class:`VulkanProbeResult` — frozen dataclass returned by
  :func:`get_vulkan_device_info`, consumed by
  :mod:`media_processing` and ``web/routes/api_system``.
* Strategy branches 1 / 2 / 2c / 2b / 3 that walk through default
  → ``__EGL_VENDOR_LIBRARY_FILENAMES`` → synthesised GLVND vendor JSON
  → ``VK_DRIVER_FILES`` → ``VK_LOADER_DEBUG=all`` diagnostic capture,
  short-circuiting as soon as a usable hardware device appears.
* NVIDIA-name detection hints used by :func:`_retry_is_useful` to
  avoid accepting a dual-GPU host's Intel iGPU when the caller really
  wanted NVIDIA.
* Cached probe result + env overrides the FFmpeg subprocess needs to
  inherit when it runs libplacebo.

Back-compat: :mod:`gpu_detection` re-exports everything public here so
external callers that did ``from plex_generate_previews.gpu_detection
import get_vulkan_device_info`` keep working.  Private helpers
(underscore-prefixed names) remain module-private here; tests that
monkey-patch them now target ``plex_generate_previews.gpu.vulkan_probe``
instead of the old module path.
"""

from __future__ import annotations

import glob
import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Optional, Tuple

from loguru import logger

_VULKAN_DEVICE_CACHE: Optional[str] = None
_VULKAN_DEVICE_PROBED: bool = False
_VULKAN_ENV_OVERRIDES: dict = {}
_VULKAN_DEBUG_BUFFER: str = ""

# Candidate paths the Vulkan loader searches for the NVIDIA ICD JSON. The
# nvidia-container-toolkit mounts it at /etc/vulkan/icd.d/, but older
# loaders and some distributions only look under /usr/share/vulkan/icd.d/
# (see nvidia-container-toolkit issue #1392). The retry strategy below
# tries each path in order.
_NVIDIA_ICD_JSON_PATHS = (
    "/etc/vulkan/icd.d/nvidia_icd.json",
    "/usr/share/vulkan/icd.d/nvidia_icd.json",
)

# Candidate paths for the GLVND NVIDIA EGL vendor config. nvidia-container-
# toolkit injects this at ``/usr/share/glvnd/egl_vendor.d/10_nvidia.json``
# when the ``graphics`` driver capability is declared; it tells the GLVND
# libEGL dispatcher which vendor library (``libEGL_nvidia.so.0``) to use.
#
# WHY THIS MATTERS for the DV5 Vulkan path:
# NVIDIA's libGLX_nvidia.so.0 is both a GLX backend AND the Vulkan ICD.
# During Vulkan ICD initialisation, its constructor dlopens ``libEGL.so.1``
# for an internal EGL capability probe. On the linuxserver/ffmpeg base
# image, libEGL.so.1 is GLVND's dispatcher (not NVIDIA's own EGL), so the
# probe goes through GLVND's vendor selection. If GLVND has no vendor hint,
# it picks whichever vendor file is first on disk — which on this image is
# Mesa's, not NVIDIA's. The EGL probe then returns a degraded context,
# NVIDIA's ICD silently marks itself unusable, and
# ``vk_icdGetInstanceProcAddr(NULL, "vkCreateInstance")`` returns NULL.
# Result: ``VK_ERROR_INCOMPATIBLE_DRIVER`` and a llvmpipe fallback.
#
# Setting ``__EGL_VENDOR_LIBRARY_FILENAMES=<path-to-10_nvidia.json>`` tells
# GLVND to use the NVIDIA vendor directly, the EGL probe succeeds, and
# libGLX_nvidia's Vulkan ICD wakes up. This is the Strategy-2 fix.
_NVIDIA_EGL_VENDOR_JSON_PATHS = (
    "/usr/share/glvnd/egl_vendor.d/10_nvidia.json",
    "/etc/glvnd/egl_vendor.d/10_nvidia.json",
)

# Cap on the size of the VK_LOADER_DEBUG=all capture buffer. One run of
# the diagnostic probe is typically 5–15 KB of loader trace; 20 KB is a
# comfortable upper bound that still fits in a GitHub issue comment.
_VULKAN_DEBUG_BUFFER_CAP = 20_000


def _is_software_vulkan_device(device: Optional[str]) -> bool:
    """Return True if ``device`` is a software rasterizer (llvmpipe/lavapipe)."""
    if not device:
        return False
    d = device.lower()
    return "llvmpipe" in d or "software" in d or "lavapipe" in d


@dataclass(frozen=True)
class VulkanProbeResult:
    """Structured result of :func:`get_vulkan_device_info`.

    Previously this function returned a plain ``dict`` with implicit
    keys, which made the contract between gpu_detection and its
    callers (api_system, media_processing, notifications) invisible
    to type checkers.  Promoted to a frozen dataclass so downstream
    code gets attribute access and misspellings surface at import
    time.

    Attributes:
        device:      The Vulkan device description string FFmpeg
                     selected (e.g. ``"NVIDIA TITAN RTX (discrete)
                     (0x1e02)"``), or ``None`` if Vulkan is
                     unavailable in the container.
        is_software: True when the selected device is a software
                     rasteriser (``llvmpipe`` / ``lavapipe``), which
                     triggers libplacebo's green-overlay bug on DV5
                     thumbnails and must short-circuit to the DV-safe
                     fps+scale retry.
    """

    device: Optional[str]
    is_software: bool


# Substrings that identify an NVIDIA GPU in FFmpeg's Vulkan device listing.
# Older proprietary drivers print just the marketing name ("Quadro P4000",
# "GeForce RTX 3080"); newer ones include the "NVIDIA" prefix.  We match
# any of these brand strings so both cases are recognised.  None of these
# collide with Intel ("Intel(R) Graphics...") or AMD ("AMD Radeon...") or
# software ("llvmpipe", "lavapipe") device names.
_NVIDIA_DEVICE_NAME_HINTS = (
    "nvidia",
    "geforce",
    "quadro",
    "tesla",
    "titan",
    "rtx",
    "gtx",
)


def _is_nvidia_vulkan_device(device: Optional[str]) -> bool:
    """Return True if ``device`` looks like an NVIDIA GPU name."""
    if not device:
        return False
    d = device.lower()
    return any(hint in d for hint in _NVIDIA_DEVICE_NAME_HINTS)


def _find_nvidia_icd_json() -> Optional[str]:
    """Return the path to ``nvidia_icd.json`` if present at a standard location.

    Checks both the nvidia-container-toolkit mount path
    (``/etc/vulkan/icd.d/``) and the loader's legacy search path
    (``/usr/share/vulkan/icd.d/``). Returns the first match or None.
    """
    for path in _NVIDIA_ICD_JSON_PATHS:
        if os.path.exists(path):
            return path
    return None


def _find_nvidia_egl_vendor_json() -> Optional[str]:
    """Return the path to the GLVND NVIDIA EGL vendor JSON if present.

    Checks both the nvidia-container-toolkit mount path
    (``/usr/share/glvnd/egl_vendor.d/``) and the per-host override
    (``/etc/glvnd/egl_vendor.d/``). Returns the first match or None.
    """
    for path in _NVIDIA_EGL_VENDOR_JSON_PATHS:
        if os.path.exists(path):
            return path
    return None


# Glob patterns for ``libEGL_nvidia.so*``. nvidia-container-toolkit mounts
# this library when the ``graphics`` driver capability is declared, and
# Strategy 2c (below) needs to know whether it's present before trying to
# route GLVND's libEGL lookup at it. If it isn't mounted, synthesising a
# ``10_nvidia.json`` that points at ``libEGL_nvidia.so.0`` is a dead end.
_LIBEGL_NVIDIA_GLOBS = (
    "/usr/lib/x86_64-linux-gnu/libEGL_nvidia.so*",
    "/usr/lib/aarch64-linux-gnu/libEGL_nvidia.so*",
    "/usr/lib/libEGL_nvidia.so*",
    "/usr/lib64/libEGL_nvidia.so*",
)


def _find_libegl_nvidia() -> Optional[str]:
    """Return the first path to ``libEGL_nvidia.so*`` if the library is present.

    Searches the standard Debian multiarch paths plus the classic
    ``/usr/lib`` / ``/usr/lib64`` fallbacks that nvidia-container-toolkit
    uses when mounting the ``graphics`` capability.  Returns the first
    match or None.  Used by :func:`_probe_vulkan_device` Strategy 2c to
    decide whether synthesising a GLVND vendor JSON is useful: if the
    library is absent, GLVND would route to a file that doesn't exist
    and the fix would no-op.
    """
    for pattern in _LIBEGL_NVIDIA_GLOBS:
        matches = glob.glob(pattern)
        if matches:
            return matches[0]
    return None


def _run_vulkan_probe(
    env_overrides: Optional[dict] = None,
) -> Tuple[Optional[str], str]:
    """Run a single Vulkan init probe and return ``(device, full_stderr)``.

    Runs a trivial FFmpeg command with ``-init_hw_device vulkan=vk`` at
    ``-loglevel debug`` and parses the ``Device N selected:`` line emitted
    by FFmpeg's Vulkan hwcontext. Returns the parsed device name (or None
    on miss/failure) and the full stderr for optional downstream use
    (e.g. a ``VK_LOADER_DEBUG=all`` diagnostic capture).

    Args:
        env_overrides: Optional env vars to merge into the subprocess
            environment. Used by the Layer-3 retry strategy to force
            ``VK_DRIVER_FILES`` and/or enable ``VK_LOADER_DEBUG=all``.
    """
    # Lazy import to avoid a circular dependency during module load — the
    # parent `gpu_detection` shim currently imports from this module.  Once
    # FFmpeg-capability detection itself moves into :mod:`gpu.ffmpeg_capabilities`
    # this can be a normal top-of-file import.
    from ..gpu_detection import _is_hwaccel_available

    if not _is_hwaccel_available("vulkan"):
        # DEBUG only: get_vulkan_device_info() will log a single
        # user-facing INFO line summarising the final outcome. Logging
        # here would fire once per retry strategy (up to 4 times) and
        # clutter the startup log on FFmpeg builds without Vulkan.
        logger.debug(
            "Vulkan probe: FFmpeg was built without Vulkan hwaccel support; "
            "libplacebo DV Profile 5 tone mapping will run in software."
        )
        return None, ""
    cmd = [
        "ffmpeg",
        "-loglevel",
        "debug",
        "-init_hw_device",
        "vulkan=vk",
        "-f",
        "lavfi",
        "-i",
        "nullsrc",
        "-frames:v",
        "1",
        "-f",
        "null",
        "-",
    ]
    env = None
    if env_overrides:
        env = os.environ.copy()
        env.update(env_overrides)
    logger.debug(
        f"Vulkan probe: running {' '.join(cmd)}"
        + (f" with env overrides {env_overrides}" if env_overrides else "")
    )
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        logger.warning(
            f"Vulkan probe failed (subprocess error: {exc}); "
            "falling back to 'no Vulkan device' for DV5 diagnosis."
        )
        return None, str(exc)
    except Exception as exc:
        logger.warning(
            f"Vulkan probe raised unexpected exception: {exc}; "
            "falling back to 'no Vulkan device' for DV5 diagnosis."
        )
        return None, str(exc)
    stderr = result.stderr or ""
    for line in stderr.splitlines():
        # Matches e.g. "[Vulkan @ 0x...] Device 0 selected: Intel(R) Graphics (RPL-S) (integrated) (0xa780)"
        # or         "[Vulkan @ 0x...] Device 0 selected: llvmpipe (LLVM 18.1.3, 256 bits) (software) (0x0)"
        if "Device" in line and "selected:" in line:
            return line.split("selected:", 1)[1].strip(), stderr
    return None, stderr


def _probe_vulkan_device() -> Optional[str]:
    """Return the Vulkan device libplacebo will use, running up to three strategies.

    **Strategy 1** — default probe with inherited environment. Lets the
    Vulkan loader pick whichever ICD it wants. This is the happy path
    for correctly-configured hosts.

    **Strategy 2** — if Strategy 1 returned a software rasterizer
    (``llvmpipe``/``lavapipe``) or nothing AND the GLVND NVIDIA EGL
    vendor JSON is present, retry the probe with
    ``__EGL_VENDOR_LIBRARY_FILENAMES`` pointing at it. See the comment
    on :data:`_NVIDIA_EGL_VENDOR_JSON_PATHS` above for the full
    mechanism; the short version is that NVIDIA's
    ``libGLX_nvidia.so.0`` runs a ``dlopen("libEGL.so.1")`` probe
    during Vulkan ICD init, GLVND picks the Mesa vendor by default on
    the linuxserver/ffmpeg base image, and that causes NVIDIA's
    ``vk_icdGetInstanceProcAddr`` to return NULL for
    ``vkCreateInstance``. The env var forces GLVND to pick the NVIDIA
    vendor, and the ICD wakes up. Verified empirically on an NVIDIA
    TITAN RTX + driver 590.48.01 + linuxserver/ffmpeg 8.0.1-cli-ls56.

    **Strategy 2b** — if the EGL vendor JSON is not present but the
    NVIDIA ICD JSON is (or if Strategy 2 ran but did not fix things),
    fall back to forcing ``VK_DRIVER_FILES`` at the NVIDIA ICD. This
    is the older heuristic; kept as a secondary because some
    nvidia-container-toolkit releases inject the ICD JSON but not the
    GLVND vendor config (see
    `nvidia-container-toolkit#1559 <https://github.com/NVIDIA/nvidia-container-toolkit/issues/1559>`_).
    If the forced probe succeeds, the env override is stashed in
    ``_VULKAN_ENV_OVERRIDES`` so :func:`get_vulkan_env_overrides` can
    feed it into the real FFmpeg invocation on the libplacebo path.

    **Strategy 3** — if 1 and both 2 branches failed, run one final
    probe with ``VK_LOADER_DEBUG=all`` (plus whichever env vars are
    available) and capture the full stderr into
    ``_VULKAN_DEBUG_BUFFER`` so users can copy-paste it into a GitHub
    issue via ``GET /api/system/vulkan/debug``. The strategy does NOT
    attempt to return a device from the diagnostic probe — it just
    captures the trace for human diagnosis.

    Returns:
        The Vulkan device description the libplacebo path will actually
        use (Strategy 1 or Strategy 2 success), the software rasterizer
        from Strategy 1 if nothing else worked, or ``None`` if Vulkan is
        completely unavailable.
    """
    global _VULKAN_ENV_OVERRIDES, _VULKAN_DEBUG_BUFFER

    # Strategy 1: default probe.
    device, _ = _run_vulkan_probe()

    # Fast-path short-circuit for the overwhelmingly common case:
    # strategy 1 returned NVIDIA or a non-NVIDIA hardware device and
    # there's no NVIDIA ICD JSON on the system to even consider.
    # Only touch the file-discovery helpers when we actually need
    # them for the dual-GPU fall-through decision below.
    if device and not _is_software_vulkan_device(device):
        # On dual-GPU hosts where the Vulkan loader enumerates Intel
        # ANV first (common under --runtime=nvidia: Mesa's Intel ICD is
        # cheap to load, NVIDIA's is gated behind a working EGL init),
        # strategy 1 returns Intel even though NVIDIA Vulkan is fully
        # functional once VK_DRIVER_FILES + __EGL_VENDOR_LIBRARY_FILENAMES
        # are set together (strategy 2b below).  Accepting Intel here
        # routes NVIDIA-worker libplacebo work onto the Intel iGPU,
        # stealing cycles from Intel workers and dropping NVIDIA DV5
        # speed ~40% from the cross-GPU frame shuttle.  So when the
        # NVIDIA ICD is present but strategy 1 didn't select NVIDIA,
        # fall through to the NVIDIA-specific retries.
        if _is_nvidia_vulkan_device(device) or _find_nvidia_icd_json() is None:
            logger.debug(
                f"Vulkan probe (strategy 1): FFmpeg selected hardware device: {device}"
            )
            return device
        logger.debug(
            f"Vulkan probe (strategy 1): selected {device!r} but NVIDIA "
            f"ICD is also present; falling through to NVIDIA-specific "
            f"retries to avoid cross-GPU libplacebo on NVIDIA workers."
        )
    elif device:
        logger.debug(
            f"Vulkan probe (strategy 1): got software device {device!r}; "
            "will attempt NVIDIA-specific retries"
        )
    else:
        logger.debug(
            "Vulkan probe (strategy 1): no 'Device N selected:' line; "
            "will attempt NVIDIA-specific retries"
        )

    nvidia_egl_vendor = _find_nvidia_egl_vendor_json()
    nvidia_icd = _find_nvidia_icd_json()

    # When NVIDIA ICD is present, strategies 2 and 2c should only
    # consider a retry a "success" if it actually returned NVIDIA.  On
    # dual-GPU hosts the EGL-only retry can still return Intel (Mesa
    # enumerates first); if we accept it here we miss strategy 2b which
    # is the combination that actually gets NVIDIA.
    require_nvidia = nvidia_icd is not None

    def _retry_is_useful(retry_device: Optional[str]) -> bool:
        if not retry_device or _is_software_vulkan_device(retry_device):
            return False
        if require_nvidia and not _is_nvidia_vulkan_device(retry_device):
            return False
        return True

    # Strategy 2: point GLVND at NVIDIA's EGL vendor via
    # __EGL_VENDOR_LIBRARY_FILENAMES. This is the verified fix for the
    # linuxserver/ffmpeg + NVIDIA case — see the doc comment on
    # _NVIDIA_EGL_VENDOR_JSON_PATHS above.  Unlike Strategy 2b below,
    # Strategy 2 does NOT set VK_DRIVER_FILES — it leaves the Vulkan
    # loader free to enumerate all ICDs, which is a gentler fix that
    # still permits Mesa fallback on hosts where NVIDIA Vulkan breaks
    # mid-run.  _retry_is_useful() on dual-GPU hosts with an NVIDIA ICD
    # present filters out non-NVIDIA hits so we still fall through to
    # Strategy 2b in that case.
    if nvidia_egl_vendor:
        logger.debug(
            f"Vulkan probe (strategy 2): forcing "
            f"__EGL_VENDOR_LIBRARY_FILENAMES={nvidia_egl_vendor}"
        )
        retry_env = {"__EGL_VENDOR_LIBRARY_FILENAMES": nvidia_egl_vendor}
        retry_device, _ = _run_vulkan_probe(retry_env)
        if _retry_is_useful(retry_device):
            logger.debug(
                f"Vulkan probe (strategy 2): success with {retry_device!r} "
                f"via __EGL_VENDOR_LIBRARY_FILENAMES={nvidia_egl_vendor}"
            )
            _VULKAN_ENV_OVERRIDES = dict(retry_env)
            return retry_device
        logger.debug(
            f"Vulkan probe (strategy 2): forcing "
            f"__EGL_VENDOR_LIBRARY_FILENAMES={nvidia_egl_vendor} "
            f"still returned {retry_device!r}; trying Strategy 2b."
        )
    else:
        logger.debug(
            "Vulkan probe: no NVIDIA GLVND EGL vendor JSON found at "
            f"{_NVIDIA_EGL_VENDOR_JSON_PATHS}; skipping Strategy 2."
        )

    # Strategy 2c: synthesise a GLVND NVIDIA EGL vendor JSON into a
    # temp file when one doesn't exist on disk AND ``libEGL_nvidia.so``
    # is present in the container.  This is the fix for users whose
    # ``nvidia-container-toolkit`` mounts the NVIDIA libraries (ICD,
    # libEGL_nvidia, libGLX_nvidia, the glvkspirv SPIR-V compiler, ...)
    # but does NOT mount the single tiny ``10_nvidia.json`` GLVND vendor
    # config that tells the libEGL dispatcher which vendor library to
    # hand out.  Without that file, GLVND picks whichever vendor config
    # is first on disk — which is Mesa's on the linuxserver/ffmpeg image
    # — the libGLX_nvidia init-time EGL probe gets a Mesa context, and
    # NVIDIA's Vulkan ICD quietly marks itself unusable.
    #
    # NVIDIA's own "minimal Docker Vulkan offscreen setup" guidance on
    # forums.developer.nvidia.com (thread id 242883) confirms that the
    # GLVND vendor JSON is required and that it is a three-line file:
    #
    #     {"file_format_version":"1.0.0",
    #      "ICD":{"library_path":"libEGL_nvidia.so.0"}}
    #
    # We write that verbatim to ``{tempdir}/plex_previews_nvidia_egl_
    # vendor.json`` and set ``__EGL_VENDOR_LIBRARY_FILENAMES`` at it.
    # The library_path stays bare so the dynamic loader resolves it via
    # the standard search path (exactly what NVIDIA's own Dockerfile
    # does).  Gated on ``libEGL_nvidia.so*`` actually being present in
    # the container so we don't fabricate a pointer to a file that
    # doesn't exist.
    if nvidia_egl_vendor is None:
        libegl_nvidia = _find_libegl_nvidia()
        if libegl_nvidia:
            synth_vendor_path = os.path.join(
                tempfile.gettempdir(), "plex_previews_nvidia_egl_vendor.json"
            )
            synth_payload = {
                "file_format_version": "1.0.0",
                "ICD": {"library_path": "libEGL_nvidia.so.0"},
            }
            try:
                with open(synth_vendor_path, "w", encoding="utf-8") as fh:
                    json.dump(synth_payload, fh)
                logger.debug(
                    f"Vulkan probe (strategy 2c): synthesised GLVND NVIDIA "
                    f"EGL vendor JSON at {synth_vendor_path} "
                    f"(libEGL_nvidia.so found at {libegl_nvidia}); retrying probe"
                )
                retry_env = {"__EGL_VENDOR_LIBRARY_FILENAMES": synth_vendor_path}
                retry_device, _ = _run_vulkan_probe(retry_env)
                if _retry_is_useful(retry_device):
                    logger.info(
                        f"Vulkan probe (strategy 2c): success with "
                        f"{retry_device!r} via synthesised GLVND vendor JSON "
                        f"at {synth_vendor_path}"
                    )
                    _VULKAN_ENV_OVERRIDES = dict(retry_env)
                    return retry_device
                logger.debug(
                    f"Vulkan probe (strategy 2c): synthesised vendor JSON "
                    f"probe still returned {retry_device!r}; trying Strategy 2b."
                )
            except OSError as exc:
                logger.debug(
                    f"Vulkan probe (strategy 2c): could not write "
                    f"{synth_vendor_path}: {exc}; trying Strategy 2b."
                )
        else:
            logger.debug(
                "Vulkan probe: no libEGL_nvidia.so* found in standard "
                f"library paths ({_LIBEGL_NVIDIA_GLOBS}); skipping Strategy 2c."
            )

    # Strategy 2b: older heuristic — force VK_DRIVER_FILES at the NVIDIA
    # ICD. Kept for the case where the ICD JSON is injected but the EGL
    # vendor config is not (nvidia-container-toolkit#1559 / partial CDI
    # manifests), and for general belt-and-suspenders coverage.
    if nvidia_icd:
        logger.debug(
            f"Vulkan probe (strategy 2b): forcing VK_DRIVER_FILES={nvidia_icd}"
        )
        # If Strategy 2 ran and found an EGL vendor, carry it through
        # the 2b retry as well so the two fixes stack.
        retry_env = {"VK_DRIVER_FILES": nvidia_icd}
        if nvidia_egl_vendor:
            retry_env["__EGL_VENDOR_LIBRARY_FILENAMES"] = nvidia_egl_vendor
        retry_device, _ = _run_vulkan_probe(retry_env)
        if _retry_is_useful(retry_device):
            logger.debug(
                f"Vulkan probe (strategy 2b): success with {retry_device!r} "
                f"via {retry_env}"
            )
            _VULKAN_ENV_OVERRIDES = dict(retry_env)
            return retry_device
        logger.debug(
            f"Vulkan probe (strategy 2b): forcing VK_DRIVER_FILES={nvidia_icd} "
            f"still returned {retry_device!r}; running diagnostic capture."
        )
    else:
        logger.debug(
            "Vulkan probe: no NVIDIA ICD JSON found at "
            f"{_NVIDIA_ICD_JSON_PATHS}; skipping Strategy 2b."
        )

    # Strategy 3: VK_LOADER_DEBUG=all capture for issue reports.
    diag_overrides: dict = {"VK_LOADER_DEBUG": "all"}
    if nvidia_egl_vendor:
        diag_overrides["__EGL_VENDOR_LIBRARY_FILENAMES"] = nvidia_egl_vendor
    if nvidia_icd:
        diag_overrides["VK_DRIVER_FILES"] = nvidia_icd
    _, diag_stderr = _run_vulkan_probe(diag_overrides)
    _VULKAN_DEBUG_BUFFER = (diag_stderr or "")[-_VULKAN_DEBUG_BUFFER_CAP:]
    # One-line WARNING at Strategy-3 exit is fine because it only runs
    # once per probe (first call to get_vulkan_device_info), and only
    # when everything else has already failed — i.e. the user DOES have
    # a real problem worth seeing in the main log.
    logger.warning(
        f"Vulkan probe: all strategies exhausted. Captured "
        f"{len(_VULKAN_DEBUG_BUFFER)} bytes of VK_LOADER_DEBUG=all output "
        "for issue reports (GET /api/system/vulkan/debug)."
    )
    if _VULKAN_DEBUG_BUFFER:
        # Surface the last few informative lines to the main log at
        # DEBUG level so a user reading `docker logs --tail` in the
        # normal INFO flow isn't overwhelmed, but issue reporters with
        # LOG_LEVEL=DEBUG get the immediate hint without hitting the
        # dashboard debug endpoint.
        for line in _VULKAN_DEBUG_BUFFER.splitlines()[-15:]:
            logger.debug(f"  ffmpeg/vulkan-loader stderr: {line}")

    # Return whatever Strategy 1 found so `get_vulkan_device_info` can
    # correctly classify it as software (or None) and render the banner.
    return device


def get_vulkan_device_info() -> VulkanProbeResult:
    """Return cached Vulkan device info for libplacebo diagnostics.

    The underlying probe (including Strategy-2 retry and Strategy-3
    diagnostic capture) is cached across calls at module level because
    it runs subprocesses and its result does not change during the
    app's lifetime — the container's Vulkan environment is fixed at
    startup.

    Returns:
        VulkanProbeResult: A frozen dataclass with ``device`` (the
            Vulkan device description string, or ``None`` if Vulkan is
            unavailable) and ``is_software`` (True when the selected
            device is a software rasteriser like ``llvmpipe`` /
            ``lavapipe``, which triggers the DV5 green overlay bug in
            libplacebo).  Callers assemble the user-facing warning
            message themselves.
    """
    global _VULKAN_DEVICE_CACHE, _VULKAN_DEVICE_PROBED
    if not _VULKAN_DEVICE_PROBED:
        logger.debug("Vulkan device info: running first-time probe")
        _VULKAN_DEVICE_CACHE = _probe_vulkan_device()
        _VULKAN_DEVICE_PROBED = True

        # First-time probe finished — log the outcome exactly once.
        # Every subsequent call returns the cached dict silently. The
        # three branches below are intentionally mutually exclusive:
        #   - INFO on success (single line, user-friendly)
        #   - WARNING on software fallback (action needed)
        #   - INFO on no-Vulkan (informational, harmless)
        probe_device = _VULKAN_DEVICE_CACHE
        if probe_device is None:
            logger.info(
                "Vulkan not available in this container; Dolby Vision "
                "Profile 5 thumbnails will render in software. Non-DV5 "
                "content is unaffected."
            )
        elif _is_software_vulkan_device(probe_device):
            logger.warning(
                f"Vulkan probe selected a software rasterizer "
                f"({probe_device}); Dolby Vision Profile 5 thumbnails "
                "will show a green overlay. Open the dashboard or "
                "GET /api/system/vulkan/debug for GPU-specific "
                "remediation steps and a full diagnostic bundle."
            )
        else:
            via = ""
            if _VULKAN_ENV_OVERRIDES:
                override_keys = ", ".join(sorted(_VULKAN_ENV_OVERRIDES))
                via = f" (via {override_keys} override)"
            logger.debug(
                f"Vulkan ready for Dolby Vision Profile 5 tone-mapping: "
                f"{probe_device}{via}"
            )

    device = _VULKAN_DEVICE_CACHE
    if device is None:
        return VulkanProbeResult(device=None, is_software=False)
    return VulkanProbeResult(
        device=device,
        is_software=_is_software_vulkan_device(device),
    )


def get_vulkan_env_overrides() -> dict:
    """Return env vars to inject into FFmpeg subprocess calls on the libplacebo path.

    Populated by the Strategy-2 (or Strategy-2b) retry in
    :func:`_probe_vulkan_device` when the default Vulkan ICD search
    did not yield a hardware device. Returns an empty dict when no
    overrides are needed (happy path: the loader finds the right ICD
    on its own).

    **Side effect by design:** if the probe has not yet run (e.g. the
    worker thread calling from :func:`media_processing._run_ffmpeg`
    on the libplacebo DV Profile 5 path beats the first
    ``/api/system/vulkan`` poll), this function triggers the probe
    synchronously via :func:`get_vulkan_device_info` before returning.
    Without this auto-trigger, any job that starts before an HTTP
    endpoint is hit would get an empty override dict and would fall
    back to software Vulkan even when the retry would have fixed it.
    """
    if not _VULKAN_DEVICE_PROBED:
        get_vulkan_device_info()
    return dict(_VULKAN_ENV_OVERRIDES)


def get_vulkan_debug_buffer() -> str:
    """Return the captured ``VK_LOADER_DEBUG=all`` stderr from the last probe.

    Populated by Strategy 3 in :func:`_probe_vulkan_device` when both
    the default probe and the ``VK_DRIVER_FILES`` retry failed. Empty
    string when no diagnostic capture was needed. Consumed by the
    ``GET /api/system/vulkan/debug`` endpoint and the "Copy diagnostic
    bundle" button on the dashboard/settings warning banner.

    Same auto-trigger behaviour as :func:`get_vulkan_env_overrides`:
    if the probe has not yet run, it runs synchronously first so the
    caller never sees an empty buffer just because no HTTP endpoint
    has been hit yet.
    """
    if not _VULKAN_DEVICE_PROBED:
        get_vulkan_device_info()
    return _VULKAN_DEBUG_BUFFER


def _reset_vulkan_device_cache() -> None:
    """Testing hook: clear the cached Vulkan probe result and diagnostic state.

    Only intended for unit tests that need to rerun the probe with a
    different mock. Clears all four module-level globals so a new
    probe strategy run starts fresh.
    """
    global _VULKAN_DEVICE_CACHE, _VULKAN_DEVICE_PROBED
    global _VULKAN_ENV_OVERRIDES, _VULKAN_DEBUG_BUFFER
    _VULKAN_DEVICE_CACHE = None
    _VULKAN_DEVICE_PROBED = False
    _VULKAN_ENV_OVERRIDES = {}
    _VULKAN_DEBUG_BUFFER = ""
