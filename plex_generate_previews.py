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
import psutil
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor

from dotenv import load_dotenv

load_dotenv()

PLEX_URL = os.environ.get('PLEX_URL', '')  # Plex server URL. can also use for local server: http://localhost:32400
PLEX_TOKEN = os.environ.get('PLEX_TOKEN', '')  # Plex Authentication Token
PLEX_BIF_FRAME_INTERVAL = int(os.environ.get('PLEX_BIF_FRAME_INTERVAL', 5))  # Interval between preview images
THUMBNAIL_QUALITY = int(os.environ.get('THUMBNAIL_QUALITY', 4))  # Preview image quality (2-6)
PLEX_LOCAL_MEDIA_PATH = os.environ.get('PLEX_LOCAL_MEDIA_PATH', '/path_to/plex/Library/Application Support/Plex Media Server/Media')  # Local Plex media path
TMP_FOLDER = os.environ.get('TMP_FOLDER', '/dev/shm/plex_generate_previews')  # Temporary folder for preview generation
PLEX_TIMEOUT = int(os.environ.get('PLEX_TIMEOUT', 60))  # Timeout for Plex API requests (seconds)

# Path mappings for remote preview generation. # So you can have another computer generate previews for your Plex server
# If you are running on your plex server, you can set both variables to ''
PLEX_LOCAL_VIDEOS_PATH_MAPPING = os.environ.get('PLEX_LOCAL_VIDEOS_PATH_MAPPING', '')  # Local video path for the script
PLEX_VIDEOS_PATH_MAPPING = os.environ.get('PLEX_VIDEOS_PATH_MAPPING', '')  # Plex server video path

GPU_THREADS = int(os.environ.get('GPU_THREADS', 4))  # Number of GPU threads for preview generation
CPU_THREADS = int(os.environ.get('CPU_THREADS', 4))  # Number of CPU threads for preview generation

# Set the timeout envvar for https://github.com/pkkid/python-plexapi
os.environ["PLEXAPI_PLEXAPI_TIMEOUT"] = str(PLEX_TIMEOUT)

if not shutil.which("mediainfo"):
    print('MediaInfo not found.  MediaInfo must be installed and available in PATH.')
    sys.exit(1)
try:
    from pymediainfo import MediaInfo
except ImportError:
    print('Dependencies Missing!  Please run "pip3 install pymediainfo".')
    sys.exit(1)
try:
    import gpustat
except ImportError:
    print('Dependencies Missing!  Please run "pip3 install gpustat".')
    sys.exit(1)

try:
    import requests
except ImportError:
    print('Dependencies Missing!  Please run "pip3 install requests".')
    sys.exit(1)

try:
    from plexapi.server import PlexServer
except ImportError:
    print('Dependencies Missing!  Please run "pip3 install plexapi".')
    sys.exit(1)

try:
    from loguru import logger
except ImportError:
    print('Dependencies Missing!  Please run "pip3 install loguru".')
    sys.exit(1)

try:
    from rich.console import Console
except ImportError:
    print('Dependencies Missing!  Please run "pip3 install rich".')
    sys.exit(1)

try:
    from rich.progress import Progress, SpinnerColumn, MofNCompleteColumn
except ImportError:
    print('Dependencies Missing!  Please run "pip3 install rich".')
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
sess = requests.Session()
sess.verify = False
plex = PlexServer(PLEX_URL, PLEX_TOKEN, timeout=PLEX_TIMEOUT, session=sess)

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
        logger.warning("NVIDIA GPU detection library (pynvml) not found. NVIDIA GPUs will not be detected.")
    except pynvml.NVMLError as e:
        logger.warning(f"Error initializing NVIDIA GPU detection {e}. NVIDIA GPUs will not be detected.")

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
        amdsmi_interface.amdsmi_shut_down()
        if found:
                vaapi_device_dir = "/dev/dri"
                if os.path.exists(vaapi_device_dir):
                    for entry in os.listdir(vaapi_device_dir):
                        if entry.startswith("renderD"):
                            return "AMD", os.path.join(vaapi_device_dir, entry)
    except ImportError:
        logger.warning("AMD GPU detection library (amdsmi) not found. AMD GPUs will not be detected.")
    except Exception as e:
        logger.warning(f"Error initializing AMD GPU detection: {e}. AMD GPUs will not be detected.")

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
        logger.warning(f"Error detecting Intel iGPU: {e}. Intel iGPUs will not be detected.")

