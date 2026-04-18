"""Configuration management for Plex Video Preview Generator.

Handles environment variable loading, validation, and provides a centralized
configuration object for the entire application.
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any

from dotenv import load_dotenv
from loguru import logger

from .utils import is_docker_environment


class ConfigValidationError(Exception):
    """Raised when configuration validation fails.

    Args:
        errors: List of human-readable validation error strings.

    """

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


# Set default ROCM_PATH if not already set to prevent KeyError in AMD SMI
if "ROCM_PATH" not in os.environ:
    os.environ["ROCM_PATH"] = "/opt/rocm"


# Jellyfin-FFmpeg ships a patched tonemap_opencl that handles Dolby Vision
# Profile 5 RPU metadata correctly; upstream FFmpeg's tonemap_opencl treats
# the base layer as plain HDR10 and produces wrong colours on DV5 content.
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


def get_path_mapping_pairs(plex_mapping: str, local_mapping: str) -> list[tuple[str, str]]:
    """Parse path mapping config into (plex_root, local_root) pairs.

    Supports: (1) single pair when both are single values; (2) mergefs: multiple
    Plex roots (semicolon-separated) with one local path — all map to that path;
    (3) same count both sides — pair by index.

    Args:
        plex_mapping: Plex path(s), semicolon-separated for multiple.
        local_mapping: Local path(s), semicolon-separated or single.

    Returns:
        List of (plex_root, local_root) tuples to try in order.

    """
    plex_list = [s.strip() for s in (plex_mapping or "").split(";") if s.strip()]
    local_list = [s.strip() for s in (local_mapping or "").split(";") if s.strip()]
    if not plex_list or not local_list:
        return []
    if len(local_list) == 1:
        return [(plex_root, local_list[0]) for plex_root in plex_list]
    if len(plex_list) == len(local_list):
        return list(zip(plex_list, local_list, strict=True))
    # Mismatched lengths: use first of each (backward compat)
    return [(plex_list[0], local_list[0])]


# -----------------------------------------------------------------------------
# Path mappings (plex_prefix, local_prefix, optional webhook_prefixes)
# -----------------------------------------------------------------------------


def _normalize_prefix(p: str) -> str:
    """Return path with consistent trailing slash for prefix matching."""
    if not p:
        return p
    return p.replace("\\", "/").rstrip("/") or "/"


def _legacy_settings_to_path_mappings(plex_mapping: str, local_mapping: str) -> list[dict[str, Any]]:
    """Convert legacy semicolon pair config into path_mappings list."""
    pairs = get_path_mapping_pairs(plex_mapping or "", local_mapping or "")
    return [
        {"plex_prefix": plex_root, "local_prefix": local_root, "webhook_prefixes": []}
        for plex_root, local_root in pairs
    ]


def normalize_path_mappings(settings: dict[str, Any]) -> list[dict[str, Any]]:
    """Build path_mappings list from settings (new format or legacy).

    New format: settings["path_mappings"] is a list of dicts with keys
    plex_prefix, local_prefix, and optionally webhook_prefixes (list of strings).
    Legacy: settings has plex_videos_path_mapping and plex_local_videos_path_mapping
    (semicolon-separated); converted to mapping rows with empty webhook_prefixes.

    Args:
        settings: Dict from settings.json or equivalent (e.g. ui_settings).

    Returns:
        List of mapping dicts: {"plex_prefix", "local_prefix", "webhook_prefixes"}.

    """
    raw = settings.get("path_mappings")
    if isinstance(raw, list) and len(raw) > 0:
        out = []
        for row in raw:
            if not isinstance(row, dict):
                continue
            plex = (row.get("plex_prefix") or "").strip()
            local = (row.get("local_prefix") or "").strip()
            if not plex or not local:
                continue
            web = row.get("webhook_prefixes")
            if isinstance(web, list):
                web = [s.strip() for s in web if s and str(s).strip()]
            else:
                web = []
            out.append({"plex_prefix": plex, "local_prefix": local, "webhook_prefixes": web})
        if out:
            return out
    # Legacy
    plex_str = (settings.get("plex_videos_path_mapping") or "").strip()
    local_str = (settings.get("plex_local_videos_path_mapping") or "").strip()
    if plex_str and local_str:
        return _legacy_settings_to_path_mappings(plex_str, local_str)
    return []


def _path_matches_prefix(path: str, prefix: str) -> bool:
    """Return True if path equals prefix or has prefix as a path prefix (no partial segment)."""
    norm = _normalize_prefix(prefix)
    if not norm:
        return False
    path = (path or "").strip().replace("\\", "/")
    return path == norm or path.startswith(norm + "/")


def normalize_exclude_paths(
    raw: list[Any] | None,
) -> list[dict[str, str]]:
    """Normalize exclude_paths from settings into list of {value, type} dicts.

    Accepts list of dicts with value/type or list of strings (treated as path prefix).
    """
    if not raw or not isinstance(raw, list):
        return []
    out = []
    for entry in raw:
        if isinstance(entry, dict):
            value = (entry.get("value") or "").strip()
            kind = (entry.get("type") or "path").strip().lower()
            if not value:
                continue
            if kind not in ("path", "regex"):
                kind = "path"
            out.append({"value": value, "type": kind})
        elif isinstance(entry, str):
            value = entry.strip()
            if value:
                out.append({"value": value, "type": "path"})
    return out


def is_path_excluded(
    local_path: str,
    exclude_paths: list[dict[str, str]] | None,
) -> bool:
    """Return True if local_path is excluded by any rule (path prefix or regex).

    Args:
        local_path: Resolved local file path (as this app sees it).
        exclude_paths: List of {"value": str, "type": "path"|"regex"} from normalize_exclude_paths.

    Returns:
        True if the path should be skipped for preview generation.

    """
    if not local_path or not exclude_paths:
        return False
    path = os.path.normpath((local_path or "").strip()).replace("\\", "/")
    if not path:
        return False
    for entry in exclude_paths:
        value = (entry.get("value") or "").strip()
        kind = (entry.get("type") or "path").strip().lower()
        if not value:
            continue
        if kind == "regex":
            try:
                if re.search(value, path):
                    return True
            except re.error:
                logger.warning(f"Invalid exclude regex, skipping: {value[:50]!r}")
                continue
        else:
            prefix = os.path.normpath(value).replace("\\", "/").rstrip("/")
            if not prefix:
                continue
            if path == prefix or path.startswith(prefix + "/"):
                return True
    return False


def path_to_canonical_local(path: str, path_mappings: list[dict[str, Any]]) -> str:
    """Map any path (Plex, webhook, or local) to canonical local path.

    Uses the first matching mapping: plex_prefix or any webhook_prefix is
    replaced by local_prefix. If no mapping matches, the path is returned
    unchanged (treated as already local).

    Args:
        path: Absolute path as seen by Plex, webhook, or this app.
        path_mappings: List from normalize_path_mappings().

    Returns:
        Path in the form this app can use for file access / comparison.

    """
    if not path or not path_mappings:
        return path or ""
    path = (path or "").strip().replace("\\", "/")
    for m in path_mappings:
        plex_prefix = _normalize_prefix(m.get("plex_prefix") or "")
        local_prefix = _normalize_prefix(m.get("local_prefix") or "")
        if plex_prefix and _path_matches_prefix(path, plex_prefix):
            rest = path[len(plex_prefix) :].lstrip("/")
            return f"{local_prefix.rstrip('/')}/{rest}" if rest else (local_prefix or "/")
        for wp in m.get("webhook_prefixes") or []:
            wp = _normalize_prefix(wp)
            if wp and _path_matches_prefix(path, wp):
                rest = path[len(wp) :].lstrip("/")
                return f"{local_prefix.rstrip('/')}/{rest}" if rest else (local_prefix or "/")
    return path


def expand_path_mapping_candidates(path: str, path_mappings: list[dict[str, Any]]) -> list[str]:
    """Return equivalent path candidates across all configured mapping rows.

    This helper expands a single input path into every plausible equivalent path
    using each mapping row. It is used for webhook matching so paths like
    ``/data/...`` can be tested against all mapped Plex roots (for example
    ``/data_16tb...``, ``/data_16tb2...``), not just the first matching row.

    Args:
        path: Absolute path reported by webhook/Plex/app.
        path_mappings: List from normalize_path_mappings().

    Returns:
        Ordered unique list of candidate paths. The original input path is first.

    """
    if not path:
        return []

    cleaned_path = str(path).strip().replace("\\", "/")
    if not cleaned_path:
        return []
    if not path_mappings:
        return [cleaned_path]

    candidates = [cleaned_path]
    seen = {cleaned_path}

    def _add_mapped_candidate(source_prefix: str, target_prefix: str) -> None:
        source = _normalize_prefix(source_prefix)
        target = _normalize_prefix(target_prefix)
        if not source or not target:
            return
        if not _path_matches_prefix(cleaned_path, source):
            return
        rest = cleaned_path[len(source) :].lstrip("/")
        candidate = f"{target.rstrip('/')}/{rest}" if rest else (target or "/")
        if candidate not in seen:
            seen.add(candidate)
            candidates.append(candidate)

    for mapping in path_mappings:
        plex_prefix = mapping.get("plex_prefix") or ""
        local_prefix = mapping.get("local_prefix") or ""
        webhook_prefixes = mapping.get("webhook_prefixes") or []

        # Bidirectional Plex/local expansion for all rows.
        _add_mapped_candidate(plex_prefix, local_prefix)
        _add_mapped_candidate(local_prefix, plex_prefix)

        # Webhook aliases should fan out into both local and Plex forms.
        for webhook_prefix in webhook_prefixes:
            _add_mapped_candidate(webhook_prefix, local_prefix)
            _add_mapped_candidate(webhook_prefix, plex_prefix)
            _add_mapped_candidate(local_prefix, webhook_prefix)
            _add_mapped_candidate(plex_prefix, webhook_prefix)

    return candidates


def plex_path_to_local(path: str, path_mappings: list[dict[str, Any]]) -> str:
    """Map a Plex-reported path to local path (for file access)."""
    return path_to_canonical_local(path, path_mappings)


def local_path_to_webhook_aliases(path: str, path_mappings: list[dict[str, Any]]) -> list[str]:
    """Return webhook-style paths that could refer to the same file as the given local path.

    Used when matching webhook payloads (e.g. /data/...) to Plex items whose
    location is a specific disk (e.g. /data_16tb1/...). For each mapping where
    path starts with local_prefix and webhook_prefixes is set, returns path with
    local_prefix replaced by that webhook prefix.

    Args:
        path: Local path (e.g. /data_16tb1/Movies/foo.mkv).
        path_mappings: List from normalize_path_mappings().

    Returns:
        List of paths in webhook form (e.g. [/data/Movies/foo.mkv]).

    """
    if not path or not path_mappings:
        return []
    path = (path or "").strip().replace("\\", "/")
    out = []
    for m in path_mappings:
        local_prefix = _normalize_prefix(m.get("local_prefix") or "")
        if not local_prefix or not _path_matches_prefix(path, local_prefix):
            continue
        for wp in m.get("webhook_prefixes") or []:
            wp = _normalize_prefix(wp)
            if not wp or wp == local_prefix:
                continue
            rest = path[len(local_prefix) :].lstrip("/")
            alias = f"{wp.rstrip('/')}/{rest}" if rest else (wp or "/")
            out.append(alias)
    return out


def _is_library_id_value(value: str) -> bool:
    """Return True when a library selector value looks like a Plex section ID."""
    return bool(value) and value.isdigit()


def split_library_selectors(values: Any) -> tuple[list[str], list[str]]:
    """Split mixed library selectors into section IDs and lowercased titles.

    Args:
        values: Sequence of selector values from settings/API payloads.

    Returns:
        Tuple of (`library_ids`, `library_titles`) with duplicates removed while
        preserving order.

    """
    if not isinstance(values, list):
        return [], []

    library_ids: list[str] = []
    library_titles: list[str] = []
    seen_ids = set()
    seen_titles = set()

    for raw_value in values:
        if raw_value is None:
            continue
        value = str(raw_value).strip()
        if not value:
            continue
        if _is_library_id_value(value):
            if value not in seen_ids:
                seen_ids.add(value)
                library_ids.append(value)
            continue
        title = value.lower()
        if title not in seen_titles:
            seen_titles.add(title)
            library_titles.append(title)

    return library_ids, library_titles


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

                            # Accept if we have either hex directories OR standard folders
                            has_hex_structure = len(hex_folders) >= 10  # Most of 0-f
                            has_standard_structure = len(found_standard) >= 3  # At least 3 standard folders

                            if not has_hex_structure and not has_standard_structure:
                                debug_info = []
                                debug_info.append(
                                    "PLEX_CONFIG_FOLDER/Media/localhost exists but does not appear to be a valid Plex database"
                                )
                                debug_info.append(
                                    "Expected: Either hex directories (0-f) OR standard Plex folders (Metadata, Cache, etc.)"
                                )
                                debug_info.append(f"Found: {sorted(found_localhost_folders)}")
                                if hex_folders:
                                    debug_info.append(f"Hex directories found: {len(hex_folders)}/16 (need 10+)")
                                if found_standard:
                                    debug_info.append(f"Standard folders found: {len(found_standard)}/5 (need 3+)")
                                debug_info.append(
                                    "This suggests the path may not point to the correct Plex Media Server database location"
                                )
                                validation_errors.append("\n".join(debug_info))
                        except PermissionError:
                            validation_errors.append(f"Permission denied reading localhost folder: {localhost_path}")
            except PermissionError:
                validation_errors.append(f"Permission denied reading PLEX_CONFIG_FOLDER: {plex_config_folder}")


VALID_TONEMAP_ALGORITHMS = ("reinhard", "mobius", "hable", "clip", "gamma", "linear")


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
            logger.debug(f"Created TMP_FOLDER: {tmp_folder}")
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
            logger.debug(f"Cannot check disk space for TMP_FOLDER ({tmp_folder}) - skipping disk space check")

    return tmp_folder_created_by_us, True


# Cache for get_cached_config(); invalidated when settings.json mtime changes or clear_config_cache() is called.
_cached_config: Config | None = None
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
        from .web.settings_manager import get_settings_manager

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
