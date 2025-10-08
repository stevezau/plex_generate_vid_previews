#!/usr/bin/env python3
import sys
import re
import subprocess
import shutil
import glob
import os
import struct
import urllib3
import array
import time
import http.client
import xml.etree.ElementTree
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from concurrent.futures import ProcessPoolExecutor, as_completed

from dotenv import load_dotenv

load_dotenv()

# Set default ROCM_PATH if not already set to prevent KeyError in AMD SMI
if 'ROCM_PATH' not in os.environ:
    os.environ['ROCM_PATH'] = '/opt/rocm'

PLEX_URL = os.environ.get('PLEX_URL', '')  # Plex server URL. can also use for local server: http://localhost:32400
PLEX_TOKEN = os.environ.get('PLEX_TOKEN', '')  # Plex Authentication Token
PLEX_BIF_FRAME_INTERVAL = int(os.environ.get('PLEX_BIF_FRAME_INTERVAL', 5))  # Interval between preview images
THUMBNAIL_QUALITY = int(os.environ.get('THUMBNAIL_QUALITY', 4))  # Preview image quality (2-6)
PLEX_LOCAL_MEDIA_PATH = os.environ.get('PLEX_LOCAL_MEDIA_PATH', '/path_to/plex/Library/Application Support/Plex Media Server/Media')  # Local Plex media path
TMP_FOLDER = os.environ.get('TMP_FOLDER', '/tmp/plex_generate_previews')  # Temporary folder for preview generation

PLEX_TIMEOUT = int(os.environ.get('PLEX_TIMEOUT', 60))  # Timeout for Plex API requests (seconds)
PLEX_LIBRARIES = [library.strip().lower() for library in os.environ.get('PLEX_LIBRARIES', '').split(',') if library.strip()]  # Comma-separated list of library names to process, case-insensitive

REGENERATE_THUMBNAILS = os.environ.get('REGENERATE_THUMBNAILS', 'false').strip().lower() in ('true', '1', 'yes')  # Force regeneration of thumbnails

# Path mappings for remote preview generation. # So you can have another computer generate previews for your Plex server
# If you are running on your plex server, you can set both variables to ''
PLEX_LOCAL_VIDEOS_PATH_MAPPING = os.environ.get('PLEX_LOCAL_VIDEOS_PATH_MAPPING', '')  # Local video path for the script
PLEX_VIDEOS_PATH_MAPPING = os.environ.get('PLEX_VIDEOS_PATH_MAPPING', '')  # Plex server video path

GPU_THREADS = int(os.environ.get('GPU_THREADS', 4))  # Number of GPU threads for preview generation
CPU_THREADS = int(os.environ.get('CPU_THREADS', 4))  # Number of CPU threads for preview generation

# Internal constants
WORKER_POOL_TIMEOUT = 30  # Timeout for worker pool shutdown (seconds)

# Set the timeout envvar for https://github.com/pkkid/python-plexapi
os.environ["PLEXAPI_PLEXAPI_TIMEOUT"] = str(PLEX_TIMEOUT)


if not shutil.which("mediainfo"):
    print('MediaInfo not found.  MediaInfo must be installed and available in PATH.')
    sys.exit(1)
try:
    from pymediainfo import MediaInfo
    import requests
    from plexapi.server import PlexServer
    from loguru import logger
    from rich.console import Console
    from rich.progress import Progress, SpinnerColumn, MofNCompleteColumn
except ImportError as e:
    print(f'Dependencies Missing!  Please run "pip3 install {e.name}".')
    sys.exit(1)

FFMPEG_PATH = shutil.which("ffmpeg")
if not FFMPEG_PATH:
    print('FFmpeg not found.  FFmpeg must be installed and available in PATH.')
    sys.exit(1)

