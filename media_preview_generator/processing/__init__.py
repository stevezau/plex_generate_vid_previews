"""Video processing pipeline — filter-chain assembly, FFmpeg execution,
HDR/DV detection, retry orchestration, BIF packing.

Sub-modules:

* :mod:`.orchestrator`   — :func:`generate_images` and
  :func:`process_item` drive the per-item pipeline, including the
  4-tier retry cascade, BIF packing, and failure tracking.
* :mod:`.filter_chain`   — builders for the ``-vf`` filter string for
  each vendor / content-format combination. The ``path_kind`` string
  is the single source of truth for which pipeline shape runs.
* :mod:`.ffmpeg_runner`  — subprocess invocation, progress parsing,
  timeout handling.
* :mod:`.hdr_detection`  — pure helpers for detecting DV/HDR content
  and matching FFmpeg stderr signatures on tonemap failure.
* :mod:`.retry_cascade`  — classifier helpers for the 4-tier FFmpeg
  retry cascade (skip-frame → sw libplacebo → DV-safe filter → CPU
  fallback).

All public and private names from the sub-modules are re-exported here
so `from media_preview_generator.processing import X` resolves.
"""

# Per-vendor processor surface (Phase A of the multi-server completion).
# Concrete VendorProcessor implementations land in Phase B; until then
# only the types + base interface + registry are exported.
from .base import VendorProcessor  # noqa: F401
from .filter_chain import (  # noqa: F401
    DV5_PATH_INTEL_OPENCL,
    DV5_PATH_LIBPLACEBO,
    DV5_PATH_VAAPI_VULKAN,
    build_dv5_vf,
)
from .hdr_detection import (  # noqa: F401
    detect_dolby_vision_rpu_error,
    detect_zscale_colorspace_error,
    is_dolby_vision,
    is_dv_no_backward_compat,
)
from .orchestrator import (  # noqa: F401
    FFMPEG_STALL_TIMEOUT_SEC,
    CancellationError,
    CodecNotSupportedError,
    ProcessingResult,
    _clean_output_images,
    _cleanup_temp_directory,
    _detect_codec_error,
    _detect_hwaccel_runtime_error,
    _diagnose_ffmpeg_exit_code,
    _ensure_directories,
    _extract_ffmpeg_error_summary,
    _generate_and_save_bif,
    _is_signal_killed,
    _notify_file_result,
    _save_ffmpeg_failure_log,
    _setup_bundle_paths,
    _verify_tmp_folder_health,
    clear_failures,
    failure_scope,
    generate_bif,
    generate_images,
    get_failures,
    log_failure_summary,
    parse_ffmpeg_progress_line,
    process_item,
    record_failure,
    set_file_result_callback,
)
from .registry import get_processor_for, register_processor, registered_types  # noqa: F401
from .retry_cascade import (  # noqa: F401
    RetryTier,
    classify_cpu_fallback_reason,
    classify_dv_safe_retry_reason,
)
from .types import ProcessableItem, ScanOutcome  # noqa: F401

# Legacy underscore-prefixed aliases — the HDR helpers used to live in
# ``media_processing`` as private names; keep them importable under the
# old spelling so existing tests / third-party code doesn't break.
_is_dolby_vision = is_dolby_vision
_is_dv_no_backward_compat = is_dv_no_backward_compat
_detect_dolby_vision_rpu_error = detect_dolby_vision_rpu_error
_detect_zscale_colorspace_error = detect_zscale_colorspace_error
