"""Media processing functions for video thumbnail generation.

Handles FFmpeg execution, BIF file generation, and all media processing
logic including HDR detection, skip frame heuristics, and GPU acceleration.
"""

import array
import contextlib
import contextvars
import glob
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
from enum import Enum
from typing import Dict, Iterator, List, Optional, Tuple

from loguru import logger

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
from .utils import sanitize_path

# Backwards-compat re-exports.  The detection helpers used to live in this
# module under underscore-prefixed names; they moved to :mod:`hdr_detection`
# as part of the media_processing split, but external tests and callers
# still import them from here.  The public-name aliases below keep both the
# old names (``_is_dolby_vision`` etc.) and the new names importable.
_is_dolby_vision = is_dolby_vision
_is_dv_no_backward_compat = is_dv_no_backward_compat
_detect_dolby_vision_rpu_error = detect_dolby_vision_rpu_error
_detect_zscale_colorspace_error = detect_zscale_colorspace_error

__all__ = [
    # DV5 filter chain
    "DV5_PATH_INTEL_OPENCL",
    "DV5_PATH_LIBPLACEBO",
    "DV5_PATH_VAAPI_VULKAN",
    "build_dv5_vf",
    # HDR / DV detection (public names)
    "detect_dolby_vision_rpu_error",
    "detect_zscale_colorspace_error",
    "is_dolby_vision",
    "is_dv_no_backward_compat",
    # HDR / DV detection (backwards-compat aliases)
    "_detect_dolby_vision_rpu_error",
    "_detect_zscale_colorspace_error",
    "_is_dolby_vision",
    "_is_dv_no_backward_compat",
]


class ProcessingResult(Enum):
    """Outcome of processing a single media item.

    Used to track what actually happened so callers can distinguish
    real work (GENERATED) from various skip/failure reasons.
    """

    GENERATED = "generated"
    SKIPPED_BIF_EXISTS = "skipped_bif_exists"
    SKIPPED_FILE_NOT_FOUND = "skipped_file_not_found"
    SKIPPED_EXCLUDED = "skipped_excluded"
    SKIPPED_INVALID_HASH = "skipped_invalid_hash"
    FAILED = "failed"
    NO_MEDIA_PARTS = "no_media_parts"


# When a media item has multiple parts, the most significant outcome wins.
_RESULT_PRIORITY = {
    ProcessingResult.GENERATED: 6,
    ProcessingResult.FAILED: 5,
    ProcessingResult.SKIPPED_FILE_NOT_FOUND: 4,
    ProcessingResult.SKIPPED_INVALID_HASH: 3,
    ProcessingResult.SKIPPED_EXCLUDED: 2,
    ProcessingResult.SKIPPED_BIF_EXISTS: 1,
    ProcessingResult.NO_MEDIA_PARTS: 0,
}

# If FFmpeg produces no progress output for this many seconds, the process is
# killed to avoid hanging the worker indefinitely (e.g. unresponsive NAS).
FFMPEG_STALL_TIMEOUT_SEC = 300

# ---------------------------------------------------------------------------
# Failure tracker — collects per-file failure info for end-of-run summary
# ---------------------------------------------------------------------------
#
# Failure records are stored in a dict keyed by job id so that concurrent
# jobs do not contaminate each other's summaries.  Every thread that wants
# to record, read, or clear failures must first enter ``failure_scope(job_id)``
# — that sets a ContextVar which all record/get/clear helpers read to know
# *which* job's records to touch.  The job runner enters the scope on its
# dispatcher thread; each worker thread enters the scope on its own thread
# using ``worker.current_job_id`` so calls made deep inside process_item /
# generate_images / _run_ffmpeg land in the right bucket.

_failure_lock = threading.Lock()
_failures_by_job: Dict[str, List[dict]] = {}
_failure_job_id_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "failure_job_id", default=None
)


@contextlib.contextmanager
def failure_scope(job_id: Optional[str]) -> Iterator[None]:
    """Bind the current thread's failure-tracking scope to ``job_id``.

    Any ``record_failure``/``get_failures``/``clear_failures``/
    ``log_failure_summary`` calls that run on this thread while the
    context manager is active will operate on the failure list for
    ``job_id`` instead of a shared global, preventing cross-job
    contamination when concurrent jobs run in the same process.

    The same scope can be safely entered from multiple threads (the
    job runner's dispatcher thread plus N worker threads) because
    the underlying storage is a dict keyed by ``job_id``.
    """
    token = _failure_job_id_var.set(job_id)
    try:
        yield
    finally:
        _failure_job_id_var.reset(token)


def record_failure(
    file_path: str, exit_code: int, reason: str, worker_type: str = ""
) -> None:
    """Record an FFmpeg / processing failure for the end-of-run summary.

    The failure is attributed to the job whose ``failure_scope`` is
    active on the current thread.  Calls made outside any scope are
    logged and dropped — that's a programming error, not a recoverable
    condition.

    Args:
        file_path: Media file that failed.
        exit_code: FFmpeg return code (0 if not FFmpeg-related).
        reason: Short human-readable reason string.
        worker_type: 'GPU', 'CPU', or '' if unknown.

    """
    job_id = _failure_job_id_var.get()
    if job_id is None:
        logger.warning(
            f"record_failure called outside failure_scope; dropping "
            f"failure for {file_path!r} (exit={exit_code}, reason={reason!r})"
        )
        return
    with _failure_lock:
        _failures_by_job.setdefault(job_id, []).append(
            {
                "file": file_path,
                "exit_code": exit_code,
                "reason": reason,
                "worker_type": worker_type,
            }
        )


def get_failures() -> List[dict]:
    """Return a copy of the current scope's failure list (thread-safe)."""
    job_id = _failure_job_id_var.get()
    if job_id is None:
        return []
    with _failure_lock:
        return list(_failures_by_job.get(job_id, []))


def clear_failures() -> None:
    """Drop the current scope's failure list.

    Call at the start of a job to reset stale state and at the end
    once the summary has been consumed to release memory.  Calls made
    outside any scope are a no-op.
    """
    job_id = _failure_job_id_var.get()
    if job_id is None:
        return
    with _failure_lock:
        _failures_by_job.pop(job_id, None)


# ---------------------------------------------------------------------------
# Per-file result callback — set by the job runner to capture every outcome
# ---------------------------------------------------------------------------

_file_result_callback_lock = threading.Lock()
_file_result_callback = None


def set_file_result_callback(callback) -> None:
    """Set the per-file result callback (called for each media part outcome).

    The callback signature is: callback(file_path, outcome_str, reason, worker)

    Args:
        callback: Callable or None to clear.
    """
    global _file_result_callback
    with _file_result_callback_lock:
        _file_result_callback = callback


def _notify_file_result(
    file_path: str, outcome: "ProcessingResult", reason: str = "", worker: str = ""
) -> None:
    """Invoke the file-result callback if one is set."""
    with _file_result_callback_lock:
        cb = _file_result_callback
    if cb is not None:
        try:
            cb(file_path, outcome.value, reason, worker)
        except Exception:
            logger.debug(f"File result callback error for {file_path}", exc_info=True)


def log_failure_summary() -> None:
    """Log a summary table of all failures recorded during this run."""
    failures = get_failures()
    if not failures:
        return

    logger.warning(f"{'=' * 80}")
    logger.warning(f"FAILURE SUMMARY — {len(failures)} file(s) failed during this run")
    logger.warning(f"{'=' * 80}")
    for i, f in enumerate(failures, 1):
        wt = f"[{f['worker_type']}] " if f["worker_type"] else ""
        logger.warning(
            f"  {i:3d}. {wt}exit={f['exit_code']} | {f['reason']} | {f['file']}"
        )
    logger.warning(f"{'=' * 80}")


try:
    from pymediainfo import MediaInfo

    # Test that native library is available
    MediaInfo.can_parse()
except ImportError:
    logger.error(
        "pymediainfo Python package not found. Please install: pip install pymediainfo"
    )
    sys.exit(1)
except OSError as e:
    if "libmediainfo" in str(e).lower():
        logger.error("MediaInfo native library not found. Please install MediaInfo:")
        if sys.platform == "darwin":
            logger.error("  macOS: brew install media-info")
        elif sys.platform.startswith("linux"):
            logger.error(
                "  Ubuntu/Debian: sudo apt-get install mediainfo libmediainfo-dev"
            )
            logger.error("  Fedora/RHEL: sudo dnf install mediainfo mediainfo-devel")
        else:
            logger.error("  See: https://mediaarea.net/en/MediaInfo/Download")
        sys.exit(1)
