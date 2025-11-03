"""
Configuration management for Plex Video Preview Generator.

Handles environment variable loading, validation, and provides a centralized
configuration object for the entire application.
"""

import os
import sys
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from typing import List, Optional
from dotenv import load_dotenv
from loguru import logger

from .utils import is_docker_environment

# Load environment variables from .env file
load_dotenv()

# Set default ROCM_PATH if not already set to prevent KeyError in AMD SMI
if 'ROCM_PATH' not in os.environ:
    os.environ['ROCM_PATH'] = '/opt/rocm'


def get_config_value(cli_args, field_name: str, env_key: str, default, value_type: type = str):
    """
    Get configuration value with proper precedence: CLI args > env vars > defaults.
    
    Args:
        cli_args: CLI arguments object or None
        field_name: Name of the CLI argument field
        env_key: Environment variable key
        default: Default value if neither CLI nor env var is set
        value_type: Type to convert the value to (str, int, bool)
        
    Returns:
        The configuration value converted to the specified type
    """
    cli_value = getattr(cli_args, field_name, None) if cli_args else None
    if cli_value is not None:
        return cli_value
    
    env_value = os.environ.get(env_key, '')
    
    # Handle boolean conversion specially
    if value_type == bool:
        if env_value.strip().lower() in ('true', '1', 'yes'):
            return True
        elif env_value.strip().lower() in ('false', '0', 'no'):
            return False
        return default
    
    # Handle other types
    if not env_value:
        return default
    
    try:
        return value_type(env_value)
    except (ValueError, TypeError):
        return default


def get_config_value_str(cli_args, field_name: str, env_key: str, default: str = '') -> str:
    """Get string configuration value."""
    return get_config_value(cli_args, field_name, env_key, default, str)


def get_config_value_int(cli_args, field_name: str, env_key: str, default: int = 0) -> int:
    """Get integer configuration value."""
    return get_config_value(cli_args, field_name, env_key, default, int)


def get_config_value_bool(cli_args, field_name: str, env_key: str, default: bool = False) -> bool:
    """Get boolean configuration value."""
    return get_config_value(cli_args, field_name, env_key, default, bool)


@dataclass
class Config:
    """Configuration object containing all application settings."""
    
    # Plex server configuration
    plex_url: str
    plex_token: str
    plex_timeout: int
    plex_libraries: List[str]
    
    # Media paths
    plex_config_folder: str
    plex_local_videos_path_mapping: str
    plex_videos_path_mapping: str
    
    # Processing configuration
    plex_bif_frame_interval: int
    thumbnail_quality: int
    regenerate_thumbnails: bool
    
    # Threading configuration
    gpu_threads: int
    cpu_threads: int
    gpu_selection: str
    
    # System paths
    tmp_folder: str
    tmp_folder_created_by_us: bool
    ffmpeg_path: str
    
    # Logging
    log_level: str
    
    # Internal constants
    worker_pool_timeout: int = 30


