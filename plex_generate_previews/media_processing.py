"""Legacy import shim — forwards to :mod:`plex_generate_previews.processing.orchestrator`.

The media-processing orchestrator (FFmpeg execution, failure tracking,
BIF generation, :func:`generate_images` + :func:`process_item`) moved
into the :mod:`.processing` subpackage. New code should import from
:mod:`.processing.orchestrator` or :mod:`.processing` directly; this
module re-exports the full public + private surface so existing
``from plex_generate_previews.media_processing import X`` callers and
their test patches keep working.
"""

# Re-export everything the old module exposed.
from .processing.filter_chain import (  # noqa: F401
    DV5_PATH_INTEL_OPENCL,
    DV5_PATH_LIBPLACEBO,
    DV5_PATH_VAAPI_VULKAN,
    build_dv5_vf,
)
from .processing.hdr_detection import (  # noqa: F401
    detect_dolby_vision_rpu_error,
    detect_zscale_colorspace_error,
    is_dolby_vision,
    is_dv_no_backward_compat,
)
from .processing.orchestrator import (  # noqa: F401
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

# Legacy underscore-prefixed aliases kept for external callers that
# still import the private names from before the hdr_detection split.
_is_dolby_vision = is_dolby_vision
_is_dv_no_backward_compat = is_dv_no_backward_compat
_detect_dolby_vision_rpu_error = detect_dolby_vision_rpu_error
_detect_zscale_colorspace_error = detect_zscale_colorspace_error

__all__ = [
    "DV5_PATH_INTEL_OPENCL",
    "DV5_PATH_LIBPLACEBO",
    "DV5_PATH_VAAPI_VULKAN",
    "FFMPEG_STALL_TIMEOUT_SEC",
    "CancellationError",
    "CodecNotSupportedError",
    "ProcessingResult",
    "_detect_dolby_vision_rpu_error",
    "_detect_zscale_colorspace_error",
    "_is_dolby_vision",
    "_is_dv_no_backward_compat",
    "build_dv5_vf",
    "clear_failures",
    "detect_dolby_vision_rpu_error",
    "detect_zscale_colorspace_error",
    "failure_scope",
    "generate_bif",
    "generate_images",
    "get_failures",
    "is_dolby_vision",
    "is_dv_no_backward_compat",
    "log_failure_summary",
    "parse_ffmpeg_progress_line",
    "process_item",
    "record_failure",
    "set_file_result_callback",
]
