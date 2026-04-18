"""GPU detection, capability probing, and Vulkan-specific tooling.

Sub-modules:

* :mod:`.detect`              — orchestrator. :func:`detect_all_gpus`
  composes the helpers below into the GPU list used by worker
  configuration and the web UI.
* :mod:`.enumeration`         — platform-specific GPU enumeration
  (Linux DRM, Apple system_profiler, WSL2 detection, lspci / PCI
  resolvers, ``get_gpu_name``, driver→vendor map).
* :mod:`.ffmpeg_capabilities` — FFmpeg version gating + hwaccel
  availability helpers.
* :mod:`.vaapi_probe`         — lru-cached ``vainfo`` driver string
  probe and the ``driver: …`` label formatter.
* :mod:`.vulkan_probe`        — multi-strategy Vulkan device probe
  (NVIDIA EGL-vendor fallback, VK_DRIVER_FILES overrides, software
  rasteriser detection, debug buffer).
"""

from .detect import (
    GPU_ACCELERATION_MAP,
    detect_all_gpus,
    format_gpu_info,
)
from .enumeration import DRIVER_VENDOR_MAP, get_gpu_name
from .ffmpeg_capabilities import MIN_FFMPEG_VERSION
from .vulkan_probe import (
    VulkanProbeResult,
    get_vulkan_debug_buffer,
    get_vulkan_device_info,
    get_vulkan_env_overrides,
)

__all__ = [
    "DRIVER_VENDOR_MAP",
    "GPU_ACCELERATION_MAP",
    "MIN_FFMPEG_VERSION",
    "VulkanProbeResult",
    "detect_all_gpus",
    "format_gpu_info",
    "get_gpu_name",
    "get_vulkan_debug_buffer",
    "get_vulkan_device_info",
    "get_vulkan_env_overrides",
]