def show_docker_help():
    """Show Docker-optimized help message with environment variables prominently displayed."""
    logger.info('🐳 Docker Environment Detected - Configuration via Environment Variables or CLI Arguments')
    logger.info('=' * 80)
    logger.info('')
    logger.info('📋 Required Environment Variables:')
    logger.info('')
    logger.info('  PLEX_URL                    Plex server URL (e.g., http://localhost:32400)')
    logger.info('  PLEX_TOKEN                  Plex authentication token')
    logger.info('  PLEX_CONFIG_FOLDER          Path to Plex Media Server configuration folder')
    logger.info('')
    logger.info('📋 Optional Environment Variables:')
    logger.info('')
    logger.info('  PLEX_TIMEOUT                Plex API timeout in seconds (default: 60)')
    logger.info('  PLEX_LIBRARIES              Comma-separated library names (e.g., "Movies, TV Shows")')
    logger.info('  PLEX_LOCAL_VIDEOS_PATH_MAPPING  Local videos path mapping')
    logger.info('  PLEX_VIDEOS_PATH_MAPPING    Plex videos path mapping')
    logger.info('  PLEX_BIF_FRAME_INTERVAL     Interval between preview images in seconds (default: 5)')
    logger.info('  THUMBNAIL_QUALITY           Preview image quality 1-10 (default: 4)')
    logger.info('  REGENERATE_THUMBNAILS       Regenerate existing thumbnails (true/false, default: false)')
    logger.info('  GPU_THREADS                 Number of GPU worker threads (default: 1)')
    logger.info('  CPU_THREADS                 Number of CPU worker threads (default: 1)')
    logger.info('  GPU_SELECTION               GPU selection: "all" or comma-separated indices (default: all)')
    logger.info('  TMP_FOLDER                  Temporary folder for processing (default: system temp dir)')
    logger.info('  LOG_LEVEL                   Logging level: DEBUG, INFO, WARNING, ERROR (default: INFO)')
    logger.info('')
    logger.info('💡 Example Docker Run Command (using environment variables):')
    logger.info('')
    logger.info('  docker run -it --rm --runtime=nvidia \\')
    logger.info('    -e PLEX_URL="http://localhost:32400" \\')
    logger.info('    -e PLEX_TOKEN="your_token_here" \\')
    logger.info('    -e PLEX_CONFIG_FOLDER="/config/plex/Library/Application Support/Plex Media Server" \\')
    logger.info('    -e GPU_THREADS=1 \\')
    logger.info('    -e CPU_THREADS=1 \\')
    logger.info('    -v /path/to/plex/config:/config \\')
    logger.info('    -v /path/to/videos:/data \\')
    logger.info('    plex_generate_vid_previews:latest')
    logger.info('')
    logger.info('💡 Example Docker Run Command (using CLI arguments):')
    logger.info('')
    logger.info('  docker run -it --rm --runtime=nvidia \\')
    logger.info('    -v /path/to/plex/config:/config \\')
    logger.info('    -v /path/to/videos:/data \\')
    logger.info('    plex_generate_vid_previews:latest \\')
    logger.info('    --plex-url "http://localhost:32400" \\')
    logger.info('    --plex-token "your_token_here" \\')
    logger.info('    --plex-config-folder "/config/plex/Library/Application Support/Plex Media Server" \\')
    logger.info('    --gpu-threads 1 \\')
    logger.info('    --cpu-threads 1')
    logger.info('')
    logger.info('🔧 For more options, use: plex-generate-previews --help')


