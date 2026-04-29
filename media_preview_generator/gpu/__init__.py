"""GPU detection, capability probing, and Vulkan-specific tooling.

Sub-modules:

* :mod:`.detect`              — orchestrator. :func:`detect_all_gpus`
  composes the helpers below into the GPU list used by worker
  configuration and the web UI.
* :mod:`.enumeration`         — platform-specific GPU enumeration
  (Linux DRM, Apple system_profiler, WSL2 detection, lspci / PCI
  resolvers, :func:`get_gpu_name`, driver→vendor map).
* :mod:`.ffmpeg_capabilities` — FFmpeg version gating + hwaccel
  availability helpers.
* :mod:`.vaapi_probe`         — lru-cached ``vainfo`` driver string
  probe and the ``driver: …`` label formatter.
* :mod:`.vulkan_probe`        — multi-strategy Vulkan device probe
  (NVIDIA EGL-vendor fallback, VK_DRIVER_FILES overrides, software
  rasteriser detection, debug buffer).

All public and private names from the sub-modules are re-exported here
so `from media_preview_generator.gpu import X` resolves.
"""

from .detect import (  # noqa: F401
    GPU_ACCELERATION_MAP,
    _build_gpu_error_detail,
    _check_device_access,
    _detect_linux_gpus,
    _detect_macos_gpus,
    _detect_windows_gpus,
    _test_acceleration_method,
    _test_hwaccel_functionality,
    detect_all_gpus,
    format_gpu_info,
)
from .enumeration import (  # noqa: F401
    DRIVER_VENDOR_MAP,
    _detect_gpu_type_from_lspci,
    _detect_nvidia_via_nvidia_smi,
    _enumerate_nvidia_gpus_via_smi,
    _get_apple_gpu_name,
    _get_gpu_devices,
    _get_gpu_vendor_from_driver,
    _get_lspci_device_name_for_pci_address,
    _get_pci_address_from_drm_device,
    _is_wsl2,
    _log_system_info,
    _parse_lspci_gpu_name,
    _scan_dev_dri_render_devices,
    get_gpu_name,
)
from .ffmpeg_capabilities import (  # noqa: F401
    MIN_FFMPEG_VERSION,
    _check_ffmpeg_version,
    _get_ffmpeg_hwaccels,
    _get_ffmpeg_version,
    _is_hwaccel_available,
)
from .vaapi_probe import (  # noqa: F401
    _INTEL_KERNEL_DRIVERS,
    _format_driver_label,
    _probe_vaapi_driver,
)
from .vulkan_probe import (  # noqa: F401
    VulkanProbeResult,
    _find_libegl_nvidia,
    _find_nvidia_egl_vendor_json,
    _find_nvidia_icd_json,
    _is_nvidia_vulkan_device,
    _is_software_vulkan_device,
    _probe_vulkan_device,
    _reset_vulkan_device_cache,
    _run_vulkan_probe,
    get_vulkan_debug_buffer,
    get_vulkan_device_info,
    get_vulkan_env_overrides,
)
