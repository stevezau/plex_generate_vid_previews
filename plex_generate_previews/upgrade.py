"""Settings migration and upgrade system.

Runs once on application startup to:

1. Seed settings.json from legacy environment variables (one-time).
2. Apply versioned schema migrations for settings structure changes.

After migration, settings.json is the single source of truth for all
application-level configuration.
"""

import os
from typing import Any, Dict, List, Optional

from loguru import logger

# -------------------------------------------------------------------------
# Schema version — bump when adding new migrations
# -------------------------------------------------------------------------
_CURRENT_SCHEMA_VERSION = 2

# -------------------------------------------------------------------------
# Env-var-to-settings migration map
# -------------------------------------------------------------------------
# Each entry: (env_var_name, settings_key, type, default)
# Types: str, int, bool, csv (comma-separated -> list)
_ENV_MIGRATION_MAP = [
    ("PLEX_URL", "plex_url", str, None),
    ("PLEX_TOKEN", "plex_token", str, None),
    ("PLEX_CONFIG_FOLDER", "plex_config_folder", str, None),
    ("PLEX_VERIFY_SSL", "plex_verify_ssl", bool, True),
    ("PLEX_TIMEOUT", "plex_timeout", int, 60),
    ("PLEX_BIF_FRAME_INTERVAL", "thumbnail_interval", int, None),
    ("THUMBNAIL_INTERVAL", "thumbnail_interval", int, None),
    ("THUMBNAIL_QUALITY", "thumbnail_quality", int, None),
    ("TONEMAP_ALGORITHM", "tonemap_algorithm", str, None),
    ("CPU_THREADS", "cpu_threads", int, None),
    ("FALLBACK_CPU_THREADS", "cpu_fallback_threads", int, None),
    ("MEDIA_PATH", "media_path", str, None),
    ("TMP_FOLDER", "tmp_folder", str, None),
    ("LOG_LEVEL", "log_level", str, None),
]

# Env vars that have been removed; warn if still set.
_DEPRECATED_ENV_VARS = [
    "GPU_SELECTION",
    "GPU_THREADS",
    "FFMPEG_THREADS",
    "PLEX_LIBRARIES",
    "REGENERATE_THUMBNAILS",
    "SORT_BY",
    "NICE_LEVEL",
]


# =========================================================================
# Public entry point
# =========================================================================


def run_migrations(settings_manager) -> None:
    """Run all pending migrations.

    Call once on application startup, before the web server starts
    serving requests.

    Args:
        settings_manager: A ``SettingsManager`` instance.

    """
    _migrate_env_vars(settings_manager)
    _migrate_schema(settings_manager)


# =========================================================================
# Environment variable migration (one-time)
# =========================================================================


def _migrate_env_vars(sm) -> None:
    """Seed settings.json from environment variables.

    Only runs once.  After migration, settings.json is the sole source of
    truth and env var fallbacks are no longer consulted.  Writes a
    ``_env_migrated`` flag into settings to prevent re-running.

    Also migrates legacy GPU env vars (GPU_THREADS, GPU_SELECTION,
    FFMPEG_THREADS) into the new ``gpu_config`` structure, and converts
    legacy path mapping env vars into the ``path_mappings`` list.
    """
    if sm.get("_env_migrated"):
        return

    updates: Dict[str, Any] = {}
    migrated_keys: list[str] = []

    for env_name, settings_key, val_type, _default in _ENV_MIGRATION_MAP:
        if sm.get(settings_key) is not None:
            continue
        raw = os.environ.get(env_name, "").strip()
        if not raw:
            continue
        try:
            if val_type is bool:
                value: Any = raw.lower() in ("true", "1", "yes")
            elif val_type is int:
                value = int(raw)
            else:
                value = raw
            updates[settings_key] = value
            migrated_keys.append(f"{env_name} -> {settings_key}")
        except (ValueError, TypeError):
            logger.warning(
                f"Could not migrate env var {env_name}={raw!r}: invalid value"
            )

    if sm.get("gpu_config") is None:
        gpu_config = _build_gpu_config_from_env()
        if gpu_config is not None:
            updates["gpu_config"] = gpu_config
            migrated_keys.append(
                "GPU_THREADS/GPU_SELECTION/FFMPEG_THREADS -> gpu_config"
            )

    if sm.get("path_mappings") is None:
        path_mappings = _build_path_mappings_from_env()
        if path_mappings is not None:
            updates["path_mappings"] = path_mappings
            migrated_keys.append(
                "PLEX_VIDEOS_PATH_MAPPING/PLEX_LOCAL_VIDEOS_PATH_MAPPING -> path_mappings"
            )

    if sm.get("selected_libraries") is None:
        libs = os.environ.get("PLEX_LIBRARIES", "").strip()
        if libs:
            updates["selected_libraries"] = [
                s.strip() for s in libs.split(",") if s.strip()
            ]
            migrated_keys.append("PLEX_LIBRARIES -> selected_libraries")

    updates["_env_migrated"] = True
    sm.apply_changes(updates=updates)

    if migrated_keys:
        logger.info(
            f"Migrated {len(migrated_keys)} env var(s) into settings.json: "
            + ", ".join(migrated_keys)
        )

    for env_name in _DEPRECATED_ENV_VARS:
        if os.environ.get(env_name):
            logger.warning(
                f"Environment variable {env_name} is deprecated and ignored. "
                "Use the Settings page in the web UI instead."
            )


