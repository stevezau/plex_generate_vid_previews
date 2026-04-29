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
_CURRENT_SCHEMA_VERSION = 9


class SchemaDowngradeError(RuntimeError):
    """Raised when settings.json was written by a newer binary than this one.

    We refuse to start in that case because allowing the load would mean
    silently dropping unknown fields on the next save — exactly the failure
    mode that wiped a user's job history during a tag-drift incident on
    feat/multi-media-server.
    """


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
            logger.warning(
                "Could not import the {} environment variable (value {!r} isn't a valid {}). "
                "Other settings migrated normally — only this one was skipped. "
                "Set the value in Settings instead, then remove the env var.",
                env_name,
                raw,
                val_type.__name__,
            )

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
        logger.info("Migrated {} env var(s) into settings.json: {}", len(migrated_keys), ", ".join(migrated_keys))

    for env_name in _DEPRECATED_ENV_VARS:
        if os.environ.get(env_name):
            logger.warning(
                "Environment variable {} is deprecated and ignored. Use the Settings page in the web UI instead.",
                env_name,
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
    if current > _CURRENT_SCHEMA_VERSION:
        # J3: refuse to boot when settings.json is newer than the binary.
        # Silent acceptance would drop unknown fields on the next save —
        # the exact failure mode that wiped jobs.json on tag-drift.
        settings_path = getattr(sm, "settings_file", None)
        bak_path = f"{settings_path}.bak" if settings_path else "<settings>.bak"
        raise SchemaDowngradeError(
            f"settings.json was written by schema v{current} but this binary supports up to "
            f"v{_CURRENT_SCHEMA_VERSION}. Refusing to start (would silently drop fields on next save). "
            f"Either run a newer build of the app, or restore the previous settings.json "
            f"(a backup is at {bak_path}) and start the older app version that wrote it."
        )
    if current == _CURRENT_SCHEMA_VERSION:
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

    if current < 8:
        all_notes += _migrate_to_v8(sm)

    if current < 9:
        all_notes += _migrate_to_v9(sm)

    sm.set("_schema_version", _CURRENT_SCHEMA_VERSION)

    if all_notes:
        logger.info("Settings schema migrated to v{}: {}", _CURRENT_SCHEMA_VERSION, ", ".join(all_notes))
        # J5: drop a one-shot flag the dashboard's notification bell reads
        # so the user sees a single "we migrated your config" card on next
        # login. Dismissal removes the flag (see notifications.py).
        from datetime import datetime as _dt
        from datetime import timezone as _tz

        bak_path = f"{getattr(sm, 'settings_file', '')}.bak" if getattr(sm, "settings_file", None) else ""
        sm.set(
            "_pending_migration_notice",
            {
                "from": current,
                "to": _CURRENT_SCHEMA_VERSION,
                "at": _dt.now(_tz.utc).isoformat(),
                "backup": bak_path,
                "notes": all_notes,
            },
        )


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
            logger.warning(
                "Old GPU settings (gpu_threads={!r}, ffmpeg_threads={!r}) aren't valid numbers — "
                "they can't be migrated to the new per-GPU layout. Defaulting to 0 GPU workers + 2 ffmpeg threads. "
                "Open Settings → GPU and configure your devices manually.",
                old_threads_val,
                sm.get("ffmpeg_threads"),
            )
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
    """No-op as of multi-server.

    Used to seed top-level ``plex_webhook_enabled`` / ``plex_webhook_public_url``
    defaults. Those keys are now per-server (under ``media_servers[i].output``)
    and SettingsManager._load() migrates legacy installs at boot. Kept here so
    the schema-version chain stays linear and existing installs at v2 still
    advance to v3 cleanly.
    """
    return []


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
                "Could not migrate your old 'Recently Added' scanner settings into the new schedules system: {}. "
                "Your other settings are intact and the app will start normally; you'll just need to recreate the "
                "scanner manually under Settings → Schedules (Add → Recently Added Scanner).",
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


def _migrate_to_v8(sm) -> list:
    """Move global ``path_mappings`` / ``exclude_paths`` into ``media_servers[0]``.

    Phase 0 + Phase 1 of the multi-server refactor taught readers and
    writers to prefer the per-server view. v8 completes the move on
    disk so the global keys can be removed entirely.

    Edge cases (every one identified in the pre-implementation audit):

    * **Nothing to migrate** — no global keys present → no-op.
    * **Empty ``media_servers``** — Plex-less install. Keep the global
      keys at the global level; warn so the user knows to assign them
      once they configure a server.
    * **Multiple ``media_servers``** — ambiguous which server should
      inherit the rules. Keep at global level; warn so the user
      reviews and assigns explicitly.
    * **Single server but not Plex** — Plex was deleted, only Emby
      remains. Apply the rules to that single non-Plex entry; same
      semantics regardless of vendor.
    * **Pre-v6 leftovers** — clean up
      ``plex_videos_path_mapping`` / ``plex_local_videos_path_mapping``
      keys at the same time (they're vestigial after v6).
    * **Atomicity** — every persistence step happens in a single
      ``apply_changes`` call, which uses ``atomic_json_save``. A
      mid-write crash leaves either the v7 file or the v8 file —
      never a partial state.
    * **Idempotency** — re-running on a v8 file is a no-op because
      ``_migrate_schema`` skips us when ``_schema_version >= 8``.
      Even without that guard, the two ``del`` semantics are safe to
      repeat.
    * **Schema bump rollback** — ``_migrate_schema`` bumps the version
      key only AFTER all migrations succeed (line 224); if v8 raises
      mid-migration, schema_version stays at 7 and the next startup
      retries.
    """
    notes: list[str] = []
    global_path_mappings = sm.get("path_mappings") or []
    global_exclude_paths = sm.get("exclude_paths") or []

    # Pre-v6 fields that may still be hanging around — clean them up
    # regardless of whether there's a media_servers entry to migrate
    # them into. They're unused after v6.
    legacy_pre_v6_present = bool(sm.get("plex_videos_path_mapping")) or bool(sm.get("plex_local_videos_path_mapping"))

    if not global_path_mappings and not global_exclude_paths and not legacy_pre_v6_present:
        return notes

    media_servers = list(sm.get("media_servers") or [])

    if not media_servers and (global_path_mappings or global_exclude_paths):
        notes.append(
            "v8: keeping global path_mappings/exclude_paths at top level — no media_servers configured yet "
            "(they'll be picked up automatically once you add a server, or moved by re-running the migration)"
        )
        # Still clean up the pre-v6 keys.
        if legacy_pre_v6_present:
            sm.apply_changes(deletes=["plex_videos_path_mapping", "plex_local_videos_path_mapping"])
            notes.append("v8: cleaned up pre-v6 plex_videos_path_mapping/plex_local_videos_path_mapping keys")
        return notes

    if len(media_servers) > 1 and (global_path_mappings or global_exclude_paths):
        notes.append(
            f"v8: {len(media_servers)} servers configured — global path_mappings ({len(global_path_mappings)}) "
            f"and exclude_paths ({len(global_exclude_paths)}) kept at top level. "
            "Open Settings → Media Servers and assign each rule explicitly to its target server."
        )
        if legacy_pre_v6_present:
            sm.apply_changes(deletes=["plex_videos_path_mapping", "plex_local_videos_path_mapping"])
            notes.append("v8: cleaned up pre-v6 plex_videos_path_mapping/plex_local_videos_path_mapping keys")
        return notes

    # Exactly one server (any type — could be Plex or Emby/Jellyfin if
    # the user deleted Plex). Apply the rules to it.
    if media_servers:
        target = dict(media_servers[0])  # shallow copy so we don't mutate input
        existing_pm = list(target.get("path_mappings") or [])
        existing_ep = list(target.get("exclude_paths") or [])
        if global_path_mappings:
            target["path_mappings"] = existing_pm + list(global_path_mappings)
        if global_exclude_paths:
            target["exclude_paths"] = existing_ep + list(global_exclude_paths)
        media_servers[0] = target

        deletes = []
        if "path_mappings" in sm.get_all():
            deletes.append("path_mappings")
        if "exclude_paths" in sm.get_all():
            deletes.append("exclude_paths")
        if legacy_pre_v6_present:
            deletes.extend(["plex_videos_path_mapping", "plex_local_videos_path_mapping"])

        sm.apply_changes(updates={"media_servers": media_servers}, deletes=deletes)

        moved_parts = []
        if global_path_mappings:
            moved_parts.append(f"path_mappings ({len(global_path_mappings)})")
        if global_exclude_paths:
            moved_parts.append(f"exclude_paths ({len(global_exclude_paths)})")
        if moved_parts:
            notes.append(
                f"v8: moved global {' + '.join(moved_parts)} into media_servers[0] ({target.get('name') or target.get('id')})"
            )
        if legacy_pre_v6_present:
            notes.append("v8: cleaned up pre-v6 plex_videos_path_mapping/plex_local_videos_path_mapping keys")

    return notes


def _migrate_to_v9(sm) -> list:
    """Dedupe per-server ``path_mappings`` and ``exclude_paths``.

    The v7 + v8 chain shipped a double-copy bug: v7 populated the new
    ``media_servers[0].path_mappings`` from the legacy global list, then
    v8 appended the same global list again. Single-Plex installs that
    upgraded through both migrations end up with every row duplicated
    (and likewise for ``exclude_paths``).

    v9 walks every server entry and dedupes both lists in place,
    preserving the first occurrence of each row. For
    ``path_mappings`` the dedupe key is the (plex_prefix, local_prefix,
    sorted webhook_prefixes) triple — two rows with the same prefixes
    but different webhook aliases are kept distinct. For
    ``exclude_paths`` the key is the (value, type) pair.

    Idempotent (re-running on a clean v9 file is a no-op) and harmless
    when no duplicates exist (just rewrites the same list).
    """
    notes: list[str] = []
    media_servers = list(sm.get("media_servers") or [])
    if not media_servers:
        return notes

    changed = False
    cleaned_servers: list[dict[str, Any]] = []
    pm_removed_total = 0
    ep_removed_total = 0

    for entry in media_servers:
        if not isinstance(entry, dict):
            cleaned_servers.append(entry)
            continue
        target = dict(entry)

        original_pm = list(target.get("path_mappings") or [])
        if original_pm:
            seen_pm: set[tuple] = set()
            deduped_pm: list[dict[str, Any]] = []
            for row in original_pm:
                if not isinstance(row, dict):
                    deduped_pm.append(row)
                    continue
                key = (
                    (row.get("plex_prefix") or "").strip(),
                    (row.get("local_prefix") or "").strip(),
                    tuple(sorted([str(w).strip() for w in (row.get("webhook_prefixes") or [])])),
                )
                if key in seen_pm:
                    continue
                seen_pm.add(key)
                deduped_pm.append(row)
            if len(deduped_pm) != len(original_pm):
                target["path_mappings"] = deduped_pm
                pm_removed_total += len(original_pm) - len(deduped_pm)
                changed = True

        original_ep = list(target.get("exclude_paths") or [])
        if original_ep:
            seen_ep: set[tuple] = set()
            deduped_ep: list[dict[str, Any]] = []
            for row in original_ep:
                if not isinstance(row, dict):
                    deduped_ep.append(row)
                    continue
                key = ((row.get("value") or "").strip(), (row.get("type") or "path").strip())
                if key in seen_ep:
                    continue
                seen_ep.add(key)
                deduped_ep.append(row)
            if len(deduped_ep) != len(original_ep):
                target["exclude_paths"] = deduped_ep
                ep_removed_total += len(original_ep) - len(deduped_ep)
                changed = True

        cleaned_servers.append(target)

    if changed:
        sm.apply_changes(updates={"media_servers": cleaned_servers})
        parts = []
        if pm_removed_total:
            parts.append(f"{pm_removed_total} duplicate path_mapping row(s)")
        if ep_removed_total:
            parts.append(f"{ep_removed_total} duplicate exclude_path row(s)")
        notes.append(f"v9: removed {' + '.join(parts)} introduced by the v7+v8 double-copy bug")

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
            "The legacy GPU_THREADS ({!r}) or FFMPEG_THREADS ({!r}) environment variable wasn't a valid number. "
            "Falling back to defaults (1 GPU worker, 2 ffmpeg threads). "
            "Open Settings → GPU after startup and configure the values you actually want.",
            gpu_threads_str,
            ffmpeg_threads_str,
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
