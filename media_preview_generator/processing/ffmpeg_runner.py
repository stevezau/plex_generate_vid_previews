"""FFmpeg subprocess runner factory for the per-file thumbnail pipeline.

This module owns the work that ``_run_ffmpeg`` used to do inside a
nested closure in :func:`media_processing.generate_images` — building
the FFmpeg argv, selecting the vendor-specific filter chain, invoking
the subprocess, streaming progress to the caller's callback, detecting
stalls, and diagnosing non-zero exit codes.

The public entry point is :func:`create_ffmpeg_runner`, a factory that
captures all the configuration state for ONE media file and returns a
callable with the same signature as the original nested
``_run_ffmpeg``:

    run = create_ffmpeg_runner(video_file=..., output_folder=..., ...)
    rc, seconds, speed, stderr_lines = run(
        use_skip=True, init_vulkan=use_libplacebo
    )

Keeping the factory-function shape (rather than a class) means the
call sites in ``generate_images``' retry cascade don't need to change
— every ``_run_ffmpeg(...)`` call becomes ``run(...)``.  The three
inner helpers (``_gpu_scale_segment``, ``_assemble_vf``, ``_run_ffmpeg``)
are bit-for-bit what they used to be; only the indentation level and
home address changed.
"""

from __future__ import annotations

import os
import signal
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable

from loguru import logger

from .filter_chain import (
    DV5_PATH_INTEL_OPENCL,
    DV5_PATH_LIBPLACEBO,
    DV5_PATH_VAAPI_VULKAN,
)