except Exception as e:
    logger.warning(f"Could not validate MediaInfo library: {e}")
    logger.warning("Proceeding anyway, but errors may occur during processing")

from .config import Config, is_path_excluded, plex_path_to_local  # noqa: E402
from .plex_client import retry_plex_call  # noqa: E402


class CodecNotSupportedError(Exception):
    """Exception raised when a video codec is not supported by GPU hardware.

    This exception signals that the file should be processed by a CPU worker
    instead of attempting CPU fallback within the GPU worker thread.
    """

    pass


class CancellationError(Exception):
    """Raised when processing is cancelled by user request.

    Propagates through the call chain to immediately stop FFmpeg,
    skip all retry/fallback paths, and exit the worker thread cleanly.
    """

    pass


def _diagnose_ffmpeg_exit_code(returncode: int) -> str:
    """Classify FFmpeg exit codes into actionable diagnostics.

    Known signal exit codes are explicitly mapped. Values greater than 128
    that do not map to known signal exits are treated as non-signal failures
    to avoid misclassifying I/O and runtime errors as signal kills.

    Args:
        returncode: FFmpeg process exit code

    Returns:
        str: Diagnostic classification string

    """
    if returncode == 0:
        return "success"

    if returncode < 0:
        return f"signal:{abs(returncode)}"

    known_signals = {
        130: "SIGINT",
        137: "SIGKILL",
        143: "SIGTERM",
    }
    if returncode in known_signals:
        return f"signal:{known_signals[returncode]}"

    if returncode == 251:
        return "io_error"

    if returncode > 128:
        return "high_exit_non_signal"

    return "error"


def _is_signal_killed(returncode: int) -> bool:
    """Detect if FFmpeg was killed by a known signal."""
    return _diagnose_ffmpeg_exit_code(returncode).startswith("signal:")


def _extract_ffmpeg_error_summary(stderr_lines: List[str]) -> str:
    """Extract a concise, human-readable error summary from FFmpeg stderr.

    Scans the last lines for the most informative error messages (e.g.
    "Error opening input", "Invalid data found", codec errors) and
    returns the single best line suitable for UI display.

    Args:
        stderr_lines: Full FFmpeg stderr output lines.

    Returns:
        Short error string, or empty string if nothing useful was found.
    """
    if not stderr_lines:
        return ""

    error_keywords = (
        "error",
        "invalid",
        "permission denied",
        "no such file",
        "cannot",
        "failed",
        "unknown",
        "unrecognized",
        "not found",
        "codec",
        "corrupt",
    )

    candidates: List[str] = []
    for line in stderr_lines[-10:]:
        stripped = line.strip()
        if not stripped:
            continue
        lower = stripped.lower()
        if not any(kw in lower for kw in error_keywords):
            continue
        # Drop FFmpeg internal address prefixes like "[in#0 @ 0x...]"
        # and demuxer tags like "[matroska,webm @ 0x...]"
        clean = re.sub(r"\[.*?@\s*0x[0-9a-fA-F]+\]\s*", "", stripped).strip()
        if not clean:
            continue
        candidates.append(clean)

    if not candidates:
        return ""

    # Prefer the last line that starts with "Error" — FFmpeg puts the
    # most user-readable summary there (e.g. "Error opening input files:
    # Invalid data found when processing input").
    for candidate in reversed(candidates):
        if candidate.lower().startswith("error"):
            return candidate

    return candidates[-1]


def _save_ffmpeg_failure_log(
    video_file: str, returncode: int, stderr_lines: List[str]
) -> None:
    """Save full FFmpeg stderr output to a per-file log for post-mortem debugging.

    Files are written to {CONFIG_DIR}/logs/ffmpeg_failures/ with a sanitised
    filename derived from the media path.  Old logs are not cleaned automatically
    — the directory is capped at 500 files (oldest removed first).

    Args:
        video_file: Path to the media file that failed.
        returncode: FFmpeg exit code.
        stderr_lines: Complete FFmpeg stderr output lines.

    """
    log_dir = os.path.join(
        os.environ.get("CONFIG_DIR", "/config"), "logs", "ffmpeg_failures"
    )
    try:
        os.makedirs(log_dir, exist_ok=True)
    except OSError:
        return  # best-effort

    # Build a safe filename from the media basename + timestamp
    base = re.sub(r"[^\w\-.]", "_", os.path.basename(video_file))
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    log_path = os.path.join(log_dir, f"{timestamp}_{base}.log")

    try:
        exit_diagnosis = _diagnose_ffmpeg_exit_code(returncode)
        with open(log_path, "w", encoding="utf-8") as fh:
            fh.write(f"file: {video_file}\n")
            fh.write(f"exit_code: {returncode}\n")
            fh.write(f"exit_diagnosis: {exit_diagnosis}\n")
            fh.write(f"signal_killed: {_is_signal_killed(returncode)}\n")
            fh.write(f"lines: {len(stderr_lines)}\n")
            fh.write("-" * 72 + "\n")
            for line in stderr_lines:
                fh.write(line + "\n")
        logger.debug(f"Saved FFmpeg failure log to {log_path}")
    except OSError:
        pass  # best-effort

    # Cap directory at 500 files — remove oldest first
    try:
        logs = sorted(
            (os.path.join(log_dir, f) for f in os.listdir(log_dir)),
            key=os.path.getmtime,
        )
        while len(logs) > 500:
            os.remove(logs.pop(0))
    except OSError:
        pass


def _verify_tmp_folder_health(
    path: str, min_free_mb: int = 512
) -> Tuple[bool, List[str]]:
    """Verify that a temporary directory is writable and has free space.

    Args:
        path: Temporary directory path to validate.
        min_free_mb: Warning threshold for free disk space in MB.

    Returns:
        Tuple of ``(is_healthy, messages)`` where messages contains warning
        and error diagnostics suitable for logging.

    """
    messages: List[str] = []

    if not path:
        return False, ["Temporary directory path is empty"]

    try:
        os.makedirs(path, exist_ok=True)
    except OSError as error:
        return False, [f"Unable to create temporary directory {path}: {error}"]

    probe_path = os.path.join(path, f".tmp_write_probe_{os.getpid()}_{time.time_ns()}")
    try:
        with open(probe_path, "w", encoding="utf-8") as probe_file:
            probe_file.write("ok")
        os.remove(probe_path)
    except OSError as error:
        try:
            if os.path.exists(probe_path):
                os.remove(probe_path)
        except OSError:
            pass
        return False, [f"Temporary directory is not writable: {path} ({error})"]

    try:
        usage = shutil.disk_usage(path)
        free_mb = usage.free / (1024 * 1024)
        if free_mb < min_free_mb:
            messages.append(
                f"Temporary directory {path} has low free space ({free_mb:.1f} MB < {min_free_mb} MB)"
            )
    except OSError as error:
        messages.append(
            f"Unable to read disk usage for temporary directory {path}: {error}"
        )

    return True, messages


def parse_ffmpeg_progress_line(
    line: str, total_duration: float, progress_callback=None
):
    """Parse a single FFmpeg progress line and call progress callback if provided.

    Args:
        line: FFmpeg output line to parse
        total_duration: Total video duration in seconds
        progress_callback: Callback function for progress updates

    """
    # Parse duration
    if "Duration:" in line:
        duration_match = re.search(r"Duration: (\d{2}):(\d{2}):(\d{2}\.\d{2})", line)
        if duration_match:
            hours, minutes, seconds = duration_match.groups()
            return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
        return total_duration

    # Parse FFmpeg progress line with all data
    elif "time=" in line:
        # Extract all FFmpeg data fields
        frame_match = re.search(r"frame=\s*(\d+)", line)
        fps_match = re.search(r"fps=\s*([0-9.]+)", line)
        q_match = re.search(r"q=([0-9.]+)", line)
        size_match = re.search(r"size=\s*(\d+)kB", line)
        time_match = re.search(r"time=(\d{2}):(\d{2}):(\d{2}\.\d{2})", line)
        bitrate_match = re.search(r"bitrate=\s*([0-9.]+)kbits/s", line)
        speed_match = re.search(r"speed=\s*([0-9]+\.?[0-9]*|\.[0-9]+)x", line)

        # Extract values
        frame = int(frame_match.group(1)) if frame_match else 0
        fps = float(fps_match.group(1)) if fps_match else 0
        q = float(q_match.group(1)) if q_match else 0
        size = int(size_match.group(1)) if size_match else 0
        bitrate = float(bitrate_match.group(1)) if bitrate_match else 0
        speed = speed_match.group(1) + "x" if speed_match else None

        if time_match:
            hours, minutes, seconds = time_match.groups()
            current_time = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
            time_str = f"{hours}:{minutes}:{seconds}"

            # Update progress (1 decimal place for UI; Issue #144)
            progress_percent = 0
            if total_duration and total_duration > 0:
                progress_percent = min(
                    100.0, round((current_time / total_duration) * 100, 1)
                )

            # Calculate remaining wall-clock time using ffmpeg speed
            remaining_time = 0
            if total_duration and total_duration > 0 and current_time < total_duration:
                remaining_media = total_duration - current_time
                speed_val = float(speed_match.group(1)) if speed_match else 0
                remaining_time = (
                    remaining_media / speed_val if speed_val > 0 else remaining_media
                )

            # Call progress callback with all FFmpeg data
            if progress_callback:
                progress_callback(
                    progress_percent,
                    current_time,
                    total_duration,
                    speed or "0.0x",
                    remaining_time,
                    frame,
                    fps,
                    q,
                    size,
                    time_str,
                    bitrate,
                )

    return total_duration


