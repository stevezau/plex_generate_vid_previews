"""Shared worker-row labelling so every code path agrees on the same shape.

Three independent code paths build the rows the dashboard's Workers panel
renders:

1. :func:`media_preview_generator.jobs.orchestrator._dispatch_processable_items` —
   the multi-server dispatch loop (Plex full scan, Emby/Jellyfin scans).
2. :class:`media_preview_generator.jobs.worker.WorkerPool` — the legacy Plex
   single-server path (only fires on a pure-Plex install today).
3. :func:`media_preview_generator.web.routes.api_jobs._build_idle_workers_from_config`
   — synthesised idle entries returned by ``GET /api/jobs/workers`` when no
   job is currently active.

Without a shared helper, each path drifted into its own label format and
the panel visibly changed shape between mid-job and idle states (the user
saw "GPU Worker 1 (NVIDIA TITAN RTX)" while a job ran, then
"NVIDIA TITAN RTX #1" the moment the job ended). This module is the single
source of truth so the panel reads identically regardless of which path
populated the row.
"""

from __future__ import annotations

import re

# ``[UHD Graphics 770]`` — the bracketed marketing name lspci-style detection
# embeds in the middle of long Intel iGPU strings like
# "Intel Corporation Raptor Lake-S GT1 [UHD Graphics 770] (rev 04)". Pulling
# this out gives the user-recognisable identifier without dragging the
# vendor / silicon revision noise into a 200px-wide card row.
_BRACKETED = re.compile(r"\[([^\]]+)\]")


def friendly_device_label(gpu_info, gpu_device: str | None, gpu_type: str | None) -> str:
    """Compact human-readable name for a GPU device.

    Examples::

        "NVIDIA TITAN RTX"
            -> "NVIDIA TITAN RTX"
        "Intel Corporation Raptor Lake-S GT1 [UHD Graphics 770] (rev 04)"
            -> "Intel UHD Graphics 770"

    Falls back to the device path, then the type, then ``"GPU"``.
    """
    if isinstance(gpu_info, dict):
        name = (gpu_info.get("name") or "").strip()
    else:
        name = (getattr(gpu_info, "name", "") or "").strip()
    if not name:
        return gpu_device or (gpu_type or "GPU")

    m = _BRACKETED.search(name)
    if m:
        bracketed = m.group(1).strip()
        vendor = (gpu_type or name.split()[0] or "").strip()
        v_low = vendor.lower()
        if v_low == "intel":
            return f"Intel {bracketed}"
        if v_low in ("amd", "radeon"):
            return f"AMD {bracketed}"
        return f"{vendor} {bracketed}" if vendor else bracketed
    return name


def gpu_worker_label(seq: int, device_label: str) -> str:
    """Stable per-job label for a GPU worker row.

    ``seq`` is the per-job GPU-worker counter (1..N), not a global identifier.
    Matches the legacy WorkerPool's ``f"GPU Worker {n} ({device_name})"``
    format that pre-dates the multi-server refactor — keeping it stable means
    long-time users don't see the labels shift around.
    """
    return f"GPU Worker {int(seq)} ({device_label})"


def cpu_worker_label(seq: int) -> str:
    """Stable per-job label for a CPU worker row."""
    return f"CPU Worker {int(seq)}"
