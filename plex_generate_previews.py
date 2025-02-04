#!/usr/bin/env python3
#import traceback
import sys
import re
import subprocess
import shutil
import glob
import os
import signal
import struct
import array
import time
import json
import textwrap
#from pathlib import Path
from concurrent.futures import ProcessPoolExecutor
import argparse
import urllib3

from dotenv import load_dotenv

load_dotenv()

PLEX_URL = os.environ.get('PLEX_URL', '')  # Plex server URL. can also use for local server: http://localhost:32400
PLEX_TOKEN = os.environ.get('PLEX_TOKEN', '')  # Plex Authentication Token
PLEX_BIF_FRAME_INTERVAL = int(os.environ.get('PLEX_BIF_FRAME_INTERVAL', 5))  # Interval between preview images
THUMBNAIL_QUALITY = int(os.environ.get('THUMBNAIL_QUALITY', 3))  # Preview image quality (2-6)
PLEX_LOCAL_MEDIA_PATH = os.environ.get('PLEX_LOCAL_MEDIA_PATH', '/path_to/plex/Library/Application Support/Plex Media Server/Media')  # Local Plex media path
TMP_FOLDER = os.environ.get('TMP_FOLDER', '/dev/shm/plex_generate_previews')  # Temporary folder for preview generation
PLEX_TIMEOUT = int(os.environ.get('PLEX_TIMEOUT', 60))  # Timeout for Plex API requests (seconds)
USE_LIB_PLACEBO = os.getenv("USE_LIB_PLACEBO", 'False').lower() in ('true', '1', 't')
FORCE_REGENERATION_OF_BIF_FILES = os.getenv("FORCE_REGENERATION_OF_BIF_FILES", 'False').lower() in ('true', '1', 't')
PLEX_MEDIA_TYPES_TO_PROCESS = os.getenv("PLEX_MEDIA_TYPES_TO_PROCESS", '').lower()
PLEX_LIBRARIES_TO_PROCESS = os.getenv("PLEX_LIBRARIES_TO_PROCESS", '')
RUN_PROCESS_AT_LOW_PRIORITY = os.getenv("RUN_PROCESS_AT_LOW_PRIORITY", "False").lower() in ('true', '1', 't')
LOG_LEVEL = os.environ.get('LOG_LEVEL', 'INFO').upper()
LOG_FILES_RETENTION = os.environ.get('LOG_FILES_RETENTION', '14 days')

# |Level name | Severity value | Logger method     |
# |-----------|----------------|-------------------|
# | TRACE     |  5             | logger.trace()    |
# | DEBUG     | 10             | logger.debug()    |
# | INFO      | 20             | logger.info()     |
# | SUCCESS   | 25             | logger.success()  |
# | WARNING   | 30             | logger.warning()  |
# | ERROR     | 40             | logger.error()    |
# | CRITICAL  | 50             | logger.critical() |

GPU_THREADS = int(os.environ.get('GPU_THREADS', 4))  # Number of GPU threads for preview generation
CPU_THREADS = int(os.environ.get('CPU_THREADS', 4))  # Number of CPU threads for preview generation

# Set the timeout envvar for https://github.com/pkkid/python-plexapi
os.environ["PLEXAPI_PLEXAPI_TIMEOUT"] = str(PLEX_TIMEOUT)

if not shutil.which("mediainfo"):
    print('MediaInfo not found.  MediaInfo must be installed and available in PATH.')
    sys.exit(1)
try:
    from pymediainfo import MediaInfo
    if MediaInfo.can_parse() is False:
        raise ImportError("MediaInfo can't parse input files")
except ImportError as e:
    print(e)
    print('Dependencies Missing!  Please run "pip3 install pymediainfo".')
    sys.exit(1)

try:
    import gpustat
except ImportError as e:
    print(e)
    print('Dependencies Missing!  Please run "pip3 install gpustat".')
    sys.exit(1)

try:
    import requests
except ImportError as e:
    print(e)
    print('Dependencies Missing!  Please run "pip3 install requests".')
    sys.exit(1)

try:
    from plexapi.server import PlexServer
except ImportError as e:
    print(e)
    print('Dependencies Missing!  Please run "pip3 install plexapi".')
    sys.exit(1)

try:
    from loguru import logger
except ImportError as e:
    print(e)
    print('Dependencies Missing!  Please run "pip3 install loguru".')
    sys.exit(1)

try:
    from rich.console import Console
except ImportError as e:
    print(e)
    print('Dependencies Missing!  Please run "pip3 install rich".')
    sys.exit(1)

try:
    from rich.progress import Progress, SpinnerColumn, MofNCompleteColumn