def _detect_codec_error(returncode: int, stderr_lines: List[str]) -> bool:
    """Detect if FFmpeg failure is due to unsupported codec/hardware decoder error.

    Checks exit codes and stderr patterns to identify codec-related errors.
    Based on FFmpeg documentation: exit code -22 (EINVAL) and 69 (max error rate)
    are common for unsupported codecs, but stderr parsing is more reliable.

    Args:
        returncode: FFmpeg exit code
        stderr_lines: List of stderr output lines (case-insensitive matching)

    Returns:
        bool: True if codec/decoder error detected, False otherwise

    """
    # Combine all stderr lines into a single lowercase string for pattern matching
    stderr_text = " ".join(stderr_lines).lower()

    # Pattern list for codec/decoder errors (based on FFmpeg documentation and common error messages)
    # Focus ONLY on errors that indicate the codec is not supported by the hardware decoder
    # Avoid patterns that could indicate other issues (corruption, memory, permissions, etc.)
    codec_error_patterns = [
        # Specific decoder errors (indicate codec not available for hardware decoder)
        "no decoder for",
        "unknown decoder",
        "decoder not found",
        "could not find codec",
        "unsupported codec id",
        # Hardware decoder specific errors (clearly indicate hardware decoder limitations)
        "hardware decoder not found",
        "hardware decoder unavailable",
        "hwaccel decoder not found",
        "hwaccel decoder unavailable",
        # Generic codec errors (check these carefully - only in GPU context after failure)
        "unsupported codec",
        "codec not supported",
    ]

    # Check for codec error patterns in stderr (primary detection method)
    for pattern in codec_error_patterns:
        if pattern in stderr_text:
            return True

    # Check exit codes that may indicate codec issues
    # -22 (EINVAL) - invalid argument, often codec-related
    # 234 (wrapped -22 on Unix systems)
    # 69 (max error rate) - FFmpeg hits error rate limit, often due to decode failures
    # 1 (generic error) when combined with codec error patterns in stderr
    # Note: We check returncode != 0 to avoid false positives on success
    if returncode != 0:
        # Primary codes for codec errors
        if returncode in [-22, 234, 69]:
            return True
        # For other non-zero codes, rely on stderr patterns (already checked above)

    return False


def _detect_hwaccel_runtime_error(stderr_lines: List[str]) -> bool:
    """Detect GPU hardware accelerator runtime errors in FFmpeg output.

    These errors indicate that the hardware decoder started successfully but
    hit a driver-level failure at runtime (surface sync errors, transfer
    failures, etc.).  They are distinct from codec-not-found errors and
    typically produce non-standard exit codes (e.g. 251).

    Retrying on CPU almost always succeeds because these are driver bugs,
    resource exhaustion, or firmware issues specific to the GPU decode path.

    Args:
        stderr_lines: List of FFmpeg stderr lines

    Returns:
        bool: True if a hardware accelerator runtime error is detected

    """
    if not stderr_lines:
        return False

    stderr_text = " ".join(stderr_lines).lower()

    hwaccel_error_patterns = [
        # VAAPI / VDPAU surface errors
        "failed to sync surface",
        "failed to transfer data to output frame",
        # Generic AVHWFramesContext failures (covers VAAPI, CUDA, D3D11VA, QSV)
        "avhwframescontext",
        # CUDA-specific decode errors
        "cuda error",
        "cuvid decode error",
        "nv12 to nv12 not supported",
        # QSV / VDPAU / D3D11VA runtime failures
        "hardware accelerator failed",
        "hwaccel initialisation returned error",
        "failed to get hw frames constraints",
        "failed to initialise vaapi connection",
        "failed to create surface",
    ]

    return any(pattern in stderr_text for pattern in hwaccel_error_patterns)


def _clean_output_images(output_folder: str) -> None:
    """Remove any ``*.jpg`` files in ``output_folder``, silently ignoring
    files that vanish or are unremovable.

    Used between FFmpeg retry tiers so the next attempt starts with an
    empty output directory.  Extracted from five identical inline blocks
    in :func:`generate_images` to keep the retry cascade readable.
    """
    for img in glob.glob(os.path.join(output_folder, "*.jpg")):
        try:
            os.remove(img)
        except OSError:
            pass