# Logging setup
console = Console()
logger.remove()
logger.add(
    lambda _: console.print(_, end=''),
    level=os.environ.get('LOG_LEVEL', 'INFO').upper(),
    format='<green>{time:YYYY/MM/DD HH:mm:ss}</green> | {level.icon}'
    + '  - <level>{message}</level>',
    enqueue=True
)

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Plex Interface
retry_strategy = Retry(
    total=3,
    backoff_factor=0.3,
    status_forcelist=[500, 502, 503, 504],
)
adapter = HTTPAdapter(max_retries=retry_strategy)
session = requests.Session()
session.verify = False
session.mount("http://", adapter)
session.mount("https://", adapter)
plex = PlexServer(PLEX_URL, PLEX_TOKEN, timeout=PLEX_TIMEOUT, session=session)

# Monkey patch XML parsing to capture raw responses on parsing errors
import plexapi.utils as utils
import xml.etree.ElementTree as ET

# Store the original function
original_parseXMLString = utils.parseXMLString

def debug_parseXMLString(xml_string):
    try:
        return original_parseXMLString(xml_string)
    except ET.ParseError as e:
        # Log the raw XML content for debugging
        logger.error(f"XML parsing failed with error: {e}")
        logger.debug(f"Raw XML content (first 2000 chars):")
        logger.debug(xml_string[:2000])
        if len(xml_string) > 2000:
            logger.debug(f"... (truncated, total length: {len(xml_string)})")
        raise
    except Exception as e:
        logger.error(f"Unexpected error in XML parsing: {e}")
        raise

# Replace the function to capture raw XML on parsing errors
utils.parseXMLString = debug_parseXMLString


def detect_gpu():
    # Check for NVIDIA GPUs
    try:
        import pynvml
        pynvml.nvmlInit()
        num_nvidia_gpus = pynvml.nvmlDeviceGetCount()
        pynvml.nvmlShutdown()
        if num_nvidia_gpus > 0:
            return 'NVIDIA', None
    except ImportError:
        logger.debug("NVIDIA GPU detection library (pynvml) not found. NVIDIA GPUs will not be detected.")
    except pynvml.NVMLError as e:
        logger.debug(f"Error initializing NVIDIA GPU detection {e}. NVIDIA GPUs will not be detected.")

    # Check for AMD GPUs
    try:
        from amdsmi import amdsmi_interface
        amdsmi_interface.amdsmi_init()
        devices = amdsmi_interface.amdsmi_get_processor_handles()
        found = None
        if len(devices) > 0:
            for device in devices:
                processor_type = amdsmi_interface.amdsmi_get_processor_type(device)
                if processor_type == amdsmi_interface.AMDSMI_PROCESSOR_TYPE_GPU:
                    found = True
        try:
            amdsmi_interface.amdsmi_shut_down()
        except:
            pass  # Ignore shutdown errors
        if found:
                vaapi_device_dir = "/dev/dri"
                if os.path.exists(vaapi_device_dir):
                    for entry in os.listdir(vaapi_device_dir):
                        if entry.startswith("renderD"):
                            return "AMD", os.path.join(vaapi_device_dir, entry)
    except ImportError:
        logger.debug("AMD GPU detection library (amdsmi) not found. AMD GPUs will not be detected.")
    except KeyError as e:
        if 'ROCM_PATH' in str(e):
            logger.debug("ROCm is not properly installed or ROCM_PATH environment variable is not set. AMD GPU detection will be disabled.")
        else:
            logger.debug(f"KeyError in AMD GPU detection: {e}. AMD GPUs will not be detected.")
    except Exception as e:
        logger.debug(f"Error initializing AMD GPU detection: {e}. AMD GPUs will not be detected.")
    finally:
        try:
            amdsmi_interface.amdsmi_shut_down()
        except:
            pass  # Ignore shutdown errors if init failed

    # Check for Intel iGPU
    try:
        drm_dir = "/sys/class/drm"
        if os.path.exists(drm_dir):
            for entry in os.listdir(drm_dir):
                if not entry.startswith("card"):
                    continue
                driver_path = os.path.join(drm_dir, entry, "device", "driver")
                if os.path.islink(driver_path) and os.path.basename(os.readlink(driver_path)) == "i915":
                    vaapi_device_dir = "/dev/dri"
                    for dev_entry in os.listdir(vaapi_device_dir):
                        if dev_entry.startswith("renderD"):
                            return "INTEL", os.path.join(vaapi_device_dir, dev_entry)
    except Exception as e:
        logger.debug(f"Error detecting Intel iGPU: {e}. Intel iGPUs will not be detected.")

    return None, None


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


