"""Legacy import shim — forwards to :mod:`plex_generate_previews.gpu`.

The GPU detection code moved into the :mod:`.gpu` subpackage. New code
should import from there directly; this module re-exports every public
and private name so existing callers and test patches of
``plex_generate_previews.gpu_detection.X`` keep resolving.
"""

from .gpu.detect import (  # noqa: F401
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
from .gpu.enumeration import (  # noqa: F401
    DRIVER_VENDOR_MAP,
    _detect_gpu_type_from_lspci,
    _detect_nvidia_via_nvidia_smi,
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
from .gpu.ffmpeg_capabilities import (  # noqa: F401
    MIN_FFMPEG_VERSION,
    _check_ffmpeg_version,
    _get_ffmpeg_hwaccels,
    _get_ffmpeg_version,
    _is_hwaccel_available,
)
from .gpu.vaapi_probe import (  # noqa: F401
    _INTEL_KERNEL_DRIVERS,
    _format_driver_label,
    _probe_vaapi_driver,
)
from .gpu.vulkan_probe import (  # noqa: F401
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
