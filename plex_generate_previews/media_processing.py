"""
Media processing functions for video thumbnail generation.

Handles FFmpeg execution, BIF file generation, and all media processing
logic including HDR detection, skip frame heuristics, and GPU acceleration.
"""

import os
import re
import struct
import array
import glob
import time
import subprocess
import shutil
import sys
import http.client
import xml.etree.ElementTree
import tempfile
from typing import Optional, List
from loguru import logger

from .utils import sanitize_path

try:
    from pymediainfo import MediaInfo
    # Test that native library is available
    MediaInfo.can_parse()
except ImportError:
    print('ERROR: pymediainfo Python package not found.')
    print('Please install: pip install pymediainfo')
    sys.exit(1)
except OSError as e:
    if 'libmediainfo' in str(e).lower():
        print('ERROR: MediaInfo native library not found.')
        print('Please install MediaInfo:')
        if sys.platform == 'darwin':  # macOS
            print('  macOS: brew install media-info')
        elif sys.platform.startswith('linux'):
            print('  Ubuntu/Debian: sudo apt-get install mediainfo libmediainfo-dev')
            print('  Fedora/RHEL: sudo dnf install mediainfo mediainfo-devel')
        else:
            print('  See: https://mediaarea.net/en/MediaInfo/Download')
        sys.exit(1)
except Exception as e:
    print(f'WARNING: Could not validate MediaInfo library: {e}')
    print('Proceeding anyway, but errors may occur during processing')

from .config import Config
from .plex_client import retry_plex_call


def parse_ffmpeg_progress_line(line: str, total_duration: float, progress_callback=None):
    """
    Parse a single FFmpeg progress line and call progress callback if provided.
    
    Args:
        line: FFmpeg output line to parse
        total_duration: Total video duration in seconds
        progress_callback: Callback function for progress updates
    """
    # Parse duration
    if 'Duration:' in line:
        duration_match = re.search(r'Duration: (\d{2}):(\d{2}):(\d{2}\.\d{2})', line)
        if duration_match:
            hours, minutes, seconds = duration_match.groups()
            return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
        return total_duration
    
    # Parse FFmpeg progress line with all data
    elif 'time=' in line:
        # Extract all FFmpeg data fields
        frame_match = re.search(r'frame=\s*(\d+)', line)
        fps_match = re.search(r'fps=\s*([0-9.]+)', line)
        q_match = re.search(r'q=([0-9.]+)', line)
        size_match = re.search(r'size=\s*(\d+)kB', line)
        time_match = re.search(r'time=(\d{2}):(\d{2}):(\d{2}\.\d{2})', line)
        bitrate_match = re.search(r'bitrate=\s*([0-9.]+)kbits/s', line)
        speed_match = re.search(r'speed=\s*([0-9]+\.?[0-9]*|\.[0-9]+)x', line)
        
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
                progress_callback(progress_percent, current_time, total_duration, speed or "0.0x", 
                                remaining_time, frame, fps, q, size, time_str, bitrate)
    
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
    stderr_text = ' '.join(stderr_lines).lower()
    
    # Pattern list for codec/decoder errors (based on FFmpeg documentation)
    codec_error_patterns = [
        'codec not supported',
        'unsupported codec',
        'unsupported codec id',
        'unknown encoder',
        'unknown decoder',
        'decoder not found',
        'no decoder for',
        'could not find codec',
        'no codec',
    ]
    
    # Check for codec error patterns in stderr (primary detection method)
    for pattern in codec_error_patterns:
        if pattern in stderr_text:
            return True
    
    # Also check exit codes: -22 (EINVAL, may be wrapped to 234 on Unix) or 69 (max error rate)
    if returncode in [-22, 234, 69] and returncode != 0:
        return True
    
    return False