def generate_images(
    video_file: str,
    output_folder: str,
    gpu: Optional[str],
    gpu_device_path: Optional[str],
    config: Config,
    progress_callback=None,
    ffmpeg_threads_override: Optional[int] = None,
    cancel_check=None,
) -> Tuple[bool, int, str, float, float, Optional[str]]:
    """Generate thumbnail images from a video using FFmpeg.

    Runs FFmpeg with hardware acceleration when configured. Attempts with
    '-skip_frame:v nokey' first on paths that support it (disabled for DV
    Profile 5 and libplacebo because the RPU side-data has inter-frame
    dependencies). If the first attempt returns non-zero, automatically
    retries without '-skip_frame'.

    If GPU processing fails with a codec error (detected via stderr parsing for
    patterns like "Codec not supported", "Unsupported codec", etc., or exit codes
    -22/EINVAL or 69/max error rate) and CPU threads are available, automatically
    falls back to CPU processing. This ensures files are processed even when the
    GPU doesn't support the codec (e.g., AV1 on RTX 2060 SUPER).

    Args:
        video_file: Path to input video file
        output_folder: Directory where thumbnail images will be written
        gpu: GPU type ('NVIDIA', 'AMD', 'INTEL', 'WINDOWS_GPU', 'APPLE', or None)
        gpu_device_path: GPU device path (e.g., '/dev/dri/renderD128' for VAAPI)
        config: Configuration object
        progress_callback: Optional progress callback for UI updates
        cancel_check: Optional callable returning True when job is cancelled

    Returns:
        (success, image_count, hw_used, seconds, speed, error_summary):
            success (bool): True if at least one image was produced
            image_count (int): Number of images written
            hw_used (bool): Whether hardware acceleration was actually used
                           (False if CPU fallback occurred)
            seconds (float): Elapsed processing time (last attempt)
            speed (str): Reported or computed FFmpeg speed string
            error_summary (str): Concise FFmpeg error excerpt on failure,
                empty string on success.

    """
    media_info = MediaInfo.parse(video_file)
    fps_value = round(1 / config.plex_bif_frame_interval, 6)

    # Filter primitives.  The final vf chain is assembled inside
    # _run_ffmpeg so it can adapt to the effective GPU on each attempt
    # (GPU→CPU retry, NVIDIA↔VAAPI overrides, etc.).
    fps_filter = f"fps=fps={fps_value}:round=up"
    base_scale = "scale=w=320:h=240:force_original_aspect_ratio=decrease"

    # vf assembly classification.  Set by HDR detection below; consumed
    # by _run_ffmpeg.  Possible values:
    #   "sdr"               — fps + scale (or GPU-scale segment)
    #   "hdr10_zscale"      — HDR10 / DV P7+8: zscale tonemap chain
    #   "libplacebo_dv5"    — DV Profile 5 with libplacebo (CPU/NVIDIA input)
    #   "libplacebo_vaapi"  — DV Profile 5 on AMD: VAAPI→Vulkan DMA-BUF
    #   "opencl_dv5_intel"  — DV Profile 5 on Intel: VAAPI→OpenCL tonemap
    #                         (Intel VAAPI + Vulkan libplacebo has an upstream
    #                         interop bug that returns VK_ERROR_OUT_OF_DEVICE_MEMORY
    #                         in containers on both iGPU and DG2 Arc; jellyfin-
    #                         ffmpeg's patched tonemap_opencl handles DV5 RPU
    #                         correctly and runs on the Intel media engine
    #                         entirely.  See issue #212.)
    path_kind = "sdr"
    # Pre-assembled filter chain for the libplacebo / OpenCL paths (they
    # already contain hwupload/hwmap/hwdownload and do not need GPU-scale
    # rewriting).
    libplacebo_vf: Optional[str] = None

    # Track whether the filter chain requires Vulkan (libplacebo) or OpenCL.
    use_libplacebo = False
    # True when DV5 uses AMD's VAAPI→Vulkan DMA-BUF libplacebo path.
    use_vaapi_dv5_path = False
    # True when DV5 uses Intel's VAAPI→OpenCL tonemap_opencl path (Jellyfin
    # pattern).  Intel's VAAPI→Vulkan libplacebo path is broken upstream on
    # Mesa ANV — see path_kind doc above.
    use_intel_opencl_dv5_path = False
    # DV5 content + software/missing Vulkan: skip both libplacebo AND
    # the zscale fallback, because zscale on a DV5 stream produces a
    # green overlay (no HDR10 base layer to read).  The SDR path_kind
    # (fps + scale, no tonemap) is the same DV-safe chain used by the
    # downstream DV-safe retry and produces dim-but-correct thumbnails.
    dv5_software_fallback = False

    # HDR10 / DV P7+8 zscale chain (sans fps and base_scale).  See
    # _assemble_vf below for how it's composed with the GPU-scale
    # segment when hardware decode is active.
    hdr10_zscale_chain = (
        "zscale=t=linear:npl=100,format=gbrpf32le,"
        f"zscale=p=bt709,tonemap={config.tonemap_algorithm}:desat=0,"
        "zscale=t=bt709:m=bt709:r=tv,format=yuv420p"
    )

    # Check if we have HDR Format. Note: Sometimes it can be returned as "None" (string) hence the check for None type or "None" (String)
    if media_info.video_tracks:
        hdr_fmt = media_info.video_tracks[0].hdr_format
        if hdr_fmt != "None" and hdr_fmt is not None:
            if _is_dv_no_backward_compat(hdr_fmt):
                # Dolby Vision Profile 5 (no HDR10 base layer).
                # Only libplacebo can handle these — there is no
                # backward-compat HDR10 stream for zscale/tonemap to
                # read.  libplacebo's apply_dolbyvision (enabled by
                # default) applies DV RPU reshaping, outputs BT.2020+PQ,
                # then tonemapping converts to SDR.
                #
                # Constraints for correct output:
                #  - No expensive pixel-touching filters before hwupload.
                #    The fps dropper IS OK (timestamp-only, preserves RPU
                #    side-data) and is required on NVIDIA Turing — placing
                #    fps inside libplacebo makes FFmpeg hwupload every
                #    decoded frame (24 fps × 4K p010) before libplacebo
                #    drops them, which exhausts the Vulkan allocator with
                #    VK_ERROR_OUT_OF_DEVICE_MEMORY.  All three libplacebo
                #    paths (Intel OpenCL, AMD VAAPI→Vulkan, NVIDIA/software)
                #    put the fps filter first.
                #  - No forced colorspace (apply_dolbyvision sets it)
                #  - No -skip_frame (RPU has inter-frame dependencies)
                #  - HW decode: NVDEC is validated (~3x speedup on 4K DV5
                #    with visually identical output); VAAPI/QSV/D3D11VA/
                #    VideoToolbox are untested on this path and stay on
                #    software decode.  See the vendor gate in _run_ffmpeg
                #    below.
                #  - Vulkan MUST be hardware.  libplacebo on a software
                #    rasterizer (llvmpipe/lavapipe) produces a green
                #    overlay on DV5 output.  Probe the Vulkan state
                #    first and drop to the DV-safe filter when software.
                from .gpu_detection import get_vulkan_device_info

                vulkan_info = get_vulkan_device_info()
                vk_device = vulkan_info.device
                vk_is_software = vulkan_info.is_software
                if vk_is_software or vk_device is None:
                    logger.warning(
                        f"Dolby Vision Profile 5 detected for {video_file} "
                        f"but Vulkan is unavailable or software only "
                        f"(device={vk_device!r}); skipping libplacebo and "
                        "using the DV-safe filter chain (dim but colour-"
                        "correct thumbnails).  See the dashboard notification "
                        "centre for the specific remediation steps."
                    )
                    dv5_software_fallback = True
                else:
                    logger.info(
                        f"Dolby Vision Profile 5 detected for {video_file}; "
                        f"using libplacebo tone mapping (hdr_format={hdr_fmt!r})"
                    )
                    use_libplacebo = True
                    # Pick the DV5 filter chain based on GPU vendor.
                    #
                    # Intel (iGPU + Arc DG2): use VAAPI decode + OpenCL tonemap.
                    #   Intel's VAAPI→Vulkan DMA-BUF interop path is broken
                    #   upstream (libplacebo's vkCreateImage returns
                    #   VK_ERROR_OUT_OF_DEVICE_MEMORY on Mesa ANV for the
                    #   format+modifier combinations used for DV5 hwmap —
                    #   reproduces on my own UHD 770 in-container and on
                    #   the reporter's Arc A380).  Jellyfin-ffmpeg's
                    #   patched tonemap_opencl reads DV RPU side-data and
                    #   produces correct colours — benchmarked 17x/0 CPU
                    #   on UHD 770.  See issue #212.
                    #
                    # AMD Radeon: use VAAPI→Vulkan DMA-BUF libplacebo.
                    #   Jellyfin ships this pattern in production for
                    #   discrete AMD cards; untested locally but FFmpeg
                    #   flags are vendor-agnostic.
                    #
                    # NVIDIA: CUDA decode (set in _run_ffmpeg) + Vulkan
                    #   libplacebo via hwupload of CPU frames.
                    #
                    # Other / no device path: software decode + libplacebo
                    #   via plain vulkan=vk + hwupload.
                    # Filter-chain assembly for each vendor's DV5 path lives
                    # in :func:`build_dv5_vf` at module top.  The reasoning
                    # about fps-first placement, contrast/saturation, and
                    # per-vendor hwmap/hwupload choices is documented there.
                    if (
                        gpu == "INTEL"
                        and gpu_device_path is not None
                        and gpu_device_path.startswith("/dev/dri/")
                    ):
                        use_intel_opencl_dv5_path = True
                        path_kind = DV5_PATH_INTEL_OPENCL
                    else:
                        use_vaapi_dv5_path = bool(
                            gpu is not None
                            and gpu != "NVIDIA"
                            and gpu_device_path is not None
                            and gpu_device_path.startswith("/dev/dri/")
                        )
                        path_kind = (
                            DV5_PATH_VAAPI_VULKAN
                            if use_vaapi_dv5_path
                            else DV5_PATH_LIBPLACEBO
                        )
                    libplacebo_vf = build_dv5_vf(
                        path_kind=path_kind,
                        tonemap_algorithm=config.tonemap_algorithm,
                        fps_value=fps_value,
                        base_scale=base_scale,
                    )
            elif _is_dolby_vision(hdr_fmt):
                # Dolby Vision Profile 7/8 with HDR10 backward-compat
                # base layer.  FFmpeg reads the HDR10 base layer by
                # default, so the standard zscale/tonemap chain works
                # correctly.  This avoids all libplacebo/RPU complexity.
                logger.info(
                    f"Dolby Vision with HDR10 fallback detected for "
                    f"{video_file}; using HDR10 base layer for tone "
                    f"mapping (hdr_format={hdr_fmt!r})"
                )
            # For both DV-with-fallback (above) and non-DV HDR, use
            # the zscale/tonemap chain.  Skip for DV5 software fallback:
            # zscale on a DV5 stream (no HDR10 base) produces a green
            # overlay, so the default fps+scale chain is used instead.
            if not use_libplacebo and not dv5_software_fallback:
                # HDR10 or DV Profile 7/8 (HDR10 base layer).  zscale
                # tonemap chain.  npl=100 (SDR reference white) is the
                # standard value for PQ-to-linear conversion.  Using
                # MaxCLL here would normalise all luminance to the
                # content peak, making typical scene content
                # (50-200 nits) map to tiny linear values that barely
                # get tone mapped → dark output.
                path_kind = "hdr10_zscale"

    def _gpu_scale_segment(
        effective_gpu: Optional[str], hw_decode_active: bool, fmt: str
    ) -> Optional[str]:
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
        effective_gpu: Optional[str],
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
        gpu_override: Optional[str] = None,
        gpu_device_path_override: Optional[str] = None,
        vf_override: Optional[str] = None,
        init_vulkan: bool = False,
        disable_vaapi_dv5: bool = False,
        path_kind_override: Optional[str] = None,
    ) -> Tuple[int, float, float, List[str]]:
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
            ffmpeg_threads_override
            if ffmpeg_threads_override is not None
            else config.ffmpeg_threads
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
            gpu_device_path_override
            if gpu_device_path_override is not None
            else gpu_device_path
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
            elif effective_gpu_device_path and effective_gpu_device_path.startswith(
                "/dev/dri/"
            ):
                # -hwaccel_device (not the deprecated -vaapi_device)
                # pairs with -hwaccel_output_format vaapi so decoded
                # frames stay in VAAPI surfaces for scale_vaapi.  Pre-
                # refactor this path also used scale_vaapi and only
                # hwdownloaded the 320x240 frame; issue #218 restores
                # that.
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
                f"Skipping HW decode for DV Profile 5 ({video_file}) on "
                f"{effective_gpu}: no VAAPI render device available; "
                f"using software decode + Vulkan/libplacebo tone mapping"
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
        logger.info(f"Encoding thumbnails for {video_file} ({hw_label})")
        logger.info(f"FFmpeg command: {' '.join(args)}")

        # When the Layer-3 probe retry in gpu_detection succeeded only with
        # VK_DRIVER_FILES set, propagate those env overrides to the real
        # FFmpeg invocation on the libplacebo DV Profile 5 path. On every
        # other path the override dict is empty and we pass env=None so
        # the child process inherits the parent environment unchanged.
        ffmpeg_env: dict | None = None
        if init_vulkan:
            from .gpu_detection import get_vulkan_env_overrides

            vulkan_overrides = get_vulkan_env_overrides()
            if vulkan_overrides:
                ffmpeg_env = os.environ.copy()
                ffmpeg_env.update(vulkan_overrides)
                logger.debug(
                    f"FFmpeg libplacebo path: injecting Vulkan env overrides "
                    f"{vulkan_overrides} into subprocess"
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
            while proc.poll() is None:
                if cancel_check and cancel_check():
                    logger.info(
                        f"Cancellation requested, terminating FFmpeg for {video_file}"
                    )
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()
                    raise CancellationError(f"Processing cancelled for {video_file}")
                if os.path.exists(output_file):
                    try:
                        with open(output_file, "r", encoding="utf-8") as f:
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
                    except (OSError, IOError):
                        pass
                if time.time() - last_progress_time > FFMPEG_STALL_TIMEOUT_SEC:
                    logger.warning(
                        f"FFmpeg stalled (no progress for {FFMPEG_STALL_TIMEOUT_SEC}s), killing process for {video_file}"
                    )
                    stalled = True
                    proc.kill()
                    proc.wait()
                    break
                time.sleep(0.005)

            # Process any remaining data
            if os.path.exists(output_file):
                try:
                    with open(output_file, "r", encoding="utf-8") as f:
                        lines = f.readlines()
                        if len(lines) > line_count:
                            for i in range(line_count, len(lines)):
                                line = lines[i].strip()
                                if line:
                                    ffmpeg_output_lines.append(line)
                                    total_duration = parse_ffmpeg_progress_line(
                                        line, total_duration, speed_capture_callback
                                    )
                except (OSError, IOError):
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
                f"FFmpeg failed with return code {proc.returncode} ({exit_diagnosis}) for {video_file}"
            )

            # Log last few stderr lines at WARNING level so users can diagnose
            # failures without needing DEBUG mode (especially for crashes/signals)
            if _is_signal_killed(proc.returncode):
                signal_detail = _diagnose_ffmpeg_exit_code(proc.returncode).split(
                    ":", 1
                )[1]
                logger.warning(
                    f"FFmpeg exited with code {proc.returncode} due to signal {signal_detail} for {video_file}"
                )
            elif exit_diagnosis == "io_error":
                logger.warning(
                    f"FFmpeg reported an I/O error while writing temp output for {video_file}; "
                    f"temp_directory={output_folder}, exists={os.path.isdir(output_folder)}"
                )
            elif exit_diagnosis == "high_exit_non_signal":
                logger.warning(
                    f"FFmpeg exited with high non-signal code {proc.returncode} for {video_file}; "
                    "likely a runtime/internal failure rather than process signal termination"
                )
            if ffmpeg_output_lines:
                tail = ffmpeg_output_lines[-5:]
                logger.warning(
                    f"FFmpeg stderr (last {len(tail)} lines) for {video_file}:"
                )
                for line in tail:
                    logger.warning(f"  {line}")

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
                logger.info(f"Permission error detected while processing {video_file}:")
                for error_line in permission_errors[
                    :3
                ]:  # Show up to 3 permission error lines
                    logger.info(f"  {error_line}")
                if len(permission_errors) > 3:
                    logger.info(
                        f"  ... and {len(permission_errors) - 3} more permission-related error(s)"
                    )

            # Log full FFmpeg output at DEBUG level for detailed troubleshooting.
            # When config.log_level=DEBUG, FFmpeg itself is invoked with
            # -loglevel debug (see ffmpeg_loglevel above), so these lines
            # include VAAPI / Vulkan / Mesa / libplacebo internals — exactly
            # what's needed to diagnose hwaccel and filter-graph failures.
            logger.debug(f"FFmpeg output ({len(ffmpeg_output_lines)} lines):")
            for i, line in enumerate(ffmpeg_output_lines):
                logger.debug(f"  {i + 1:3d}: {line}")

            # Save full FFmpeg stderr to a per-file log for post-mortem debugging
            _save_ffmpeg_failure_log(video_file, proc.returncode, ffmpeg_output_lines)

        end_local = time.time()
        seconds_local = round(end_local - start_local, 1)
        # Calculate fallback speed if needed
        if (
            speed_local == "0.0x"
            and total_duration
            and total_duration > 0
            and seconds_local > 0
        ):
            calculated_speed = total_duration / seconds_local
            speed_local = f"{calculated_speed:.0f}x"

        return proc.returncode, seconds_local, speed_local, ffmpeg_output_lines

    # DV Profile 5 paths cannot use -skip_frame (RPU side-data has
    # inter-frame dependencies). Everything else attempts skip_frame
    # first and falls back via the retry below if the decoder rejects it.
    use_skip_initial = not (use_libplacebo or dv5_software_fallback)

    # Ensure output folder exists
    os.makedirs(output_folder, exist_ok=True)

    # First attempt
    rc, seconds, speed, stderr_lines = _run_ffmpeg(
        use_skip_initial, init_vulkan=use_libplacebo
    )
    stderr_lines_all: List[str] = list(stderr_lines) if stderr_lines else []

    # Retry once without skip_frame only if FFmpeg returned non-zero and we tried with skip
    # (If we didn't use skip initially, retrying without skip would just repeat the same command)
    did_retry = False
    retry_rc = rc
    retry_stderr_lines = stderr_lines

    if rc != 0 and use_skip_initial:
        if cancel_check and cancel_check():
            raise CancellationError(f"Processing cancelled for {video_file}")
        did_retry = True
        logger.warning(
            f"No thumbnails generated from {video_file} with -skip_frame; retrying without skip-frame"
        )
        # Clean up any partial files from first attempt (no need to rename if we're retrying)
        _clean_output_images(output_folder)
        retry_rc, seconds, speed, retry_stderr_lines = _run_ffmpeg(
            use_skip=False, init_vulkan=use_libplacebo
        )
        # Update rc and stderr_lines to retry results for codec error detection
        rc = retry_rc
        stderr_lines = retry_stderr_lines
        if retry_stderr_lines:
            stderr_lines_all.extend(retry_stderr_lines)

    # Count images first to see if we have any (even if rc != 0, we might have partial success)
    image_count = len(glob.glob(os.path.join(output_folder, "img*.jpg")))

    # Hardware DV5 path unavailable — retry with software decode + libplacebo.
    #
    # Two known upstream scenarios trigger this:
    #   * Intel iGPU/dGPU under NVIDIA Container Runtime (before the
    #     init-dri-by-path s6 fixup lands on the host): NEO enumerates
    #     GPUs via /dev/dri/by-path/, which the NVIDIA runtime only
    #     populates for the NVIDIA cards it manages.  tonemap_opencl
    #     then fails with "No matching devices found" (CL -19).
    #   * AMD Radeon / Intel Arc DG2 via Mesa ANV: VAAPI→Vulkan DMA-BUF
    #     import succeeds but libplacebo's vkCreateImage returns
    #     VK_ERROR_OUT_OF_DEVICE_MEMORY (see libplacebo!117, mpv#8702)
    #     — generic format/modifier-not-supported, not real OOM.
    #
    # Software decode + libplacebo still produces correct DV tonemapping
    # at ~5-10× (CPU-bound HEVC) — preferable to falling through to the
    # DV-safe fps+scale chain (~1.7× and dim output).
    did_sw_libplacebo_retry = False
    if (
        rc != 0
        and image_count == 0
        and (use_vaapi_dv5_path or use_intel_opencl_dv5_path)
    ):
        if cancel_check and cancel_check():
            raise CancellationError(f"Processing cancelled for {video_file}")
        did_sw_libplacebo_retry = True
        hw_name = "Intel OpenCL" if use_intel_opencl_dv5_path else "VAAPI+Vulkan"
        if use_intel_opencl_dv5_path:
            reason = (
                "Intel OpenCL init failed — uncommon, usually a "
                "container runtime / ICD conflict"
            )
        else:
            reason = (
                "VAAPI→Vulkan libplacebo interop upstream bug "
                "(Mesa ANV / amdvlk on some driver+GPU combos)"
            )
        logger.warning(
            f"Hardware {hw_name} DV5 path unavailable for {video_file} "
            f"({reason}); falling back to software decode + libplacebo "
            f"(correct DV tonemapping, ~5-10× typical)"
        )
        stderr_excerpt = (
            "\n".join(stderr_lines_all[-5:]) if stderr_lines_all else "No stderr output"
        )
        logger.debug(f"FFmpeg stderr excerpt (last 5 lines): {stderr_excerpt}")
        _clean_output_images(output_folder)
        # Same filter shape as the NVIDIA/software primary DV5 path; run
        # here without hardware decode so it works on any host with a
        # hardware Vulkan device.
        sw_libplacebo_vf = build_dv5_vf(
            path_kind=DV5_PATH_LIBPLACEBO,
            tonemap_algorithm=config.tonemap_algorithm,
            fps_value=fps_value,
            base_scale=base_scale,
        )
        rc, seconds, speed, stderr_lines = _run_ffmpeg(
            use_skip=False,
            init_vulkan=True,
            disable_vaapi_dv5=True,
            vf_override=sw_libplacebo_vf,
        )
        if stderr_lines:
            stderr_lines_all.extend(stderr_lines)
        image_count = len(glob.glob(os.path.join(output_folder, "img*.jpg")))

    did_dv_safe_retry = False

    # Dolby Vision / HDR colorspace errors can abort FFmpeg when the
    # zscale/tonemap or libplacebo filter chain encounters unsupported
    # transfer characteristics or RPU parsing failures.
    # On both CPU and GPU, retry once with a DV-safe filter chain that
    # avoids zscale/tonemap/libplacebo entirely.
    if rc != 0 and image_count == 0:
        if cancel_check and cancel_check():
            raise CancellationError(f"Processing cancelled for {video_file}")
        is_dv_rpu = _detect_dolby_vision_rpu_error(stderr_lines_all)
        is_zscale = _detect_zscale_colorspace_error(stderr_lines_all)
        # libplacebo failure: if the libplacebo chain was active and FFmpeg
        # failed, fall back to the basic fps+scale chain regardless of the
        # specific error message (Vulkan not available, driver issue, etc.).
        is_libplacebo_fail = use_libplacebo and not is_dv_rpu and not is_zscale
        if is_dv_rpu or is_zscale or is_libplacebo_fail:
            did_dv_safe_retry = True
            if is_dv_rpu:
                diag_label = "Dolby Vision RPU parsing error"
            elif is_zscale:
                diag_label = "zscale colorspace conversion error"
            else:
                diag_label = "libplacebo tone mapping error"
            stderr_excerpt_source = (
                stderr_lines_all if stderr_lines_all else stderr_lines
            )
            stderr_excerpt = (
                "\n".join(stderr_excerpt_source[-5:])
                if len(stderr_excerpt_source) > 0
                else "No stderr output"
            )
            logger.warning(
                f"{diag_label} detected for {video_file}; retrying with DV-safe filter chain (fps+scale)"
            )
            logger.debug(f"FFmpeg stderr excerpt (last 5 lines): {stderr_excerpt}")

            # Clean up any partial files before retrying
            _clean_output_images(output_folder)

            # DV-safe filter: avoid zscale/tonemap; mirror the known-working
            # workaround in issue #130.  path_kind_override="sdr" lets
            # _assemble_vf build the vendor-correct SDR chain — including
            # scale_cuda / scale_vaapi + hwdownload when GPU decode is
            # still active — so the retry doesn't choke on -hwaccel_output_format
            # surfaces feeding a CPU-only scale filter.
            rc, seconds, speed, stderr_lines = _run_ffmpeg(
                use_skip=False, path_kind_override="sdr"
            )
            if stderr_lines:
                stderr_lines_all.extend(stderr_lines)
            image_count = len(glob.glob(os.path.join(output_folder, "img*.jpg")))

            if rc != 0 and image_count == 0:
                if gpu is not None:
                    # Still failing on GPU even with DV-safe filter -> hand off to CPU worker.
                    _clean_output_images(output_folder)
                    raise CodecNotSupportedError(
                        f"{diag_label} in GPU context for {video_file}"
                    )
                else:
                    # Already on CPU: no further fallback available without remuxing/bitstream filtering.
                    logger.error(
                        f"{diag_label} detected for {video_file}; unable to generate thumbnails on CPU even with DV-safe filter"
                    )
                    logger.info(
                        "If this persists, try upgrading FFmpeg or re-encoding/remuxing the file to remove Dolby Vision metadata."
                    )

    # Check for codec errors or crash signals after every prior retry tier
    # has had a chance: skip-frame retry (earliest, ~line 1614), software-
    # libplacebo retry (~line 1660), DV-safe fps+scale retry (~line 1721).
    # If this is still a GPU context and a codec/crash error is detected,
    # raise so the worker pool can hand off to a CPU worker.

    if rc != 0 and image_count == 0 and gpu is not None:
        if cancel_check and cancel_check():
            raise CancellationError(f"Processing cancelled for {video_file}")
        should_fallback = _detect_codec_error(rc, stderr_lines)
        fallback_reason = "codec error"

        # Detect GPU hardware accelerator runtime errors (surface sync, transfer
        # failures, CUDA errors etc.) — these are driver-level issues that almost
        # always succeed on CPU.
        if not should_fallback and _detect_hwaccel_runtime_error(stderr_lines_all):
            should_fallback = True
            fallback_reason = "hardware accelerator runtime error"

        # Also fall back to CPU if FFmpeg was killed by a signal (crash, OOM, segfault).
        # GPU decode paths can trigger driver bugs or OOM that don't occur on CPU.
        if not should_fallback and _is_signal_killed(rc):
            should_fallback = True
            signal_num = rc - 128 if rc > 128 else rc
            fallback_reason = f"signal kill (signal {signal_num})"

        if should_fallback:
            # Log relevant stderr excerpt for debugging
            stderr_excerpt = (
                "\n".join(stderr_lines[-5:])
                if len(stderr_lines) > 0
                else "No stderr output"
            )
            logger.warning(
                f"GPU processing failed with {fallback_reason} (exit code {rc}) for {video_file}; will hand off to CPU worker"
            )
            logger.debug(f"FFmpeg stderr excerpt (last 5 lines): {stderr_excerpt}")
            # Clean up any partial files from GPU attempts
            _clean_output_images(output_folder)
            # Raise exception to signal worker pool to re-queue for CPU worker
            raise CodecNotSupportedError(
                f"GPU processing failed ({fallback_reason}) for {video_file} (exit code {rc})"
            )

    if rc != 0 and image_count == 0 and gpu is None:
        if _detect_codec_error(rc, stderr_lines):
            logger.warning(
                f"Processing failed with codec error (exit code {rc}) for {video_file}; file may be corrupted or unsupported"
            )

    # Rename images only after all retries and error checks are complete
    if image_count > 0:
        for image in glob.glob(f"{output_folder}/img*.jpg"):
            frame_no = int(re.search(r"(\d+)", os.path.basename(image)).group(1)) - 1
            frame_second = frame_no * config.plex_bif_frame_interval
            os.rename(image, os.path.join(output_folder, f"{frame_second:010d}.jpg"))
        image_count = len(glob.glob(os.path.join(output_folder, "*.jpg")))

    hw = gpu is not None
    success = image_count > 0
    error_summary = ""

    if success:
        fallback_suffix = (
            " (DV-safe retry)"
            if did_dv_safe_retry
            else (
                " (sw libplacebo retry)"
                if did_sw_libplacebo_retry
                else (" (retry no-skip)" if did_retry else "")
            )
        )
        logger.info(
            f"Generated Video Preview for {video_file} HW={hw} TIME={seconds}seconds SPEED={speed} IMAGES={image_count}{fallback_suffix}"
        )
    else:
        fallback_suffix = (
            " after DV-safe retry"
            if did_dv_safe_retry
            else (
                " after sw libplacebo retry"
                if did_sw_libplacebo_retry
                else (" after retry" if did_retry else "")
            )
        )
        logger.error(
            f"Failed to generate thumbnails for {video_file}; 0 images produced{fallback_suffix}"
        )
        error_summary = _extract_ffmpeg_error_summary(stderr_lines_all)
        worker_ctx = "GPU" if gpu is not None else "CPU"
        reason = (
            f"FFmpeg exit {rc} ({_diagnose_ffmpeg_exit_code(rc)}){fallback_suffix}"
            if rc != 0
            else f"0 images{fallback_suffix}"
        )
        if error_summary:
            reason = f"{reason} — {error_summary}"
        record_failure(video_file, rc, reason, worker_type=worker_ctx)

    return success, image_count, hw, seconds, speed, error_summary


