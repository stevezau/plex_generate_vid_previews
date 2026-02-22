"""
Media processing functions for video thumbnail generation.

Handles FFmpeg execution, BIF file generation, and all media processing
logic including HDR detection, skip frame heuristics, and GPU acceleration.
"""

import array
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
from typing import List, Optional, Tuple

from loguru import logger

from .utils import sanitize_path

# ---------------------------------------------------------------------------
# Failure tracker — collects per-file failure info for end-of-run summary
# ---------------------------------------------------------------------------

_failure_lock = threading.Lock()
_failures: List[dict] = []


def record_failure(
    file_path: str, exit_code: int, reason: str, worker_type: str = ""
) -> None:
    """Record an FFmpeg / processing failure for the end-of-run summary.

    Args:
        file_path: Media file that failed.
        exit_code: FFmpeg return code (0 if not FFmpeg-related).
        reason: Short human-readable reason string.
        worker_type: 'GPU', 'CPU', or '' if unknown.
    """
    with _failure_lock:
        _failures.append(
            {
                "file": file_path,
                "exit_code": exit_code,
                "reason": reason,
                "worker_type": worker_type,
            }
        )


def get_failures() -> List[dict]:
    """Return a copy of the failure list (thread-safe)."""
    with _failure_lock:
        return list(_failures)


def clear_failures() -> None:
    """Reset the failure list (call between runs)."""
    with _failure_lock:
        _failures.clear()


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
    print("ERROR: pymediainfo Python package not found.")
    print("Please install: pip install pymediainfo")
    sys.exit(1)
except OSError as e:
    if "libmediainfo" in str(e).lower():
        print("ERROR: MediaInfo native library not found.")
        print("Please install MediaInfo:")
        if sys.platform == "darwin":  # macOS
            print("  macOS: brew install media-info")
        elif sys.platform.startswith("linux"):
            print("  Ubuntu/Debian: sudo apt-get install mediainfo libmediainfo-dev")
            print("  Fedora/RHEL: sudo dnf install mediainfo mediainfo-devel")
        else:
            print("  See: https://mediaarea.net/en/MediaInfo/Download")
        sys.exit(1)
except Exception as e:
    print(f"WARNING: Could not validate MediaInfo library: {e}")
    print("Proceeding anyway, but errors may occur during processing")

from .config import Config  # noqa: E402
from .plex_client import retry_plex_call  # noqa: E402