def generate_images(video_file, output_folder, gpu, gpu_device_path):
    media_info = MediaInfo.parse(video_file)
    vf_parameters = "fps=fps={}:round=up,scale=w=320:h=240:force_original_aspect_ratio=decrease".format(
        round(1 / PLEX_BIF_FRAME_INTERVAL, 6))

    # Check if we have a HDR Format. Note: Sometimes it can be returned as "None" (string) hence the check for None type or "None" (String)
    if media_info.video_tracks:
        if media_info.video_tracks[0].hdr_format != "None" and media_info.video_tracks[0].hdr_format is not None:
            vf_parameters = "fps=fps={}:round=up,zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv,format=yuv420p,scale=w=320:h=240:force_original_aspect_ratio=decrease".format(round(1 / PLEX_BIF_FRAME_INTERVAL, 6))
    args = [
        FFMPEG_PATH, "-loglevel", "info",
        "-threads:v", "1",  # fix: was '-threads:0 1'
    ]

    use_skip = heuristic_allows_skip(FFMPEG_PATH, video_file)
    if use_skip:
        args += ["-skip_frame:v", "nokey"]

    args += [
        "-i", video_file, "-an", "-sn", "-dn",
        "-q:v", str(THUMBNAIL_QUALITY),
        "-vf", vf_parameters,
        '{}/img-%06d.jpg'.format(output_folder)
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
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    # Allow time for it to start
    time.sleep(1)

    out, err = proc.communicate()
    if proc.returncode != 0:
        err_lines = err.decode('utf-8', 'ignore').split('\n')[-5:]
        logger.error(err_lines)
        logger.error('Problem trying to ffmpeg images for {}'.format(video_file))

    logger.debug('FFMPEG Command output')
    logger.debug(out)

    # Speed
    end = time.time()
    seconds = round(end - start, 1)
    speed = re.findall('speed= ?([0-9]+\\.?[0-9]*|\\.[0-9]+)x', err.decode('utf-8', 'ignore'))
    if speed:
        speed = speed[-1]

    # Optimize and Rename Images
    for image in glob.glob('{}/img*.jpg'.format(output_folder)):
        frame_no = int(os.path.basename(image).strip('-img').strip('.jpg')) - 1
        frame_second = frame_no * PLEX_BIF_FRAME_INTERVAL
        os.rename(image, os.path.join(output_folder, '{:010d}.jpg'.format(frame_second)))

    logger.info('Generated Video Preview for {} HW={} TIME={}seconds SPEED={}x '.format(video_file, hw, seconds, speed))


def generate_bif(bif_filename, images_path):
    """
    Build a .bif file
    @param bif_filename name of .bif file to create
    @param images_path Directory of image files 00000001.jpg
    """
    magic = [0x89, 0x42, 0x49, 0x46, 0x0d, 0x0a, 0x1a, 0x0a]
    version = 0

    images = [img for img in os.listdir(images_path) if os.path.splitext(img)[1] == '.jpg']
    images.sort()

    f = open(bif_filename, "wb")
    array.array('B', magic).tofile(f)
    f.write(struct.pack("<I", version))
    f.write(struct.pack("<I", len(images)))
    f.write(struct.pack("<I", 1000 * PLEX_BIF_FRAME_INTERVAL))
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
        data = open(os.path.join(images_path, image), "rb").read()
        f.write(data)

    f.close()
    logger.debug(f'Generated BIF file: {bif_filename}')


def process_item(item_key, gpu, gpu_device_path):
    try:
        data = plex.query('{}/tree'.format(item_key))
    except (requests.exceptions.RequestException, http.client.BadStatusLine, xml.etree.ElementTree.ParseError) as e:
        logger.error(f"Failed to query Plex for item {item_key}: {e}")
        logger.error(f"Exception type: {type(e).__name__}")
        # For XML parsing errors, provide additional context
        if isinstance(e, xml.etree.ElementTree.ParseError):
            logger.error(f"XML parsing error - Plex server returned malformed XML response")
            logger.error(f"This usually indicates server issues, network problems, or corrupted responses")
        # For connection errors, log more details
        elif hasattr(e, 'request') and e.request:
            logger.error(f"Request URL: {e.request.url}")
            logger.error(f"Request method: {e.request.method}")
            logger.error(f"Request headers: {e.request.headers}")
        return
    except Exception as e:
        logger.error(f"Error querying Plex for item {item_key}: {e}")
        return

    def sanitize_path(path):
        if os.name == 'nt':
            path = path.replace('/', '\\')
        return path

    for media_part in data.findall('.//MediaPart'):
        if 'hash' in media_part.attrib:
            # Filter Processing by HDD Path
            if len(sys.argv) > 1:
                if sys.argv[1] not in media_part.attrib['file']:
                    return
            bundle_hash = media_part.attrib['hash']
            media_file = sanitize_path(media_part.attrib['file'].replace(PLEX_VIDEOS_PATH_MAPPING, PLEX_LOCAL_VIDEOS_PATH_MAPPING))

            if not os.path.isfile(media_file):
                logger.warning('Skipping as file not found {}'.format(media_file))
                continue

            try:
                bundle_file = sanitize_path('{}/{}{}'.format(bundle_hash[0], bundle_hash[1::1], '.bundle'))
            except Exception as e:
                logger.error('Error generating bundle_file for {} due to {}:{}'.format(media_file, type(e).__name__, str(e)))
                continue

            bundle_path = sanitize_path(os.path.join(PLEX_LOCAL_MEDIA_PATH, 'localhost', bundle_file))
            indexes_path = sanitize_path(os.path.join(bundle_path, 'Contents', 'Indexes'))
            index_bif = sanitize_path(os.path.join(indexes_path, 'index-sd.bif'))
            tmp_path = sanitize_path(os.path.join(TMP_FOLDER, bundle_hash))

            if os.path.isfile(index_bif) and REGENERATE_THUMBNAILS:
                logger.debug('Found existing thumbnails for {}, deleting the thumbnail index at {} so we can regenerate'.format(media_file, index_bif))
                try:
                    os.remove(index_bif)
                except Exception as e:
                    logger.error('Error {} deleting index file {}: {}'.format(type(e).__name__, media_file, str(e)))
                    continue

            if not os.path.isfile(index_bif):
                logger.debug('Generating bundle_file for {} at {}'.format(media_file, index_bif))

                if not os.path.isdir(indexes_path):
                    try:
                        os.makedirs(indexes_path)
                    except OSError as e:
                        logger.error('Error generating images for {}. `{}:{}` error when creating index path {}'.format(media_file, type(e).__name__, str(e), indexes_path))
                        continue

                try:
                    if not os.path.isdir(tmp_path):
                        os.makedirs(tmp_path)
                except OSError as e:
                    logger.error('Error generating images for {}. `{}:{}` error when creating tmp path {}'.format(media_file, type(e).__name__, str(e), tmp_path))
                    continue

                try:
                    generate_images(media_file, tmp_path, gpu, gpu_device_path)
                except Exception as e:
                    logger.error('Error generating images for {}. `{}: {}` error when generating images'.format(media_file, type(e).__name__, str(e)))
                    # Clean up temp directory on error
                    try:
                        if os.path.exists(tmp_path):
                            shutil.rmtree(tmp_path)
                    except Exception as cleanup_error:
                        logger.warning(f"Failed to clean up temp directory {tmp_path}: {cleanup_error}")
                    continue

                try:
                    generate_bif(index_bif, tmp_path)
                except Exception as e:
                    # Remove bif, as it prob failed to generate
                    try:
                        if os.path.exists(index_bif):
                            os.remove(index_bif)
                    except Exception as remove_error:
                        logger.warning(f"Failed to remove failed BIF file {index_bif}: {remove_error}")
                    logger.error('Error generating images for {}. `{}:{}` error when generating bif'.format(media_file, type(e).__name__, str(e)))
                    continue
                finally:
                    # Always clean up temp directory
                    try:
                        if os.path.exists(tmp_path):
                            shutil.rmtree(tmp_path)
                    except Exception as cleanup_error:
                        logger.warning(f"Failed to clean up temp directory {tmp_path}: {cleanup_error}")


def filter_duplicate_locations(media_items):
    seen_locations = set()
    filtered_items = []
    
    for key, locations in media_items:            
        # Check if any location has been seen before
        if any(location in seen_locations for location in locations):
            continue
            
        # Add all locations to seen set and keep this item
        seen_locations.update(locations)
        filtered_items.append(key)  # Only return the key, not the tuple
    
    return filtered_items


def run(gpu, gpu_device_path):
    try:
        sections = plex.library.sections()
    except (requests.exceptions.RequestException, http.client.BadStatusLine, xml.etree.ElementTree.ParseError) as e:
        logger.error(f"Failed to get Plex library sections: {e}")
        logger.error(f"Exception type: {type(e).__name__}")
        if isinstance(e, xml.etree.ElementTree.ParseError):
            logger.error(f"XML parsing error - Plex server returned malformed XML response")
            logger.error(f"This usually indicates server issues, network problems, or corrupted responses")
        logger.error("Cannot proceed without library access. Please check your Plex server status.")
        return
    
    for section in sections:
        # Skip libraries that aren't in the PLEX_LIBRARIES list if it's not empty
        if PLEX_LIBRARIES and section.title.lower() not in PLEX_LIBRARIES:
            logger.info('Skipping library \'{}\' as it\'s not in the configured libraries list'.format(section.title))
            continue

        logger.info('Getting the media files from library \'{}\''.format(section.title))

        try:
            if section.METADATA_TYPE == 'episode':
                # Get episodes with locations for duplicate filtering
                media_with_locations = [(m.key, m.locations) for m in section.search(libtype='episode')]
                # Filter out multi episode files based on file locations
                media = filter_duplicate_locations(media_with_locations)
            elif section.METADATA_TYPE == 'movie':
                media = [m.key for m in section.search()]
            else:
                logger.info('Skipping library {} as \'{}\' is unsupported'.format(section.title, section.METADATA_TYPE))
                continue
        except (requests.exceptions.RequestException, http.client.BadStatusLine, xml.etree.ElementTree.ParseError) as e:
            logger.error(f"Failed to search library '{section.title}': {e}")
            logger.error(f"Exception type: {type(e).__name__}")
            if isinstance(e, xml.etree.ElementTree.ParseError):
                logger.error(f"XML parsing error - Plex server returned malformed XML response")
                logger.error(f"This usually indicates server issues, network problems, or corrupted responses")
            logger.warning(f"Skipping library '{section.title}' due to error")
            continue

        logger.info('Got {} media files for library {}'.format(len(media), section.title))
        logger.info(f'Processing with GPU({GPU_THREADS}) + CPU({CPU_THREADS}) threads | Queue capacity: {GPU_THREADS + CPU_THREADS}')

        # Create separate worker pools for CPU and GPU
        cpu_pool = None
        gpu_pool = None
        
        # Initialize CPU pool if CPU_THREADS > 0
        if CPU_THREADS > 0:
            cpu_pool = ProcessPoolExecutor(max_workers=CPU_THREADS)
            logger.debug(f'Initialized CPU pool with {CPU_THREADS} workers')
        
        # Initialize GPU pool if GPU_THREADS > 0 and GPU is available
        if GPU_THREADS > 0 and gpu:
            gpu_pool = ProcessPoolExecutor(max_workers=GPU_THREADS)
            logger.debug(f'Initialized GPU pool with {GPU_THREADS} workers')
        
        # Dynamic task assignment - submit tasks as slots become available
        gpu_futures = []
        cpu_futures = []
        media_queue = list(media)  # Copy the list
        completed_tasks = 0
        failed_tasks = 0
        
        with Progress(SpinnerColumn(), *Progress.get_default_columns(), MofNCompleteColumn(), console=console) as progress:
            task = progress.add_task("Processing media", total=len(media))
            
            # Submit initial batch of tasks
            while media_queue and (len(gpu_futures) + len(cpu_futures)) < (GPU_THREADS + CPU_THREADS):
                key = media_queue.pop(0)
                
                # Prefer GPU if available
                if gpu_pool and len(gpu_futures) < GPU_THREADS:
                    future = gpu_pool.submit(process_item, key, gpu, gpu_device_path)
                    gpu_futures.append(future)
                    logger.debug(f"Queue: GPU({len(gpu_futures)}/{GPU_THREADS}) CPU({len(cpu_futures)}/{CPU_THREADS}) | GPU has free slot, added job to GPU queue")
                elif cpu_pool and len(cpu_futures) < CPU_THREADS:
                    future = cpu_pool.submit(process_item, key, None, None)
                    cpu_futures.append(future)
                    logger.debug(f"Queue: GPU({len(gpu_futures)}/{GPU_THREADS}) CPU({len(cpu_futures)}/{CPU_THREADS}) | CPU has free slot, added job to CPU queue")
                else:
                    # No slots available, put task back
                    media_queue.insert(0, key)
                    break
            
            # Process completed tasks and submit new ones
            while gpu_futures or cpu_futures or media_queue:
                # Check for completed GPU tasks
                completed_gpu = [f for f in gpu_futures if f.done()]
                for future in completed_gpu:
                    gpu_futures.remove(future)
                    try:
                        future.result()
                        completed_tasks += 1
                        progress.update(task, advance=1)
                        logger.debug(f"GPU task completed, slot freed | Queue: GPU({len(gpu_futures)}/{GPU_THREADS}) CPU({len(cpu_futures)}/{CPU_THREADS})")
                    except Exception as e:
                        failed_tasks += 1
                        logger.error(f"GPU task failed: {e}")
                
                # Check for completed CPU tasks
                completed_cpu = [f for f in cpu_futures if f.done()]
                for future in completed_cpu:
                    cpu_futures.remove(future)
                    try:
                        future.result()
                        completed_tasks += 1
                        progress.update(task, advance=1)
                        logger.debug(f"CPU task completed, slot freed | Queue: GPU({len(gpu_futures)}/{GPU_THREADS}) CPU({len(cpu_futures)}/{CPU_THREADS})")
                    except Exception as e:
                        failed_tasks += 1
                        logger.error(f"CPU task failed: {e}")
                
                # Submit new tasks to available slots
                while media_queue and (len(gpu_futures) + len(cpu_futures)) < (GPU_THREADS + CPU_THREADS):
                    key = media_queue.pop(0)
                    
                    # Prefer GPU if available
                    if gpu_pool and len(gpu_futures) < GPU_THREADS:
                        future = gpu_pool.submit(process_item, key, gpu, gpu_device_path)
                        gpu_futures.append(future)
                        logger.debug(f"Queue: GPU({len(gpu_futures)}/{GPU_THREADS}) CPU({len(cpu_futures)}/{CPU_THREADS}) | GPU has free slot, added job to GPU queue")
                    elif cpu_pool and len(cpu_futures) < CPU_THREADS:
                        future = cpu_pool.submit(process_item, key, None, None)
                        cpu_futures.append(future)
                        logger.debug(f"Queue: GPU({len(gpu_futures)}/{GPU_THREADS}) CPU({len(cpu_futures)}/{CPU_THREADS}) | CPU has free slot, added job to CPU queue")
                    else:
                        # No slots available, put task back
                        media_queue.insert(0, key)
                        break
                
                # Small delay to prevent busy waiting
                if gpu_futures or cpu_futures:
                    time.sleep(0.1)
        
        logger.info(f'Processing complete: {completed_tasks} successful, {failed_tasks} failed | Final queue: GPU({len(gpu_futures)}/{GPU_THREADS}) CPU({len(cpu_futures)}/{CPU_THREADS})')
        
        # Clean up worker pools with timeout
        if cpu_pool:
            cpu_pool.shutdown(wait=True, timeout=WORKER_POOL_TIMEOUT)
            logger.debug("CPU pool shutdown completed")
        if gpu_pool:
            gpu_pool.shutdown(wait=True, timeout=WORKER_POOL_TIMEOUT)
            logger.debug("GPU pool shutdown completed")


if __name__ == '__main__':
    logger.info('Please log issues here https://github.com/stevezau/plex_generate_vid_previews/issues')

    # Validate required configuration
    required_config = [
        (PLEX_URL, 'PLEX_URL'),
        (PLEX_TOKEN, 'PLEX_TOKEN'),
        (os.path.exists(PLEX_LOCAL_MEDIA_PATH), f'PLEX_LOCAL_MEDIA_PATH ({PLEX_LOCAL_MEDIA_PATH}) does not exist'),
        (os.path.exists(os.path.join(PLEX_LOCAL_MEDIA_PATH, 'localhost')), 
         f'PLEX_LOCAL_MEDIA_PATH should contain "localhost" folder - check your mapping')
    ]
    
    for condition, error_msg in required_config:
        if not condition:
            logger.error(error_msg)
            exit(1)

    # Output Debug info on variables
    logger.debug('PLEX_URL = {}'.format(PLEX_URL))
    logger.debug('PLEX_BIF_FRAME_INTERVAL = {}'.format(PLEX_BIF_FRAME_INTERVAL))
    logger.debug('THUMBNAIL_QUALITY = {}'.format(THUMBNAIL_QUALITY))
    logger.debug('PLEX_LOCAL_MEDIA_PATH = {}'.format(PLEX_LOCAL_MEDIA_PATH))
    logger.debug('TMP_FOLDER = {}'.format(TMP_FOLDER))
    logger.debug('PLEX_TIMEOUT = {}'.format(PLEX_TIMEOUT))
    logger.debug('PLEX_LOCAL_VIDEOS_PATH_MAPPING = {}'.format(PLEX_LOCAL_VIDEOS_PATH_MAPPING))
    logger.debug('PLEX_VIDEOS_PATH_MAPPING = {}'.format(PLEX_VIDEOS_PATH_MAPPING))
    logger.debug('GPU_THREADS = {}'.format(GPU_THREADS))
    logger.debug('CPU_THREADS = {}'.format(CPU_THREADS))
    logger.debug('REGENERATE_THUMBNAILS = {}'.format(REGENERATE_THUMBNAILS))

    # Validate thread configuration
    if CPU_THREADS == 0 and GPU_THREADS == 0:
        logger.error('Both CPU_THREADS and GPU_THREADS are set to 0.')
        logger.error('At least one processing method must be enabled.')
        logger.error('Please set CPU_THREADS and/or GPU_THREADS to a value greater than 0.')
        exit(1)

    # detect GPU's
    gpu, gpu_device_path = None, None
    if GPU_THREADS > 0:
        gpu, gpu_device_path = detect_gpu()
        if gpu == 'NVIDIA':
            logger.info('Found NVIDIA GPU')
        elif gpu == 'AMD':
            logger.info(f'Found AMD GPU {gpu_device_path}')
        elif gpu == 'INTEL':
            logger.info(f'Found INTEL GPU {gpu_device_path}')
        if not gpu:
            # Exit and require user to explicitly set GPU_THREADS to 0
            logger.error(f'No GPUs detected but GPU_THREADS is set to {GPU_THREADS}.')
            logger.error('Please set the GPU_THREADS environment variable to 0 to use CPU-only processing.')
            logger.error('If you think this is an error please log an issue here https://github.com/stevezau/plex_generate_vid_previews/issues')
            exit(1)

    try:
        # Clean TMP Folder
        if os.path.isdir(TMP_FOLDER):
            shutil.rmtree(TMP_FOLDER)
        os.makedirs(TMP_FOLDER)
        run(gpu, gpu_device_path)
    except KeyboardInterrupt:
        logger.info("Received interrupt signal, shutting down gracefully...")
        # Graceful shutdown - no need to re-raise
    except Exception as e:
        logger.error(f"Unexpected error in main execution: {e}")
        raise
    finally:
        # Always clean up temp folder
        try:
            if os.path.isdir(TMP_FOLDER):
                shutil.rmtree(TMP_FOLDER)
                logger.debug(f"Cleaned up temp folder: {TMP_FOLDER}")
        except Exception as cleanup_error:
            logger.warning(f"Failed to clean up temp folder {TMP_FOLDER}: {cleanup_error}")
