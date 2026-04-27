"""Settings migration and upgrade system.

Runs once on application startup to:

1. Seed settings.json from legacy environment variables (one-time).
2. Apply versioned schema migrations for settings structure changes.

After migration, settings.json is the single source of truth for all
application-level configuration.
"""

import os
from typing import Any

from loguru import logger

# -------------------------------------------------------------------------
# Schema version — bump when adding new migrations
# -------------------------------------------------------------------------
_CURRENT_SCHEMA_VERSION = 7

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
    "FALLBACK_CPU_THREADS",
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

    updates: dict[str, Any] = {}
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
            logger.warning(f"Could not migrate env var {env_name}={raw!r}: invalid value")

    if sm.get("gpu_config") is None:
        gpu_config = _build_gpu_config_from_env()
        if gpu_config is not None:
            updates["gpu_config"] = gpu_config
            migrated_keys.append("GPU_THREADS/GPU_SELECTION/FFMPEG_THREADS -> gpu_config")

    if sm.get("path_mappings") is None:
        path_mappings = _build_path_mappings_from_env()
        if path_mappings is not None:
            updates["path_mappings"] = path_mappings
            migrated_keys.append("PLEX_VIDEOS_PATH_MAPPING/PLEX_LOCAL_VIDEOS_PATH_MAPPING -> path_mappings")

    if sm.get("selected_libraries") is None:
        libs = os.environ.get("PLEX_LIBRARIES", "").strip()
        if libs:
            updates["selected_libraries"] = [s.strip() for s in libs.split(",") if s.strip()]
            migrated_keys.append("PLEX_LIBRARIES -> selected_libraries")

    updates["_env_migrated"] = True
    sm.apply_changes(updates=updates)

    if migrated_keys:
        logger.info(f"Migrated {len(migrated_keys)} env var(s) into settings.json: " + ", ".join(migrated_keys))

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
        v3 -- Seeds defaults for the Plex direct webhook (historical —
              the Recently Added keys previously seeded here are now
              cleaned up by v4 since the scanner is a first-class
              schedule type).
        v4 -- Converts the beta-era ``recently_added_*`` settings into
              real schedule entries and deletes the legacy keys.  Also
              removes the obsolete ``system_recently_added_scan``
              APScheduler job if it exists.
        v5 -- Removes the obsolete ``cpu_fallback_threads`` key.  The
              dedicated CPU-fallback worker pool has been replaced by
              in-place CPU retry inside the GPU worker.  If a legacy
              setting was larger than ``cpu_threads``, fold the value
              into ``cpu_threads`` so existing deployments keep the same
              CPU concurrency after the upgrade.
        v6 -- Strips stale ``{"device": "cuda", ...}`` entries from
              ``gpu_config``.  NVIDIA GPUs are now enumerated per-device
              as ``cuda:0``, ``cuda:1``, etc. (issue #221); any legacy
              generic ``cuda`` entries no longer match detected GPUs and
              would otherwise stay orphaned in settings.
        v7 -- Synthesizes a ``media_servers`` array from the legacy
              flat ``plex_*`` settings as part of the multi-media-server
              refactor.  Existing single-Plex deployments get one
              entry with id ``"plex-default"`` so the new dispatcher
              has a server to route to.  Legacy ``plex_*`` keys are
              kept (read-path compatibility) and removed in a later
              migration once all callers have been updated.
    """
    current = sm.get("_schema_version", 1)
    if current >= _CURRENT_SCHEMA_VERSION:
        return

    all_notes: list[str] = []

    if current < 2:
        all_notes += _migrate_to_v2(sm)

    if current < 3:
        all_notes += _migrate_to_v3(sm)

    if current < 4:
        all_notes += _migrate_to_v4(sm)

    if current < 5:
        all_notes += _migrate_to_v5(sm)

    if current < 6:
        all_notes += _migrate_to_v6(sm)

    if current < 7:
        all_notes += _migrate_to_v7(sm)

    sm.set("_schema_version", _CURRENT_SCHEMA_VERSION)

    if all_notes:
        logger.info(f"Settings schema migrated to v{_CURRENT_SCHEMA_VERSION}: " + ", ".join(all_notes))


def _migrate_to_v2(sm) -> list:
    """Migrate flat gpu_threads/ffmpeg_threads to per-GPU gpu_config.

    Returns:
        List of human-readable descriptions of what was migrated.
    """
    notes: list[str] = []
    updates: dict[str, Any] = {}
    deletes: list[str] = []

    has_gpu_config = sm.get("gpu_config") is not None
    old_threads_val = sm.get("gpu_threads")

    if not has_gpu_config and old_threads_val is not None:
        try:
            old_threads = int(old_threads_val)
            old_ffmpeg = int(sm.get("ffmpeg_threads", 2))
        except (ValueError, TypeError):
            logger.warning("Invalid gpu_threads/ffmpeg_threads in settings, skipping migration")
            old_threads = 0
            old_ffmpeg = 2

        try:
            from .gpu.detect import detect_all_gpus

            detected = detect_all_gpus()
        except Exception:
            logger.debug(
                "Settings migration: GPU detection raised during v3 upgrade; proceeding with an empty GPU list.",
                exc_info=True,
            )
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
            notes.append(f"gpu_threads={old_threads} -> gpu_config ({count} GPU(s), {per_gpu}+ workers each)")

    for stale_key in ("gpu_threads", "ffmpeg_threads"):
        if sm.get(stale_key) is not None:
            deletes.append(stale_key)

    if deletes:
        notes.append(f"removed stale keys: {', '.join(deletes)}")

    if updates or deletes:
        sm.apply_changes(updates=updates, deletes=deletes)

    return notes


def _migrate_to_v3(sm) -> list:
    """Seed defaults for the Plex direct webhook (off by default).

    The legacy Recently Added keys that used to be seeded here are now
    cleaned up by ``_migrate_to_v4`` since the scanner is a first-class
    schedule type.
    """
    notes: list[str] = []
    updates: dict[str, Any] = {}

    plex_webhook_defaults = {
        "plex_webhook_enabled": False,
        "plex_webhook_public_url": "",
    }
    for key, default in plex_webhook_defaults.items():
        if sm.get(key) is None:
            updates[key] = default

    if updates:
        sm.apply_changes(updates=updates)
        notes.append(f"seeded {len(updates)} auto-trigger default(s)")

    return notes


def _migrate_to_v4(sm) -> list:
    """Convert legacy ``recently_added_*`` settings into schedule entries.

    The Recently Added scanner used to be a hidden APScheduler "system
    job" with its own settings keys.  It's now a standard schedule type
    — see ``scheduler.execute_scheduled_job`` for the dispatch branch.

    This migration:
      1. Reads the legacy ``recently_added_*`` keys.
      2. If the scanner was enabled, creates equivalent schedule(s)
         through the existing ``ScheduleManager`` — one per entry in
         ``recently_added_libraries``, or a single all-libraries
         schedule when that list is empty.
      3. Deletes all four legacy keys from settings.
      4. Best-effort removes the obsolete ``system_recently_added_scan``
         APScheduler job if it exists in the persistent jobstore.
    """
    notes: list[str] = []
    legacy_keys = [
        "recently_added_enabled",
        "recently_added_interval_minutes",
        "recently_added_lookback_hours",
        "recently_added_libraries",
    ]
    # Nothing to do if none of the legacy keys exist (fresh install).
    if not any(sm.get(k) is not None for k in legacy_keys):
        return notes

    enabled = bool(sm.get("recently_added_enabled", False))
    interval = int(sm.get("recently_added_interval_minutes", 15) or 15)
    interval = max(5, min(1440, interval))
    lookback = int(sm.get("recently_added_lookback_hours", 24) or 24)
    lookback = max(1, min(720, lookback))
    raw_libs = sm.get("recently_added_libraries", None)
    library_entries: list[str] = []
    if isinstance(raw_libs, list):
        library_entries = [str(v).strip() for v in raw_libs if str(v).strip()]

    if enabled:
        try:
            from .web.scheduler import get_schedule_manager

            # Pass the settings manager's config dir explicitly so tests
            # (and any caller using a non-default CONFIG_DIR) end up with
            # a ScheduleManager writing to the right jobstore location.
            manager = get_schedule_manager(config_dir=str(sm.config_dir))
            if not library_entries:
                schedule = manager.create_schedule(
                    name="Recently Added Scanner",
                    interval_minutes=interval,
                    library_id=None,
                    library_name="",
                    config={
                        "job_type": "recently_added",
                        "lookback_hours": lookback,
                    },
                    enabled=True,
                )
                notes.append(f"v4: created 1 recently-added schedule ({schedule['id']})")
            else:
                for entry in library_entries:
                    schedule = manager.create_schedule(
                        name=f"Recently Added Scanner — {entry}",
                        interval_minutes=interval,
                        library_id=entry if entry.isdigit() else None,
                        library_name=entry if not entry.isdigit() else "",
                        config={
                            "job_type": "recently_added",
                            "lookback_hours": lookback,
                        },
                        enabled=True,
                    )
                notes.append(
                    f"v4: created {len(library_entries)} recently-added schedule(s) from legacy library override"
                )

            # Best-effort: remove the stale system job if it's still in
            # the persistent APScheduler jobstore.
            try:
                manager.scheduler.remove_job("system_recently_added_scan")
            except Exception:
                pass
        except Exception as exc:
            logger.warning(
                "v4 migration: failed to create recently-added schedule(s): {}",
                exc,
            )

    sm.apply_changes(deletes=legacy_keys)
    notes.append(f"v4: removed {len(legacy_keys)} legacy recently_added_* keys")
    return notes


def _migrate_to_v5(sm) -> list:
    """Remove obsolete cpu_fallback_threads key; preserve concurrency intent.

    The dedicated CPU-fallback worker pool has been replaced by in-place
    CPU retry inside the GPU worker — when a GPU hits
    ``CodecNotSupportedError``, the same worker now runs a CPU pass on
    the same item rather than re-queuing to a separate pool.  To avoid
    silently reducing CPU capacity on upgrade, fold any legacy
    ``cpu_fallback_threads`` value into ``cpu_threads`` (using the
    larger of the two) before deleting the key.
    """
    notes: list[str] = []
    legacy_val = sm.get("cpu_fallback_threads")
    if legacy_val is None:
        return notes

    try:
        legacy_int = int(legacy_val)
    except (ValueError, TypeError):
        legacy_int = 0

    updates: dict[str, Any] = {}
    if legacy_int > 0:
        current_cpu = sm.get("cpu_threads")
        try:
            current_cpu_int = int(current_cpu) if current_cpu is not None else 1
        except (ValueError, TypeError):
            current_cpu_int = 1
        if legacy_int > current_cpu_int:
            updates["cpu_threads"] = legacy_int
            notes.append(f"v5: folded cpu_fallback_threads={legacy_int} into cpu_threads (was {current_cpu_int})")

    sm.apply_changes(updates=updates or None, deletes=["cpu_fallback_threads"])
    notes.append("v5: removed obsolete cpu_fallback_threads key")
    return notes


def _legacy_plex_to_media_server(sm) -> dict[str, Any] | None:
    """Build a single ``media_servers`` entry from legacy ``plex_*`` keys.

    Returns ``None`` when there is nothing to migrate (e.g. fresh install
    with no Plex configured). The returned dict matches the JSON shape
    persisted in ``settings.json`` (not a :class:`ServerConfig` instance);
    the runtime hydrates it into the dataclass at load time.

    Used by the v7 migration and by tests; exposed so other code paths
    (e.g. legacy-only deployments hitting the new server registry) can
    synthesize the same shape on demand.
    """
    plex_url = (sm.get("plex_url") or "").strip()
    plex_token = (sm.get("plex_token") or "").strip()
    if not plex_url and not plex_token:
        # Nothing to migrate; fresh install will add servers via the UI.
        return None

    selected_libraries = sm.get("plex_libraries") or sm.get("selected_libraries") or []
    selected_library_ids = sm.get("plex_library_ids") or []
    libraries: list[dict[str, Any]] = []
    if isinstance(selected_library_ids, list) and selected_library_ids:
        for lib_id in selected_library_ids:
            libraries.append(
                {
                    "id": str(lib_id),
                    "name": str(lib_id),
                    "remote_paths": [],
                    "enabled": True,
                }
            )
    elif isinstance(selected_libraries, list):
        for name in selected_libraries:
            libraries.append(
                {
                    "id": str(name),
                    "name": str(name),
                    "remote_paths": [],
                    "enabled": True,
                }
            )

    auth: dict[str, Any] = {"method": "token", "token": plex_token} if plex_token else {}

    return {
        "id": "plex-default",
        "type": "plex",
        "name": "Plex",
        "enabled": True,
        "url": plex_url,
        "auth": auth,
        "verify_ssl": bool(sm.get("plex_verify_ssl", True)),
        "timeout": int(sm.get("plex_timeout", 60) or 60),
        "libraries": libraries,
        "path_mappings": list(sm.get("path_mappings") or []),
        "output": {
            "adapter": "plex_bundle",
            "plex_config_folder": sm.get("plex_config_folder") or "",
            "frame_interval": int(sm.get("plex_bif_frame_interval") or sm.get("thumbnail_interval") or 10),
        },
    }


def _migrate_to_v7(sm) -> list:
    """Add a ``media_servers`` array synthesised from legacy ``plex_*`` keys.

    No-op when ``media_servers`` already exists. Legacy ``plex_*`` keys
    are *not* removed yet — the read path continues to honour them so
    Phase 1 ships the schema change without breaking existing single-Plex
    deployments. A later migration removes them once all callers have
    been routed through the server registry.
    """
    notes: list[str] = []
    if sm.get("media_servers") is not None:
        return notes

    entry = _legacy_plex_to_media_server(sm)
    if entry is None:
        # Fresh install: write an empty list so callers can rely on the key
        # always being present.
        sm.apply_changes(updates={"media_servers": []})
        notes.append("v7: initialised empty media_servers array")
        return notes

    sm.apply_changes(updates={"media_servers": [entry]})
    notes.append("v7: synthesised media_servers[0] from legacy plex_* settings")
    return notes


def _migrate_to_v6(sm) -> list:
    """Strip stale generic ``"cuda"`` gpu_config entries (issue #221).

    Prior to the per-device NVIDIA enumeration rewrite, NVIDIA GPUs were
    registered with a generic ``device: "cuda"`` string.  Multi-GPU
    hosts silently collapsed onto that single entry.  NVIDIA GPUs are
    now keyed by their nvidia-smi index (``cuda:0``, ``cuda:1``, ...),
    so any legacy ``"cuda"`` entry no longer matches a detected GPU and
    should be dropped so the UI repopulates cleanly on next re-scan.
    """
    notes: list[str] = []
    raw = sm.get("gpu_config")
    if not isinstance(raw, list):
        return notes

    kept = [e for e in raw if not (isinstance(e, dict) and e.get("device") == "cuda")]
    removed = len(raw) - len(kept)
    if removed == 0:
        return notes

    sm.set("gpu_config", kept)
    notes.append(f"v6: removed {removed} stale generic 'cuda' gpu_config entry(ies)")
    return notes


# =========================================================================
# Helper: build gpu_config from legacy GPU env vars
# =========================================================================


def _build_gpu_config_from_env() -> list[dict[str, Any]] | None:
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
            f"Invalid GPU_THREADS={gpu_threads_str!r} or FFMPEG_THREADS={ffmpeg_threads_str!r}, using defaults"
        )
        gpu_threads = 1
        ffmpeg_threads = 2

    try:
        from .gpu.detect import detect_all_gpus

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
            enabled_indices = {int(x.strip()) for x in gpu_selection_str.split(",") if x.strip()}
        except ValueError:
            enabled_indices = set(range(len(detected)))

    enabled_count = sum(1 for i in range(len(detected)) if i in enabled_indices)
    if enabled_count == 0:
        enabled_count = len(detected)
        enabled_indices = set(range(len(detected)))

    per_gpu_workers = gpu_threads // enabled_count if gpu_threads > 0 else 0
    remainder = gpu_threads - (per_gpu_workers * enabled_count) if gpu_threads > 0 else 0

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


def _build_path_mappings_from_env() -> list[dict[str, Any]] | None:
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