def heuristic_allows_skip(ffmpeg_path: str, video_file: str) -> bool:
    """
    Using the first 10 frames of file to decide if -skip_frame:v nokey is safe.
    Uses -err_detect explode + -xerror to bail immediately on the first decode error.
    Returns True if the probe succeeds, else False. Logs a short tail if available.
    """
    null_sink = "NUL" if os.name == "nt" else "/dev/null"
    cmd = [
        ffmpeg_path,
        "-hide_banner", "-nostats",
        "-v", "error",            # only errors
        "-xerror",                # make errors set non-zero exit
        "-err_detect", "explode", # fail fast on decode issues
        "-skip_frame:v", "nokey",
        "-threads:v", "1",
        "-i", video_file,
        "-an", "-sn", "-dn",
        "-frames:v", "10",         # stop as soon as one frame decodes
        "-f", "null", null_sink
    ]
    proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, encoding='utf-8', errors='replace')
    ok = (proc.returncode == 0)
    if not ok:
        last = (proc.stderr or "").strip().splitlines()[-1:]  # tail(1)
        logger.debug(f"skip_frame probe FAILED at 0s: rc={proc.returncode} msg={last}")
    else:
        logger.debug("skip_frame probe OK at 0s")
    return ok


def generate_images(video_file: str, output_folder: str, gpu: Optional[str], 
                   gpu_device_path: Optional[str], config: Config, progress_callback=None) -> tuple:
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
        if media_info.video_tracks[0].hdr_format != "None" and media_info.video_tracks[0].hdr_format is not None:
            vf_parameters = f"fps=fps={fps_value}:round=up,zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv,format=yuv420p,{base_scale}"
    
    def _run_ffmpeg(use_skip: bool, gpu_override: Optional[str] = None, gpu_device_path_override: Optional[str] = None) -> tuple:
        """Run FFmpeg once and return (returncode, seconds, speed, stderr_lines)."""
        # Build FFmpeg command with proper argument ordering
        # Hardware acceleration flags must come BEFORE the input file (-i)
        args = [
            config.ffmpeg_path, "-loglevel", "info",
            "-threads:v", "1",
        ]

        # Add hardware acceleration for decoding (before -i flag)
        # Allow overriding GPU settings for CPU fallback
        effective_gpu = gpu_override if gpu_override is not None else gpu
        effective_gpu_device_path = gpu_device_path_override if gpu_device_path_override is not None else gpu_device_path
        
        hw_local = False
        use_gpu = effective_gpu is not None
        if use_gpu:
            hw_local = True
            if effective_gpu == 'NVIDIA':
                args += ["-hwaccel", "cuda"]
            elif effective_gpu == 'WINDOWS_GPU':
                args += ["-hwaccel", "d3d11va"]
            elif effective_gpu == 'APPLE':
                args += ["-hwaccel", "videotoolbox"]
            elif effective_gpu_device_path and effective_gpu_device_path.startswith('/dev/dri/'):
                args += ["-hwaccel", "vaapi", "-vaapi_device", effective_gpu_device_path]

        # Add skip_frame option for faster decoding (if safe)
        if use_skip:
            args += ["-skip_frame:v", "nokey"]

        # Add input file and output options
        args += [
            "-i", video_file, "-an", "-sn", "-dn",
            "-q:v", str(config.thumbnail_quality),
            "-vf", vf_parameters,
            f'{output_folder}/img-%06d.jpg'
        ]

        start_local = time.time()
        logger.debug(f'Executing: {" ".join(args)}')

        # Use file polling approach for non-blocking, high-frequency progress monitoring
        import threading
        thread_id = threading.get_ident()
        output_file = os.path.join(tempfile.gettempdir(), f'ffmpeg_output_{os.getpid()}_{thread_id}_{time.time_ns()}.log')
        proc = subprocess.Popen(args, stderr=open(output_file, 'w', encoding='utf-8'), stdout=subprocess.DEVNULL)

        # Signal that FFmpeg process has started
        if progress_callback:
            progress_callback(0, 0, 0, "0.0x", media_file=video_file)

        # Track progress
        total_duration = None
        speed_local = "0.0x"
        ffmpeg_output_lines = []
        line_count = 0

        def speed_capture_callback(progress_percent, current_duration, total_duration_param, speed_value, 
                                  remaining_time=None, frame=0, fps=0, q=0, size=0, time_str="00:00:00.00", bitrate=0):
            nonlocal speed_local
            if speed_value and speed_value != "0.0x":
                speed_local = speed_value
            if progress_callback:
                progress_callback(progress_percent, current_duration, total_duration_param, speed_value, 
                                remaining_time, frame, fps, q, size, time_str, bitrate, media_file=video_file)

        time.sleep(0.02)
        while proc.poll() is None:
            if os.path.exists(output_file):
                try:
                    with open(output_file, 'r', encoding='utf-8') as f:
                        lines = f.readlines()
                        if len(lines) > line_count:
                            for i in range(line_count, len(lines)):
                                line = lines[i].strip()
                                if line:
                                    ffmpeg_output_lines.append(line)
                                    total_duration = parse_ffmpeg_progress_line(line, total_duration, speed_capture_callback)
                            line_count = len(lines)
                except (OSError, IOError):
                    pass
            time.sleep(0.005)

        # Process any remaining data
        if os.path.exists(output_file):
            try:
                with open(output_file, 'r', encoding='utf-8') as f:
                    lines = f.readlines()
                    if len(lines) > line_count:
                        for i in range(line_count, len(lines)):
                            line = lines[i].strip()
                            if line:
                                ffmpeg_output_lines.append(line)
                                total_duration = parse_ffmpeg_progress_line(line, total_duration, speed_capture_callback)
            except (OSError, IOError):
                pass

        try:
            os.remove(output_file)
        except OSError:
            pass

        # Error logging
        if proc.returncode != 0:
            logger.error(f'FFmpeg failed with return code {proc.returncode} for {video_file}')
            if logger.level("DEBUG").no <= logger._core.min_level:
                logger.debug(f"FFmpeg output ({len(ffmpeg_output_lines)} lines):")
                for i, line in enumerate(ffmpeg_output_lines[-10:]):
                    logger.debug(f"  {i+1:3d}: {line}")

        end_local = time.time()
        seconds_local = round(end_local - start_local, 1)
        # Calculate fallback speed if needed
        if speed_local == "0.0x" and total_duration and total_duration > 0 and seconds_local > 0:
            calculated_speed = total_duration / seconds_local
            speed_local = f"{calculated_speed:.0f}x"

        return proc.returncode, seconds_local, speed_local, ffmpeg_output_lines

    # Decide initial skip usage from heuristic
    use_skip_initial = heuristic_allows_skip(config.ffmpeg_path, video_file)

    # Ensure output folder exists
    os.makedirs(output_folder, exist_ok=True)

    # First attempt
    rc, seconds, speed, stderr_lines = _run_ffmpeg(use_skip_initial)

    # Rename images produced by this attempt
    for image in glob.glob(f'{output_folder}/img*.jpg'):
        frame_no = int(os.path.basename(image).strip('-img').strip('.jpg')) - 1
        frame_second = frame_no * config.plex_bif_frame_interval
        os.rename(image, os.path.join(output_folder, f'{frame_second:010d}.jpg'))

    image_count = len(glob.glob(os.path.join(output_folder, '*.jpg')))

    # Retry once without skip_frame only if FFmpeg returned non-zero and we tried with skip
    did_retry = False
    if rc != 0 and use_skip_initial:
        did_retry = True
        logger.warning(f"No thumbnails generated from {video_file} with -skip_frame; retrying without skip-frame")
        # Clean up any partial files from first attempt
        for img in glob.glob(os.path.join(output_folder, '*.jpg')):
            try:
                os.remove(img)
            except Exception:
                pass
        rc, seconds, speed, stderr_lines = _run_ffmpeg(use_skip=False)
        # Rename images from retry
        for image in glob.glob(f'{output_folder}/img*.jpg'):
            frame_no = int(os.path.basename(image).strip('-img').strip('.jpg')) - 1
            frame_second = frame_no * config.plex_bif_frame_interval
            os.rename(image, os.path.join(output_folder, f'{frame_second:010d}.jpg'))
        image_count = len(glob.glob(os.path.join(output_folder, '*.jpg')))

    # CPU fallback: if GPU processing failed with codec error and CPU threads are available
    did_cpu_fallback = False
    if rc != 0 and image_count == 0 and gpu is not None:
        # Detect if failure is due to unsupported codec
        if _detect_codec_error(rc, stderr_lines):
            if config.cpu_threads > 0:
                did_cpu_fallback = True
                logger.warning(f"GPU processing failed with codec error (exit code {rc}) for {video_file}; falling back to CPU processing")
                
                # Log relevant stderr excerpt for debugging
                stderr_excerpt = '\n'.join(stderr_lines[-5:]) if len(stderr_lines) > 0 else "No stderr output"
                logger.debug(f"FFmpeg stderr excerpt (last 5 lines): {stderr_excerpt}")
                
                # Clean up any partial files from GPU attempt
                for img in glob.glob(os.path.join(output_folder, '*.jpg')):
                    try:
                        os.remove(img)
                    except Exception:
                        pass
                
                # Retry with CPU (no GPU acceleration)
                rc, seconds, speed, stderr_lines = _run_ffmpeg(use_skip=use_skip_initial, gpu_override=None, gpu_device_path_override=None)
                
                # Rename images from CPU fallback attempt
                for image in glob.glob(f'{output_folder}/img*.jpg'):
                    frame_no = int(os.path.basename(image).strip('-img').strip('.jpg')) - 1
                    frame_second = frame_no * config.plex_bif_frame_interval
                    os.rename(image, os.path.join(output_folder, f'{frame_second:010d}.jpg'))
                image_count = len(glob.glob(os.path.join(output_folder, '*.jpg')))
            else:
                # CPU threads disabled, cannot fallback
                logger.warning(f"GPU processing failed with codec error (exit code {rc}) for {video_file}, but CPU fallback is disabled (CPU_THREADS=0); file will be skipped")

    hw = (gpu is not None and not did_cpu_fallback)
    success = image_count > 0

    if success:
        fallback_suffix = " (CPU fallback)" if did_cpu_fallback else (" (retry no-skip)" if did_retry else "")
        logger.info(f'Generated Video Preview for {video_file} HW={hw} TIME={seconds}seconds SPEED={speed} IMAGES={image_count}{fallback_suffix}')
    else:
        fallback_suffix = " (after CPU fallback)" if did_cpu_fallback else (" after retry" if did_retry else "")
        logger.error(f'Failed to generate thumbnails for {video_file}; 0 images produced{fallback_suffix}')

    return success, image_count, hw, seconds, speed