def get_amd_ffmpeg_processes():
    from amdsmi import amdsmi_init, amdsmi_shut_down, amdsmi_get_processor_handles, amdsmi_get_gpu_process_list
    try:
        amdsmi_init()
        gpu_handles = amdsmi_get_processor_handles()
        ffmpeg_processes = []

        for gpu in gpu_handles:
            processes = amdsmi_get_gpu_process_list(gpu)
            for process in processes:
                if process['name'].lower().startswith('ffmpeg'):
                    ffmpeg_processes.append(process)

        return ffmpeg_processes
    finally:
        amdsmi_shut_down()

def get_intel_ffmpeg_processes():
    vaapi_device_dir = "/dev/dri"
    intel_gpu_processes = []

    try:
        if os.path.exists(vaapi_device_dir):
            for entry in os.listdir(vaapi_device_dir):
                if entry.startswith("renderD"):
                    device_path = os.path.join(vaapi_device_dir, entry)
                    # Checking for processes
                    gpu_stats_query = gpustat.core.new_query()  # Assuming gpustat is available

                    # Check if gpu_stats_query is not None or empty
                    if gpu_stats_query:
                        for gpu_stats in gpu_stats_query:
                            if hasattr(gpu_stats, 'processes') and gpu_stats.processes:
                                for process in gpu_stats.processes:
                                    if 'ffmpeg' in process["command"].lower():
                                        intel_gpu_processes.append(process["command"])
                    else:
                        print(f"Warning: No GPU stats found for {device_path}")
    except Exception as e:
        print(f"Error detecting Intel GPU processes: {e}")
    
    return intel_gpu_processes

def generate_images(video_file, output_folder, gpu, gpu_device_path):
    media_info = MediaInfo.parse(video_file)
    vf_parameters = "fps=fps={}:round=up,scale=w=320:h=240:force_original_aspect_ratio=decrease".format(
        round(1 / PLEX_BIF_FRAME_INTERVAL, 6))

    # Check if we have a HDR Format. Note: Sometimes it can be returned as "None" (string) hence the check for None type or "None" (String)
    if media_info.video_tracks:
        if media_info.video_tracks[0].hdr_format != "None" and media_info.video_tracks[0].hdr_format is not None:
            vf_parameters = "fps=fps={}:round=up,zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv,format=yuv420p,scale=w=320:h=240:force_original_aspect_ratio=decrease".format(round(1 / PLEX_BIF_FRAME_INTERVAL, 6))
    args = [
        FFMPEG_PATH, "-loglevel", "info", "-skip_frame:v", "nokey", "-threads:0", "1", "-i",
        video_file, "-an", "-sn", "-dn", "-q:v", str(THUMBNAIL_QUALITY),
        "-vf",
        vf_parameters, '{}/img-%06d.jpg'.format(output_folder)
    ]

    start = time.time()
    hw = False

    if gpu == 'NVIDIA':
        gpu_stats_query = gpustat.core.new_query()
        logger.debug('Trying to determine how many GPU threads running')
        if len(gpu_stats_query):
            gpu_ffmpeg = []
            for gpu_stats in gpu_stats_query:
                for process in gpu_stats.processes:
                    if 'ffmpeg' in process["command"].lower():
                        gpu_ffmpeg.append(process["command"])

            logger.debug('Counted {} ffmpeg GPU threads running'.format(len(gpu_ffmpeg)))
            if len(gpu_ffmpeg) > GPU_THREADS:
                logger.debug('Hit limit on GPU threads, defaulting back to CPU')
            if len(gpu_ffmpeg) < GPU_THREADS or CPU_THREADS == 0:
                hw = True
                args.insert(5, "-hwaccel")
                args.insert(6, "cuda")
    else:
        # AMD or Intel
        
        if gpu == 'INTEL': 
            gpu_ffmpeg = get_intel_ffmpeg_processes()
        else:
            gpu_ffmpeg = get_amd_ffmpeg_processes()
            
        logger.debug('Counted {} ffmpeg GPU threads running'.format(len(gpu_ffmpeg)))
        if len(gpu_ffmpeg) > GPU_THREADS:
            logger.debug('Hit limit on GPU threads, defaulting back to CPU')

        if len(gpu_ffmpeg) < GPU_THREADS or CPU_THREADS == 0:
            hw = True
            args.insert(5, "-hwaccel")
            args.insert(6, "vaapi")
            args.insert(7, "-vaapi_device")
            args.insert(8, gpu_device_path)
            # Adjust vf_parameters for Intel 
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

    logger.debug('Running ffmpeg')
    logger.debug(' '.join(args))
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