except ImportError as e:
    print(e)
    print('Dependencies Missing!  Please run "pip3 install rich".')
    sys.exit(1)

FFMPEG_PATH = shutil.which("ffmpeg")
if not FFMPEG_PATH:
    print('FFmpeg not found.  FFmpeg must be installed and available in PATH.')
    sys.exit(1)

class UltimateHelpFormatter(
    argparse.RawTextHelpFormatter,
    argparse.ArgumentDefaultsHelpFormatter
):
    pass

ansi_plex_orange = "\033[48;5;166;97;1m"
ansi_default = "\033[0m"

parser = argparse.ArgumentParser(
    prog = "plex_generate_vid_previews",
    description = textwrap.dedent(f"""\
        {ansi_plex_orange}✱Plex Preview Thumbnail Generator✱{ansi_default}
        This program is designed to speed up the process of generating preview thumbnails for your Plex media library.
    """),
    epilog = "Text at the bottom of help",
    formatter_class = UltimateHelpFormatter
)
parser.add_argument(
    "-s",
    "--search",
    help = "search Plex for title"
)
parser.add_argument(
    "-e",
    "--episode_title",
    action="store_true",
    help = "if searching a library with shows then search episode titles, if not specified show titles are searched"
)
parser.add_argument(
    "-f",
    "--force",
    action="store_true",
    help = "force regeneration of BIF"
)
parser.add_argument(
    "-p",
    "--lib_placebo",
    action="store_true",
    help = textwrap.dedent("""\
        use libplacebo for HDR tone-mapping, otherwise
        use default libavfilter tone-mapping.
        """)
)
parser.add_argument(
    "-q",
    "--thumbnail_quality",
    required = False,
    type = int,
    choices = range(2,6),
    metavar = "[2-6]",
    default = 3,
    help = textwrap.dedent("""\
        preview image quality %(metavar)s (default: %(default)s):
            -q, --thumbnail_quality=%(default)s good balance between quality and file-size (default and recommend setting)
            -q, --thumbnail_quality=2 the highest quality and largest file size
            -q, --thumbnail_quality=6 the lowest quality and smallest file size
        """)
)
parser.add_argument(
    "-i",
    "--bif_interval",
    required = False,
    type = int,
    choices = range(1,30),
    metavar = "[1-30]",
    default = 4,
    help = textwrap.dedent("""\
        interval between preview images in seconds %(metavar)s (default: %(default)s):
            -i, --bif_interval=%(default)s  generate a preview thumbnail every %(default)s seconds (default and recommend setting)†
            -i, --bif_interval=1  generate a preview thumbnail every second (largest file size, longest processing time, best resolution for trick-play)
            -i, --bif_interval=30 generate a preview thumbnail every 30 seconds (smaller file size, shorter processing time, worst resolutionm for trick-play)
        †preview thumbnails are only generated from keyframes, in some video sources these can be 10+seconds apart,
        """)
)
parser.add_argument(
    "-l",
    "--loglevel",
    required = False,
    default = "INFO",
    choices = list(logger._core.levels.keys()), #pylint: disable=protected-access # logouru Delgan provides no methods to access logoru state "WONTFIX"
    help = textwrap.dedent("""\
        set the log level (default: %(default)s)
            --loglevel=TRACE
            --loglevel=DEBUG
            --loglevel=INFO
            --loglevel=SUCCESS
            --loglevel=WARNING
            --loglevel=ERROR
            --loglevel=CRITICAL
        """)
)

cli_args = parser.parse_args(
    namespace=argparse.Namespace(
        force = FORCE_REGENERATION_OF_BIF_FILES,
        bif_interval = PLEX_BIF_FRAME_INTERVAL,
        thumbnail_quality = THUMBNAIL_QUALITY,
        loglevel = LOG_LEVEL
    )
)

if cli_args.force:
    FORCE_REGENERATION_OF_BIF_FILES = True

if cli_args.lib_placebo:
    USE_LIB_PLACEBO = True

if cli_args.thumbnail_quality:
    THUMBNAIL_QUALITY = cli_args.thumbnail_quality

if cli_args.bif_interval:
    PLEX_BIF_FRAME_INTERVAL = cli_args.bif_interval

if cli_args.loglevel:
    LOG_LEVEL = cli_args.loglevel.upper()

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Compile REGEXs
pattern_fps   = re.compile(
    r'^fps= ?(?=.)([+-]?([0-9]*)(\.([0-9]+))?)([eE][+-]?\d+)?$',
    flags=re.MULTILINE
)
pattern_speed = re.compile(
    r'^speed= ?(?=.)([+-]?([0-9]*)(\.([0-9]+))?)([eE][+-]?\d+)?x$',
    flags=re.MULTILINE
)