def _validate_plex_config(plex_url: str, plex_token: str, plex_config_folder: str, 
                          missing_params: list, validation_errors: list) -> None:
    """
    Validate Plex server configuration parameters.
    
    Args:
        plex_url: Plex server URL
        plex_token: Plex authentication token
        plex_config_folder: Path to Plex config folder
        missing_params: List to append missing parameter errors
        validation_errors: List to append validation errors
    """
    # Check basic required parameters first
    if not plex_url:
        if is_docker_environment():
            missing_params.append('PLEX_URL is required (set PLEX_URL environment variable)')
        else:
            missing_params.append('PLEX_URL is required (use --plex-url or set PLEX_URL environment variable)')
    elif not plex_url.startswith(('http://', 'https://')):
        validation_errors.append(f'PLEX_URL must start with http:// or https:// (got: {plex_url})')
    
    if not plex_token:
        if is_docker_environment():
            missing_params.append('PLEX_TOKEN is required (set PLEX_TOKEN environment variable)')
        else:
            missing_params.append('PLEX_TOKEN is required (use --plex-token or set PLEX_TOKEN environment variable)')
    
    # Check PLEX_CONFIG_FOLDER
    if not plex_config_folder or plex_config_folder == '/path_to/plex/Library/Application Support/Plex Media Server':
        if is_docker_environment():
            missing_params.append('PLEX_CONFIG_FOLDER is required (set PLEX_CONFIG_FOLDER environment variable)')
        else:
            missing_params.append('PLEX_CONFIG_FOLDER is required (use --plex-config-folder or set PLEX_CONFIG_FOLDER environment variable)')
    else:
        # Path is provided, validate it
        if not os.path.exists(plex_config_folder):
            # Enhanced debugging for path issues
            debug_info = []
            debug_info.append(f'PLEX_CONFIG_FOLDER ({plex_config_folder}) does not exist')
            
            # Walk back to find existing parent directories
            current_path = plex_config_folder
            found_existing = False
            while current_path and current_path != '/' and current_path != os.path.dirname(current_path):
                parent_path = os.path.dirname(current_path)
                if os.path.exists(parent_path):
                    if not found_existing:
                        debug_info.append(f'Checked folder path and found that up to this directory exists: {parent_path}')
                        found_existing = True
                    try:
                        contents = os.listdir(parent_path)
                        debug_info.append(f'Contents of {parent_path}:')
                        if contents:
                            for item in sorted(contents)[:10]:  # Show first 10 items
                                item_path = os.path.join(parent_path, item)
                                item_type = "DIR" if os.path.isdir(item_path) else "FILE"
                                debug_info.append(f'  {item_type}: {item}')
                            if len(contents) > 10:
                                debug_info.append(f'  ... and {len(contents) - 10} more items')
                        else:
                            debug_info.append('  (empty directory)')
                    except PermissionError:
                        debug_info.append(f'  Permission denied reading {parent_path}')
                    break
                current_path = parent_path
            
            if not found_existing:
                debug_info.append('Checked folder path but no parent directories exist')
            
            # Show current working directory and environment
            debug_info.append(f'Current working directory: {os.getcwd()}')
            debug_info.append(f'User: {os.getenv("USER", "unknown")}')
            
            validation_errors.append('\n'.join(debug_info))
        else:
            # Config folder exists, validate it contains Plex server structure
            try:
                config_contents = os.listdir(plex_config_folder)
                found_folders = [item for item in config_contents if os.path.isdir(os.path.join(plex_config_folder, item))]
                
                # Check for essential Plex server folders (only require Media)
                essential_folders = ['Media']
                found_essential = [folder for folder in essential_folders if folder in found_folders]
                
                if len(found_essential) < len(essential_folders):  # Need all essential folders
                    debug_info = []
                    debug_info.append(f'PLEX_CONFIG_FOLDER exists but does not appear to be a valid Plex Media Server directory')
                    debug_info.append(f'Are you sure you mapped the right Plex folder?')
                    debug_info.append(f'Expected: Essential Plex folders (Media)')
                    debug_info.append(f'Found: {sorted(found_folders)}')
                    debug_info.append(f'Missing: {sorted([f for f in essential_folders if f not in found_folders])}')
                    debug_info.append(f'💡 Tip: Point to the main Plex directory:')
                    debug_info.append(f'   Linux: /var/lib/plexmediaserver/Library/Application Support/Plex Media Server')
                    debug_info.append(f'   Docker: /config/plex/Library/Application Support/Plex Media Server')
                    debug_info.append(f'   Windows: C:\\Users\\[Username]\\AppData\\Local\\Plex Media Server')
                    debug_info.append(f'   macOS: ~/Library/Application Support/Plex Media Server')
                    validation_errors.append('\n'.join(debug_info))
                else:
                    # Config folder looks good, now check Media/localhost
                    media_path = os.path.join(plex_config_folder, 'Media')
                    localhost_path = os.path.join(media_path, 'localhost')
                    
                    if not os.path.exists(media_path):
                        validation_errors.append(f'PLEX_CONFIG_FOLDER/Media directory does not exist: {media_path}')
                    elif not os.path.exists(localhost_path):
                        validation_errors.append(f'PLEX_CONFIG_FOLDER/Media/localhost directory does not exist: {localhost_path}')
                    else:
                        # localhost folder exists, validate it contains Plex database structure
                        try:
                            localhost_contents = os.listdir(localhost_path)
                            found_localhost_folders = [item for item in localhost_contents if os.path.isdir(os.path.join(localhost_path, item))]
                            
                            # Check for either hex directories (0-f) or standard Plex folders
                            hex_folders = [item for item in localhost_contents if len(item) == 1 and item in '0123456789abcdef']
                            standard_folders = ['Metadata', 'Cache', 'Plug-ins', 'Logs', 'Plug-in Support']
                            found_standard = [folder for folder in standard_folders if folder in found_localhost_folders]
                            
                            # Accept if we have either hex directories OR standard folders
                            has_hex_structure = len(hex_folders) >= 10  # Most of 0-f
                            has_standard_structure = len(found_standard) >= 3  # At least 3 standard folders
                            
                            if not has_hex_structure and not has_standard_structure:
                                debug_info = []
                                debug_info.append(f'PLEX_CONFIG_FOLDER/Media/localhost exists but does not appear to be a valid Plex database')
                                debug_info.append(f'Expected: Either hex directories (0-f) OR standard Plex folders (Metadata, Cache, etc.)')
                                debug_info.append(f'Found: {sorted(found_localhost_folders)}')
                                if hex_folders:
                                    debug_info.append(f'Hex directories found: {len(hex_folders)}/16 (need 10+)')
                                if found_standard:
                                    debug_info.append(f'Standard folders found: {len(found_standard)}/5 (need 3+)')
                                debug_info.append(f'This suggests the path may not point to the correct Plex Media Server database location')
                                validation_errors.append('\n'.join(debug_info))
                        except PermissionError:
                            validation_errors.append(f'Permission denied reading localhost folder: {localhost_path}')
            except PermissionError:
                validation_errors.append(f'Permission denied reading PLEX_CONFIG_FOLDER: {plex_config_folder}')