def _setup_bundle_paths(bundle_hash: str, config: Config) -> Tuple[str, str, str]:
    """Set up all bundle-related paths.

    Args:
        bundle_hash: Bundle hash from Plex
        config: Configuration object

    Returns:
        Tuple of (indexes_path, index_bif, tmp_path)

    """
    bundle_file = sanitize_path(f"{bundle_hash[0]}/{bundle_hash[1::1]}.bundle")
    bundle_path = sanitize_path(
        os.path.join(config.plex_config_folder, "Media", "localhost", bundle_file)
    )
    indexes_path = sanitize_path(os.path.join(bundle_path, "Contents", "Indexes"))
    index_bif = sanitize_path(os.path.join(indexes_path, "index-sd.bif"))
    tmp_path = sanitize_path(os.path.join(config.working_tmp_folder, bundle_hash))
    return indexes_path, index_bif, tmp_path


def _ensure_directories(indexes_path: str, tmp_path: str, media_file: str) -> bool:
    """Ensure required directories exist.

    Args:
        indexes_path: Path to indexes directory
        tmp_path: Path to temporary directory
        media_file: Media file path for error messages

    Returns:
        True if directories are ready, False if creation failed

    """
    if not os.path.isdir(indexes_path):
        try:
            os.makedirs(indexes_path)
        except PermissionError as e:
            logger.error(
                f"Permission denied creating index path {indexes_path} for {media_file}: {e}"
            )
            logger.info(
                f"Please check directory permissions for: {os.path.dirname(indexes_path)}"
            )
            return False
        except OSError as e:
            logger.error(
                f"Error generating images for {media_file}. `{type(e).__name__}:{str(e)}` error when creating index path {indexes_path}"
            )
            return False

    if not os.path.isdir(tmp_path):
        try:
            os.makedirs(tmp_path)
        except PermissionError as e:
            logger.error(
                f"Permission denied creating tmp path {tmp_path} for {media_file}: {e}"
            )
            logger.info(
                f"Please check directory permissions for: {os.path.dirname(tmp_path)}"
            )
            return False
        except OSError as e:
            logger.error(
                f"Error generating images for {media_file}. `{type(e).__name__}:{str(e)}` error when creating tmp path {tmp_path}"
            )
            return False

    return True


