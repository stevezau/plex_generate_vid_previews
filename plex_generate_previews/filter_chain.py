"""FFmpeg ``-vf`` filter-chain builders for the DV5 tone-map paths.

Today this module only owns the DV Profile 5 builder; the SDR / HDR10
/ DV Profile 7+8 chains are still assembled inline in
:func:`media_processing.generate_images` because they depend on the
nested ``_gpu_scale_segment`` helper's captured state.  When that
closure is promoted in the upcoming :mod:`ffmpeg_runner` extraction
(Tier 3 PR B), the rest of the chain builders should move here too.

Everything is re-exported from :mod:`media_processing` for backwards
compatibility.
"""

from __future__ import annotations

# Recognised DV5 filter-chain kinds.  Each maps to a specific hardware-path
# produced by :func:`build_dv5_vf`.  Kept as plain strings (not an Enum) so
# existing callers comparing against ``path_kind`` continue to work.
DV5_PATH_INTEL_OPENCL = "opencl_dv5_intel"
DV5_PATH_VAAPI_VULKAN = "libplacebo_vaapi"
DV5_PATH_LIBPLACEBO = "libplacebo_dv5"


def build_dv5_vf(
    path_kind: str,
    tonemap_algorithm: str,
    fps_value: float,
    base_scale: str,
) -> str:
    """Return the ``-vf`` filter string for a DV Profile 5 thumbnail run.

    Three vendor variants share the same high-level shape:

        fps-drop → upload-to-GPU → tonemap → hwdownload → format → scale

    but differ in the GPU hop and tonemap filter:

    * ``opencl_dv5_intel``   – VAAPI → OpenCL hwmap, ``tonemap_opencl``
      (Jellyfin's DV-aware patch).  The only path that handles DV5 RPU on
      an Intel iGPU without spilling to libplacebo on a different device.
    * ``libplacebo_vaapi``   – VAAPI → Vulkan hwmap (DMA-BUF), libplacebo.
      Used for AMD Radeon and (historically) Intel before we moved Intel
      to OpenCL because Mesa ANV's Vulkan interop is broken on DV5.
    * ``libplacebo_dv5``     – CPU → Vulkan hwupload, libplacebo.  Used
      for NVIDIA (where CUDA→Vulkan interop is not available in FFmpeg)
      and for the software-decode + libplacebo retry fallback.

    ``fps`` is placed first across all variants, which is:
      * required on NVIDIA Turing (fps-inside-libplacebo exhausts the
        Vulkan image allocator with VK_ERROR_OUT_OF_DEVICE_MEMORY at
        4K p010 — see commit 70cba4e);
      * significantly faster on Intel OpenCL (17× vs 2.7× measured); and
      * RPU-safe because fps only drops frames on timestamps — it never
        touches pixel data or side-data.

    ``contrast=1.3, saturation=1.3`` on the libplacebo paths restore the
    punch that the default PQ→SDR curve leaves on the table (details in
    the callsite history; visually verified across MIB dark/mid/bright).

    Raises:
        ValueError: if ``path_kind`` is not a recognised DV5 variant.
    """
    fps = f"fps=fps={fps_value}:round=up"
    if path_kind == DV5_PATH_INTEL_OPENCL:
        return (
            f"{fps},"
            "setparams=color_primaries=bt2020:"
            "color_trc=smpte2084:colorspace=bt2020nc,"
            "hwmap=derive_device=opencl:mode=read,"
            f"tonemap_opencl=format=nv12:p=bt709:t=bt709:"
            f"m=bt709:tonemap={tonemap_algorithm}"
            f":peak=100:desat=0,"
            f"hwdownload,format=nv12,format=yuv420p,{base_scale}"
        )
    libplacebo_opts = (
        f"libplacebo=tonemapping={tonemap_algorithm}"
        f":format=yuv420p"
        f":contrast=1.3:saturation=1.3"
    )
    if path_kind == DV5_PATH_VAAPI_VULKAN:
        return (
            f"{fps},"
            f"hwmap=derive_device=vulkan,"
            f"{libplacebo_opts},"
            f"hwdownload,format=yuv420p,{base_scale}"
        )
    if path_kind == DV5_PATH_LIBPLACEBO:
        return (
            f"{fps},hwupload,{libplacebo_opts},hwdownload,format=yuv420p,{base_scale}"
        )
    raise ValueError(f"Unknown DV5 path_kind: {path_kind!r}")