def _validate_processing_config(plex_bif_frame_interval: int, thumbnail_quality: int, 
                                plex_timeout: int, validation_errors: list) -> None:
    """
    Validate processing configuration parameters.
    
    Args:
        plex_bif_frame_interval: Frame interval in seconds
        thumbnail_quality: Thumbnail quality (1-10)
        plex_timeout: Plex API timeout in seconds
        validation_errors: List to append validation errors
    """
    # Validate numeric ranges
    if plex_bif_frame_interval < 1 or plex_bif_frame_interval > 60:
        validation_errors.append(f'PLEX_BIF_FRAME_INTERVAL must be between 1-60 seconds (got: {plex_bif_frame_interval})')
    
    if thumbnail_quality < 1 or thumbnail_quality > 10:
        validation_errors.append(f'THUMBNAIL_QUALITY must be between 1-10 (got: {thumbnail_quality})')
    
    if plex_timeout < 10 or plex_timeout > 3600:
        validation_errors.append(f'PLEX_TIMEOUT must be between 10-3600 seconds (got: {plex_timeout})')


def _validate_thread_config(gpu_threads: int, cpu_threads: int, gpu_selection: str, 
                            validation_errors: list) -> tuple[bool, str]:
    """
    Validate thread configuration parameters.
    
    Args:
        gpu_threads: Number of GPU worker threads
        cpu_threads: Number of CPU worker threads
        gpu_selection: GPU selection string
        validation_errors: List to append validation errors
        
    Returns:
        tuple: (should_exit, error_message) - (True, message) if both threads are 0, (False, "") otherwise
    """
    # Validate thread counts
    if gpu_threads < 0 or gpu_threads > 32:
        validation_errors.append(f'GPU_THREADS must be between 0-32 (got: {gpu_threads})')
    
    if cpu_threads < 0 or cpu_threads > 32:
        validation_errors.append(f'CPU_THREADS must be between 0-32 (got: {cpu_threads})')
    
    # Validate gpu_selection format
    if gpu_selection.lower() != 'all':
        try:
            # Parse comma-separated GPU indices
            gpu_indices = [int(x.strip()) for x in gpu_selection.split(',') if x.strip()]
            if not gpu_indices:
                validation_errors.append(f'GPU_SELECTION must be "all" or comma-separated GPU indices (got: {gpu_selection})')
            elif any(idx < 0 for idx in gpu_indices):
                validation_errors.append(f'GPU_SELECTION indices must be non-negative (got: {gpu_selection})')
        except ValueError:
            validation_errors.append(f'GPU_SELECTION must be "all" or comma-separated integers (got: {gpu_selection})')
    
    # Check if both threads are 0 (this requires immediate exit, not just a validation error)
    if cpu_threads == 0 and gpu_threads == 0:
        return True, 'Both CPU_THREADS and GPU_THREADS are set to 0. At least one processing method must be enabled.'
    
    return False, ""


def _validate_paths(tmp_folder: str, validation_errors: list) -> tuple[bool, bool]:
    """
    Validate path configuration and create tmp folder if needed.
    
    Args:
        tmp_folder: Temporary folder path
        validation_errors: List to append validation errors
        
    Returns:
        tuple: (tmp_folder_created_by_us, success) - whether we created the folder and if validation passed
    """
    tmp_folder_created_by_us = False
    
    # Handle tmp_folder: create if missing
    if not os.path.exists(tmp_folder):
        # Create the directory
        try:
            os.makedirs(tmp_folder, exist_ok=True)
            tmp_folder_created_by_us = True
            logger.debug(f'Created TMP_FOLDER: {tmp_folder}')
        except OSError as e:
            validation_errors.append(f'Failed to create TMP_FOLDER ({tmp_folder}): {e}')
            return tmp_folder_created_by_us, False
    
    # Validate tmp_folder is writable
    if os.path.exists(tmp_folder) and not os.access(tmp_folder, os.W_OK):
        validation_errors.append(f'TMP_FOLDER ({tmp_folder}) is not writable')
        validation_errors.append(f'Please fix permissions: chmod 755 {tmp_folder}')
    
    # Check available disk space in tmp_folder
    if os.path.exists(tmp_folder):
        try:
            statvfs = os.statvfs(tmp_folder)
            free_space_gb = (statvfs.f_frsize * statvfs.f_bavail) / (1024**3)
            if free_space_gb < 1:  # Less than 1GB
                validation_errors.append(f'TMP_FOLDER has less than 1GB free space ({free_space_gb:.1f}GB available)')
        except (OSError, AttributeError):
            # AttributeError: os.statvfs doesn't exist on Windows
            # OSError: Cannot access the folder for other reasons
            logger.debug(f'Cannot check disk space for TMP_FOLDER ({tmp_folder}) - skipping disk space check')
    
    return tmp_folder_created_by_us, True