# =========================================================================
# Schema migrations (versioned)
# =========================================================================


def _migrate_schema(sm) -> None:
    """Run incremental schema migrations on settings.

    Each migration is gated by ``_schema_version`` so it runs at most
    once.

    Migrations:
        v2 -- Per-GPU config (gpu_config).  If a flat ``gpu_threads``
              key exists without ``gpu_config``, detect GPUs and build
              the per-GPU structure.  Removes stale ``gpu_threads`` and
              ``ffmpeg_threads`` flat keys.
    """
    current = sm.get("_schema_version", 1)
    if current >= _CURRENT_SCHEMA_VERSION:
        return

    all_notes: list[str] = []

    if current < 2:
        all_notes += _migrate_to_v2(sm)

    sm.set("_schema_version", _CURRENT_SCHEMA_VERSION)

    if all_notes:
        logger.info(
            f"Settings schema migrated to v{_CURRENT_SCHEMA_VERSION}: "
            + ", ".join(all_notes)
        )


def _migrate_to_v2(sm) -> list:
    """Migrate flat gpu_threads/ffmpeg_threads to per-GPU gpu_config.

    Returns:
        List of human-readable descriptions of what was migrated.
    """
    notes: list[str] = []
    updates: Dict[str, Any] = {}
    deletes: list[str] = []

    has_gpu_config = sm.get("gpu_config") is not None
    old_threads_val = sm.get("gpu_threads")

    if not has_gpu_config and old_threads_val is not None:
        try:
            old_threads = int(old_threads_val)
            old_ffmpeg = int(sm.get("ffmpeg_threads", 2))
        except (ValueError, TypeError):
            logger.warning(
                "Invalid gpu_threads/ffmpeg_threads in settings, skipping migration"
            )
            old_threads = 0
            old_ffmpeg = 2

        try:
            from .gpu_detection import detect_all_gpus

            detected = detect_all_gpus()
        except Exception:
            detected = []

        if detected and old_threads > 0:
            count = len(detected)
            per_gpu = old_threads // count
            remainder = old_threads - per_gpu * count
            gpu_config = []
            for gpu_type, gpu_device, gpu_info in detected:
                workers = per_gpu
                if remainder > 0:
                    workers += 1
                    remainder -= 1
                gpu_config.append(
                    {
                        "device": gpu_device,
                        "name": gpu_info.get("name", f"{gpu_type} GPU"),
                        "type": gpu_type,
                        "enabled": True,
                        "workers": workers,
                        "ffmpeg_threads": old_ffmpeg,
                    }
                )
            updates["gpu_config"] = gpu_config
            notes.append(
                f"gpu_threads={old_threads} -> gpu_config "
                f"({count} GPU(s), {per_gpu}+ workers each)"
            )

    for stale_key in ("gpu_threads", "ffmpeg_threads"):
        if sm.get(stale_key) is not None:
            deletes.append(stale_key)

    if deletes:
        notes.append(f"removed stale keys: {', '.join(deletes)}")

    if updates or deletes:
        sm.apply_changes(updates=updates, deletes=deletes)

    return notes