def generate_bif(bif_filename: str, images_path: str, config: Config) -> None:
    """
    Build a .bif file from thumbnail images.
    
    Args:
        bif_filename: Path to output .bif file
        images_path: Directory containing .jpg thumbnail images
        config: Configuration object
    """
    magic = [0x89, 0x42, 0x49, 0x46, 0x0d, 0x0a, 0x1a, 0x0a]
    version = 0

    images = [img for img in os.listdir(images_path) if os.path.splitext(img)[1] == '.jpg']
    images.sort()

    with open(bif_filename, "wb") as f:
        array.array('B', magic).tofile(f)
        f.write(struct.pack("<I", version))
        f.write(struct.pack("<I", len(images)))
        f.write(struct.pack("<I", 1000 * config.plex_bif_frame_interval))
        array.array('B', [0x00 for x in range(20, 64)]).tofile(f)

        bif_table_size = 8 + (8 * len(images))
        image_index = 64 + bif_table_size
        timestamp = 0

        # Get the length of each image
        for image in images:
            statinfo = os.stat(os.path.join(images_path, image))
            f.write(struct.pack("<I", timestamp))
            f.write(struct.pack("<I", image_index))
            timestamp += 1
            image_index += statinfo.st_size

        f.write(struct.pack("<I", 0xffffffff))
        f.write(struct.pack("<I", image_index))

        # Now copy the images
        for image in images:
            with open(os.path.join(images_path, image), "rb") as img_file:
                data = img_file.read()
            f.write(data)
    logger.debug(f'Generated BIF file: {bif_filename}')


