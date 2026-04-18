"""GPU detection, capability probing, and Vulkan-specific tooling.

This package is being carved out of the legacy :mod:`gpu_detection`
monolith.  Today only :mod:`.vulkan_probe` lives here; follow-up
refactors will move the VAAPI driver probe, the FFmpeg capability
checks, and the lspci / DRM enumeration into sibling modules
(``vaapi_probe``, ``ffmpeg_capabilities``, ``enumeration``).

Public API is re-exported so callers can do
``from plex_generate_previews.gpu import get_vulkan_device_info``;
the legacy :mod:`gpu_detection` module also keeps forwarding the same
symbols for anything that still imports them from the old location.
"""

from .vulkan_probe import (
    VulkanProbeResult,
    get_vulkan_debug_buffer,
    get_vulkan_device_info,
    get_vulkan_env_overrides,
)

__all__ = [
    "VulkanProbeResult",
    "get_vulkan_debug_buffer",
    "get_vulkan_device_info",
    "get_vulkan_env_overrides",
]