class CodecNotSupportedError(Exception):
    """
    Exception raised when a video codec is not supported by GPU hardware.

    This exception signals that the file should be processed by a CPU worker
    instead of attempting CPU fallback within the GPU worker thread.
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


def _detect_dolby_vision_rpu_error(stderr_lines: List[str]) -> bool:
    """
    Detect FFmpeg Dolby Vision RPU parsing failures that can abort processing.

    This is intentionally narrow to avoid false positives. It matches a small
    allow-list of known fatal signatures from upstream FFmpeg/libdovi output.

    Args:
        stderr_lines: List of FFmpeg stderr lines

    Returns:
        bool: True if the Dolby Vision RPU error is detected
    """
    if not stderr_lines:
        return False

    # Known fatal Dolby Vision parsing signatures (extend as new cases are reported).
    # Keep these specific to avoid triggering on benign informational/warning messages.
    fatal_signatures = [
        "multiple dolby vision rpus found in one au",
        # Some FFmpeg builds append additional context after the core message.
        "multiple dolby vision rpus found in one au. skipping previous.",
    ]

    stderr_text = " ".join(stderr_lines).lower()
    return any(sig in stderr_text for sig in fatal_signatures)


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


def _detect_zscale_colorspace_error(stderr_lines: List[str]) -> bool:
    """Detect zscale filter failures caused by unsupported colorspace conversions.

    Dolby Vision Profile 5 (and some other HDR flavours) use IPT-PQ or
    proprietary transfer characteristics that zscale cannot map to linear.
    FFmpeg emits ``code 3074: no path between colorspaces`` and then crashes
    the filter graph, typically producing exit code 187.

    Args:
        stderr_lines: List of FFmpeg stderr lines

    Returns:
        bool: True if a zscale colorspace error is detected
    """
    if not stderr_lines:
        return False

    stderr_text = " ".join(stderr_lines).lower()

    # Exact substring signatures — the most reliable patterns.
    fatal_signatures = [
        "no path between colorspaces",
        "zscale: generic error in an external library",
    ]
    if any(sig in stderr_text for sig in fatal_signatures):
        return True

    # FFmpeg may log the filter name in brackets with an address, e.g.
    # [Parsed_zscale_1 @ 0x55eb] Generic error in an external library
    # [vf#0:0/zscale @ 0x5f3a] Generic error in an external library
    # Match these even when "zscale:" doesn't appear as a bare prefix.
    if re.search(
        r"parsed_zscale_\d+.*generic error in an external library", stderr_text
    ):
        return True
    if re.search(
        r"zscale\s*@\s*0x[0-9a-f]+\].*generic error in an external library",
        stderr_text,
    ):
        return True

    return False


def _is_dv_no_backward_compat(hdr_format: Optional[str]) -> bool:
    """Detect Dolby Vision content without a backward-compatible HDR base layer.

    DV Profile 5 (and similar) uses IPT-PQ transfer characteristics that the
    zscale tonemap filter chain cannot handle (``code 3074: no path between
    colorspaces``).  When the Dolby Vision metadata does **not** include a
    backward-compatible layer (HDR10, HLG, etc.) we must skip zscale/tonemap
    entirely to avoid a guaranteed crash.

    MediaInfo reports ``hdr_format`` as a comma-separated description, e.g.:

    * ``"Dolby Vision, Version 1.0, dvhe.05.06, BL+EL+RPU"``  (Profile 5)
    * ``"Dolby Vision, Version 1.0, dvhe.08.06, BL+RPU, HDR10 compatible / SMPTE ST 2086"``
    * ``"Dolby Vision / SMPTE ST 2086, HDR10 compatible"``

    The function returns ``True`` when Dolby Vision is present **and** no
    backward-compatible HDR format (HDR10, HLG, PQ10, SMPTE ST 2086, etc.)
    is declared alongside it.

    Args:
        hdr_format: Value of ``MediaInfo.video_tracks[0].hdr_format``.

    Returns:
        bool: ``True`` if content is DV without backward compat (unsafe for
              zscale/tonemap), ``False`` otherwise.
    """
    if not hdr_format or hdr_format == "None":
        return False

    hdr_lower = hdr_format.lower()

    # Must contain Dolby Vision to be relevant
    if "dolby vision" not in hdr_lower:
        return False

    # Profiles that use IPT-PQ transfer — unsafe for zscale/tonemap.
    # Profile 5 (HEVC): dvhe.05  |  Profile 4 (HEVC, non-backward-compat): dvhe.04
    # AV1 DV Profile 5: dvav.05  |  AV1 DV set/entry: dvav.se
    dv_unsafe_profiles = ["dvhe.05", "dvhe.04", "dvav.05", "dvav.se"]
    if any(tag in hdr_lower for tag in dv_unsafe_profiles):
        return True

    # Backward-compatible keywords — if any are present alongside DV, the
    # base layer is HDR10/HLG/etc. and zscale/tonemap will work.
    backward_compat_keywords = [
        "hdr10",
        "hlg",
        "pq10",
        "smpte st 2086",
        "smpte st 2094",
        "compatible",
        "compat",
    ]
    has_backward_compat = any(kw in hdr_lower for kw in backward_compat_keywords)

    # DV present but NO backward-compatible layer → unsafe for zscale
    return not has_backward_compat


def parse_ffmpeg_progress_line(
    line: str, total_duration: float, progress_callback=None
):
    """
    Parse a single FFmpeg progress line and call progress callback if provided.

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

            # Update progress
            progress_percent = 0
            if total_duration and total_duration > 0:
                progress_percent = min(100, int((current_time / total_duration) * 100))

            # Calculate remaining time from FFmpeg data
            remaining_time = 0
            if total_duration and total_duration > 0 and current_time < total_duration:
                remaining_time = total_duration - current_time

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
    """
    Detect if FFmpeg failure is due to unsupported codec/hardware decoder error.

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


def heuristic_allows_skip(ffmpeg_path: str, video_file: str) -> bool:
    """Probe the first 10 frames to decide if ``-skip_frame:v nokey`` is safe.

    Uses ``-err_detect explode`` + ``-xerror`` to bail immediately on the
    first decode error.  Returns ``True`` if the probe succeeds (zero exit
    code), else ``False``.

    .. note::

       For Dolby Vision content this function almost always returns ``False``
       because ``-err_detect explode`` triggers on RPU NAL-unit parsing
       artefacts.  This is **benign** — it simply forces the slower no-skip
       decode path, which is the correct behaviour for DV anyway.

    Args:
        ffmpeg_path: Path to the FFmpeg binary.
        video_file: Path to the media file to probe.

    Returns:
        bool: ``True`` when skip_frame is safe, ``False`` otherwise.
    """
    null_sink = "NUL" if os.name == "nt" else "/dev/null"
    cmd = [
        ffmpeg_path,
        "-hide_banner",
        "-nostats",
        "-v",
        "error",  # only errors
        "-xerror",  # make errors set non-zero exit
        "-err_detect",
        "explode",  # fail fast on decode issues
        "-skip_frame:v",
        "nokey",
        "-threads:v",
        "1",
        "-i",
        video_file,
        "-an",
        "-sn",
        "-dn",
        "-frames:v",
        "10",  # stop as soon as one frame decodes
        "-f",
        "null",
        null_sink,
    ]
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        logger.debug(f"skip_frame probe timed out for {video_file}")
        return False
    ok = proc.returncode == 0
    if not ok:
        last = (proc.stderr or "").strip().splitlines()[-1:]  # tail(1)
        logger.debug(f"skip_frame probe FAILED at 0s: rc={proc.returncode} msg={last}")
    else:
        logger.debug("skip_frame probe OK at 0s")
    return ok


def generate_images(
    video_file: str,
    output_folder: str,
    gpu: Optional[str],
    gpu_device_path: Optional[str],
    config: Config,
    progress_callback=None,
) -> tuple:
    """
    Generate thumbnail images from a video using FFmpeg.

    Runs FFmpeg with hardware acceleration when configured. If the skip-frame
    heuristic allowed it, attempts with '-skip_frame:v nokey' first. If that
    yields zero images or returns non-zero, automatically retries without '-skip_frame'.

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

    Returns:
        (success, image_count, hw_used, seconds, speed):
            success (bool): True if at least one image was produced
            image_count (int): Number of images written
            hw_used (bool): Whether hardware acceleration was actually used
                           (False if CPU fallback occurred)
            seconds (float): Elapsed processing time (last attempt)
            speed (str): Reported or computed FFmpeg speed string
    """
    media_info = MediaInfo.parse(video_file)
    fps_value = round(1 / config.plex_bif_frame_interval, 6)

    # Base video filter for SDR content
    base_scale = "scale=w=320:h=240:force_original_aspect_ratio=decrease"
    vf_parameters = f"fps=fps={fps_value}:round=up,{base_scale}"

    # Check if we have HDR Format. Note: Sometimes it can be returned as "None" (string) hence the check for None type or "None" (String)
    if media_info.video_tracks:
        hdr_fmt = media_info.video_tracks[0].hdr_format
        if hdr_fmt != "None" and hdr_fmt is not None:
            if _is_dv_no_backward_compat(hdr_fmt):
                # Dolby Vision without backward-compatible HDR layer (e.g.
                # Profile 5 IPT-PQ).  zscale cannot tonemap this content and
                # will crash with "no path between colorspaces".  Use the
                # basic fps+scale chain from the start.
                logger.info(
                    f"Dolby Vision without backward-compatible HDR detected for {video_file}; "
                    f"skipping zscale/tonemap (hdr_format={hdr_fmt!r})"
                )
                # vf_parameters already set to the base SDR chain — keep it.
            else:
                # Standard HDR (HDR10, HLG, DV+HDR10 compat, etc.) — use
                # zscale/tonemap to convert to SDR.
                #
                # Determine nominal peak luminance (npl) from MediaInfo MaxCLL.
                # If available, this gives zscale the actual mastering peak so
                # tonemap can preserve highlight detail.  When absent, omit npl
                # entirely so FFmpeg 7+ auto-detects from stream side-data
                # (better than the old hardcoded npl=100 which lost highlights).
                npl_param = ""
                maxcll_raw = getattr(
                    media_info.video_tracks[0],
                    "maximum_content_light_level",
                    None,
                )
                if maxcll_raw is not None:
                    try:
                        # MediaInfo returns e.g. "1000" or "1000 cd/m2"
                        maxcll_value = int(str(maxcll_raw).split()[0])
                        if maxcll_value > 0:
                            npl_param = f":npl={maxcll_value}"
                            logger.debug(
                                f"Using MaxCLL={maxcll_value} as npl for {video_file}"
                            )
                    except (ValueError, IndexError):
                        logger.debug(
                            f"Could not parse MaxCLL={maxcll_raw!r} for {video_file}; "
                            f"omitting npl (FFmpeg will auto-detect)"
                        )
                else:
                    logger.debug(
                        f"No MaxCLL metadata for {video_file}; "
                        f"omitting npl (FFmpeg will auto-detect)"
                    )

                vf_parameters = (
                    f"fps=fps={fps_value}:round=up,"
                    f"zscale=t=linear{npl_param},format=gbrpf32le,"
                    f"zscale=p=bt709,tonemap=tonemap=hable:desat=0,"
                    f"zscale=t=bt709:m=bt709:r=tv,format=yuv420p,{base_scale}"
                )

    def _run_ffmpeg(
        use_skip: bool,
        gpu_override: Optional[str] = None,
        gpu_device_path_override: Optional[str] = None,
        vf_override: Optional[str] = None,
    ) -> tuple:
        """Run FFmpeg once and return (returncode, seconds, speed, stderr_lines)."""
        # Build FFmpeg command with proper argument ordering
        # Hardware acceleration flags must come BEFORE the input file (-i)
        effective_vf = vf_override if vf_override is not None else vf_parameters
        args = [
            config.ffmpeg_path,
            "-loglevel",
            "info",
            "-threads:v",
            "1",
        ]

        # Add hardware acceleration for decoding (before -i flag)
        # Allow overriding GPU settings for CPU fallback
        effective_gpu = gpu_override if gpu_override is not None else gpu
        effective_gpu_device_path = (
            gpu_device_path_override
            if gpu_device_path_override is not None
            else gpu_device_path
        )

        use_gpu = effective_gpu is not None
        if use_gpu:
            if effective_gpu == "NVIDIA":
                args += ["-hwaccel", "cuda"]
            elif effective_gpu == "WINDOWS_GPU":
                args += ["-hwaccel", "d3d11va"]
            elif effective_gpu == "APPLE":
                args += ["-hwaccel", "videotoolbox"]
            elif effective_gpu_device_path and effective_gpu_device_path.startswith(
                "/dev/dri/"
            ):
                args += [
                    "-hwaccel",
                    "vaapi",
                    "-vaapi_device",
                    effective_gpu_device_path,
                ]

        # Add skip_frame option for faster decoding (if safe)
        if use_skip:
            args += ["-skip_frame:v", "nokey"]

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
        logger.debug(f"Executing: {' '.join(args)}")

        # Use file polling approach for non-blocking, high-frequency progress monitoring
        import threading

        thread_id = threading.get_ident()
        output_file = os.path.join(
            tempfile.gettempdir(),
            f"ffmpeg_output_{os.getpid()}_{thread_id}_{time.time_ns()}.log",
        )
        stderr_fh = open(output_file, "w", encoding="utf-8")
        try:
            proc = subprocess.Popen(args, stderr=stderr_fh, stdout=subprocess.DEVNULL)

            # Signal that FFmpeg process has started
            if progress_callback:
                progress_callback(0, 0, 0, "0.0x", media_file=video_file)

            # Track progress
            total_duration = None
            speed_local = "0.0x"
            ffmpeg_output_lines = []
            line_count = 0

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
                    except (OSError, IOError):
                        pass
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

        # Error logging
        if proc.returncode != 0:
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
                    if keyword.lower() in line_lower:
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

            # Log full FFmpeg output at DEBUG level for detailed troubleshooting
            logger.debug(f"FFmpeg output ({len(ffmpeg_output_lines)} lines):")
            for i, line in enumerate(ffmpeg_output_lines[-10:]):
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

    # Decide initial skip usage from heuristic
    use_skip_initial = heuristic_allows_skip(config.ffmpeg_path, video_file)

    # Ensure output folder exists
    os.makedirs(output_folder, exist_ok=True)

    # First attempt
    rc, seconds, speed, stderr_lines = _run_ffmpeg(use_skip_initial)
    stderr_lines_all: List[str] = list(stderr_lines) if stderr_lines else []

    # Retry once without skip_frame only if FFmpeg returned non-zero and we tried with skip
    # (If we didn't use skip initially, retrying without skip would just repeat the same command)
    did_retry = False
    retry_rc = rc
    retry_stderr_lines = stderr_lines

    if rc != 0 and use_skip_initial:
        did_retry = True
        logger.warning(
            f"No thumbnails generated from {video_file} with -skip_frame; retrying without skip-frame"
        )
        # Clean up any partial files from first attempt (no need to rename if we're retrying)
        for img in glob.glob(os.path.join(output_folder, "*.jpg")):
            try:
                os.remove(img)
            except Exception:
                pass
        retry_rc, seconds, speed, retry_stderr_lines = _run_ffmpeg(use_skip=False)
        # Update rc and stderr_lines to retry results for codec error detection
        rc = retry_rc
        stderr_lines = retry_stderr_lines
        if retry_stderr_lines:
            stderr_lines_all.extend(retry_stderr_lines)

    # Count images first to see if we have any (even if rc != 0, we might have partial success)
    image_count = len(glob.glob(os.path.join(output_folder, "img*.jpg")))

    did_dv_safe_retry = False

    # Dolby Vision / HDR colorspace errors can abort FFmpeg when the
    # zscale/tonemap filter chain encounters unsupported transfer
    # characteristics (e.g. DV Profile 5 IPT-PQ) or RPU parsing failures.
    # On both CPU and GPU, retry once with a DV-safe filter chain that
    # avoids zscale/tonemap entirely.
    if rc != 0 and image_count == 0:
        is_dv_rpu = _detect_dolby_vision_rpu_error(stderr_lines_all)
        is_zscale = _detect_zscale_colorspace_error(stderr_lines_all)
        if is_dv_rpu or is_zscale:
            did_dv_safe_retry = True
            diag_label = (
                "Dolby Vision RPU parsing error"
                if is_dv_rpu
                else "zscale colorspace conversion error"
            )
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
            for img in glob.glob(os.path.join(output_folder, "*.jpg")):
                try:
                    os.remove(img)
                except Exception:
                    pass

            # DV-safe filter: avoid zscale/tonemap; mirror the known-working workaround in issue #130.
            dv_safe_vf = f"fps=fps={fps_value}:round=up,{base_scale}"

            rc, seconds, speed, stderr_lines = _run_ffmpeg(
                use_skip=False, vf_override=dv_safe_vf
            )
            if stderr_lines:
                stderr_lines_all.extend(stderr_lines)
            image_count = len(glob.glob(os.path.join(output_folder, "img*.jpg")))

            if rc != 0 and image_count == 0:
                if gpu is not None:
                    # Still failing on GPU even with DV-safe filter -> hand off to CPU worker.
                    for img in glob.glob(os.path.join(output_folder, "*.jpg")):
                        try:
                            os.remove(img)
                        except Exception:
                            pass
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

    # Check for codec errors or crash signals after DV-safe retry (if any) and skip-frame retries.
    # If in GPU context and codec/crash error detected, raise exception for worker pool to handle

    if rc != 0 and image_count == 0 and gpu is not None:
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
            for img in glob.glob(os.path.join(output_folder, "*.jpg")):
                try:
                    os.remove(img)
                except Exception:
                    pass
            # Raise exception to signal worker pool to re-queue for CPU worker
            raise CodecNotSupportedError(
                f"GPU processing failed ({fallback_reason}) for {video_file} (exit code {rc})"
            )

    # CPU fallback: Only perform CPU fallback when not in GPU context (e.g., when gpu is None)
    # This preserves the existing fallback behavior for non-GPU processing paths
    did_cpu_fallback = False
    if rc != 0 and image_count == 0 and gpu is None:
        # Detect if failure is due to unsupported codec (even on CPU, for edge cases)
        if _detect_codec_error(rc, stderr_lines):
            logger.warning(
                f"Processing failed with codec error (exit code {rc}) for {video_file}; file may be corrupted or unsupported"
            )
            # For CPU context, we can't fallback further, so just log and continue
            # The function will return failure status

    # Rename images only after all retries and error checks are complete
    # This ensures we don't rename images that will be cleaned up due to errors
    if image_count > 0:
        # Rename images from img-*.jpg format to timestamp-based names
        for image in glob.glob(f"{output_folder}/img*.jpg"):
            frame_no = int(os.path.basename(image).strip("-img").strip(".jpg")) - 1
            frame_second = frame_no * config.plex_bif_frame_interval
            os.rename(image, os.path.join(output_folder, f"{frame_second:010d}.jpg"))
        # Re-count after renaming to get final count (includes both renamed and any existing timestamped images)
        image_count = len(glob.glob(os.path.join(output_folder, "*.jpg")))

    hw = gpu is not None and not did_cpu_fallback
    success = image_count > 0

    if success:
        fallback_suffix = (
            " (CPU fallback)"
            if did_cpu_fallback
            else (
                " (DV-safe retry)"
                if did_dv_safe_retry
                else (" (retry no-skip)" if did_retry else "")
            )
        )
        logger.info(
            f"Generated Video Preview for {video_file} HW={hw} TIME={seconds}seconds SPEED={speed} IMAGES={image_count}{fallback_suffix}"
        )
    else:
        fallback_suffix = (
            " (after CPU fallback)"
            if did_cpu_fallback
            else (
                " after DV-safe retry"
                if did_dv_safe_retry
                else (" after retry" if did_retry else "")
            )
        )
        logger.error(
            f"Failed to generate thumbnails for {video_file}; 0 images produced{fallback_suffix}"
        )
        # Record for end-of-run summary
        worker_ctx = "GPU" if (gpu is not None and not did_cpu_fallback) else "CPU"
        reason = (
            f"FFmpeg exit {rc} ({_diagnose_ffmpeg_exit_code(rc)}){fallback_suffix}"
            if rc != 0
            else f"0 images{fallback_suffix}"
        )
        record_failure(video_file, rc, reason, worker_type=worker_ctx)

    return success, image_count, hw, seconds, speed


def _setup_bundle_paths(bundle_hash: str, config: Config) -> Tuple[str, str, str]:
    """
    Set up all bundle-related paths.

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
    """
    Ensure required directories exist.

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
    """
    Clean up temporary directory, logging warnings on failure.

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
) -> None:
    """
    Generate images and create BIF file.

    Args:
        media_file: Path to media file
        tmp_path: Temporary directory for images
        index_bif: Path to output BIF file
        gpu: GPU type for acceleration
        gpu_device_path: GPU device path
        config: Configuration object
        progress_callback: Callback function for progress updates

    Raises:
        CodecNotSupportedError: If codec is not supported by GPU
        RuntimeError: If thumbnail generation produced 0 images
    """
    try:
        gen_result = generate_images(
            media_file, tmp_path, gpu, gpu_device_path, config, progress_callback
        )
    except CodecNotSupportedError:
        # Clean up temp directory before re-raising
        _cleanup_temp_directory(tmp_path)
        raise
    except Exception as e:
        logger.error(
            f"Error generating images for {media_file}. `{type(e).__name__}:{str(e)}` error when generating images"
        )
        _cleanup_temp_directory(tmp_path)
        raise RuntimeError(f"Failed to generate images: {e}") from e

    # Determine image count from result or by scanning
    image_count = 0
    if isinstance(gen_result, tuple) and len(gen_result) >= 2:
        _, image_count = bool(gen_result[0]), int(gen_result[1])
    else:
        if os.path.isdir(tmp_path):
            image_count = len(glob.glob(os.path.join(tmp_path, "*.jpg")))

    if image_count == 0:
        logger.error(f"No thumbnails generated for {media_file}; skipping BIF creation")
        _cleanup_temp_directory(tmp_path)
        raise RuntimeError(f"Thumbnail generation produced 0 images for {media_file}")

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
    """
    Build a .bif file from thumbnail images.

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
    logger.debug(f"Generated BIF file: {bif_filename}")


def process_item(
    item_key: str,
    gpu: Optional[str],
    gpu_device_path: Optional[str],
    config: Config,
    plex,
    progress_callback=None,
) -> None:
    """
    Process a single media item: generate thumbnails and BIF file.

    This is the core processing function that handles:
    - Plex API queries
    - Path mapping for remote generation
    - Bundle hash generation
    - Plex directory structure creation
    - Thumbnail generation with FFmpeg
    - BIF file creation
    - Cleanup

    Args:
        item_key: Plex media item key
        gpu: GPU type for acceleration
        gpu_device_path: GPU device path
        config: Configuration object
        plex: Plex server instance
        progress_callback: Callback function for progress updates
    """
    try:
        data = retry_plex_call(plex.query, f"{item_key}/tree")
    except Exception as e:
        logger.error(f"Failed to query Plex for item {item_key} after retries: {e}")
        logger.error(f"Exception type: {type(e).__name__}")
        # For connection errors, log more details
        if hasattr(e, "request") and e.request:
            logger.error(f"Request URL: {e.request.url}")
            logger.error(f"Request method: {e.request.method}")
            # Sanitize headers to avoid leaking tokens
            safe_headers = {
                k: ("****" if "token" in k.lower() else v)
                for k, v in e.request.headers.items()
            }
            logger.error(f"Request headers: {safe_headers}")
        return

    for media_part in data.findall(".//MediaPart"):
        if "hash" in media_part.attrib:
            bundle_hash = media_part.attrib["hash"]
            # Apply path mapping if both mapping parameters are provided (for remote generation)
            if (
                config.plex_videos_path_mapping
                and config.plex_local_videos_path_mapping
            ):
                media_file = sanitize_path(
                    media_part.attrib["file"].replace(
                        config.plex_videos_path_mapping,
                        config.plex_local_videos_path_mapping,
                    )
                )
            else:
                # Use file path directly (for local generation)
                media_file = sanitize_path(media_part.attrib["file"])

            # Validate bundle_hash has sufficient length (at least 2 characters)
            if not bundle_hash or len(bundle_hash) < 2:
                hash_value = f'"{bundle_hash}"' if bundle_hash else "(empty)"
                logger.warning(
                    f"Skipping {media_file} due to invalid bundle hash from Plex: {hash_value} (length: {len(bundle_hash) if bundle_hash else 0}, required: >= 2)"
                )
                continue

            if not os.path.isfile(media_file):
                logger.warning(f"Skipping as file not found {media_file}")
                continue

            try:
                indexes_path, index_bif, tmp_path = _setup_bundle_paths(
                    bundle_hash, config
                )
            except Exception as e:
                logger.error(
                    f"Error generating bundle_file for {media_file} due to {type(e).__name__}:{str(e)}"
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
                    continue

            if os.path.isfile(index_bif):
                logger.debug(
                    f"Skipping {media_file} — BIF already exists at {index_bif}"
                )
                continue

            if not os.path.isfile(index_bif):
                logger.debug(f"Generating thumbnails for {media_file} -> {index_bif}")

                # Ensure directories exist
                if not _ensure_directories(indexes_path, tmp_path, media_file):
                    continue

                # Generate images and create BIF file
                try:
                    _generate_and_save_bif(
                        media_file,
                        tmp_path,
                        index_bif,
                        gpu,
                        gpu_device_path,
                        config,
                        progress_callback,
                    )
                except CodecNotSupportedError:
                    # Re-raise so worker can handle codec errors
                    raise
                except RuntimeError as e:
                    # RuntimeError from _generate_and_save_bif means generation failed
                    # Log and continue to next media part
                    logger.error(f"Error processing {media_file}: {str(e)}")
                    continue
                except Exception as e:
                    logger.error(
                        f"Error processing {media_file}: {type(e).__name__}: {str(e)}"
                    )
                    continue
                finally:
                    # Always clean up temp directory (may already be cleaned up by _generate_and_save_bif)
                    _cleanup_temp_directory(tmp_path)