def set_logger(logger_):
    """
    Pass logger handler to spawned Windows processes.
    (NB Linux fork wouldn't require this).
    """
    global logger
    logger = logger_


def setup_logging():
    """Logging setup"""
    LOG_FORMAT = '<green>{time:YYYY/MM/DD HH:mm:ss}</green> | {level.icon}  - <level>{message}</level>'
    global console
    console = Console(color_system='truecolor')
    logger.remove()
    logger.add(
        lambda _: console.print(_, end=''),
        level = LOG_LEVEL,
        format = LOG_FORMAT,
        enqueue = True
    )
    logger.add(
        os.path.join("logs", "plex_generate_previews_{time}.log"),
        retention=LOG_FILES_RETENTION,
        level = LOG_LEVEL,
        format = LOG_FORMAT,
        colorize = False,
        enqueue = True
    )


def signal_handler(arg1, arg2):
    """
    Signal interrupt handler.

    """
    print(f"⚠️Received SIGTERM or SIGINT {arg1} {arg2}⚠️")
    print("Sending SIGKILL to PPID...")
    os.kill(os.getpid(), 9)

# Register SIGERM signal handler
signal.signal(signal.SIGTERM, signal_handler)

# Register SIGINT (CTRL-C) signal handler
signal.signal(signal.SIGINT, signal_handler)


def set_process_niceness(niceness=25):
    """ Set the niceness/priority of the process."""

    isWindows = os.name == "nt"

    if isWindows:
        try:
            import win32api, win32process

            # <https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-setpriorityclass>
            #
            # priorityclasses:
            #  win32process.IDLE_PRIORITY_CLASS,            # Process whose threads run only when the system is idle. The threads of the process are preempted
                                                            # by the threads of any process running in a higher priority class. An example is a screen saver.
                                                            # The idle-priority class is inherited by child processes.
            #  win32process.BELOW_NORMAL_PRIORITY_CLASS,    # IDLE_PRIORITY_CLASS < x < NORMAL_PRIORITY_CLASS
            #  win32process.NORMAL_PRIORITY_CLASS,          # Process with no special scheduling needs.
            #  win32process.ABOVE_NORMAL_PRIORITY_CLASS,    # NORMAL_PRIORITY_CLASS < x < HIGH_PRIORITY_CLASS
            #  win32process.HIGH_PRIORITY_CLASS,            # Process that performs time-critical tasks that must be executed immediately.  The threads of the
                                                            # process preempt the threads of normal or idle priority class processes. An example is the Task
                                                            # List, which must respond quickly when called by the user, regardless of the load on the operating
                                                            # system. Use extreme care when using the high-priority class, because a high-priority class
                                                            # application can use nearly all available CPU time.
            #  win32process.REALTIME_PRIORITY_CLASS

            win_priority_map = {
                10  : "IDLE_PRIORITY_CLASS",                # Task Manager displays "Low"
                25  : "BELOW_NORMAL_PRIORITY_CLASS",
                50  : "NORMAL_PRIORITY_CLASS",
                75  : "ABOVE_NORMAL_PRIORITY_CLASS",
                #90  : "HIGH_PRIORITY_CLASS",
                #100 : "REALTIME_PRIORITY_CLASS",
            }

            # Check if the niceness value is valid
            if niceness not in win_priority_map:
                raise ValueError(f"Invalid priority value: {niceness}. Valid values are 10, 25, 50, 75, 90, and 100.")

            priority_class = win_priority_map.get(niceness, "NORMAL_PRIORITY_CLASS")

            win32process.SetPriorityClass(
                win32api.GetCurrentProcess(),
                getattr(win32process, priority_class)
            )

            logger.info(f"Process priority set to Windows priority class {priority_class} from an input niceness value os {niceness}.")
            if niceness > 50:
                logger.warning(("Process priority set above normal priority class, "
                                "this is a privileged operation and schedules the process as high priority."))

        except ImportError:
            logger.error("The 'pywin32' module is required to set priority on Windows.")
        except ValueError as e:
            logger.error(f"Error: {e}")
        except Exception as e:
            logger.critical(f"Failed to set process priority on Windows: {e}")

    else:
        nice_value_map = {
            10: 20,
            25: 10,
            50: 0,
            75: -5,
            90: -10,
            100: -20,
        }

        nice_value = nice_value_map.get(niceness, 0)

        try:
            os.nice(nice_value)
        except PermissionError:
            logger.error("Insufficient permissions to change the process priority. Try running as an administrator.")
        except Exception as e:
            logger.critical(f"Failed to set priority: {e}")

        logger.info(f"Process niceness set to {niceness}.")
        if niceness < 0:
            logger.warning(f"Process niceness set to a negative value ({niceness}), a privileged operation and schedules the process as high priority.")


