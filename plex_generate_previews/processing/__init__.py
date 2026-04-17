"""Video processing pipeline — filter-chain assembly, FFmpeg execution,
HDR/DV detection, retry orchestration, and BIF packing.

Everything needed to take one video file and produce its thumbnail
index.  Higher-level library/job orchestration lives in
:mod:`job_orchestrator`; the top-level :mod:`media_processing` module
re-exports the public API of this package for backwards compatibility.

Sub-modules:

* :mod:`.hdr_detection`  — pure helpers for detecting DV/HDR content
  and matching FFmpeg stderr signatures on tonemap failure.
* :mod:`.filter_chain`   — builders for the ``-vf`` filter string for
  each vendor / content-format combination.  The ``path_kind`` string
  is the single source of truth for which pipeline shape runs.
* (future) :mod:`.ffmpeg_runner`  — subprocess invocation, progress
  parsing, timeout handling.
* (future) :mod:`.retry_cascade`  — tier-by-tier fallback logic.
* (future) :mod:`.bif_writer`     — pack JPEGs into the Plex BIF index.
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
]
