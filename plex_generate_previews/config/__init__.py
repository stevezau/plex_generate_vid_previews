"""Configuration management for Plex Video Preview Generator.

Handles environment variable loading, validation, and provides a centralized
configuration object for the entire application.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any

from dotenv import load_dotenv
from loguru import logger

from ..utils import is_docker_environment
from .paths import (  # noqa: F401
    _legacy_settings_to_path_mappings,
    expand_path_mapping_candidates,
    get_path_mapping_pairs,
    is_path_excluded,
    local_path_to_webhook_aliases,
    normalize_exclude_paths,
    normalize_path_mappings,
    path_to_canonical_local,
    plex_path_to_local,
    split_library_selectors,
)
from .validation import (  # noqa: F401
    VALID_TONEMAP_ALGORITHMS,
    ConfigValidationError,
    _validate_paths,
    _validate_plex_config,
    _validate_processing_config,
    _validate_thread_config,
    thread_totals_from_ui_settings,
    validate_processing_thread_totals,
)

# When both are installed we always prefer the Jellyfin build.
_JELLYFIN_FFMPEG_PATH = "/usr/lib/jellyfin-ffmpeg/ffmpeg"


def _resolve_ffmpeg_path() -> str | None:
    """Pick the FFmpeg binary to use.

    Returns the path to jellyfin-ffmpeg when installed and executable,
    otherwise whatever ``ffmpeg`` resolves to via ``PATH``.  ``None``
    means no working FFmpeg was found.
    """
    if os.path.isfile(_JELLYFIN_FFMPEG_PATH) and os.access(_JELLYFIN_FFMPEG_PATH, os.X_OK):
        return _JELLYFIN_FFMPEG_PATH
    return shutil.which("ffmpeg")


def get_config_value(cli_args, field_name: str, env_key: str, default, value_type: type = str):
    """Get configuration value with proper precedence: CLI args > env vars > defaults.

    Args:
        cli_args: CLI arguments object or None
        field_name: Name of the CLI argument field
        env_key: Environment variable key
        default: Default value if neither CLI nor env var is set
        value_type: Type to convert the value to (str, int, bool)

    Returns:
        The configuration value converted to the specified type

    """
    cli_value = None
    if cli_args is not None:
        try:
            cli_vars = vars(cli_args)
        except TypeError:
            cli_vars = {}
        if field_name in cli_vars:
            cli_value = cli_vars[field_name]
    if cli_value is not None:
        return cli_value

    env_value = os.environ.get(env_key, "")

    # Handle boolean conversion specially
    if value_type is bool:
        if env_value.strip().lower() in ("true", "1", "yes"):
            return True
        elif env_value.strip().lower() in ("false", "0", "no"):
            return False
        return default

    # Handle other types
    if not env_value:
        return default

    try:
        return value_type(env_value)
    except (ValueError, TypeError):
        return default


def get_config_value_str(cli_args, field_name: str, env_key: str, default: str = "") -> str:
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
    plex_verify_ssl: bool
    plex_libraries: list[str]

    # Media paths
    plex_config_folder: str
    plex_local_videos_path_mapping: str
    plex_videos_path_mapping: str
    # Resolved path mapping rows: [{"plex_prefix", "local_prefix", "webhook_prefixes"}]
    path_mappings: list[dict[str, Any]]

    # Processing configuration
    plex_bif_frame_interval: int
    thumbnail_quality: int
    regenerate_thumbnails: bool
    sort_by: str | None

    # Threading configuration
    gpu_threads: int
    cpu_threads: int
    ffmpeg_threads: int

    # System paths
    tmp_folder: str
    tmp_folder_created_by_us: bool
    ffmpeg_path: str

    # Logging
    log_level: str

    # HDR-to-SDR tone mapping algorithm for the zscale/tonemap filter path.
    # Valid values: reinhard, mobius, hable, clip, gamma, linear
    tonemap_algorithm: str = "hable"

    # Per-GPU configuration: list of dicts with keys
    # device, name, type, enabled, workers, ffmpeg_threads
    gpu_config: list[dict[str, Any]] = field(default_factory=list)

    # Runtime state (set after construction)
    working_tmp_folder: str = ""

    # Internal constants
    worker_pool_timeout: int = 30

    # When set, filter libraries by Plex section key (ID) instead of plex_libraries (names)
    plex_library_ids: list[str] | None = None

    # Runtime-only file targets for webhook-triggered single-file processing.
    webhook_paths: list[str] | None = None
    # Exclude paths: list of {"value": str, "type": "path"|"regex"}; path = prefix match, regex = full match
    exclude_paths: list[dict[str, str]] | None = None

    def __repr__(self) -> str:
        """Return a string representation with plex_token redacted."""
        fields = []
        for f in self.__dataclass_fields__:
            val = getattr(self, f)
            if f == "plex_token" and val:
                val = "***REDACTED***"
            fields.append(f"{f}={val!r}")
        return f"Config({', '.join(fields)})"


def show_docker_help():
    """Show help message pointing users to the web UI for configuration."""
    logger.info("🐳 Docker Environment Detected")
    logger.info("=" * 80)
    logger.info("")
    logger.info("Configuration is managed through the web UI (Settings page).")
    logger.info("Open http://<host>:<port> in your browser to get started.")
    logger.info("")
    logger.info("📋 One-time seed environment variables (applied on first startup):")
    logger.info("")
    logger.info("  PLEX_URL                    Plex server URL (e.g., http://localhost:32400)")
    logger.info("  PLEX_TOKEN                  Plex authentication token")
    logger.info("  PLEX_CONFIG_FOLDER          Path to Plex config folder")
    logger.info("  PLEX_TIMEOUT                Plex API timeout in seconds (default: 60)")
    logger.info("  PLEX_LIBRARIES              Comma-separated library names")
    logger.info("  LOG_LEVEL                   Logging level: DEBUG, INFO, WARNING, ERROR")
    logger.info("")
    logger.info("  These are migrated into settings.json on first run and")
    logger.info("  ignored afterwards. Use the web UI to change them.")
    logger.info("")
    logger.info("📋 Infrastructure environment variables (always active):")
    logger.info("")
    logger.info("  CONFIG_DIR                  Config directory (default: /config)")
    logger.info("  WEB_PORT                    Web UI port (default: 8080)")
    logger.info("  PUID / PGID                 Run-as user/group IDs")
    logger.info("  TZ                          Timezone")
    logger.info("")


# Cache for get_cached_config(); invalidated when settings.json mtime changes or clear_config_cache() is called.
_cached_config: "Config | None" = None
_cached_config_mtime: float | None = None


def get_cached_config():
    """Return config, using cache if settings.json has not changed since last load.

    Use this for read-only config access (e.g. API that returns current config).
    Full validation (FFmpeg, Plex, paths) runs only on cache miss or when
    settings file is modified. Call clear_config_cache() when settings are saved.

    Returns:
        Config or None: Same as load_config().

    """
    global _cached_config, _cached_config_mtime
    settings_path = os.path.join(os.environ.get("CONFIG_DIR", "/config"), "settings.json")
    mtime = os.path.getmtime(settings_path) if os.path.exists(settings_path) else 0.0
    if _cached_config is not None and _cached_config_mtime == mtime:
        return _cached_config
    try:
        config = load_config()
    except ConfigValidationError:
        return None
    if config is not None:
        _cached_config = config
        _cached_config_mtime = mtime
    return config


def clear_config_cache() -> None:
    """Invalidate config cache so next get_cached_config() runs full load_config()."""
    global _cached_config, _cached_config_mtime
    _cached_config = None
    _cached_config_mtime = None


def load_config() -> Config:
    """Load and validate configuration from settings.json and environment variables.

    settings.json is the primary source. Environment variables act as
    fallbacks (for backward compat / first-run before migration).

    Returns:
        Validated configuration object.

    Raises:
        ConfigValidationError: If configuration validation fails (e.g. missing
            required params, invalid ranges).  Note: both CPU and GPU workers
            being 0 is *not* treated as an error — a warning is logged and a
            valid Config is returned so the web UI remains usable.

    """
    # Load .env file so environment variables are available
    load_dotenv()
    # Try to load settings from settings.json (UI-configured settings)
    ui_settings = {}
    try:
        from ..web.settings_manager import get_settings_manager

        settings_manager = get_settings_manager()
        ui_settings = settings_manager.get_all()
        if ui_settings:
            logger.debug(f"Loaded {len(ui_settings)} settings from settings.json")
    except Exception as e:
        logger.debug(f"Could not load settings.json: {e}")

    def get_value(settings_key, env_key, default, value_type=str):
        """Get config value from settings.json, falling back to env then default."""
        if settings_key in ui_settings and ui_settings[settings_key] not in (None, ""):
            val = ui_settings[settings_key]
            if value_type is bool:
                return bool(val)
            elif value_type is int:
                try:
                    return int(val)
                except (ValueError, TypeError):
                    return default
            else:
                return str(val) if val else default

        env_value = os.environ.get(env_key, "")
        if env_value:
            if value_type is bool:
                return env_value.strip().lower() in ("true", "1", "yes")
            elif value_type is int:
                try:
                    return int(env_value)
                except (ValueError, TypeError):
                    return default
            return env_value

        return default

    # Load configuration: settings.json > env vars > defaults
    plex_url = get_value("plex_url", "PLEX_URL", "", str)
    plex_token = get_value("plex_token", "PLEX_TOKEN", "", str)
    plex_timeout = get_value("plex_timeout", "PLEX_TIMEOUT", 60, int)
    plex_verify_ssl = get_value("plex_verify_ssl", "PLEX_VERIFY_SSL", True, bool)

    # Handle plex_libraries (comma-separated string OR list from settings.json)
    plex_libraries_setting = ui_settings.get("selected_libraries", [])
    plex_library_ids: list[str] | None = None
    if isinstance(plex_libraries_setting, list) and plex_libraries_setting:
        selected_ids, selected_titles = split_library_selectors(plex_libraries_setting)
        plex_libraries = selected_titles
        plex_library_ids = selected_ids or None
    else:
        plex_libraries_raw = get_value("selected_libraries", "PLEX_LIBRARIES", "", str)
        plex_libraries = [library.strip().lower() for library in plex_libraries_raw.split(",") if library.strip()]

    plex_config_folder = get_value(
        "plex_config_folder",
        "PLEX_CONFIG_FOLDER",
        "/path_to/plex/Library/Application Support/Plex Media Server",
        str,
    )
    plex_local_videos_path_mapping = get_value(
        "plex_local_videos_path_mapping",
        "PLEX_LOCAL_VIDEOS_PATH_MAPPING",
        "",
        str,
    )
    plex_videos_path_mapping = get_value(
        "plex_videos_path_mapping",
        "PLEX_VIDEOS_PATH_MAPPING",
        "",
        str,
    )

    path_mappings = normalize_path_mappings(ui_settings)
    if not path_mappings and (plex_videos_path_mapping or plex_local_videos_path_mapping):
        path_mappings = _legacy_settings_to_path_mappings(plex_videos_path_mapping, plex_local_videos_path_mapping)
    exclude_paths = normalize_exclude_paths(ui_settings.get("exclude_paths"))

    plex_bif_frame_interval = get_value("thumbnail_interval", "PLEX_BIF_FRAME_INTERVAL", 5, int)
    thumbnail_quality = get_value("thumbnail_quality", "THUMBNAIL_QUALITY", 4, int)
    tonemap_algorithm = get_value("tonemap_algorithm", "TONEMAP_ALGORITHM", "hable", str).strip().lower()
    regenerate_thumbnails = get_value("regenerate_thumbnails", "REGENERATE_THUMBNAILS", False, bool)

    sort_by_raw = get_value("sort_by", "SORT_BY", "newest", str)
    sort_by = sort_by_raw.strip().lower() if sort_by_raw else "newest"

    # Load per-GPU config from settings (populated by settings UI or migration)
    gpu_config = ui_settings.get("gpu_config", [])
    if isinstance(gpu_config, list):
        gpu_config = [entry for entry in gpu_config if isinstance(entry, dict) and entry.get("device")]
    else:
        gpu_config = []

    # Compute totals from per-GPU config
    if gpu_config:
        gpu_threads = sum(entry.get("workers", 0) for entry in gpu_config if entry.get("enabled", True))
        enabled_ffmpeg = [entry.get("ffmpeg_threads", 2) for entry in gpu_config if entry.get("enabled", True)]
        ffmpeg_threads = max(enabled_ffmpeg) if enabled_ffmpeg else 2
    else:
        gpu_threads = get_value("gpu_threads", "GPU_THREADS", 1, int)
        ffmpeg_threads = get_value("ffmpeg_threads", "FFMPEG_THREADS", 2, int)

    cpu_threads = get_value("cpu_threads", "CPU_THREADS", 1, int)

    tmp_folder = get_value("tmp_folder", "TMP_FOLDER", tempfile.gettempdir(), str)

    log_level = get_value("log_level", "LOG_LEVEL", "INFO", str).upper()

    # Initialize validation lists
    missing_params = []
    validation_errors = []

    # Validate log level
    valid_log_levels = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
    if log_level not in valid_log_levels:
        validation_errors.append(f"LOG_LEVEL must be one of {valid_log_levels} (got: {log_level})")

    # Validate sort_by
    valid_sort_by = ["newest", "oldest"]
    if sort_by is not None and sort_by not in valid_sort_by:
        validation_errors.append(f"SORT_BY must be one of {valid_sort_by} or empty (got: {sort_by})")

    ffmpeg_path = _resolve_ffmpeg_path()
    if not ffmpeg_path:
        logger.error("FFmpeg not found. FFmpeg must be installed and available in PATH.")
        sys.exit(1)

    # Test FFmpeg actually works and log its version
    logger.debug(f"FFmpeg path: {ffmpeg_path}")
    try:
        _ffmpeg_start = time.monotonic()
        result = subprocess.run(
            [ffmpeg_path, "-version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        _ffmpeg_elapsed = time.monotonic() - _ffmpeg_start
        if result.returncode != 0:
            logger.error(f"FFmpeg exited with code {result.returncode} after {_ffmpeg_elapsed:.1f}s")
            if result.stderr:
                logger.error(f"FFmpeg stderr: {result.stderr.strip()[:500]}")
            if result.stdout:
                logger.debug(f"FFmpeg stdout: {result.stdout.strip()[:500]}")
            validation_errors.append("FFmpeg found but not working properly")
        else:
            version_line = result.stdout.split("\n")[0].strip() if result.stdout else "unknown"
            logger.debug(f"FFmpeg: {version_line} (checked in {_ffmpeg_elapsed:.1f}s)")
    except subprocess.TimeoutExpired:
        logger.error(f"FFmpeg version check timed out after 30s (path: {ffmpeg_path})")
        validation_errors.append("FFmpeg found but cannot execute properly")
    except OSError as exc:
        logger.error(f"FFmpeg version check failed with OS error: {exc} (path: {ffmpeg_path})")
        validation_errors.append("FFmpeg found but cannot execute properly")

    # Validate configuration using helper functions
    _validate_plex_config(plex_url, plex_token, plex_config_folder, missing_params, validation_errors)
    _validate_processing_config(
        plex_bif_frame_interval,
        thumbnail_quality,
        plex_timeout,
        tonemap_algorithm,
        validation_errors,
    )
    no_workers, _thread_note = _validate_thread_config(
        gpu_threads,
        cpu_threads,
        ffmpeg_threads,
        validation_errors,
    )
    tmp_folder_created_by_us, _ = _validate_paths(tmp_folder, validation_errors)

    # Handle missing parameters (show help)
    if missing_params:
        logger.error("❌ Configuration Error: Missing required parameters:")
        for i, error_msg in enumerate(missing_params, 1):
            logger.error(f"   {i}. {error_msg}")
        logger.info("")

        if is_docker_environment():
            show_docker_help()
        else:
            logger.info(
                "💡 Open the web UI at http://localhost:8080 and complete the setup wizard to configure these settings."
            )

        raise ConfigValidationError(missing_params)

    # Handle validation errors (standard error messages)
    if validation_errors:
        logger.error("❌ Configuration Error:")
        for i, error_msg in enumerate(validation_errors, 1):
            logger.error(f"   {i}. {error_msg}")
        raise ConfigValidationError(validation_errors)

    # Both CPU and GPU workers are 0 — not fatal; jobs will stay pending until the
    # user adds workers.  Log a visible warning so it's obvious in the container log.
    if no_workers:
        logger.warning(
            "⚠️  Both cpu_threads and gpu_threads are 0 — jobs will remain pending until workers are configured."
        )
        logger.info("💡 Open the Settings page in the web UI to add GPU or CPU workers.")

    config = Config(
        plex_url=plex_url,
        plex_token=plex_token,
        plex_timeout=plex_timeout,
        plex_verify_ssl=plex_verify_ssl,
        plex_libraries=plex_libraries,
        plex_config_folder=plex_config_folder,
        plex_local_videos_path_mapping=plex_local_videos_path_mapping,
        plex_videos_path_mapping=plex_videos_path_mapping,
        path_mappings=path_mappings,
        exclude_paths=exclude_paths,
        plex_bif_frame_interval=plex_bif_frame_interval,
        thumbnail_quality=thumbnail_quality,
        tonemap_algorithm=tonemap_algorithm,
        regenerate_thumbnails=regenerate_thumbnails,
        sort_by=sort_by,
        gpu_threads=gpu_threads,
        cpu_threads=cpu_threads,
        ffmpeg_threads=ffmpeg_threads,
        gpu_config=gpu_config,
        tmp_folder=tmp_folder,
        tmp_folder_created_by_us=tmp_folder_created_by_us,
        ffmpeg_path=ffmpeg_path,
        log_level=log_level,
        plex_library_ids=plex_library_ids,
    )

    # Set the timeout envvar for https://github.com/pkkid/python-plexapi
    os.environ["PLEXAPI_TIMEOUT"] = str(config.plex_timeout)

    # Output debug information
    logger.debug(f"PLEX_URL = {config.plex_url}")
    logger.debug(f"PLEX_TOKEN = {'*' * 10}...{'*' * 10}")  # Mask token for security
    logger.debug(f"PLEX_BIF_FRAME_INTERVAL = {config.plex_bif_frame_interval}")
    logger.debug(f"THUMBNAIL_QUALITY = {config.thumbnail_quality}")
    logger.debug(f"TONEMAP_ALGORITHM = {config.tonemap_algorithm}")
    logger.debug(f"PLEX_CONFIG_FOLDER = {config.plex_config_folder}")
    logger.debug(f"TMP_FOLDER = {config.tmp_folder}")
    logger.debug(f"PLEX_TIMEOUT = {config.plex_timeout}")
    logger.debug(f"PLEX_VERIFY_SSL = {config.plex_verify_ssl}")
    logger.debug(f"PLEX_LOCAL_VIDEOS_PATH_MAPPING = {config.plex_local_videos_path_mapping}")
    logger.debug(f"PLEX_VIDEOS_PATH_MAPPING = {config.plex_videos_path_mapping}")
    logger.debug(f"path_mappings = {len(config.path_mappings)} row(s)")
    logger.debug(f"gpu_threads = {config.gpu_threads}")
    logger.debug(f"cpu_threads = {config.cpu_threads}")
    logger.debug(f"ffmpeg_threads = {config.ffmpeg_threads}")
    logger.debug(f"gpu_config = {len(config.gpu_config)} GPU(s) configured")
    logger.debug(f"regenerate_thumbnails = {config.regenerate_thumbnails}")
    logger.debug(f"sort_by = {config.sort_by}")

    return config
