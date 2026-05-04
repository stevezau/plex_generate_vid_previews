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
    # Per-path ``{server_id: item_id}`` hints supplied by vendor webhooks
    # (Plex / Emby / Jellyfin) — the payload already names the item, so the
    # orchestrator can skip Plex's path-to-item resolution and call
    # ``process_canonical_path`` directly with the hint. Keyed by canonical
    # path so a single job carrying multiple webhook_paths can carry a
    # different per-server hint per path. None = no hint, fall back to the
    # legacy Plex-resolves-then-fans-out flow.
    webhook_item_id_hints: dict[str, dict[str, str]] | None = None
    # Exclude paths: list of {"value": str, "type": "path"|"regex"}; path = prefix match, regex = full match
    exclude_paths: list[dict[str, str]] | None = None
    # When a job is pinned to one configured media-server (via the Schedules
    # picker, manual scan, or per-server webhook URL), the dispatcher drops
    # publishers from every other server. None = legacy "publish to every
    # owning server" behaviour.
    server_id_filter: str | None = None

    # Phase K2: human-readable name of the configured server this Config view
    # was derived from (set by ``derive_legacy_plex_view`` when called with
    # ``server_id=``). Log emitters in plex_client.py / orchestrator.py use it
    # to prefix lines as ``[<name>] ...`` so multi-server installs get clear
    # attribution. None when the Config wasn't built per-server (legacy
    # global view, setup wizard, etc.) — emitters fall back to the unprefixed
    # wording.
    server_display_name: str | None = None

    def __repr__(self) -> str:
        """Return a string representation with plex_token redacted."""
        fields = []
        for f in self.__dataclass_fields__:
            val = getattr(self, f)
            if f == "plex_token" and val:
                val = "***REDACTED***"
            fields.append(f"{f}={val!r}")
        return f"Config({', '.join(fields)})"

    # --------------------------------------------------------------- aliases
    # Vendor-neutral alias for the Plex-named ``plex_bif_frame_interval``
    # field. Phase G of the multi-server completion: the underlying value
    # is loaded from the ``thumbnail_interval`` settings key and applies
    # uniformly to every vendor (Plex BIF, Emby BIF, Jellyfin trickplay
    # sidecars), so the Plex-branded field name is misleading. New code
    # should use ``thumbnail_interval``; the legacy attribute keeps
    # working as the dataclass field of record so existing callers and
    # tests don't have to migrate atomically.
    @property
    def thumbnail_interval(self) -> int:
        """Frame interval (seconds) used by every output adapter."""
        return self.plex_bif_frame_interval

    @thumbnail_interval.setter
    def thumbnail_interval(self, value: int) -> None:
        self.plex_bif_frame_interval = int(value)


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


def derive_legacy_plex_view(media_servers: list, server_id: str | None = None) -> dict:
    """Project a Plex entry from ``media_servers`` into legacy ``plex_*`` keys.

    The new multi-server model stores Plex config inside ``media_servers[i]``
    where ``type == "plex"``. But every legacy consumer (plex_client,
    job_runner, recent_added_scanner, the Plex bundle output path) still
    reads flat ``plex_url`` / ``plex_token`` / ``selected_libraries`` /
    ``path_mappings`` / ``exclude_paths`` / ``plex_config_folder`` keys
    from settings.

    This function returns a dict that looks like the legacy flat view but
    is derived from the per-server config, so the read path can prefer it
    and the legacy globals become a fallback only.

    Args:
        media_servers: The persisted ``media_servers`` array.
        server_id: When set, project from that exact entry (used by job_runner
            when a job is pinned to a specific Plex server in a multi-Plex
            install). When None or unmatched, falls back to the first enabled
            Plex entry — the historical behaviour.

    Returns an empty dict when no matching Plex server is configured —
    callers should fall back to the legacy global keys in that case.
    """
    if not isinstance(media_servers, list):
        return {}

    def _is_enabled_plex(e):
        return isinstance(e, dict) and (e.get("type") or "").lower() == "plex" and e.get("enabled", True)

    plex_entry = None
    if server_id:
        plex_entry = next(
            (e for e in media_servers if _is_enabled_plex(e) and e.get("id") == server_id),
            None,
        )
    if plex_entry is None:
        plex_entry = next((e for e in media_servers if _is_enabled_plex(e)), None)
    if plex_entry is None:
        return {}

    auth = plex_entry.get("auth") or {}
    output = plex_entry.get("output") or {}
    libs_raw = plex_entry.get("libraries") or []
    selected_libraries: list[str] = []
    if isinstance(libs_raw, list):
        for lib in libs_raw:
            if not isinstance(lib, dict) or not lib.get("enabled", True):
                continue
            lib_id = lib.get("id") or lib.get("name")
            if lib_id:
                selected_libraries.append(str(lib_id))

    view: dict = {}
    if plex_entry.get("url"):
        view["plex_url"] = plex_entry["url"]
    token = auth.get("token") if isinstance(auth, dict) else None
    if token:
        view["plex_token"] = token
    if "verify_ssl" in plex_entry:
        view["plex_verify_ssl"] = bool(plex_entry["verify_ssl"])
    if "timeout" in plex_entry:
        try:
            view["plex_timeout"] = int(plex_entry["timeout"])
        except (TypeError, ValueError):
            pass
    if isinstance(output, dict) and output.get("plex_config_folder"):
        view["plex_config_folder"] = output["plex_config_folder"]
    if selected_libraries:
        view["selected_libraries"] = selected_libraries
    pm = plex_entry.get("path_mappings")
    if isinstance(pm, list) and pm:
        view["path_mappings"] = pm
    ep = plex_entry.get("exclude_paths")
    if isinstance(ep, list) and ep:
        view["exclude_paths"] = ep
    # K2: pass the human-readable display name so log emitters that consume
    # the derived Config can prefix messages as "[<name>] ...". Falls back to
    # the entry id when no name is set.
    display_name = (plex_entry.get("name") or plex_entry.get("id") or "").strip()
    if display_name:
        view["server_display_name"] = display_name
    return view