def process_item(item_key: str, gpu: Optional[str], gpu_device_path: Optional[str], 
                config: Config, plex, progress_callback=None) -> None:
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
        data = retry_plex_call(plex.query, f'{item_key}/tree')
    except (Exception, http.client.BadStatusLine, xml.etree.ElementTree.ParseError) as e:
        logger.error(f"Failed to query Plex for item {item_key} after retries: {e}")
        logger.error(f"Exception type: {type(e).__name__}")
        # For connection errors, log more details
        if hasattr(e, 'request') and e.request:
            logger.error(f"Request URL: {e.request.url}")
            logger.error(f"Request method: {e.request.method}")
            logger.error(f"Request headers: {e.request.headers}")
        return
    except Exception as e:
        logger.error(f"Error querying Plex for item {item_key}: {e}")
        return

    for media_part in data.findall('.//MediaPart'):
        if 'hash' in media_part.attrib:
            bundle_hash = media_part.attrib['hash']
            # Apply path mapping if both mapping parameters are provided (for remote generation)
            if config.plex_videos_path_mapping and config.plex_local_videos_path_mapping:
                media_file = sanitize_path(media_part.attrib['file'].replace(config.plex_videos_path_mapping, config.plex_local_videos_path_mapping))
            else:
                # Use file path directly (for local generation)
                media_file = sanitize_path(media_part.attrib['file'])

            if not os.path.isfile(media_file):
                logger.warning(f'Skipping as file not found {media_file}')
                continue

            try:
                bundle_file = sanitize_path(f'{bundle_hash[0]}/{bundle_hash[1::1]}.bundle')
            except Exception as e:
                logger.error(f'Error generating bundle_file for {media_file} due to {type(e).__name__}:{str(e)}')
                continue

            bundle_path = sanitize_path(os.path.join(config.plex_config_folder, 'Media', 'localhost', bundle_file))
            indexes_path = sanitize_path(os.path.join(bundle_path, 'Contents', 'Indexes'))
            index_bif = sanitize_path(os.path.join(indexes_path, 'index-sd.bif'))
            tmp_path = sanitize_path(os.path.join(config.working_tmp_folder, bundle_hash))

            if os.path.isfile(index_bif) and config.regenerate_thumbnails:
                logger.debug(f'Found existing thumbnails for {media_file}, deleting the thumbnail index at {index_bif} so we can regenerate')
                try:
                    os.remove(index_bif)
                    logger.debug(f'Successfully deleted existing BIF file: {index_bif}')
                except Exception as e:
                    logger.error(f'Error {type(e).__name__} deleting index file {media_file}: {str(e)}')
                    continue

            if not os.path.isfile(index_bif):
                logger.debug(f'Generating bundle_file for {media_file} at {index_bif}')

                if not os.path.isdir(indexes_path):
                    try:
                        os.makedirs(indexes_path)
                    except OSError as e:
                        logger.error(f'Error generating images for {media_file}. `{type(e).__name__}:{str(e)}` error when creating index path {indexes_path}')
                        continue

                try:
                    if not os.path.isdir(tmp_path):
                        os.makedirs(tmp_path)
                except OSError as e:
                    logger.error(f'Error generating images for {media_file}. `{type(e).__name__}:{str(e)}` error when creating tmp path {tmp_path}')
                    continue

                try:
                    gen_result = generate_images(media_file, tmp_path, gpu, gpu_device_path, config, progress_callback)
                except Exception as e:
                    logger.error(f'Error generating images for {media_file}. `{type(e).__name__}:{str(e)}` error when generating images')
                    # Clean up temp directory on error
                    try:
                        if os.path.exists(tmp_path):
                            shutil.rmtree(tmp_path)
                    except Exception as cleanup_error:
                        logger.warning(f"Failed to clean up temp directory {tmp_path}: {cleanup_error}")
                    continue

                # Prefer explicit return contract; fallback to scanning if absent
                success = None
                image_count = None
                if isinstance(gen_result, tuple) and len(gen_result) >= 2:
                    success, image_count = bool(gen_result[0]), int(gen_result[1])
                else:
                    image_count = 0
                    if os.path.isdir(tmp_path):
                        image_count = len(glob.glob(os.path.join(tmp_path, '*.jpg')))
                    success = image_count > 0
                if not success or image_count == 0:
                    # Treat as failure and do not produce a BIF
                    logger.error(f'No thumbnails generated for {media_file}; skipping BIF creation')
                    # Cleanup temp and mark failure for caller by raising
                    try:
                        if os.path.exists(tmp_path):
                            shutil.rmtree(tmp_path)
                    except Exception as cleanup_error:
                        logger.warning(f"Failed to clean up temp directory {tmp_path}: {cleanup_error}")
                    raise RuntimeError(f"Thumbnail generation produced 0 images for {media_file}")

                try:
                    generate_bif(index_bif, tmp_path, config)
                except Exception as e:
                    # Remove bif, as it prob failed to generate
                    try:
                        if os.path.exists(index_bif):
                            os.remove(index_bif)
                    except Exception as remove_error:
                        logger.warning(f"Failed to remove failed BIF file {index_bif}: {remove_error}")
                    logger.error(f'Error generating images for {media_file}. `{type(e).__name__}:{str(e)}` error when generating bif')
                    continue
                finally:
                    # Always clean up temp directory
                    try:
                        if os.path.exists(tmp_path):
                            shutil.rmtree(tmp_path)
                    except Exception as cleanup_error:
                        logger.warning(f"Failed to clean up temp directory {tmp_path}: {cleanup_error}")