def detect_gpu():
    # Check for NVIDIA GPUs
    try:
        import pynvml
        pynvml.nvmlInit()
        num_nvidia_gpus = pynvml.nvmlDeviceGetCount()
        pynvml.nvmlShutdown()
        if num_nvidia_gpus > 0:
            return 'NVIDIA'
    except ImportError:
        logger.warning("NVIDIA GPU detection library (pynvml) not found. NVIDIA GPUs will not be detected.")
    except pynvml.NVMLError as e:
        logger.warning(f"Error initializing NVIDIA GPU detection {e}. NVIDIA GPUs will not be detected.")


def parse_path_mappings():
    """
    Path mappings for remote preview generation.
    So you can have another computer generate previews for your Plex server.
    If you are running on your plex server, you can set both variables to ''
    """

    PLEX_LOCAL_VIDEOS_PATH_MAPPINGS_JSON = os.environ.get(
        'PLEX_LOCAL_VIDEOS_PATH_MAPPINGS_JSON',
        '{}'
    ).replace('\\', r'\\')

    try:
        PLEX_LOCAL_VIDEOS_PATH_MAPPINGS = json.loads(PLEX_LOCAL_VIDEOS_PATH_MAPPINGS_JSON)
    except json.JSONDecodeError as e:
        logger.error((
            f"PLEX_LOCAL_VIDEOS_PATH_MAPPINGS:"
            f" Unable to decode JSON, Error: {e}."
        ))
        logger.error("Check the environmental variable PLEX_LOCAL_VIDEOS_PATH_MAPPINGS_JSON for correct JSON formatting:")
        logger.error(PLEX_LOCAL_VIDEOS_PATH_MAPPINGS_JSON)
        sys.exit(1)

    return PLEX_LOCAL_VIDEOS_PATH_MAPPINGS


def sizeof_fmt(num, suffix="B", to_si=False, precision=1):
    """Human Readable Binary(IEC)/Decimal(SI) Numbers"""
    scale = 1000.0
    if not to_si:
        suffix = "i" + suffix
        scale = 1024.0
    for unit in ("", "K", "M", "G", "T", "P", "E", "Z"):
        if abs(num) < scale:
            return f"{num:3.{precision}f}{unit}{suffix}"
        num /= scale
    return f"{num:.1f}Y{suffix}"

def sanitize_path(path):
    if os.name == 'nt':
        path = path.replace('/', '\\')
    return path

class MediaError(ValueError):
    '''raise this when there is an error with the video media file'''

class FfmpegError(ValueError):
    '''raise this when FFmpeg fails to generate preview images'''

def hdr_format_str(video_track):
    video_parameters = ""
    if video_track:
        if video_track.duration:
            video_parameters += f", duration={(video_track.duration)}"
        if video_track.hdr_format:
            video_parameters += f", hdr_format={(video_track.hdr_format)}"
        if video_track.other_hdr_format:
            video_parameters += f", other_hdr_format={(video_track.other_hdr_format)}"
        if video_track.hdr_format_profile:
            video_parameters += f", hdr_format_profile={(video_track.hdr_format_profile)}"
        if video_track.hdr_format_level:
            video_parameters += f", hdr_format_level={(video_track.hdr_format_level)}"
        if video_track.hdr_format_settings:
            video_parameters += f", hdr_format_settings={(video_track.hdr_format_settings)}"
        if video_track.hdr_format_compatibility:
            video_parameters += f", hdr_format_compatibility={(video_track.hdr_format_compatibility)}"
        if video_track.bit_rate:
            video_parameters += f", bit_rate={sizeof_fmt(video_track.bit_rate, to_si=True, suffix='bps', precision=3)}"
        return video_parameters
    else:
        return "no video_track"

# hdr_format=Dolby Vision
# other_hdr_format=['Dolby Vision, Version 1.0, dvhe.05.06, BL+RPU']
# hdr_format_profile=dvhe.05, hdr_format_level=06, hdr_format_settings=BL+RPU

# hdr_format=SMPTE ST 2086
# other_hdr_format=['SMPTE ST 2086, HDR10 compatible']
# hdr_format_compatibility=HDR10

# hdr_format=Dolby Vision / SMPTE ST 2086
# other_hdr_format=['Dolby Vision, Version 1.0, dvhe.08.06, BL+RPU, HDR10 compatible / SMPTE ST 2086, HDR10 compatible']
# hdr_format_profile=dvhe.08, hdr_format_level=06, hdr_format_settings=BL+RPU
# hdr_format_compatibility=HDR10 / HDR10