def load_config(*, log_validation_errors: bool = True) -> Config:
    """Load and validate configuration from settings.json and environment variables.

    settings.json is the primary source. Environment variables act as
    fallbacks (for backward compat / first-run before migration).

    Within settings.json, when a Plex entry exists in ``media_servers``,
    its fields take precedence over the legacy flat ``plex_*`` keys —
    that's the multi-server data path the rest of the app already uses.
    Legacy flat keys remain as a fallback so existing single-Plex
    installs keep working through the migration window.

    Args:
        log_validation_errors: When ``True`` (default), prints user-facing
            ❌ Configuration Error lines on missing/invalid settings before
            raising :class:`ConfigValidationError`. Best-effort callers
            (e.g. webhook router falling back to a minimal shim) should
            pass ``False`` so a non-Plex deployment doesn't spam ERROR
            lines on every webhook.

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
            logger.debug("Loaded {} settings from settings.json", len(ui_settings))
    except Exception as e:
        logger.debug("Could not load settings.json: {}", e)

    # Overlay the derived per-server Plex view on top of the legacy flat keys
    # so reads prefer media_servers[0] when it exists (without the legacy keys
    # being removed yet — they're still the back-compat fallback).
    plex_view = derive_legacy_plex_view(ui_settings.get("media_servers") or [])
    if plex_view:
        ui_settings = {**ui_settings, **plex_view}

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

    # Accept either the modern ``thumbnail_interval`` (vendor-neutral) or the
    # legacy ``plex_bif_frame_interval`` key. settings_manager and parts of
    # the bootstrap (run_app.py, migrations) write the legacy key; without
    # this fallback those values are silently ignored and the env-var/default
    # wins instead — which is exactly how a 30s clip ends up with 13 frames
    # in a "5s-interval" BIF when a stale .env sets PLEX_BIF_FRAME_INTERVAL=2.
    _interval_setting = ui_settings.get("thumbnail_interval")
    if _interval_setting in (None, ""):
        _interval_setting = ui_settings.get("plex_bif_frame_interval")
    if _interval_setting in (None, ""):
        plex_bif_frame_interval = get_value("__never_set__", "PLEX_BIF_FRAME_INTERVAL", 5, int)
    else:
        try:
            plex_bif_frame_interval = int(_interval_setting)
        except (TypeError, ValueError):
            plex_bif_frame_interval = get_value("__never_set__", "PLEX_BIF_FRAME_INTERVAL", 5, int)
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
    valid_sort_by = ["newest", "oldest", "random"]
    if sort_by is not None and sort_by not in valid_sort_by:
        validation_errors.append(f"SORT_BY must be one of {valid_sort_by} or empty (got: {sort_by})")

    ffmpeg_path = _resolve_ffmpeg_path()
    if not ffmpeg_path:
        logger.error(
            "FFmpeg is not installed (or not on the system PATH). This app cannot generate any previews without it — "
            "the process will exit now. If you're using Docker, the official image already includes FFmpeg; this error usually "
            "means a custom image is missing it. If you're running from source, install FFmpeg from your package manager "
            "(apt install ffmpeg / brew install ffmpeg) and restart."
        )
        sys.exit(1)

    # Test FFmpeg actually works and log its version
    logger.debug("FFmpeg path: {}", ffmpeg_path)
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
            logger.error(
                "FFmpeg is installed but failed to run (it returned an error after {:.1f}s). "
                "Without a working FFmpeg this app can't generate any previews. "
                "Try running 'ffmpeg -version' on the host or inside the container — if that also fails, your "
                "FFmpeg install is broken (re-install or rebuild the container). FFmpeg's own error output: {}",
                _ffmpeg_elapsed,
                (result.stderr or "").strip()[:500] or "(no error output)",
            )
            if result.stdout:
                logger.debug("FFmpeg stdout: {}", result.stdout.strip()[:500])
            validation_errors.append("FFmpeg found but not working properly")
        else:
            version_line = result.stdout.split("\n")[0].strip() if result.stdout else "unknown"
            logger.debug("FFmpeg: {} (checked in {:.1f}s)", version_line, _ffmpeg_elapsed)
    except subprocess.TimeoutExpired:
        logger.error(
            "FFmpeg didn't respond within 30 seconds when asked for its version (binary at {}). "
            "This usually means FFmpeg is hung or the binary is corrupt. The app cannot generate previews until "
            "this is fixed — try running 'ffmpeg -version' manually; if it also hangs, re-install FFmpeg or "
            "rebuild the container.",
            ffmpeg_path,
        )
        validation_errors.append("FFmpeg found but cannot execute properly")
    except OSError as exc:
        logger.error(
            "Could not run FFmpeg at {}: {}. This is usually a permission problem (the binary isn't executable) "
            "or a missing system library. The app cannot generate previews until FFmpeg works — fix the binary "
            "permissions (chmod +x) or rebuild the container.",
            ffmpeg_path,
            exc,
        )
        validation_errors.append("FFmpeg found but cannot execute properly")

    # Plex validation only applies when the user actually has a Plex server.
    # An Emby/Jellyfin-only install has no Plex entry in media_servers and would
    # otherwise be blocked at startup by missing PLEX_URL/PLEX_TOKEN/PLEX_CONFIG_FOLDER.
    raw_media_servers = ui_settings.get("media_servers") or []
    has_plex_server = any(
        isinstance(e, dict) and (e.get("type") or "").lower() == "plex" and e.get("enabled", True)
        for e in raw_media_servers
    )
    has_legacy_plex = bool(plex_url or plex_token)
    if has_plex_server or has_legacy_plex or not raw_media_servers:
        # Validate Plex when a Plex server exists, when the legacy globals are set,
        # or when there's no media_servers config at all (fresh install — Setup
        # Wizard is Plex-first so this is the safe default).
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
        if log_validation_errors:
            logger.error("❌ Configuration Error: Missing required parameters:")
            for i, error_msg in enumerate(missing_params, 1):
                logger.error("   {}. {}", i, error_msg)
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
        if log_validation_errors:
            logger.error("❌ Configuration Error:")
            for i, error_msg in enumerate(validation_errors, 1):
                logger.error("   {}. {}", i, error_msg)
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
        # server_display_name intentionally NOT set here — load_config()
        # always projects from media_servers[0] (no server_id param), so
        # surfacing that name on the returned Config would be misleading
        # for any job pinned to a non-first Plex server. job_runner sets
        # this field explicitly per-job after looking up the right entry.
        server_display_name=None,
    )

    # Set the timeout envvar for https://github.com/pkkid/python-plexapi
    os.environ["PLEXAPI_TIMEOUT"] = str(config.plex_timeout)

    # Output debug information
    logger.debug("PLEX_URL = {}", config.plex_url)
    logger.debug("PLEX_TOKEN = {}...{}", "*" * 10, "*" * 10)  # Mask token for security
    logger.debug("PLEX_BIF_FRAME_INTERVAL = {}", config.plex_bif_frame_interval)
    logger.debug("THUMBNAIL_QUALITY = {}", config.thumbnail_quality)
    logger.debug("TONEMAP_ALGORITHM = {}", config.tonemap_algorithm)
    logger.debug("PLEX_CONFIG_FOLDER = {}", config.plex_config_folder)
    logger.debug("TMP_FOLDER = {}", config.tmp_folder)
    logger.debug("PLEX_TIMEOUT = {}", config.plex_timeout)
    logger.debug("PLEX_VERIFY_SSL = {}", config.plex_verify_ssl)
    logger.debug("PLEX_LOCAL_VIDEOS_PATH_MAPPING = {}", config.plex_local_videos_path_mapping)
    logger.debug("PLEX_VIDEOS_PATH_MAPPING = {}", config.plex_videos_path_mapping)
    logger.debug("path_mappings = {} row(s)", len(config.path_mappings))
    logger.debug("gpu_threads = {}", config.gpu_threads)
    logger.debug("cpu_threads = {}", config.cpu_threads)
    logger.debug("ffmpeg_threads = {}", config.ffmpeg_threads)
    logger.debug("gpu_config = {} GPU(s) configured", len(config.gpu_config))
    logger.debug("regenerate_thumbnails = {}", config.regenerate_thumbnails)
    logger.debug("sort_by = {}", config.sort_by)

    return config
