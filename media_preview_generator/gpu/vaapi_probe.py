"""VA-API driver probing — ask ``vainfo`` what user-space driver actually
sits under the kernel DRM driver.

Kernel drivers like ``i915`` or ``xe`` say almost nothing about what
VA-API will actually do at runtime: those names map to many different
user-space media drivers (iHD 25.x, iHD 24.x, i965, etc.) that differ
in codec support and bug profile.  ``_probe_vaapi_driver`` runs
``vainfo --display drm --device <render-node>`` and pulls the
``Driver version:`` line so log lines can include the real driver
identity.

Cached for process lifetime — the user-space driver doesn't change at
runtime and multiple startup log sites probe the same device.
"""

from __future__ import annotations

import subprocess
from functools import cache

# Kernel drivers that correspond to Intel GPUs (worth probing vainfo for
# the user-space VA-API driver identity, since the kernel driver name
# alone is misleading — i915/xe sit underneath iHD).
_INTEL_KERNEL_DRIVERS = frozenset({"i915", "xe"})


@cache
def _probe_vaapi_driver(render_device: str) -> str | None:
    """Return the user-space VA-API driver version string for a render node.

    Runs ``vainfo --display drm --device <render_device>`` and extracts
    the ``Driver version:`` line. Returns None on any failure (missing
    binary, timeout, parse failure) so callers can fall back to a
    legacy log format.

    Cached for the lifetime of the process: the underlying VA-API
    driver does not change at runtime, and three log sites probe the
    same device during startup.

    Args:
        render_device: Path to a DRM render node (e.g. ``/dev/dri/renderD128``).

    Returns:
        Optional[str]: The raw driver version string (e.g.
        ``"Intel iHD driver for Intel(R) Gen Graphics - 25.3.4"``) on
        success, or None if the probe could not determine a driver.
    """
    try:
        result = subprocess.run(
            ["vainfo", "--display", "drm", "--device", render_device],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    marker = "Driver version:"
    for line in result.stdout.splitlines():
        idx = line.find(marker)
        if idx == -1:
            continue
        value = line[idx + len(marker) :].strip()
        return value or None
    return None


def _format_driver_label(render_device: str, kernel_driver: str) -> str:
    """Build the parenthesised driver label for a GPU log line.

    For Intel GPUs the label shows both the kernel DRM driver (``i915``
    or ``xe``) and the user-space VA-API driver from ``vainfo``. For
    everything else, or when ``vainfo`` is unavailable, the label falls
    back to the legacy ``driver: <kernel_driver>`` format.

    Args:
        render_device: Render node path (e.g. ``/dev/dri/renderD128``).
        kernel_driver: Kernel driver name read from
            ``/sys/class/drm/cardX/device/driver``.

    Returns:
        str: Label without enclosing parens, suitable for inclusion in
        debug log lines, e.g. ``"kernel driver: i915, va-api driver:
        Intel iHD driver for Intel(R) Gen Graphics - 25.3.4"`` or
        ``"driver: i915"``.
    """
    if kernel_driver in _INTEL_KERNEL_DRIVERS:
        vaapi_driver = _probe_vaapi_driver(render_device)
        if vaapi_driver:
            return f"kernel driver: {kernel_driver}, va-api driver: {vaapi_driver}"
    return f"driver: {kernel_driver}"