def _cleanup_temp_directory(tmp_path: str) -> None:
    """Clean up temporary directory, logging warnings on failure.

    Args:
        tmp_path: Path to temporary directory

    """
    try:
        if os.path.exists(tmp_path):
            logger.debug(f"Cleaning up temp directory: {tmp_path}")
            shutil.rmtree(tmp_path)
            logger.debug(f"Cleaned up temp directory: {tmp_path}")
        else:
            logger.debug(f"Temp directory already absent, skipping cleanup: {tmp_path}")
    except Exception as cleanup_error:
        logger.warning(f"Failed to clean up temp directory {tmp_path}: {cleanup_error}")


def _generate_and_save_bif(
    media_file: str,
    tmp_path: str,
    index_bif: str,
    gpu: Optional[str],
    gpu_device_path: Optional[str],
    config: Config,
    progress_callback=None,
    ffmpeg_threads_override: Optional[int] = None,
    cancel_check=None,
) -> None:
    """Generate images and create BIF file.

    Args:
        media_file: Path to media file.
        tmp_path: Temporary directory for images.
        index_bif: Path to output BIF file.
        gpu: GPU type for acceleration.
        gpu_device_path: GPU device path.
        config: Configuration object.
        progress_callback: Callback function for progress updates.
        ffmpeg_threads_override: Per-GPU FFmpeg thread cap (overrides
            config.ffmpeg_threads when set).
        cancel_check: Optional callable returning True when job is cancelled.

    Raises:
        CancellationError: If job was cancelled during processing.
        CodecNotSupportedError: If codec is not supported by GPU.
        RuntimeError: If thumbnail generation produced 0 images.

    """
    try:
        gen_result = generate_images(
            media_file,
            tmp_path,
            gpu,
            gpu_device_path,
            config,
            progress_callback,
            ffmpeg_threads_override=ffmpeg_threads_override,
            cancel_check=cancel_check,
        )
    except (CancellationError, CodecNotSupportedError):
        _cleanup_temp_directory(tmp_path)
        raise
    except Exception as e:
        logger.error(
            f"Error generating images for {media_file}. `{type(e).__name__}:{str(e)}` error when generating images"
        )
        _cleanup_temp_directory(tmp_path)
        raise RuntimeError(f"Failed to generate images: {e}") from e

    # Determine image count and error summary from result or by scanning
    image_count = 0
    ffmpeg_error = ""
    if isinstance(gen_result, tuple) and len(gen_result) >= 2:
        _, image_count = bool(gen_result[0]), int(gen_result[1])
        if len(gen_result) >= 6:
            ffmpeg_error = gen_result[5] or ""
    else:
        if os.path.isdir(tmp_path):
            image_count = len(glob.glob(os.path.join(tmp_path, "*.jpg")))

    if image_count == 0:
        logger.error(f"No thumbnails generated for {media_file}; skipping BIF creation")
        _cleanup_temp_directory(tmp_path)
        detail = f" ({ffmpeg_error})" if ffmpeg_error else ""
        raise RuntimeError(
            f"Thumbnail generation produced 0 images for {media_file}{detail}"
        )

    # Generate BIF file
    try:
        generate_bif(index_bif, tmp_path, config)
    except PermissionError as e:
        # Remove BIF if generation failed
        try:
            if os.path.exists(index_bif):
                os.remove(index_bif)
        except Exception as remove_error:
            logger.warning(
                f"Failed to remove failed BIF file {index_bif}: {remove_error}"
            )
        logger.error(
            f"Permission denied generating BIF file {index_bif} for {media_file}: {e}"
        )
        logger.info(f"Please check write permissions for: {os.path.dirname(index_bif)}")
        raise
    except Exception as e:
        # PermissionError is already handled above, so this catches other exceptions
        logger.error(
            f"Error generating images for {media_file}. `{type(e).__name__}:{str(e)}` error when generating bif"
        )
        # Remove BIF if generation failed
        try:
            if os.path.exists(index_bif):
                os.remove(index_bif)
        except Exception as remove_error:
            logger.warning(
                f"Failed to remove failed BIF file {index_bif}: {remove_error}"
            )
        raise