def create_ffmpeg_runner(
    *,
    video_file: str,
    output_folder: str,
    gpu: str | None,
    gpu_device_path: str | None,
    config,
    progress_callback: Callable | None,
    ffmpeg_threads_override: int | None,
    cancel_check: Callable | None,
    pause_check: Callable | None = None,
    path_kind: str,
    libplacebo_vf: str | None,
    use_libplacebo: bool,
    dv5_software_fallback: bool,
    base_scale: str,
    fps_filter: str,
    hdr10_zscale_chain: str,
) -> Callable[..., tuple[int, float, float, list[str]]]:
    """Factory: return a configured ffmpeg-runner closure for one media item.

    The returned callable has the same signature as the original nested
    ``_run_ffmpeg`` function and captures every piece of per-file state
    (video_file, filter-chain choice, retry flags, etc.) exactly as the
    original closure did.  Callers in :func:`media_processing.generate_images`
    replace ``def _run_ffmpeg(...)`` with a single assignment and then
    invoke the returned callable through the full retry cascade.
    """
    # Import at factory-call time to avoid a circular dependency at module
    # load (orchestrator imports create_ffmpeg_runner from here). Runs
    # once per media file, not once per FFmpeg invocation.
    from .generator import (
        FFMPEG_STALL_TIMEOUT_SEC,
        CancellationError,
        _diagnose_ffmpeg_exit_code,
        _is_signal_killed,
        _save_ffmpeg_failure_log,
        parse_ffmpeg_progress_line,
    )

    def _gpu_scale_segment(effective_gpu: str | None, hw_decode_active: bool, fmt: str) -> str | None:
        """GPU-side scale + hwdownload segment for the active vendor,
        or None to keep CPU scale in place (software decode, DV5
        libplacebo paths, unsupported vendor).  ``fmt`` is ``nv12`` for
        8-bit paths, ``p010le`` for the HDR10 zscale chain.

        scale_cuda supports ``force_divisible_by=2`` directly.
        scale_vaapi does not, so a tiny CPU ``scale=trunc(iw/2)*2:
        trunc(ih/2)*2`` runs after hwdownload — essentially free on a
        320xN frame, a no-op on already-even dims.  Letterboxed 2.4:1
        content would otherwise produce odd heights (e.g. 320x133) and
        break zscale's 4:2:0 subsampling requirement.
        """
        if not hw_decode_active:
            return None
        if effective_gpu == "NVIDIA":
            return (
                f"scale_cuda=w=320:h=240:force_original_aspect_ratio=decrease:"
                f"force_divisible_by=2:format={fmt},hwdownload,format={fmt}"
            )
        if effective_gpu in {"INTEL", "AMD"}:
            return (
                f"scale_vaapi=w=320:h=240:force_original_aspect_ratio=decrease:"
                f"format={fmt},hwdownload,format={fmt},"
                f"scale=trunc(iw/2)*2:trunc(ih/2)*2"
            )
        return None

    def _assemble_vf(
        effective_gpu: str | None,
        hw_decode_active: bool,
        effective_kind: str,
    ) -> str:
        """Build the vf chain for the current attempt.

        For SDR and HDR10/DV P7+8 paths, the chain is vendor-aware: on
        NVIDIA/VAAPI the downscale runs on the GPU and only the final
        320x240 frame is hwdownloaded, so mjpeg encode (CPU-only) works
        on a tiny frame instead of a full 4K one.  For HDR10, that also
        means the zscale tonemap chain processes 320x240 frames rather
        than source-resolution frames.

        DV Profile 5 libplacebo / OpenCL chains are pre-assembled and
        returned as-is — they already contain hwupload/hwmap/hwdownload
        (and fps for the OpenCL variant) and are not touched by the
        GPU-scale optimisation.

        ``effective_kind`` lets the DV-safe retry collapse the HDR10
        zscale path to an SDR fps+scale chain while preserving the
        GPU-scale segment (so the retry doesn't lose the perf win
        just because zscale / RPU parsing failed).
        """
        if effective_kind in {
            DV5_PATH_LIBPLACEBO,
            DV5_PATH_VAAPI_VULKAN,
            DV5_PATH_INTEL_OPENCL,
        }:
            assert libplacebo_vf is not None
            return libplacebo_vf
        if effective_kind == "hdr10_zscale":
            gpu_seg = _gpu_scale_segment(effective_gpu, hw_decode_active, "p010le")
            if gpu_seg is not None:
                return f"{fps_filter},{gpu_seg},{hdr10_zscale_chain}"
            return f"{fps_filter},{hdr10_zscale_chain},{base_scale}"
        # SDR (also covers dv5_software_fallback — DV-safe fps+scale).
        gpu_seg = _gpu_scale_segment(effective_gpu, hw_decode_active, "nv12")
        if gpu_seg is not None:
            return f"{fps_filter},{gpu_seg}"
        return f"{fps_filter},{base_scale}"

    def _run_ffmpeg(
        use_skip: bool,
        gpu_override: str | None = None,
        gpu_device_path_override: str | None = None,
        vf_override: str | None = None,
        init_vulkan: bool = False,
        disable_vaapi_dv5: bool = False,
        path_kind_override: str | None = None,
    ) -> tuple[int, float, float, list[str]]:
        """Run FFmpeg once and return (returncode, seconds, speed, stderr_lines)."""
        # Build FFmpeg command with proper argument ordering
        # Hardware acceleration flags must come BEFORE the input file (-i)
        # Propagate the app's log level to FFmpeg so DEBUG reports include
        # full VAAPI / Vulkan / Mesa / libplacebo internals (thousands of
        # lines per 4K job).  INFO is the everyday default.
        ffmpeg_loglevel = "debug" if config.log_level == "DEBUG" else "info"
        args = [
            config.ffmpeg_path,
            "-loglevel",
            ffmpeg_loglevel,
        ]

        # Cap FFmpeg's global and filter-graph thread pools for GPU workers.
        # GPU decode is offloaded to hardware, so the CPU threads are mostly
        # idle overhead; capping them prevents thread oversubscription when
        # running multiple workers.  CPU paths are left uncapped so software
        # decode can use all available cores.
        effective_gpu = gpu_override if gpu_override is not None else gpu
        effective_ffmpeg_threads = (
            ffmpeg_threads_override if ffmpeg_threads_override is not None else config.ffmpeg_threads
        )
        if effective_gpu is not None and effective_ffmpeg_threads > 0:
            args += [
                "-threads",
                str(effective_ffmpeg_threads),
                "-filter_threads",
                str(effective_ffmpeg_threads),
            ]

        # Hardware acceleration for decoding (before -i flag).
        #
        # Non-libplacebo paths (HDR10, SDR, DV Profile 7/8 via zscale on
        # the HDR10 base layer) have always benefited from HW decode
        # across all supported vendors.
        #
        # The DV Profile 5 libplacebo path (``init_vulkan=True``) used
        # to blanket-skip HW decode on non-NVIDIA vendors.  That gate
        # was added in ``a06ed98`` after a bad P7/8 + libplacebo output
        # (issue #178, P7/8 now uses zscale on the HDR10 base layer so
        # the original reason no longer applies) and then re-validated
        # on 2026-04-12 against a CPU path that was still pinned to 2
        # threads.  A 2026-04-16 bench on Intel UHD 770 (Raptor Lake-S)
        # with the ``-threads:v 0`` fix in place compared:
        #   - software decode + libplacebo:  12.9x, ~10 cores saturated
        #   - VAAPI decode + drm→va@dr→vk@dr: 16.1x,  ~0 cores (1s CPU)
        # Output was pixel-identical (PSNR=inf) across dark, mid, and
        # bright scenes.  So on Linux VAAPI GPUs (Intel iGPU/Arc + AMD
        # Radeon) we now use zero-copy VAAPI→Vulkan DMA-BUF interop.
        # NVIDIA keeps CUDA.  Non-Linux platforms don't reach the
        # libplacebo branch and are unaffected.
        effective_gpu_device_path = (
            gpu_device_path_override if gpu_device_path_override is not None else gpu_device_path
        )
        use_gpu = effective_gpu is not None
        use_intel_opencl_dv5 = (
            init_vulkan
            and use_gpu
            and effective_gpu == "INTEL"
            and effective_gpu_device_path is not None
            and effective_gpu_device_path.startswith("/dev/dri/")
            and not disable_vaapi_dv5
        )
        use_vaapi_dv5 = (
            init_vulkan
            and use_gpu
            and effective_gpu not in ("NVIDIA", "INTEL")
            and effective_gpu_device_path is not None
            and effective_gpu_device_path.startswith("/dev/dri/")
            and not disable_vaapi_dv5
        )

        # Device init for the DV5 tone-mapping context.  Intel and non-Intel
        # VAAPI GPUs take different paths because Intel's VAAPI→Vulkan DMA-BUF
        # interop is broken upstream (libplacebo's vkCreateImage returns
        # VK_ERROR_OUT_OF_DEVICE_MEMORY on Mesa ANV for the format+modifier
        # combinations used for DV5).  Intel gets VAAPI→OpenCL (via Jellyfin-
        # ffmpeg's DV RPU-aware tonemap_opencl patch).  AMD keeps VAAPI→Vulkan
        # derived from a common DRM device (drm=dr → vaapi=va@dr →
        # vulkan=vk@dr), which is Jellyfin's proven pattern for discrete AMD.
        # NVIDIA and software fallback use plain vulkan=vk for libplacebo.
        if init_vulkan:
            if use_intel_opencl_dv5:
                args += [
                    "-init_hw_device",
                    f"vaapi=va:{effective_gpu_device_path}",
                    "-init_hw_device",
                    "opencl=ocl@va",
                    "-filter_hw_device",
                    "ocl",
                ]
            elif use_vaapi_dv5:
                args += [
                    "-init_hw_device",
                    f"drm=dr:{effective_gpu_device_path}",
                    "-init_hw_device",
                    "vaapi=va@dr",
                    "-init_hw_device",
                    "vulkan=vk@dr",
                    "-filter_hw_device",
                    "vk",
                ]
            else:
                args += ["-init_hw_device", "vulkan=vk", "-filter_hw_device", "vk"]

        # Paths that can keep frames on the GPU end-to-end and use
        # scale_cuda / scale_vaapi.  DV5 libplacebo uses hwupload from
        # CPU frames (NVIDIA) or hwmap from VAAPI frames (Intel/AMD),
        # so -hwaccel_output_format is either harmful (NVIDIA — breaks
        # hwupload) or already set (VAAPI DV5 branch below).
        effective_kind = path_kind_override or path_kind
        keep_on_gpu = effective_kind in {"sdr", "hdr10_zscale"}

        hw_decode_active = False
        if use_gpu and effective_gpu == "NVIDIA":
            args += ["-hwaccel", "cuda"]
            # On multi-GPU hosts each NVIDIA card is registered with a
            # device path of ``cuda:<index>`` (see gpu/detect.py).  Pass
            # the index through to FFmpeg via ``-hwaccel_device`` so
            # work actually lands on the selected GPU (issue #221).
            if effective_gpu_device_path and effective_gpu_device_path.startswith("cuda:"):
                cuda_idx = effective_gpu_device_path.split(":", 1)[1]
                if cuda_idx:
                    args += ["-hwaccel_device", cuda_idx]
            if keep_on_gpu:
                # Keep decoded CUDA surfaces on the GPU so scale_cuda
                # can downscale there and only the 320x240 frame is
                # hwdownloaded to the mjpeg encoder.  Without this,
                # FFmpeg silently downloads every 4K frame to host
                # RAM (~990 MB RSS per worker on 4K HDR10, issue #218).
                args += ["-hwaccel_output_format", "cuda"]
            hw_decode_active = True
        elif use_intel_opencl_dv5 or use_vaapi_dv5:
            # Intel DV5 via VAAPI decode + OpenCL tonemap, OR AMD DV5 via
            # VAAPI decode + Vulkan libplacebo.  Same hwaccel flags (VAAPI
            # decode, frames stay as VAAPI surfaces); the device init
            # block above picks OpenCL vs Vulkan for the tone-map stage.
            args += [
                "-hwaccel",
                "vaapi",
                "-hwaccel_device",
                "va",
                "-hwaccel_output_format",
                "vaapi",
            ]
            hw_decode_active = True
        elif use_gpu and not init_vulkan:
            if effective_gpu == "WINDOWS_GPU":
                args += ["-hwaccel", "d3d11va"]
                hw_decode_active = True
            elif effective_gpu == "APPLE":
                args += ["-hwaccel", "videotoolbox"]
                hw_decode_active = True
            elif effective_gpu_device_path and effective_gpu_device_path.startswith("/dev/dri/"):
                # -hwaccel_device (not the deprecated -vaapi_device)
                # pairs with -hwaccel_output_format vaapi so decoded
                # frames stay in VAAPI surfaces for scale_vaapi; the
                # 320x240 frame is hwdownloaded at the end (issue #218).
                args += [
                    "-hwaccel",
                    "vaapi",
                    "-hwaccel_device",
                    effective_gpu_device_path,
                ]
                if keep_on_gpu:
                    args += ["-hwaccel_output_format", "vaapi"]
                hw_decode_active = True
        elif use_gpu and init_vulkan:
            logger.debug(
                "Skipping HW decode for DV Profile 5 ({}) on {}: no VAAPI render device available; using software decode + Vulkan/libplacebo tone mapping",
                video_file,
                effective_gpu,
            )

        # Cap the video decoder to 1 thread ONLY when decode is offloaded
        # to a hardware accelerator.  With hwaccel the CPU thread is just
        # an orchestrator and the cap prevents thread oversubscription
        # across parallel GPU workers.  For software decode — pure CPU
        # workers, or DV Profile 5 on non-NVIDIA GPUs where the vendor
        # gate above skips hwaccel — let FFmpeg pick the default thread
        # count so 4K HEVC can saturate available cores.  Fixes issue
        # #212 (DV P5 pinned to one core at ~0.8x before this gate).
        if hw_decode_active:
            args += ["-threads:v", "1"]
        elif init_vulkan and use_gpu:
            # DV Profile 5 on non-NVIDIA GPUs: the vendor gate above
            # fell through to software decode, but the global
            # "-threads N" / "-filter_threads N" above is still in the
            # command.  FFmpeg treats "-threads N" as the default for
            # every codec pool including the video decoder, so without
            # an explicit override the HEVC 4K 10-bit decoder would
            # run on only N threads (2 by default).  Set "-threads:v 0"
            # to tell FFmpeg "pick the optimal count for this decoder",
            # which lets it saturate available cores while the global
            # "-threads N" keeps filter-graph / libplacebo threads
            # bounded.  Fixes issue #212 second-order: bfa67e2 removed
            # the explicit "-threads:v 1" cap but left the global cap
            # bleeding into the decoder.
            args += ["-threads:v", "0"]

        # Add skip_frame option for faster decoding (if safe).
        # Disabled for DV Profile 5 (init_vulkan) — RPU side-data has
        # inter-frame dependencies that break with keyframe-only decode.
        if use_skip and not init_vulkan:
            args += ["-skip_frame:v", "nokey"]

        # Assemble the vf chain now that effective_gpu / hw_decode_active
        # are known.  Explicit vf_override (software libplacebo retry)
        # is honoured verbatim.  path_kind_override lets the DV-safe
        # retry collapse HDR10 to SDR while preserving the GPU-scale
        # segment.
        if vf_override is not None:
            effective_vf = vf_override
        else:
            effective_vf = _assemble_vf(effective_gpu, hw_decode_active, effective_kind)

        # Add input file and output options
        args += [
            "-i",
            video_file,
            "-an",
            "-sn",
            "-dn",
            "-q:v",
            str(config.thumbnail_quality),
            "-vf",
            effective_vf,
            f"{output_folder}/img-%06d.jpg",
        ]

        start_local = time.time()
        hw_label = "GPU" if gpu else "CPU"
        logger.info("Encoding thumbnails for {} ({})", video_file, hw_label)
        # Full argv at DEBUG only — at INFO with 4-tier retries on a 50K-item
        # library this would be 200K+ lines. Also: the -i path may include
        # vendor-side credentials in some users' setups; keep it off the
        # default log surface.
        logger.debug("FFmpeg command: {}", " ".join(args))

        # When the Layer-3 probe retry in gpu_detection succeeded only with
        # VK_DRIVER_FILES set, propagate those env overrides to the real
        # FFmpeg invocation on the libplacebo DV Profile 5 path. On every
        # other path the override dict is empty and we pass env=None so
        # the child process inherits the parent environment unchanged.
        ffmpeg_env: dict | None = None
        if init_vulkan:
            from ..gpu.vulkan_probe import get_vulkan_env_overrides

            vulkan_overrides = get_vulkan_env_overrides()
            if vulkan_overrides:
                ffmpeg_env = os.environ.copy()
                ffmpeg_env.update(vulkan_overrides)
                logger.debug(
                    "FFmpeg libplacebo path: injecting Vulkan env overrides {} into subprocess", vulkan_overrides
                )

        # Use file polling approach for non-blocking, high-frequency progress monitoring
        thread_id = threading.get_ident()
        output_file = os.path.join(
            tempfile.gettempdir(),
            f"ffmpeg_output_{os.getpid()}_{thread_id}_{time.time_ns()}.log",
        )
        stderr_fh = open(output_file, "w", encoding="utf-8")
        try:
            proc = subprocess.Popen(
                args,
                stderr=stderr_fh,
                stdout=subprocess.DEVNULL,
                env=ffmpeg_env,
            )

            # Signal that FFmpeg process has started
            if progress_callback:
                progress_callback(0, 0, 0, "0.0x", media_file=video_file)

            # Track progress
            total_duration = None
            speed_local = "0.0x"
            ffmpeg_output_lines = []
            line_count = 0
            last_progress_time = time.time()
            stalled = False

            def speed_capture_callback(
                progress_percent,
                current_duration,
                total_duration_param,
                speed_value,
                remaining_time=None,
                frame=0,
                fps=0,
                q=0,
                size=0,
                time_str="00:00:00.00",
                bitrate=0,
            ):
                nonlocal speed_local
                if speed_value and speed_value != "0.0x":
                    speed_local = speed_value
                if progress_callback:
                    progress_callback(
                        progress_percent,
                        current_duration,
                        total_duration_param,
                        speed_value,
                        remaining_time,
                        frame,
                        fps,
                        q,
                        size,
                        time_str,
                        bitrate,
                        media_file=video_file,
                    )

            time.sleep(0.02)
            paused_locally = False
            while proc.poll() is None:
                if cancel_check and cancel_check():
                    logger.info("Cancellation requested, terminating FFmpeg for {}", video_file)
                    # If we were paused, resume first so SIGTERM can be delivered.
                    if paused_locally:
                        try:
                            proc.send_signal(signal.SIGCONT)
                        except (ProcessLookupError, OSError):
                            pass
                        paused_locally = False
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()
                    raise CancellationError(f"Processing cancelled for {video_file}")

                # Hard-pause via SIGSTOP/SIGCONT — freezes the FFmpeg process
                # in place without losing work. When pause_check returns True
                # we suspend the subprocess; when it flips back to False we
                # resume. This is the live-halt behaviour that existed before
                # the multi-server refactor: clicking Pause stops in-flight
                # FFmpeg immediately rather than waiting for the current item
                # to complete. SIGSTOP also stops the kernel from advancing
                # the stall-detection clock, so a 30-min pause won't trip the
                # FFMPEG_STALL_TIMEOUT_SEC kill below.
                if pause_check and pause_check():
                    if not paused_locally:
                        try:
                            proc.send_signal(signal.SIGSTOP)
                            paused_locally = True
                            last_progress_time = time.time()  # freeze stall clock
                            logger.info(
                                "FFmpeg paused for {} (PID {}) — global pause is active",
                                video_file,
                                proc.pid,
                            )
                        except (ProcessLookupError, OSError):
                            pass
                    time.sleep(0.2)
                    continue
                if paused_locally:
                    try:
                        proc.send_signal(signal.SIGCONT)
                        paused_locally = False
                        last_progress_time = time.time()  # restart stall clock
                        logger.info("FFmpeg resumed for {} (PID {})", video_file, proc.pid)
                    except (ProcessLookupError, OSError):
                        pass
                if os.path.exists(output_file):
                    try:
                        with open(output_file, encoding="utf-8") as f:
                            lines = f.readlines()
                            if len(lines) > line_count:
                                for i in range(line_count, len(lines)):
                                    line = lines[i].strip()
                                    if line:
                                        ffmpeg_output_lines.append(line)
                                        total_duration = parse_ffmpeg_progress_line(
                                            line, total_duration, speed_capture_callback
                                        )
                                line_count = len(lines)
                                last_progress_time = time.time()
                    except OSError:
                        pass
                if time.time() - last_progress_time > FFMPEG_STALL_TIMEOUT_SEC:
                    logger.warning(
                        "FFmpeg stopped making progress for {}s while processing {} — killing it. "
                        "This usually means a slow disk, a stuck hardware decoder, or a damaged file. "
                        "Other files in the queue will keep processing; this one will be marked failed. "
                        "If it happens often on the same file, try toggling hardware acceleration off "
                        "in Settings → GPU.",
                        FFMPEG_STALL_TIMEOUT_SEC,
                        video_file,
                    )
                    stalled = True
                    proc.kill()
                    proc.wait()
                    break
                time.sleep(0.005)

            # Process any remaining data
            if os.path.exists(output_file):
                try:
                    with open(output_file, encoding="utf-8") as f:
                        lines = f.readlines()
                        if len(lines) > line_count:
                            for i in range(line_count, len(lines)):
                                line = lines[i].strip()
                                if line:
                                    ffmpeg_output_lines.append(line)
                                    total_duration = parse_ffmpeg_progress_line(
                                        line, total_duration, speed_capture_callback
                                    )
                except OSError:
                    pass
        finally:
            # Ensure stderr file handle is always closed
            stderr_fh.close()
            try:
                os.remove(output_file)
            except OSError:
                pass

        # Error logging (skip generic failure log when we killed due to stall; already logged above)
        if proc.returncode != 0 and not stalled:
            exit_diagnosis = _diagnose_ffmpeg_exit_code(proc.returncode)
            logger.error(
                "FFmpeg failed while extracting frames from {} (exit code {}: {}). "
                "See the FFmpeg stderr lines logged below — they usually point at the cause "
                "(unsupported codec, broken hardware acceleration, corrupted file, full disk). "
                "Other files in the queue will keep processing. "
                "If this happens often on the same file, try toggling hardware acceleration off "
                "in Settings → GPU.",
                video_file,
                proc.returncode,
                exit_diagnosis,
            )

            # Log last few stderr lines at WARNING level so users can diagnose
            # failures without needing DEBUG mode (especially for crashes/signals)
            if _is_signal_killed(proc.returncode):
                signal_detail = _diagnose_ffmpeg_exit_code(proc.returncode).split(":", 1)[1]
                logger.warning(
                    "FFmpeg was killed by the operating system (exit code {}, signal {}) while processing {}. "
                    "Common causes: the system ran out of memory, the container was OOM-killed, "
                    "or someone manually stopped the process. Check container memory limits and "
                    "system free RAM. Other files in the queue will keep processing.",
                    proc.returncode,
                    signal_detail,
                    video_file,
                )
            elif exit_diagnosis == "io_error":
                logger.warning(
                    "FFmpeg could not write temporary thumbnail files for {} (working folder: {}, exists: {}). "
                    "This usually means the working folder is full, missing, or not writable. "
                    "Free up disk space or change the working folder under Settings → Advanced. "
                    "Other files in the queue will keep processing.",
                    video_file,
                    output_folder,
                    os.path.isdir(output_folder),
                )
            elif exit_diagnosis == "high_exit_non_signal":
                logger.warning(
                    "FFmpeg crashed with an unusual exit code ({}) while processing {}. "
                    "This usually points to an internal FFmpeg bug, a broken hardware acceleration "
                    "driver, or a malformed video file. The stderr lines below should explain. "
                    "Other files in the queue will keep processing.",
                    proc.returncode,
                    video_file,
                )
            if ffmpeg_output_lines:
                tail = ffmpeg_output_lines[-5:]
                logger.warning(
                    "FFmpeg's last {} stderr lines for {} (these usually identify the cause):",
                    len(tail),
                    video_file,
                )
                for line in tail:
                    logger.warning("  {}", line)

            # Check for permission-related errors in FFmpeg output
            # FFmpeg outputs "Permission denied" in messages like "av_interleaved_write_frame(): Permission denied"
            # We use lowercase for case-insensitive matching
            permission_keywords = ["permission denied", "access denied"]
            permission_errors = []
            for line in ffmpeg_output_lines:
                line_lower = line.lower()
                for keyword in permission_keywords:
                    if keyword in line_lower:
                        permission_errors.append(line.strip())
                        break

            # Log permission errors at INFO level so users can see them without DEBUG
            if permission_errors:
                logger.info("Permission error detected while processing {}:", video_file)
                for error_line in permission_errors[:3]:  # Show up to 3 permission error lines
                    logger.info("  {}", error_line)
                if len(permission_errors) > 3:
                    logger.info("  ... and {} more permission-related error(s)", len(permission_errors) - 3)

            # Log full FFmpeg output at DEBUG level for detailed troubleshooting.
            # When config.log_level=DEBUG, FFmpeg itself is invoked with
            # -loglevel debug (see ffmpeg_loglevel above), so these lines
            # include VAAPI / Vulkan / Mesa / libplacebo internals — exactly
            # what's needed to diagnose hwaccel and filter-graph failures.
            logger.debug("FFmpeg output ({} lines):", len(ffmpeg_output_lines))
            for i, line in enumerate(ffmpeg_output_lines):
                logger.debug("  {:3d}: {}", i + 1, line)

            # Save full FFmpeg stderr to a per-file log for post-mortem debugging
            _save_ffmpeg_failure_log(video_file, proc.returncode, ffmpeg_output_lines)

        end_local = time.time()
        seconds_local = round(end_local - start_local, 1)
        # Calculate fallback speed if needed
        if speed_local == "0.0x" and total_duration and total_duration > 0 and seconds_local > 0:
            calculated_speed = total_duration / seconds_local
            speed_local = f"{calculated_speed:.0f}x"

        return proc.returncode, seconds_local, speed_local, ffmpeg_output_lines

    return _run_ffmpeg