# =========================================================================
# Helper: build gpu_config from legacy GPU env vars
# =========================================================================


def _build_gpu_config_from_env() -> Optional[List[Dict[str, Any]]]:
    """Build gpu_config list from legacy GPU env vars.

    Reads GPU_THREADS, GPU_SELECTION, and FFMPEG_THREADS from the
    environment and creates a gpu_config structure.  Returns None if
    no GPU env vars are set.
    """
    gpu_threads_str = os.environ.get("GPU_THREADS", "").strip()
    gpu_selection_str = os.environ.get("GPU_SELECTION", "").strip()
    ffmpeg_threads_str = os.environ.get("FFMPEG_THREADS", "").strip()

    if not gpu_threads_str and not gpu_selection_str and not ffmpeg_threads_str:
        return None

    try:
        gpu_threads = int(gpu_threads_str) if gpu_threads_str else 1
        ffmpeg_threads = int(ffmpeg_threads_str) if ffmpeg_threads_str else 2
    except (ValueError, TypeError):
        logger.warning(
            f"Invalid GPU_THREADS={gpu_threads_str!r} or FFMPEG_THREADS={ffmpeg_threads_str!r}, "
            "using defaults"
        )
        gpu_threads = 1
        ffmpeg_threads = 2

    try:
        from .gpu_detection import detect_all_gpus

        detected = detect_all_gpus()
    except Exception:
        logger.debug("GPU detection unavailable during env migration", exc_info=True)
        detected = []

    if not detected:
        return []

    if gpu_selection_str.lower() in ("all", ""):
        enabled_indices = set(range(len(detected)))
    else:
        try:
            enabled_indices = {
                int(x.strip()) for x in gpu_selection_str.split(",") if x.strip()
            }
        except ValueError:
            enabled_indices = set(range(len(detected)))

    enabled_count = sum(1 for i in range(len(detected)) if i in enabled_indices)
    if enabled_count == 0:
        enabled_count = len(detected)
        enabled_indices = set(range(len(detected)))

    per_gpu_workers = gpu_threads // enabled_count if gpu_threads > 0 else 0
    remainder = (
        gpu_threads - (per_gpu_workers * enabled_count) if gpu_threads > 0 else 0
    )

    gpu_config = []
    for i, (gpu_type, gpu_device, gpu_info) in enumerate(detected):
        is_enabled = i in enabled_indices
        workers = per_gpu_workers if is_enabled else 0
        if is_enabled and remainder > 0:
            workers += 1
            remainder -= 1
        gpu_config.append(
            {
                "device": gpu_device,
                "name": gpu_info.get("name", f"{gpu_type} GPU"),
                "type": gpu_type,
                "enabled": is_enabled and gpu_threads > 0,
                "workers": workers,
                "ffmpeg_threads": ffmpeg_threads,
            }
        )

    return gpu_config


# =========================================================================
# Helper: build path_mappings from legacy env vars
# =========================================================================


def _build_path_mappings_from_env() -> Optional[List[Dict[str, Any]]]:
    """Build path_mappings list from legacy path mapping env vars.

    Returns None if no relevant env vars are set.
    """
    plex_str = os.environ.get("PLEX_VIDEOS_PATH_MAPPING", "").strip()
    local_str = os.environ.get("PLEX_LOCAL_VIDEOS_PATH_MAPPING", "").strip()

    if not plex_str or not local_str:
        return None

    try:
        from .config import get_path_mapping_pairs

        pairs = get_path_mapping_pairs(plex_str, local_str)
    except Exception:
        logger.debug("Path mapping migration failed", exc_info=True)
        return None

    if not pairs:
        return None

    return [
        {
            "plex_prefix": plex_root,
            "local_prefix": local_root,
            "webhook_prefixes": [],
        }
        for plex_root, local_root in pairs
    ]
