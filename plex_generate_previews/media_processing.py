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
from typing import Optional
from loguru import logger

from .utils import sanitize_path

try:
    from pymediainfo import MediaInfo
except ImportError:
    print('MediaInfo not found. MediaInfo must be installed and available in PATH.')
    sys.exit(1)

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
    proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    ok = (proc.returncode == 0)
    if not ok:
        last = (proc.stderr or "").strip().splitlines()[-1:]  # tail(1)
        logger.debug(f"skip_frame probe FAILED at 0s: rc={proc.returncode} msg={last}")
    else:
        logger.debug("skip_frame probe OK at 0s")
    return ok


def generate_images(video_file: str, output_folder: str, gpu: Optional[str], 
                   gpu_device_path: Optional[str], config: Config, progress_callback=None) -> None:
    """
    Generate thumbnail images from video file using FFmpeg.
    
    Args:
        video_file: Path to input video file
        output_folder: Directory to save thumbnail images
        gpu: GPU type ('NVIDIA', 'AMD', 'INTEL', 'WSL2', or None)
        gpu_device_path: GPU device path for VAAPI
        config: Configuration object
        progress_callback: Callback function for progress updates
    """
    media_info = MediaInfo.parse(video_file)
    fps_value = round(1 / config.plex_bif_frame_interval, 6)
    vf_parameters = f"fps=fps={fps_value}:round=up,scale=w=320:h=240:force_original_aspect_ratio=decrease"

    # Check if we have a HDR Format. Note: Sometimes it can be returned as "None" (string) hence the check for None type or "None" (String)
    if media_info.video_tracks:
        if media_info.video_tracks[0].hdr_format != "None" and media_info.video_tracks[0].hdr_format is not None:
            vf_parameters = f"fps=fps={fps_value}:round=up,zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv,format=yuv420p,scale=w=320:h=240:force_original_aspect_ratio=decrease"
    
    args = [
        config.ffmpeg_path, "-loglevel", "info",
        "-threads:v", "1",  # fix: was '-threads:0 1'
    ]

    use_skip = heuristic_allows_skip(config.ffmpeg_path, video_file)
    if use_skip:
        args += ["-skip_frame:v", "nokey"]

    args += [
        "-i", video_file, "-an", "-sn", "-dn",
        "-q:v", str(config.thumbnail_quality),
        "-vf", vf_parameters,
        f'{output_folder}/img-%06d.jpg'
    ]

    start = time.time()
    hw = False

    # Determine GPU usage - if gpu is set, use GPU
    use_gpu = gpu is not None
    
    # Apply GPU acceleration if using GPU
    if use_gpu:
        hw = True
        
        if gpu == 'NVIDIA':
            args.insert(5, "-hwaccel")
            args.insert(6, "cuda")
        elif gpu == 'WSL2':
            args.insert(5, "-hwaccel")
            args.insert(6, "d3d11va")
        else:
            # AMD or Intel VAAPI
            args.insert(5, "-hwaccel")
            args.insert(6, "vaapi")
            args.insert(7, "-vaapi_device")
            args.insert(8, gpu_device_path)
            # Check if Intel GPU (Intel devices typically have 'renderD' in path)
            if gpu == 'INTEL':
                vf_parameters = vf_parameters.replace(
                    "scale=w=320:h=240:force_original_aspect_ratio=decrease",
                    "format=nv12,hwupload,scale_vaapi=w=320:h=240:force_original_aspect_ratio=decrease,hwdownload,format=nv12")
            else: 
                # Adjust vf_parameters for AMD VAAPI
                vf_parameters = vf_parameters.replace(
                    "scale=w=320:h=240:force_original_aspect_ratio=decrease",
                    "format=nv12|vaapi,hwupload,scale_vaapi=w=320:h=240:force_original_aspect_ratio=decrease")
            
            args[args.index("-vf") + 1] = vf_parameters

    logger.debug(f'Executing: {" ".join(args)}')
    
    # Use file polling approach for non-blocking, high-frequency progress monitoring
    # This is faster than subprocess.PIPE which would block on readline() calls
    # Use high-resolution timestamp and thread ID to ensure unique file per worker
    import threading
    thread_id = threading.get_ident()
    output_file = f'/tmp/ffmpeg_output_{os.getpid()}_{thread_id}_{time.time_ns()}.log'
    proc = subprocess.Popen(args, stderr=open(output_file, 'w'), stdout=subprocess.DEVNULL)
    
    # Signal that FFmpeg process has started
    if progress_callback:
        progress_callback(0, 0, 0, "0.0x")

    # Track progress
    total_duration = None
    current_time = 0
    speed = "0.0x"
    progress_percent = 0
    ffmpeg_output_lines = []  # Store all FFmpeg output for debugging
    line_count = 0
    
    # Create a wrapper callback to capture speed updates
    def speed_capture_callback(progress_percent, current_duration, total_duration, speed_value, 
                              remaining_time=None, frame=0, fps=0, q=0, size=0, time_str="00:00:00.00", bitrate=0):
        nonlocal speed
        if speed_value and speed_value != "0.0x":
            speed = speed_value
        if progress_callback:
            progress_callback(progress_percent, current_duration, total_duration, speed_value, 
                            remaining_time, frame, fps, q, size, time_str, bitrate)
    
    # Allow time for it to start
    time.sleep(0.02)

    # Parse FFmpeg output using file polling (much faster)
    poll_count = 0
    while proc.poll() is None:
        poll_count += 1
        if os.path.exists(output_file):
            try:
                with open(output_file, 'r') as f:
                    lines = f.readlines()
                    if len(lines) > line_count:
                        # Process new lines
                        for i in range(line_count, len(lines)):
                            line = lines[i].strip()
                            if line:
                                ffmpeg_output_lines.append(line)
                                # Parse FFmpeg output line
                                total_duration = parse_ffmpeg_progress_line(line, total_duration, speed_capture_callback)
                        line_count = len(lines)
            except (OSError, IOError):
                # Handle file access issues gracefully
                pass
        
        time.sleep(0.005)  # Poll every 5ms for very responsive updates
    
    # Process any remaining data in the output file
    if os.path.exists(output_file):
        try:
            with open(output_file, 'r') as f:
                lines = f.readlines()
                if len(lines) > line_count:
                    # Process any remaining lines
                    for i in range(line_count, len(lines)):
                        line = lines[i].strip()
                        if line:
                            ffmpeg_output_lines.append(line)
                            # Parse any remaining progress lines
                            total_duration = parse_ffmpeg_progress_line(line, total_duration, speed_capture_callback)
        except (OSError, IOError):
            pass
    
    # Clean up the output file
    try:
        os.remove(output_file)
    except OSError:
        pass
    
    # Check for errors
    if proc.returncode != 0:
        logger.error(f'FFmpeg failed with return code {proc.returncode} for {video_file}')
        # Only show detailed output in debug mode
        if logger.level("DEBUG").no <= logger._core.min_level:
            logger.debug(f"FFmpeg output ({len(ffmpeg_output_lines)} lines):")
            for i, line in enumerate(ffmpeg_output_lines[-10:]):  # Show last 10 lines only
                logger.debug(f"  {i+1:3d}: {line}")

    # Final timing
    end = time.time()
    seconds = round(end - start, 1)

    # Calculate fallback speed if no valid speed was captured
    if speed == "0.0x" and total_duration and total_duration > 0 and seconds > 0:
        calculated_speed = total_duration / seconds
        speed = f"{calculated_speed:.0f}x"

    # Optimize and Rename Images
    for image in glob.glob(f'{output_folder}/img*.jpg'):
        frame_no = int(os.path.basename(image).strip('-img').strip('.jpg')) - 1
        frame_second = frame_no * config.plex_bif_frame_interval
        os.rename(image, os.path.join(output_folder, f'{frame_second:010d}.jpg'))

    logger.info(f'Generated Video Preview for {video_file} HW={hw} TIME={seconds}seconds SPEED={speed}')


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
            tmp_path = sanitize_path(os.path.join(config.tmp_folder, bundle_hash))

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
                    generate_images(media_file, tmp_path, gpu, gpu_device_path, config, progress_callback)
                except Exception as e:
                    logger.error(f'Error generating images for {media_file}. `{type(e).__name__}:{str(e)}` error when generating images')
                    # Clean up temp directory on error
                    try:
                        if os.path.exists(tmp_path):
                            shutil.rmtree(tmp_path)
                    except Exception as cleanup_error:
                        logger.warning(f"Failed to clean up temp directory {tmp_path}: {cleanup_error}")
                    continue

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