def generate_bif(bif_filename: str, images_path: str, config: Config) -> None:
    """Build a .bif file from thumbnail images.

    Args:
        bif_filename: Path to output .bif file
        images_path: Directory containing .jpg thumbnail images
        config: Configuration object

    Raises:
        PermissionError: If permission denied accessing files or directories

    """
    magic = [0x89, 0x42, 0x49, 0x46, 0x0D, 0x0A, 0x1A, 0x0A]
    version = 0

    try:
        images = [
            img for img in os.listdir(images_path) if os.path.splitext(img)[1] == ".jpg"
        ]
    except PermissionError as e:
        logger.error(f"Permission denied reading images directory {images_path}: {e}")
        logger.info(f"Please check read permissions for: {images_path}")
        raise
    images.sort()

    try:
        f = open(bif_filename, "wb")
    except PermissionError as e:
        logger.error(f"Permission denied writing BIF file {bif_filename}: {e}")
        logger.info(
            f"Please check write permissions for: {os.path.dirname(bif_filename)}"
        )
        raise

    try:
        with f:
            array.array("B", magic).tofile(f)
            f.write(struct.pack("<I", version))
            f.write(struct.pack("<I", len(images)))
            f.write(struct.pack("<I", 1000 * config.plex_bif_frame_interval))
            array.array("B", [0x00 for x in range(20, 64)]).tofile(f)

            bif_table_size = 8 + (8 * len(images))
            image_index = 64 + bif_table_size
            timestamp = 0

            # Get the length of each image
            for image in images:
                try:
                    statinfo = os.stat(os.path.join(images_path, image))
                except PermissionError as e:
                    logger.error(
                        f"Permission denied reading image file {os.path.join(images_path, image)}: {e}"
                    )
                    logger.info(f"Please check read permissions for: {images_path}")
                    raise
                f.write(struct.pack("<I", timestamp))
                f.write(struct.pack("<I", image_index))
                timestamp += 1
                image_index += statinfo.st_size

            f.write(struct.pack("<I", 0xFFFFFFFF))
            f.write(struct.pack("<I", image_index))

            # Now copy the images
            for image in images:
                try:
                    with open(os.path.join(images_path, image), "rb") as img_file:
                        data = img_file.read()
                except PermissionError as e:
                    logger.error(
                        f"Permission denied reading image file {os.path.join(images_path, image)}: {e}"
                    )
                    logger.info(f"Please check read permissions for: {images_path}")
                    raise
                f.write(data)
    except PermissionError:
        # Re-raise PermissionError (already logged above)
        raise
    logger.info(f"Generated BIF file: {bif_filename} ({len(images)} thumbnails)")