def generate_images(video_file, output_folder, gpu):
    """generate video preview images"""

    media_info = MediaInfo.parse(video_file)

    vf_parameters = f"fps=fps={round(1 / PLEX_BIF_FRAME_INTERVAL, 6)}:round=up,scale=w=320:h=240:force_original_aspect_ratio=decrease"
    hdr = False
    dovi_only = False

    # Check if we have a HDR Format. Note: Sometimes it can be returned as "None" (string) hence the check for None type or "None" (String)
    if media_info.video_tracks:
        if media_info.video_tracks[0].hdr_format != "None" and media_info.video_tracks[0].hdr_format is not None:
            hdr = True
            dovi_only = media_info.video_tracks[0].hdr_format_compatibility is None
            logger.debug("HDR format reported by MediaInfo")
            if USE_LIB_PLACEBO or dovi_only:
                if not USE_LIB_PLACEBO and dovi_only:
                    logger.debug("Video file contains only DoVi, forcing use of libplacebo for this file")
                # libplacebo - Flexible GPU-accelerated processing filter based on libplacebo <https://code.videolan.org/videolan/libplacebo>
                #   -init_hw_device vulkan ^
                #   dithering=ordered_fixed  (for max performance)
                #   dither_lut_size=6 (x=1...8, 2^x)
                #   apply_filmgrain=false
                #
                # Convert input to standard sRGB JPEG:
                #   libplacebo=format=yuv420p:colorspace=bt470bg:color_primaries=bt709:color_trc=iec61966-2-1:range=pc
                #
                #
                # Rescale input to fit into standard 1080p, with high quality scaling:
                #  libplacebo=w=1920:h=1080:force_original_aspect_ratio=decrease:normalize_sar=true:upscaler=ewa_lanczos:downscaler=ewa_lanczos
                #
                #
                # Suppress CPU-based AV1/H.274 film grain application in the decoder, in favor of doing it with this filter.
                # Note that this is only a gain if the frames are either already on the GPU, or if you’re using libplacebo
                # for other purposes, since otherwise the VRAM roundtrip will more than offset any expected speedup.
                #  ffmpeg -export_side_data +film_grain ... -vf libplacebo=apply_filmgrain=true
                #
                #
                # Interop with VAAPI hwdec to avoid round-tripping through RAM:
                #   ffmpeg -init_hw_device vulkan -hwaccel vaapi -hwaccel_output_format vaapi ... -vf libplacebo
                #
                # -vf "hwupload,libplacebo=tonemapping=bt.2446a:colorspace=bt709:color_primaries=bt709:color_trc=bt709:range=limited,hwdownload,format=yuv420p10" ^

                # tonemap_opencl:
                #   -i INPUT -vf "format=p010,hwupload,tonemap_opencl=t=bt2020:tonemap=linear:format=p010,hwdownload,format=p010" OUTPUT

                # tonemap_vaapi - currently only accepts HDR10 as input:
                #   tonemap_vaapi=format=p010:t=bt2020-10

                vf_parameters = f"hwupload,libplacebo=fps=1/{PLEX_BIF_FRAME_INTERVAL}:frame_mixer=none:tonemapping=bt.2446a:colorspace=bt709:color_primaries=bt709:color_trc=bt709:range=tv:w=320:h=240:force_original_aspect_ratio=decrease:format=yuv420p10le,hwdownload,format=yuv420p10le"
            else:
                vf_parameters = f"fps=fps={round(1 / PLEX_BIF_FRAME_INTERVAL, 6)}:round=up,zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv,format=yuv420p,scale=w=320:h=240:force_original_aspect_ratio=decrease"
    else:
        raise MediaError("No video tracks were detected by MediaInfo")

    args = [
        FFMPEG_PATH, "-loglevel", "error", "-nostats", "-progress", "-", "-stats_period", "10000000000", "-skip_frame:v", "nokey", "-threads:0", "1", "-i",
        video_file, "-an", "-sn", "-dn", "-qscale:v", str(THUMBNAIL_QUALITY),
        "-vf",
        vf_parameters, sanitize_path(f"{output_folder}/img-%06d.jpg")
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

            logger.debug(f"Counted {len(gpu_ffmpeg)} ffmpeg GPU threads running")
            if len(gpu_ffmpeg) > GPU_THREADS:
                logger.debug('Hit limit on GPU threads, defaulting back to CPU')
            if len(gpu_ffmpeg) < GPU_THREADS or CPU_THREADS == 0:
                hw = True
                if USE_LIB_PLACEBO or dovi_only:
                    args.insert(8, "-init_hw_device")
                    args.insert(9, "vulkan")
                else:
                    args.insert(8, "-hwaccel")
                    args.insert(9, "cuda")

    logger.debug('Running ffmpeg')
    logger.debug(' '.join(args))

    logger.debug(f"{video_file}:" + hdr_format_str(media_info.video_tracks[0]))

    with subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as proc:
        # Allow time for it to start
        time.sleep(1)
        stdout, stderr = proc.communicate()

    # @TODO add progress bar on subprocesses processing
    # change ffmpeg -stats_periond=10(?) and collect stats
    #
    # progress=continue ... progress=end
    #
    # frame=83
    # fps=0.00
    # stream_0_0_q=3.0
    # bitrate=N/A
    # total_size=N/A
    # out_time_us=415000000
    # out_time_ms=415000000
    # out_time=00:06:55.000000
    # dup_frames=0
    # drop_frames=0
    # speed=1.27e+03x
    # progress=continue
    # frame=179
    # fps=0.00
    # stream_0_0_q=3.0
    # bitrate=N/A
    # total_size=N/A
    # out_time_us=890000000
    # out_time_ms=890000000
    # out_time=00:14:50.000000
    # dup_frames=0
    # drop_frames=0
    # speed=1.39e+03x
    # progress=continue
    # frame=272
    # fps=0.00
    # stream_0_0_q=3.0
    # bitrate=N/A
    # total_size=N/A
    # out_time_us=1360000000
    # out_time_ms=1360000000
    # out_time=00:22:40.000000
    # dup_frames=0
    # drop_frames=0
    # speed=1.42e+03x
    # progress=continue
    # frame=294
    # fps=285.43
    # stream_0_0_q=3.0
    # bitrate=N/A
    # total_size=N/A
    # out_time_us=1470000000
    # out_time_ms=1470000000
    # out_time=00:24:30.000000
    # dup_frames=0
    # drop_frames=0
    # speed=1.43e+03x
    # progress=end

    if proc.returncode != 0:
        err_lines = stderr.decode("utf-8", "replace").split("\n")[-5:]
        logger.error(err_lines)
        logger.error(f"ffmpeg error whilst generating images for {video_file}")
        raise FfmpegError("ffmpeg error whilst generating images")

    logger.debug("FFMPEG Command output")
    logger.debug(stdout)
    logger.debug(stderr)

    # Speed
    end = time.time()
    seconds = round(end - start, 1)

    fps   = pattern_fps.findall(stdout.decode("utf-8", "replace"))
    speed = pattern_speed.findall(stdout.decode("utf-8", "replace"))

    # select first group of last match (in case stats are printed more often)
    if speed:
        speed = speed[-1][0]

    if fps:
        fps = fps[-1][0]

    # Optimize and Rename Images
    for image in glob.glob(f"{output_folder}/img*.jpg"):
        frame_no = int(os.path.basename(image).strip("-img").strip(".jpg")) - 1
        frame_second = frame_no * PLEX_BIF_FRAME_INTERVAL
        os.rename(image, os.path.join(output_folder, f"{frame_second:010d}.jpg"))

    return {
        'hw' : hw,
        'seconds' : seconds,
        'speed' : speed,
        'fps' : fps,
        'hdr' : hdr
    }


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

    if len(images) == 0:
        logger.error(f"No images found when generating BIF {images_path} {bif_filename}")
        raise FfmpegError("No images found when generating BIF")

    with open(bif_filename, "wb") as f:
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

        return os.stat(bif_filename).st_size


def process_item(item_key, gpu, path_mappings):
    sess = requests.Session()
    sess.verify = False
    plex = PlexServer(PLEX_URL, PLEX_TOKEN, timeout=PLEX_TIMEOUT, session=sess)

    logger.debug(f"Processing Plex item-key={item_key}")

    data = plex.query(f"{item_key}/tree")

    for media_part in data.findall('.//MediaPart'):
        if 'hash' in media_part.attrib:
            # Filter Processing by HDD Path
            # if len(sys.argv) > 1:
            #     if sys.argv[1] not in media_part.attrib['file']:
            #         logger.complete()
            #         return
            bundle_hash = media_part.attrib['hash']
            media_part_file = media_part.attrib['file']

            logger.debug(f"{bundle_hash=} {media_part_file=}")

            local_path = [value for key, value in path_mappings.items() if media_part_file.startswith(key)]
            if len(local_path) > 1:
                logger.error(f"More than one server paths matched, something is wrong with the local<->server mappings {local_path=}")

            server_path = [key for key, value in path_mappings.items() if value == local_path[0]]

            logger.debug(f"{server_path=} {local_path=}")

            media_file = sanitize_path(media_part_file.replace(server_path[0], local_path[0]))

            if not os.path.isfile(media_file):
                logger.error(f'Skipping as file not found {media_file}')
                continue

            try:
                bundle_file = sanitize_path(f"{bundle_hash[0]}/{bundle_hash[1::1]}.bundle")
            except Exception as e:
                logger.error(f"Error generating bundle_file for {media_file} due to {type(e).__name__}:{str(e)}")
                continue

            bundle_path = sanitize_path(os.path.join(PLEX_LOCAL_MEDIA_PATH, 'localhost', bundle_file))
            indexes_path = sanitize_path(os.path.join(bundle_path, 'Contents', 'Indexes'))
            index_bif = sanitize_path(os.path.join(indexes_path, 'index-sd.bif'))
            tmp_path = sanitize_path(os.path.join(TMP_FOLDER, bundle_hash))

            if not os.path.isfile(index_bif) or FORCE_REGENERATION_OF_BIF_FILES:
                logger.debug(f"Generating bundle_file for {media_file} at {index_bif}")

                if not os.path.isdir(indexes_path):
                    try:
                        os.makedirs(indexes_path)
                    except OSError as e:
                        logger.error(f"Error generating images for {media_file}. `{type(e).__name__}:{str(e)}` error when creating index path {indexes_path}")
                        continue

                try:
                    if not os.path.isdir(tmp_path):
                        os.makedirs(tmp_path)
                except OSError as e:
                    logger.error(f"Error generating images for {media_file}. `{type(e).__name__}:{str(e)}` error when creating tmp path {tmp_path}")
                    continue

                try:
                    results_gen_imgs = generate_images(media_file, tmp_path, gpu)
                except Exception as e:
                    logger.error(f"Error generating images for {media_file}. `{type(e).__name__}: {str(e)}` error when generating images.")
                    # logger.error(f"{traceback.print_exception(e)}")
                    if os.path.exists(tmp_path):
                        shutil.rmtree(tmp_path)
                    continue

                try:
                    bif_filesize = generate_bif(index_bif, tmp_path)
                except Exception as e:
                    # Remove bif, as it prob failed to generate
                    if os.path.exists(index_bif):
                        os.remove(index_bif)
                    logger.error(f"Error generating images for {media_file}. `{type(e).__name__}:{str(e)}` error when generating bif")
                    continue
                finally:
                    if os.path.exists(tmp_path):
                        shutil.rmtree(tmp_path)

                logger.info((
                    f"Generated Video Preview SIZE={sizeof_fmt(bif_filesize, precision=2):>9}"
                    f" HW={results_gen_imgs['hw']!r:<5}"
                    f" {'HDR' if results_gen_imgs['hdr'] else 'SDR'}"
                    f" TIME={results_gen_imgs['seconds']:>6} seconds"
                    f" SPEED={(str(results_gen_imgs['speed']) + 'x'):>6}"
                    f" FPS={results_gen_imgs['fps']:>6}"
                    f" for {media_file}"
                ))
            else:
                logger.debug(f"Not generating bundle_file for {media_file} at {index_bif} as it already exists!")

    logger.complete()

def run(gpu, path_mappings):
    # Ignore SSL Errors
    sess = requests.Session()
    sess.verify = False

    plex = PlexServer(PLEX_URL, PLEX_TOKEN, session=sess, timeout = 60)

    for section in plex.library.sections():
        if section.title not in PLEX_LIBRARIES_TO_PROCESS:
            logger.info(f"Skipping library {section.title} as not in list of libraries to process {PLEX_LIBRARIES_TO_PROCESS}")
            continue

        if section.type not in PLEX_MEDIA_TYPES_TO_PROCESS:
            logger.info(f"Skipping library {section.title} as not in list of media types to process {PLEX_MEDIA_TYPES_TO_PROCESS}")
            continue

        logger.info(f"Getting the media files from library \'{section.title}\'")

        # ['movie', 'show', 'artist', 'photo']
        if section.type == 'show':
            if cli_args.search:
                # this returns show(s) that match the title search string, not all the episodes of the show.
                # process_item() fetches the XML tree of the show, and the episodes are then iterated.
                # This impacts the progress bar, and ProcessPoolExecuter()
                # @TODO add enhanced Plex filter searching
                if cli_args.episode_title:
                    media = [m.key for m in section.search(title = cli_args.search, libtype = 'episode')]   # Episode Title search
                else:
                    media = [m.key for m in section.search(title = cli_args.search)]                      # Show Title search

            else:
                media = [m.key for m in section.search(libtype = 'episode')]
        elif section.type == 'movie':
            if cli_args.search:
                media = [m.key for m in section.search(title = cli_args.search)]
            else:
                media = [m.key for m in section.search()]
        else:
            logger.info(f"Skipping library {section.title} as \'{section.type}\' is unsupported")
            continue

        logger.info(f"Got {len(media)} media files for library {section.title}")

        if len(media) == 0:
            continue

        with Progress(SpinnerColumn(), *Progress.get_default_columns(), MofNCompleteColumn(), console=console) as progress:
            with ProcessPoolExecutor(initializer=set_logger, initargs=(logger, ), max_workers=CPU_THREADS + GPU_THREADS) as process_pool:
                futures = [process_pool.submit(process_item, key, gpu, path_mappings) for key in media]
                for future in progress.track(futures):
                    future.result()

if __name__ == '__main__':

    setup_logging()
    plex_local_videos_path_mappings = parse_path_mappings()

    logger.info(f"⚠️NVIDIA GPUs only supported.")
    logger.debug("LOG_LEVELS" + str(list(logger._core.levels.keys()))) #pylint: disable=protected-access
    logger.info(f"PLEX_LIBRARIES_TO_PROCESS={PLEX_LIBRARIES_TO_PROCESS}")
    logger.info(f"PLEX_MEDIA_TYPES_TO_PROCESS={PLEX_MEDIA_TYPES_TO_PROCESS}")
    logger.info(f"PLEX_BIF_FRAME_INTERVAL={PLEX_BIF_FRAME_INTERVAL}")
    logger.info(f"THUMBNAIL_QUALITY={THUMBNAIL_QUALITY}")
    logger.info(f"FORCE_REGENERATION_OF_BIF_FILES={FORCE_REGENERATION_OF_BIF_FILES}")
    logger.info(f"USE_LIB_PLACEBO={USE_LIB_PLACEBO}")
    logger.info(f"RUN_PROCESS_AT_LOW_PRIORITY={RUN_PROCESS_AT_LOW_PRIORITY}")
    logger.info(f"LOG_LEVEL={LOG_LEVEL}")
    logger.info(f"LOG_FILES_RETENTION={LOG_FILES_RETENTION}")
    logger.debug("PLEX_LOCAL_VIDEOS_PATH_MAPPINGS = " + json.dumps(plex_local_videos_path_mappings, indent=4))
    if USE_LIB_PLACEBO:
        logger.info('Using libplacebo for HDR tone-mapping.')
    if FORCE_REGENERATION_OF_BIF_FILES:
        logger.warning('⚠️Force regeneration of BIF files is enabled, this will regenerate *all* files!⚠️')
    if RUN_PROCESS_AT_LOW_PRIORITY:
        set_process_niceness()
        logger.info('Running processes at lower-priority')
    if cli_args.search:
        logger.info(f"🔸Searching for media titles matching {cli_args.search}🔸")
    if "show" in PLEX_MEDIA_TYPES_TO_PROCESS:
        if cli_args.episode_title:
            logger.info("🔸If media library contains shows, searching episode titles🔸")

    if not os.path.exists(PLEX_LOCAL_MEDIA_PATH):
        logger.error(f'{PLEX_LOCAL_MEDIA_PATH} does not exist, please edit PLEX_LOCAL_MEDIA_PATH environment variable')
        sys.exit(1)

    if not os.path.exists(os.path.join(PLEX_LOCAL_MEDIA_PATH, 'localhost')):
        logger.error(f'You set PLEX_LOCAL_MEDIA_PATH to "{PLEX_LOCAL_MEDIA_PATH}". There should be a folder called "localhost" in that directory but it does not exist which suggests you haven\'t mapped it correctly. Please fix the PLEX_LOCAL_MEDIA_PATH environment variable')
        sys.exit(1)

    if PLEX_URL == '':
        logger.error('Please set the PLEX_URL environment variable')
        sys.exit(1)

    if PLEX_TOKEN == '':
        logger.error('Please set the PLEX_TOKEN environment variable')
        sys.exit(1)

    # detect GPU's
    DETECTED_GPU = detect_gpu()
    if DETECTED_GPU == 'NVIDIA':
        logger.info('Found NVIDIA GPU')
    if not DETECTED_GPU:
        logger.warning('No NVIDIA GPUs detected. Defaulting to CPU ONLY.')

    try:
        # Clean TMP Folder
        if os.path.isdir(TMP_FOLDER):
            shutil.rmtree(TMP_FOLDER)
        os.makedirs(TMP_FOLDER)
        run(DETECTED_GPU, plex_local_videos_path_mappings)
    finally:
        if os.path.isdir(TMP_FOLDER):
            shutil.rmtree(TMP_FOLDER)