def process_item(item_key, gpu, gpu_device_path):
    data = plex.query('{}/tree'.format(item_key))

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
                logger.error('Skipping as file not found {}'.format(media_file))
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
                    logger.error('Error generating images for {}. `{}: {}` error when gnerating images'.format(media_file, type(e).__name__, str(e)))
                    if os.path.exists(tmp_path):
                        shutil.rmtree(tmp_path)
                    continue

                try:
                    generate_bif(index_bif, tmp_path)
                except Exception as e:
                    # Remove bif, as it prob failed to generate
                    if os.path.exists(index_bif):
                        os.remove(index_bif)
                    logger.error('Error generating images for {}. `{}:{}` error when generating bif'.format(media_file, type(e).__name__, str(e)))
                    continue
                finally:
                    if os.path.exists(tmp_path):
                        shutil.rmtree(tmp_path)


def run(gpu, gpu_device_path):
    for section in plex.library.sections():
        logger.info('Getting the media files from library \'{}\''.format(section.title))

        if section.METADATA_TYPE == 'episode':
            media = [m.key for m in section.search(libtype='episode')]
        elif section.METADATA_TYPE == 'movie':
            media = [m.key for m in section.search()]
        else:
            logger.info('Skipping library {} as \'{}\' is unsupported'.format(section.title, section.METADATA_TYPE))
            continue

        logger.info('Got {} media files for library {}'.format(len(media), section.title))

        with Progress(SpinnerColumn(), *Progress.get_default_columns(), MofNCompleteColumn(), console=console) as progress:
            with ProcessPoolExecutor(max_workers=CPU_THREADS + GPU_THREADS) as process_pool:
                futures = [process_pool.submit(process_item, key, gpu, gpu_device_path) for key in media]
                for future in progress.track(futures):
                    future.result()


if __name__ == '__main__':
    logger.info('GPU Detection (with AMD and INTEL Support) was recently added to this script.')
    logger.info('Please log issues here https://github.com/stevezau/plex_generate_vid_previews/issues')

    if not os.path.exists(PLEX_LOCAL_MEDIA_PATH):
        logger.error(
            '%s does not exist, please edit PLEX_LOCAL_MEDIA_PATH environment variable' % PLEX_LOCAL_MEDIA_PATH)
        exit(1)

    if not os.path.exists(os.path.join(PLEX_LOCAL_MEDIA_PATH, 'localhost')):
        logger.error(
            'You set PLEX_LOCAL_MEDIA_PATH to "%s". There should be a folder called "localhost" in that directory but it does not exist which suggests you haven\'t mapped it correctly. Please fix the PLEX_LOCAL_MEDIA_PATH environment variable' % PLEX_LOCAL_MEDIA_PATH)
        exit(1)

    if PLEX_URL == '':
        logger.error('Please set the PLEX_URL environment variable')
        exit(1)

    if PLEX_TOKEN == '':
        logger.error('Please set the PLEX_TOKEN environment variable')
        exit(1)

    # detect GPU's
    gpu, gpu_device_path = detect_gpu()
    if gpu == 'NVIDIA':
        logger.info('Found NVIDIA GPU')
    elif gpu == 'AMD':
        logger.info(f'Found AMD GPU {gpu_device_path}')
    elif gpu == 'INTEL':
        logger.info(f'Found INTEL GPU {gpu_device_path}')
    if not gpu:
        logger.warning('No GPUs detected. Defaulting to CPU ONLY.')
        logger.warning('If you think this is an error please log an issue here https://github.com/stevezau/plex_generate_vid_previews/issues')

    try:
        # Clean TMP Folder
        if os.path.isdir(TMP_FOLDER):
            shutil.rmtree(TMP_FOLDER)
        os.makedirs(TMP_FOLDER)
        run(gpu, gpu_device_path)
    finally:
        if os.path.isdir(TMP_FOLDER):
            shutil.rmtree(TMP_FOLDER)