def process_item(
    item_key: str,
    gpu: Optional[str],
    gpu_device_path: Optional[str],
    config: Config,
    plex,
    progress_callback=None,
    ffmpeg_threads_override: Optional[int] = None,
    cancel_check=None,
    worker_name: str = "",
) -> ProcessingResult:
    """Process a single media item: generate thumbnails and BIF file.

    This is the core processing function that handles:
    - Plex API queries
    - Path mapping for remote generation
    - Bundle hash generation
    - Plex directory structure creation
    - Thumbnail generation with FFmpeg
    - BIF file creation
    - Cleanup

    Args:
        item_key: Plex media item key.
        gpu: GPU type for acceleration.
        gpu_device_path: GPU device path.
        config: Configuration object.
        plex: Plex server instance.
        progress_callback: Callback function for progress updates.
        ffmpeg_threads_override: Per-GPU FFmpeg thread cap (overrides
            config.ffmpeg_threads when set).
        cancel_check: Optional callable returning True when job is cancelled.
        worker_name: Display name of the worker processing this item.

    Returns:
        ProcessingResult indicating the outcome. When an item has multiple
        media parts, the most significant outcome is returned (GENERATED
        wins over any skip; FAILED wins over skips other than file-not-found).

    """
    try:
        data = retry_plex_call(plex.query, f"{item_key}/tree")
    except Exception as e:
        logger.error(f"Failed to query Plex for item {item_key} after retries: {e}")
        logger.error(f"Exception type: {type(e).__name__}")
        if hasattr(e, "request") and e.request:
            logger.error(f"Request URL: {e.request.url}")
            logger.error(f"Request method: {e.request.method}")
            safe_headers = {
                k: ("****" if "token" in k.lower() else v)
                for k, v in e.request.headers.items()
            }
            logger.error(f"Request headers: {safe_headers}")
        _notify_file_result(
            f"item:{item_key}",
            ProcessingResult.FAILED,
            f"Plex API query failed: {type(e).__name__}",
            worker_name,
        )
        return ProcessingResult.FAILED

    best_result = ProcessingResult.NO_MEDIA_PARTS

    def _update_best(result: ProcessingResult) -> None:
        nonlocal best_result
        if _RESULT_PRIORITY[result] > _RESULT_PRIORITY[best_result]:
            best_result = result

    for media_part in data.findall(".//MediaPart"):
        if "hash" in media_part.attrib:
            bundle_hash = media_part.attrib["hash"]
            plex_path = media_part.attrib["file"]
            mappings = getattr(config, "path_mappings", None) or []
            if mappings:
                media_file = sanitize_path(plex_path_to_local(plex_path, mappings))
            else:
                media_file = sanitize_path(plex_path)

            if is_path_excluded(media_file, getattr(config, "exclude_paths", None)):
                logger.info(f"Skipping (excluded path): {media_file}")
                _update_best(ProcessingResult.SKIPPED_EXCLUDED)
                _notify_file_result(
                    media_file,
                    ProcessingResult.SKIPPED_EXCLUDED,
                    "Path excluded by filter",
                    worker_name,
                )
                continue

            if not bundle_hash or len(bundle_hash) < 2:
                hash_value = f'"{bundle_hash}"' if bundle_hash else "(empty)"
                logger.warning(
                    f"Skipping {media_file} due to invalid bundle hash from Plex: {hash_value} (length: {len(bundle_hash) if bundle_hash else 0}, required: >= 2)"
                )
                _update_best(ProcessingResult.SKIPPED_INVALID_HASH)
                _notify_file_result(
                    media_file,
                    ProcessingResult.SKIPPED_INVALID_HASH,
                    f"Invalid bundle hash: {hash_value}",
                    worker_name,
                )
                continue

            if not os.path.isfile(media_file):
                logger.warning(f"Skipping as file not found {media_file}")
                _update_best(ProcessingResult.SKIPPED_FILE_NOT_FOUND)
                _notify_file_result(
                    media_file,
                    ProcessingResult.SKIPPED_FILE_NOT_FOUND,
                    "File not found on disk",
                    worker_name,
                )
                continue

            try:
                indexes_path, index_bif, tmp_path = _setup_bundle_paths(
                    bundle_hash, config
                )
            except Exception as e:
                logger.error(
                    f"Error generating bundle_file for {media_file} due to {type(e).__name__}:{str(e)}"
                )
                _update_best(ProcessingResult.FAILED)
                _notify_file_result(
                    media_file,
                    ProcessingResult.FAILED,
                    f"Bundle path error: {type(e).__name__}: {e}",
                    worker_name,
                )
                continue

            if os.path.isfile(index_bif) and config.regenerate_thumbnails:
                logger.debug(
                    f"Deleting existing BIF file at {index_bif} to regenerate thumbnails for {media_file}"
                )
                try:
                    os.remove(index_bif)
                except Exception as e:
                    logger.error(
                        f"Error {type(e).__name__} deleting index file {media_file}: {str(e)}"
                    )
                    _update_best(ProcessingResult.FAILED)
                    _notify_file_result(
                        media_file,
                        ProcessingResult.FAILED,
                        f"Could not delete existing BIF: {e}",
                        worker_name,
                    )
                    continue

            if os.path.isfile(index_bif):
                logger.info(
                    f"Skipping {media_file} — BIF already exists at {index_bif}"
                )
                _update_best(ProcessingResult.SKIPPED_BIF_EXISTS)
                _notify_file_result(
                    media_file,
                    ProcessingResult.SKIPPED_BIF_EXISTS,
                    f"BIF exists at {index_bif}",
                    worker_name,
                )
                continue

            logger.info(f"Generating BIF for {media_file} -> {index_bif}")

            if not _ensure_directories(indexes_path, tmp_path, media_file):
                _update_best(ProcessingResult.FAILED)
                _notify_file_result(
                    media_file,
                    ProcessingResult.FAILED,
                    "Failed to create output directories",
                    worker_name,
                )
                continue

            try:
                _generate_and_save_bif(
                    media_file,
                    tmp_path,
                    index_bif,
                    gpu,
                    gpu_device_path,
                    config,
                    progress_callback,
                    ffmpeg_threads_override=ffmpeg_threads_override,
                    cancel_check=cancel_check,
                )
                _update_best(ProcessingResult.GENERATED)
                _notify_file_result(
                    media_file,
                    ProcessingResult.GENERATED,
                    "",
                    worker_name,
                )
            except (CancellationError, CodecNotSupportedError):
                raise
            except RuntimeError as e:
                logger.error(f"Error processing {media_file}: {str(e)}")
                _update_best(ProcessingResult.FAILED)
                _notify_file_result(
                    media_file,
                    ProcessingResult.FAILED,
                    str(e),
                    worker_name,
                )
                continue
            except Exception as e:
                logger.error(
                    f"Error processing {media_file}: {type(e).__name__}: {str(e)}"
                )
                _update_best(ProcessingResult.FAILED)
                _notify_file_result(
                    media_file,
                    ProcessingResult.FAILED,
                    f"{type(e).__name__}: {e}",
                    worker_name,
                )
                continue
            finally:
                _cleanup_temp_directory(tmp_path)

    return best_result