def load_config(cli_args=None) -> Config:
    """
    Load and validate configuration from CLI arguments and environment variables.
    CLI arguments take precedence over environment variables.
    
    Args:
        cli_args: Parsed CLI arguments or None
        
    Returns:
        Config: Validated configuration object
        
    Raises:
        SystemExit: If required configuration is missing or invalid
    """
    # Extract CLI values (None if not provided)
    if cli_args is None:
        cli_args = None  # Empty namespace
    
    # Load configuration with precedence: CLI args > env vars > defaults
    plex_url = get_config_value_str(cli_args, 'plex_url', 'PLEX_URL', '')
    plex_token = get_config_value_str(cli_args, 'plex_token', 'PLEX_TOKEN', '')
    plex_timeout = get_config_value_int(cli_args, 'plex_timeout', 'PLEX_TIMEOUT', 60)
    
    # Handle plex_libraries (special case for comma-separated values)
    plex_libraries = get_config_value_str(cli_args, 'plex_libraries', 'PLEX_LIBRARIES', '')
    plex_libraries = [library.strip().lower() for library in plex_libraries.split(',') if library.strip()]
    
    plex_config_folder = get_config_value_str(cli_args, 'plex_config_folder', 'PLEX_CONFIG_FOLDER', '/path_to/plex/Library/Application Support/Plex Media Server')
    plex_local_videos_path_mapping = get_config_value_str(cli_args, 'plex_local_videos_path_mapping', 'PLEX_LOCAL_VIDEOS_PATH_MAPPING', '')
    plex_videos_path_mapping = get_config_value_str(cli_args, 'plex_videos_path_mapping', 'PLEX_VIDEOS_PATH_MAPPING', '')
    
    plex_bif_frame_interval = get_config_value_int(cli_args, 'plex_bif_frame_interval', 'PLEX_BIF_FRAME_INTERVAL', 5)
    thumbnail_quality = get_config_value_int(cli_args, 'thumbnail_quality', 'THUMBNAIL_QUALITY', 4)
    regenerate_thumbnails = get_config_value_bool(cli_args, 'regenerate_thumbnails', 'REGENERATE_THUMBNAILS', False)
    
    gpu_threads = get_config_value_int(cli_args, 'gpu_threads', 'GPU_THREADS', 1)
    cpu_threads = get_config_value_int(cli_args, 'cpu_threads', 'CPU_THREADS', 1)
    gpu_selection = get_config_value_str(cli_args, 'gpu_selection', 'GPU_SELECTION', 'all')
    
    # Use system temp directory (respects TMPDIR, TEMP, TMP env vars)
    tmp_folder = get_config_value_str(cli_args, 'tmp_folder', 'TMP_FOLDER', tempfile.gettempdir())
    
    # Handle log_level (case insensitive)
    log_level = get_config_value_str(cli_args, 'log_level', 'LOG_LEVEL', 'INFO').upper()
    
    # Initialize validation lists
    missing_params = []
    validation_errors = []
    
    # Validate log level
    valid_log_levels = ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']
    if log_level not in valid_log_levels:
        validation_errors.append(f'LOG_LEVEL must be one of {valid_log_levels} (got: {log_level})')
    
    # Update logging level early so debug statements work
    if log_level in valid_log_levels:
        from .logging_config import setup_logging
        setup_logging(log_level)
    
    # Find FFmpeg path
    ffmpeg_path = shutil.which("ffmpeg")
    if not ffmpeg_path:
        logger.error('FFmpeg not found. FFmpeg must be installed and available in PATH.')
        sys.exit(1)
    
    # Test FFmpeg actually works
    try:
        result = subprocess.run([ffmpeg_path, '-version'], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=5)
        if result.returncode != 0:
            validation_errors.append('FFmpeg found but not working properly')
    except (subprocess.TimeoutExpired, FileNotFoundError):
        validation_errors.append('FFmpeg found but cannot execute properly')
    
    # Validate configuration using helper functions
    _validate_plex_config(plex_url, plex_token, plex_config_folder, missing_params, validation_errors)
    _validate_processing_config(plex_bif_frame_interval, thumbnail_quality, plex_timeout, validation_errors)
    should_exit, thread_error = _validate_thread_config(gpu_threads, cpu_threads, gpu_selection, validation_errors)
    tmp_folder_created_by_us, _ = _validate_paths(tmp_folder, validation_errors)
    
    # Handle missing parameters (show help)
    if missing_params:
        logger.error('❌ Configuration Error: Missing required parameters:')
        for i, error_msg in enumerate(missing_params, 1):
            logger.error(f'   {i}. {error_msg}')
        logger.info('')
        
        # Show Docker-optimized help if running in Docker, otherwise show CLI help
        if is_docker_environment():
            show_docker_help()
        else:
            logger.info('📋 Showing help for all available options:')
            logger.info('=' * 60)
            # Show help automatically
            sys.argv = [sys.argv[0], '--help']
            try:
                # Import locally to avoid circular imports
                from .cli import parse_arguments
                parse_arguments()
            except SystemExit:
                pass
        
        return None  # Return None to indicate validation failure
    
    # Handle validation errors (standard error messages)
    if validation_errors:
        logger.error('❌ Configuration Error:')
        for i, error_msg in enumerate(validation_errors, 1):
            logger.error(f'   {i}. {error_msg}')
        return None  # Return None to indicate validation failure
    
    # Check if both threads are 0 (requires immediate exit)
    if should_exit:
        logger.error('❌ Configuration Error: Both CPU_THREADS and GPU_THREADS are set to 0.')
        logger.error('📋 At least one processing method must be enabled.')
        logger.info('💡 Use --help to see all available options.')
        logger.info('💡 Example: plex-generate-previews --cpu-threads 4 --gpu-threads 2')
        sys.exit(1)
    
    config = Config(
        plex_url=plex_url,
        plex_token=plex_token,
        plex_timeout=plex_timeout,
        plex_libraries=plex_libraries,
        plex_config_folder=plex_config_folder,
        plex_local_videos_path_mapping=plex_local_videos_path_mapping,
        plex_videos_path_mapping=plex_videos_path_mapping,
        plex_bif_frame_interval=plex_bif_frame_interval,
        thumbnail_quality=thumbnail_quality,
        regenerate_thumbnails=regenerate_thumbnails,
        gpu_threads=gpu_threads,
        cpu_threads=cpu_threads,
        gpu_selection=gpu_selection,
        tmp_folder=tmp_folder,
        tmp_folder_created_by_us=tmp_folder_created_by_us,
        ffmpeg_path=ffmpeg_path,
        log_level=log_level
    )
    
    # Set the timeout envvar for https://github.com/pkkid/python-plexapi
    os.environ["PLEXAPI_TIMEOUT"] = str(config.plex_timeout)
    
    # Output debug information
    logger.debug(f'PLEX_URL = {config.plex_url}')
    logger.debug(f'PLEX_TOKEN = {"*" * 10}...{"*" * 10}')  # Mask token for security
    logger.debug(f'PLEX_BIF_FRAME_INTERVAL = {config.plex_bif_frame_interval}')
    logger.debug(f'THUMBNAIL_QUALITY = {config.thumbnail_quality}')
    logger.debug(f'PLEX_CONFIG_FOLDER = {config.plex_config_folder}')
    logger.debug(f'TMP_FOLDER = {config.tmp_folder}')
    logger.debug(f'PLEX_TIMEOUT = {config.plex_timeout}')
    logger.debug(f'PLEX_LOCAL_VIDEOS_PATH_MAPPING = {config.plex_local_videos_path_mapping}')
    logger.debug(f'PLEX_VIDEOS_PATH_MAPPING = {config.plex_videos_path_mapping}')
    logger.debug(f'GPU_THREADS = {config.gpu_threads}')
    logger.debug(f'CPU_THREADS = {config.cpu_threads}')
    logger.debug(f'GPU_SELECTION = {config.gpu_selection}')
    logger.debug(f'REGENERATE_THUMBNAILS = {config.regenerate_thumbnails}')
    
    return config
