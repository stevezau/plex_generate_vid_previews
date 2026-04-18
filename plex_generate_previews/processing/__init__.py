"""Video processing pipeline — filter-chain assembly, FFmpeg execution,
HDR/DV detection, retry orchestration.

Sub-modules:

* :mod:`.hdr_detection`  — pure helpers for detecting DV/HDR content
  and matching FFmpeg stderr signatures on tonemap failure.
* :mod:`.filter_chain`   — builders for the ``-vf`` filter string for
  each vendor / content-format combination. The ``path_kind`` string
  is the single source of truth for which pipeline shape runs.
* :mod:`.ffmpeg_runner`   — subprocess invocation, progress parsing,
  timeout handling.
* :mod:`.retry_cascade`   — classifier helpers for the 4-tier FFmpeg
  retry cascade (skip-frame → sw libplacebo → DV-safe filter → CPU
  fallback).
"""

from .filter_chain import (
    DV5_PATH_INTEL_OPENCL,
    DV5_PATH_LIBPLACEBO,
    DV5_PATH_VAAPI_VULKAN,
    build_dv5_vf,
)
from .hdr_detection import (
    detect_dolby_vision_rpu_error,
    detect_zscale_colorspace_error,
    is_dolby_vision,
    is_dv_no_backward_compat,
)
from .retry_cascade import (
    RetryTier,
    classify_cpu_fallback_reason,
    classify_dv_safe_retry_reason,
)

__all__ = [
    # DV5 filter chain
    "DV5_PATH_INTEL_OPENCL",
    "DV5_PATH_LIBPLACEBO",
    "DV5_PATH_VAAPI_VULKAN",
    "build_dv5_vf",
    # HDR / DV detection
    "detect_dolby_vision_rpu_error",
    "detect_zscale_colorspace_error",
    "is_dolby_vision",
    "is_dv_no_backward_compat",
    # Retry cascade
    "RetryTier",
    "classify_cpu_fallback_reason",
    "classify_dv_safe_retry_reason",
]
