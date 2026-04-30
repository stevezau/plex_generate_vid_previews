"""Configuration validation — the ``_validate_*`` helpers called from
:func:`config.load_config`.

Each validator checks one concern (Plex connection, processing params,
thread totals, on-disk paths) and either returns a bool or raises
:class:`ConfigValidationError` with an explanatory message. Keeping
them separate from :class:`Config` makes the rules auditable in
isolation and lets the web-UI validator on the Settings page reuse
them without dragging in the rest of the config loader.
"""

import os
from typing import Any

from loguru import logger

from ..utils import is_docker_environment


class ConfigValidationError(Exception):
    """Raised when configuration validation fails.

    Args:
        errors: List of human-readable validation error strings.

    """

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


VALID_TONEMAP_ALGORITHMS = ("reinhard", "mobius", "hable", "clip", "gamma", "linear")


def _validate_plex_config(
    plex_url: str,
    plex_token: str,
    plex_config_folder: str,
    missing_params: list,
    validation_errors: list,
) -> None:
    """Validate Plex server configuration parameters.

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
            missing_params.append("PLEX_URL is required (set PLEX_URL environment variable)")
        else:
            missing_params.append("PLEX_URL is required (configure via web UI or set PLEX_URL environment variable)")
    elif not plex_url.startswith(("http://", "https://")):
        validation_errors.append(f"PLEX_URL must start with http:// or https:// (got: {plex_url})")

    if not plex_token:
        if is_docker_environment():
            missing_params.append("PLEX_TOKEN is required (set PLEX_TOKEN environment variable)")
        else:
            missing_params.append(
                "PLEX_TOKEN is required (configure via web UI or set PLEX_TOKEN environment variable)"
            )

    # Check PLEX_CONFIG_FOLDER
    if not plex_config_folder or plex_config_folder == "/path_to/plex/Library/Application Support/Plex Media Server":
        if is_docker_environment():
            missing_params.append("PLEX_CONFIG_FOLDER is required (set PLEX_CONFIG_FOLDER environment variable)")
        else:
            missing_params.append(
                "PLEX_CONFIG_FOLDER is required (configure via web UI or set PLEX_CONFIG_FOLDER environment variable)"
            )
    else:
        # Path is provided, validate it
        if not os.path.exists(plex_config_folder):
            # Enhanced debugging for path issues
            debug_info = []
            debug_info.append(f"PLEX_CONFIG_FOLDER ({plex_config_folder}) does not exist")

            # Walk back to find existing parent directories
            current_path = plex_config_folder
            found_existing = False
            while current_path and current_path != "/" and current_path != os.path.dirname(current_path):
                parent_path = os.path.dirname(current_path)
                if os.path.exists(parent_path):
                    if not found_existing:
                        debug_info.append(
                            f"Checked folder path and found that up to this directory exists: {parent_path}"
                        )
                        found_existing = True
                    try:
                        contents = os.listdir(parent_path)
                        debug_info.append(f"Contents of {parent_path}:")
                        if contents:
                            for item in sorted(contents)[:10]:  # Show first 10 items
                                item_path = os.path.join(parent_path, item)
                                item_type = "DIR" if os.path.isdir(item_path) else "FILE"
                                debug_info.append(f"  {item_type}: {item}")
                            if len(contents) > 10:
                                debug_info.append(f"  ... and {len(contents) - 10} more items")
                        else:
                            debug_info.append("  (empty directory)")
                    except PermissionError:
                        debug_info.append(f"  Permission denied reading {parent_path}")
                    break
                current_path = parent_path

            if not found_existing:
                debug_info.append("Checked folder path but no parent directories exist")

            # Show current working directory and environment
            debug_info.append(f"Current working directory: {os.getcwd()}")
            debug_info.append(f"User: {os.getenv('USER', 'unknown')}")

            validation_errors.append("\n".join(debug_info))
        else:
            # Config folder exists, validate it contains Plex server structure
            try:
                config_contents = os.listdir(plex_config_folder)
                found_folders = [
                    item for item in config_contents if os.path.isdir(os.path.join(plex_config_folder, item))
                ]

                # Check for essential Plex server folders (only require Media)
                essential_folders = ["Media"]
                found_essential = [folder for folder in essential_folders if folder in found_folders]

                if len(found_essential) < len(essential_folders):  # Need all essential folders
                    debug_info = []
                    debug_info.append(
                        "PLEX_CONFIG_FOLDER exists but does not appear to be a valid Plex Media Server directory"
                    )
                    debug_info.append("Are you sure you mapped the right Plex folder?")
                    debug_info.append("Expected: Essential Plex folders (Media)")
                    debug_info.append(f"Found: {sorted(found_folders)}")
                    debug_info.append(f"Missing: {sorted([f for f in essential_folders if f not in found_folders])}")
                    debug_info.append("💡 Tip: Point to the main Plex directory:")
                    debug_info.append(
                        "   Linux: /var/lib/plexmediaserver/Library/Application Support/Plex Media Server"
                    )
                    debug_info.append("   Docker: /config/plex/Library/Application Support/Plex Media Server")
                    debug_info.append("   Windows: C:\\Users\\[Username]\\AppData\\Local\\Plex Media Server")
                    debug_info.append("   macOS: ~/Library/Application Support/Plex Media Server")
                    validation_errors.append("\n".join(debug_info))
                else:
                    # Config folder looks good, now check Media/localhost
                    media_path = os.path.join(plex_config_folder, "Media")
                    localhost_path = os.path.join(media_path, "localhost")

                    if not os.path.exists(media_path):
                        validation_errors.append(f"PLEX_CONFIG_FOLDER/Media directory does not exist: {media_path}")
                    elif not os.path.exists(localhost_path):
                        validation_errors.append(
                            f"PLEX_CONFIG_FOLDER/Media/localhost directory does not exist: {localhost_path}"
                        )
                    else:
                        # localhost folder exists, validate it contains Plex database structure
                        try:
                            localhost_contents = os.listdir(localhost_path)
                            found_localhost_folders = [
                                item for item in localhost_contents if os.path.isdir(os.path.join(localhost_path, item))
                            ]

                            # Check for either hex directories (0-f) or standard Plex folders
                            hex_folders = [
                                item for item in localhost_contents if len(item) == 1 and item in "0123456789abcdef"
                            ]
                            standard_folders = [
                                "Metadata",
                                "Cache",
                                "Plug-ins",
                                "Logs",
                                "Plug-in Support",
                            ]
                            found_standard = [
                                folder for folder in standard_folders if folder in found_localhost_folders
                            ]

                            # A single hex shard or one standard subfolder is enough
                            # to call this a real Plex Media/localhost dir. Fresh
                            # installs with one scanned library legitimately have
                            # only a handful of shards; the rest populate as
                            # previews are generated. The previous "need 10+"
                            # threshold spammed ERROR on every healthy fresh setup.
                            has_hex_structure = len(hex_folders) >= 1
                            has_standard_structure = len(found_standard) >= 1

                            if not has_hex_structure and not has_standard_structure:
                                # Downgraded from error to warning. A legitimately
                                # fresh Plex install has an empty Media/localhost
                                # until the first preview lands; the bundle adapter
                                # creates dirs on demand. Hard-failing here also
                                # blocked Emby/Jellyfin-only jobs on Plex+E/J
                                # installs (the validator runs on every load_config
                                # regardless of which servers the job targets).
                                logger.warning(
                                    "PLEX_CONFIG_FOLDER/Media/localhost is empty at {}. "
                                    "If you actually have a Plex install you've been using, that's "
                                    "suspicious — verify the path. If this is a fresh Plex (or you "
                                    "intend to scan Emby/Jellyfin libraries only), this is harmless: "
                                    "the bundle adapter will create dirs on demand.",
                                    localhost_path,
                                )
                        except PermissionError:
                            validation_errors.append(f"Permission denied reading localhost folder: {localhost_path}")
            except PermissionError:
                validation_errors.append(f"Permission denied reading PLEX_CONFIG_FOLDER: {plex_config_folder}")


def _validate_processing_config(
    plex_bif_frame_interval: int,
    thumbnail_quality: int,
    plex_timeout: int,
    tonemap_algorithm: str,
    validation_errors: list,
) -> None:
    """Validate processing configuration parameters.

    Args:
        plex_bif_frame_interval: Frame interval in seconds
        thumbnail_quality: Thumbnail quality (1-10)
        plex_timeout: Plex API timeout in seconds
        tonemap_algorithm: HDR-to-SDR tone mapping algorithm name
        validation_errors: List to append validation errors

    """
    if plex_bif_frame_interval < 1 or plex_bif_frame_interval > 60:
        validation_errors.append(
            f"PLEX_BIF_FRAME_INTERVAL must be between 1-60 seconds (got: {plex_bif_frame_interval})"
        )

    if thumbnail_quality < 1 or thumbnail_quality > 10:
        validation_errors.append(f"THUMBNAIL_QUALITY must be between 1-10 (got: {thumbnail_quality})")

    if plex_timeout < 10 or plex_timeout > 3600:
        validation_errors.append(f"PLEX_TIMEOUT must be between 10-3600 seconds (got: {plex_timeout})")

    if tonemap_algorithm not in VALID_TONEMAP_ALGORITHMS:
        validation_errors.append(
            f"TONEMAP_ALGORITHM must be one of {', '.join(VALID_TONEMAP_ALGORITHMS)} (got: {tonemap_algorithm})"
        )


def _validate_thread_config(
    gpu_threads: int,
    cpu_threads: int,
    ffmpeg_threads: int,
    validation_errors: list,
) -> tuple[bool, str]:
    """Validate thread configuration parameters.

    Args:
        gpu_threads: Total GPU worker threads (sum across enabled GPUs).
        cpu_threads: Number of CPU worker threads.
        ffmpeg_threads: Default CPU usage limit per FFmpeg process for
            GPU jobs (0 = no limit).
        validation_errors: List to append validation errors.

    Returns:
        Tuple of ``(no_workers, message)`` — ``True`` when both CPU and GPU
        totals are 0 (caller decides how to handle), ``False`` otherwise.

    """
    if gpu_threads < 0 or gpu_threads > 32:
        validation_errors.append(f"gpu_threads must be between 0-32 (got: {gpu_threads})")

    if cpu_threads < 0 or cpu_threads > 32:
        validation_errors.append(f"cpu_threads must be between 0-32 (got: {cpu_threads})")

    if ffmpeg_threads < 0 or ffmpeg_threads > 32:
        validation_errors.append(f"ffmpeg_threads must be between 0-32 (got: {ffmpeg_threads})")

    if cpu_threads == 0 and gpu_threads == 0:
        return (
            True,
            "Both cpu_threads and gpu_threads are 0.",
        )

    return False, ""


def thread_totals_from_ui_settings(ui_settings: dict[str, Any]) -> tuple[int, int]:
    """Compute ``gpu_threads`` and ``cpu_threads`` like ``load_config``.

    Uses the same precedence as ``load_config`` (settings.json, then env, then defaults).

    Args:
        ui_settings: Settings dict (e.g. merged ``settings.json`` content).

    Returns:
        Tuple of ``(gpu_threads, cpu_threads)``.

    """

    def get_value(settings_key, env_key, default, value_type=str):
        if settings_key in ui_settings and ui_settings[settings_key] not in (None, ""):
            val = ui_settings[settings_key]
            if value_type is bool:
                return bool(val)
            if value_type is int:
                try:
                    return int(val)
                except (ValueError, TypeError):
                    return default
            return str(val) if val else default

        env_value = os.environ.get(env_key, "")
        if env_value:
            if value_type is bool:
                return env_value.strip().lower() in ("true", "1", "yes")
            if value_type is int:
                try:
                    return int(env_value)
                except (ValueError, TypeError):
                    return default
            return env_value

        return default

    gpu_config = ui_settings.get("gpu_config", [])
    if isinstance(gpu_config, list):
        gpu_config = [entry for entry in gpu_config if isinstance(entry, dict) and entry.get("device")]
    else:
        gpu_config = []

    if gpu_config:
        gpu_threads = sum(entry.get("workers", 0) for entry in gpu_config if entry.get("enabled", True))
    else:
        gpu_threads = get_value("gpu_threads", "GPU_THREADS", 1, int)

    cpu_threads = get_value("cpu_threads", "CPU_THREADS", 1, int)
    return gpu_threads, cpu_threads


def validate_processing_thread_totals(ui_settings: dict[str, Any]) -> tuple[bool, str]:
    """Check whether settings have any processing workers configured.

    Args:
        ui_settings: Full or merged settings dict.

    Returns:
        ``(True, "")`` when at least one worker type is > 0, or
        ``(False, warning_message)`` when both CPU and GPU totals are 0.

    """
    gpu_t, cpu_t = thread_totals_from_ui_settings(ui_settings)
    if cpu_t == 0 and gpu_t == 0:
        return (
            False,
            "No workers configured — jobs will remain pending until GPU or CPU workers are added.",
        )
    return True, ""


def _validate_paths(tmp_folder: str, validation_errors: list) -> tuple[bool, bool]:
    """Validate path configuration and create tmp folder if needed.

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
            logger.debug("Created TMP_FOLDER: {}", tmp_folder)
        except OSError as e:
            validation_errors.append(f"Failed to create TMP_FOLDER ({tmp_folder}): {e}")
            return tmp_folder_created_by_us, False

    # Validate tmp_folder is writable
    if os.path.exists(tmp_folder) and not os.access(tmp_folder, os.W_OK):
        validation_errors.append(f"TMP_FOLDER ({tmp_folder}) is not writable")
        validation_errors.append(f"Please fix permissions: chmod 755 {tmp_folder}")

    # Check available disk space in tmp_folder
    if os.path.exists(tmp_folder):
        try:
            statvfs = os.statvfs(tmp_folder)
            free_space_gb = (statvfs.f_frsize * statvfs.f_bavail) / (1024**3)
            if free_space_gb < 1:  # Less than 1GB
                validation_errors.append(f"TMP_FOLDER has less than 1GB free space ({free_space_gb:.1f}GB available)")
        except (OSError, AttributeError):
            # AttributeError: os.statvfs doesn't exist on Windows
            # OSError: Cannot access the folder for other reasons
            logger.debug("Cannot check disk space for TMP_FOLDER ({}) - skipping disk space check", tmp_folder)

    return tmp_folder_created_by_us, True
