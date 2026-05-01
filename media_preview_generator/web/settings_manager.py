"""Settings manager for persistent configuration.

Manages user-configurable settings stored in /config/settings.json.
Settings are the single source of truth for all application-level
configuration.  Migration/upgrade logic lives in ``upgrade.py``.
"""

import copy
import json
import os
import threading
import uuid
from pathlib import Path
from typing import Any

from loguru import logger


def _distribute_gpu_threads_into_dict(settings: dict[str, Any], value: int) -> None:
    """Distribute a total GPU worker count across enabled GPUs in ``gpu_config``.

    Mutates ``settings`` in place (same rules as ``SettingsManager._distribute_gpu_threads``).

    Args:
        settings: Settings dict containing ``gpu_config``.
        value: Total workers to assign across enabled GPUs.

    """
    raw = settings.get("gpu_config")
    if not isinstance(raw, list):
        return
    config = [e for e in raw if isinstance(e, dict)]
    enabled = [e for e in config if e.get("enabled", True)]
    if not enabled:
        return
    per_gpu = max(0, value // len(enabled))
    remainder = max(0, value - per_gpu * len(enabled))
    for entry in config:
        if entry.get("enabled", True):
            entry["workers"] = per_gpu
            if remainder > 0:
                entry["workers"] += 1
                remainder -= 1
    settings["gpu_config"] = config


def preview_settings_after_update(base: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
    """Return settings dict after applying the same merge rules as ``SettingsManager.update``.

    Produces the effective configuration state without mutating ``base``.

    Args:
        base: Current settings (e.g. from ``get_all()``).
        updates: Incoming partial update (may include ``gpu_threads``).

    Returns:
        Deep copy of ``base`` with ``updates`` applied.

    """
    out = copy.deepcopy(base)
    to_apply = {k: v for k, v in updates.items() if k != "gpu_threads"}
    out.update(to_apply)
    if "gpu_threads" in updates:
        _distribute_gpu_threads_into_dict(out, int(updates["gpu_threads"]))
    return out


class SettingsManager:
    """Manages persistent settings stored in a JSON file."""

    def __init__(self, config_dir: str | None = None):
        """Initialize settings manager with config directory."""
        if config_dir is None:
            config_dir = os.environ.get("CONFIG_DIR", "/config")
        self.config_dir = Path(config_dir)
        self.settings_file = self.config_dir / "settings.json"
        self.client_id_file = self.config_dir / "client_id"
        self.setup_state_file = self.config_dir / "setup_state.json"
        self._settings: dict[str, Any] = {}
        self._setup_state: dict[str, Any] = {}
        self._client_id: str | None = None
        self._lock = threading.RLock()
        self._load()
        self._load_setup_state()

    def _load(self) -> None:
        """Load settings from file."""
        if self.settings_file.exists():
            try:
                with open(self.settings_file) as f:
                    self._settings = json.load(f)
                logger.debug("Loaded settings from {}", self.settings_file)
            except Exception as e:
                # J4: when a .bak exists from a prior atomic_json_save_with_backup,
                # surface its path in the log so users have a one-line recovery
                # hint without having to dig through docs.
                bak = self.settings_file.with_suffix(self.settings_file.suffix + ".bak")
                bak_hint = (
                    f" A backup is at {bak} (mv it to {self.settings_file} and restart to recover)."
                    if bak.exists()
                    else ""
                )
                logger.error(
                    "Could not read settings file at {} ({}: {}).{}"
                    " Falling back to defaults — your previously-saved configuration will not be loaded. "
                    "The file may be corrupted or have wrong permissions; check it is valid JSON and readable "
                    "by this process. Back up the file before any manual edits.",
                    self.settings_file,
                    type(e).__name__,
                    e,
                    bak_hint,
                )
                self._settings = {}
        else:
            self._settings = {}
        self._migrate_global_plex_webhook_to_per_server()

    def _migrate_global_plex_webhook_to_per_server(self) -> None:
        """One-shot migration: move legacy global Plex webhook keys onto the Plex server entry.

        Pre-multi-server installs stored ``plex_webhook_public_url`` /
        ``plex_webhook_enabled`` at the top level. With multi-server they need
        to live on each Plex server's ``output`` so the registration can use
        the right per-server token. Multi-server is brand-new on this branch
        so any settings.json hitting this code holds at most one Plex server;
        the migration is a straight copy.
        """
        legacy_url = (self._settings.get("plex_webhook_public_url") or "").strip()
        legacy_enabled = self._settings.get("plex_webhook_enabled")
        if not legacy_url and legacy_enabled is None:
            return  # nothing to migrate

        media_servers = self._settings.get("media_servers") or []
        plex_entry = next(
            (s for s in media_servers if isinstance(s, dict) and s.get("type") == "plex"),
            None,
        )
        if plex_entry is not None:
            output = plex_entry.setdefault("output", {})
            if legacy_url and not output.get("webhook_public_url"):
                output["webhook_public_url"] = legacy_url
            if legacy_enabled is not None and "webhook_enabled" not in output:
                output["webhook_enabled"] = bool(legacy_enabled)
            logger.info(
                "Migrated legacy global plex_webhook_* keys onto Plex server {!r}",
                plex_entry.get("name") or plex_entry.get("id"),
            )

        self._settings.pop("plex_webhook_public_url", None)
        self._settings.pop("plex_webhook_enabled", None)
        self._save()

    def _save(self) -> None:
        """Save settings to file atomically."""
        try:
            from ..utils import atomic_json_save_with_backup

            atomic_json_save_with_backup(str(self.settings_file), self._settings, permissions=0o600)
            logger.debug("Saved settings to {}", self.settings_file)
            try:
                from ..config import clear_config_cache

                clear_config_cache()
            except (ImportError, AttributeError):
                pass
        except Exception as e:
            logger.error(
                "Could not save settings to {} ({}: {}). "
                "Your changes were NOT persisted and will be lost on restart. Check the config "
                "directory exists and is writable, and that the disk isn't full.",
                self.settings_file,
                type(e).__name__,
                e,
            )
            raise

    def _load_setup_state(self) -> None:
        """Load setup wizard state from file."""
        if self.setup_state_file.exists():
            try:
                with open(self.setup_state_file) as f:
                    self._setup_state = json.load(f)
                logger.debug("Loaded setup state from {}", self.setup_state_file)
            except Exception as e:
                logger.error(
                    "Could not read setup-state file at {} ({}: {}). "
                    "The setup wizard will treat this as a fresh install — you may be asked "
                    "to re-complete first-run setup. Check the file is valid JSON and readable.",
                    self.setup_state_file,
                    type(e).__name__,
                    e,
                )
                self._setup_state = {}
        else:
            self._setup_state = {}

    def _save_setup_state(self) -> None:
        """Save setup wizard state to file atomically."""
        try:
            from ..utils import atomic_json_save_with_backup

            atomic_json_save_with_backup(str(self.setup_state_file), self._setup_state)
            logger.debug("Saved setup state to {}", self.setup_state_file)
        except Exception as e:
            logger.error(
                "Could not save setup-state to {} ({}: {}). "
                "Wizard progress was NOT persisted; check the config directory is writable.",
                self.setup_state_file,
                type(e).__name__,
                e,
            )
            raise

    def get(self, key: str, default: Any = None) -> Any:
        """Get a setting value."""
        with self._lock:
            return self._settings.get(key, default)

    def set(self, key: str, value: Any) -> None:
        """Set a setting value and save."""
        with self._lock:
            self._settings[key] = value
            self._save()

    def get_all(self) -> dict[str, Any]:
        """Get all settings."""
        with self._lock:
            return self._settings.copy()

    def update(self, settings: dict[str, Any]) -> None:
        """Update multiple settings at once.

        ``gpu_threads`` is special-cased: its value is distributed across
        enabled GPUs in ``gpu_config`` rather than stored as a raw int.
        The caller's dict is not modified.
        """
        with self._lock:
            to_apply = {k: v for k, v in settings.items() if k != "gpu_threads"}
            self._settings.update(to_apply)
            if "gpu_threads" in settings:
                self._distribute_gpu_threads(int(settings["gpu_threads"]))
            self._save()

    def delete(self, key: str) -> None:
        """Delete a setting."""
        with self._lock:
            if key in self._settings:
                del self._settings[key]
                self._save()

    def apply_changes(
        self,
        updates: dict[str, Any] = None,
        deletes: list[str] = None,
    ) -> None:
        """Apply a batch of updates and deletions atomically.

        Unlike ``update()``, no special-casing is applied — values are
        written directly.  Intended for migrations and bulk operations.

        Args:
            updates: Key/value pairs to set.
            deletes: Keys to remove.

        """
        with self._lock:
            if updates:
                self._settings.update(updates)
            if deletes:
                for key in deletes:
                    self._settings.pop(key, None)
            self._save()

    # =========================================================================
    # Convenience properties (settings.json is the sole source)
    #
    # Plex-flavoured fields (plex_url / plex_token / plex_verify_ssl /
    # plex_config_folder / selected_libraries) are derived from the first
    # enabled Plex entry in ``media_servers`` when present, falling back to
    # the legacy top-level keys for installs that haven't been migrated yet.
    # See ``config.derive_legacy_plex_view`` for the projection rules.
    # =========================================================================

    def _plex_view(self) -> dict:
        """Return the legacy-flat view derived from ``media_servers[0]``.

        Cached locally per call rather than at construction time so any
        update via ``set`` / ``update`` / ``apply_changes`` is picked up
        immediately by the next read.
        """
        # Local import to avoid a config↔settings_manager import cycle.
        from ..config import derive_legacy_plex_view

        return derive_legacy_plex_view(self.get("media_servers") or [])

    @property
    def plex_url(self) -> str | None:
        """Plex server URL."""
        return self._plex_view().get("plex_url") or self.get("plex_url")

    @plex_url.setter
    def plex_url(self, value: str) -> None:
        self.set("plex_url", value)

    @property
    def plex_token(self) -> str | None:
        """Plex authentication token."""
        return self._plex_view().get("plex_token") or self.get("plex_token")

    @plex_token.setter
    def plex_token(self, value: str) -> None:
        self.set("plex_token", value)

    @property
    def plex_config_folder(self) -> str | None:
        """Plex configuration folder path."""
        return self._plex_view().get("plex_config_folder") or self.get("plex_config_folder") or "/plex"

    @plex_config_folder.setter
    def plex_config_folder(self, value: str) -> None:
        self.set("plex_config_folder", value)

    @property
    def plex_verify_ssl(self) -> bool:
        """Whether to verify Plex server TLS certificates."""
        view = self._plex_view()
        if "plex_verify_ssl" in view:
            return bool(view["plex_verify_ssl"])
        val = self.get("plex_verify_ssl")
        if val is not None:
            return bool(val)
        return True

    @plex_verify_ssl.setter
    def plex_verify_ssl(self, value: bool) -> None:
        self.set("plex_verify_ssl", bool(value))

    @property
    def media_path(self) -> str | None:
        """Local media root path."""
        return self.get("media_path")

    @media_path.setter
    def media_path(self, value: str) -> None:
        self.set("media_path", value)

    @property
    def thumbnail_interval(self) -> int:
        """Seconds between thumbnail captures."""
        return int(self.get("thumbnail_interval") or 2)

    @thumbnail_interval.setter
    def thumbnail_interval(self, value: int) -> None:
        self.set("thumbnail_interval", value)

    @property
    def gpu_config(self) -> list[dict[str, Any]]:
        """Per-GPU configuration list.

        Each entry has: device, name, type, enabled, workers, ffmpeg_threads.
        Always returns a list (never ``None``).
        """
        val = self.get("gpu_config")
        if not isinstance(val, list):
            return []
        return [e for e in val if isinstance(e, dict)]

    @gpu_config.setter
    def gpu_config(self, value: list[dict[str, Any]]) -> None:
        self.set("gpu_config", value)

    @property
    def gpu_threads(self) -> int:
        """Total GPU worker threads (computed from gpu_config)."""
        config = self.gpu_config
        if not config:
            return 0
        return sum(entry.get("workers", 0) for entry in config if entry.get("enabled", True))

    @gpu_threads.setter
    def gpu_threads(self, value: int) -> None:
        with self._lock:
            self._distribute_gpu_threads(int(value))
            self._save()

    def _distribute_gpu_threads(self, value: int) -> None:
        """Distribute a total worker count across enabled GPUs in gpu_config.

        Must be called while holding ``self._lock``.  Does nothing when
        there are no enabled GPUs or gpu_config is missing/invalid.
        """
        _distribute_gpu_threads_into_dict(self._settings, value)

    @property
    def cpu_threads(self) -> int:
        """Number of CPU worker threads."""
        val = self.get("cpu_threads")
        return int(val) if val is not None else 1

    @cpu_threads.setter
    def cpu_threads(self, value: int) -> None:
        self.set("cpu_threads", value)

    @property
    def thumbnail_quality(self) -> int:
        """Thumbnail quality (1-10, default 4)."""
        return int(self.get("thumbnail_quality") or 4)

    @thumbnail_quality.setter
    def thumbnail_quality(self, value: int) -> None:
        self.set("thumbnail_quality", value)

    @property
    def tonemap_algorithm(self) -> str:
        """HDR-to-SDR tone mapping algorithm (default: hable)."""
        return str(self.get("tonemap_algorithm") or "hable").strip().lower()

    @tonemap_algorithm.setter
    def tonemap_algorithm(self, value: str) -> None:
        self.set("tonemap_algorithm", str(value).strip().lower())

    @property
    def selected_libraries(self) -> list[str]:
        """List of selected library IDs (Plex)."""
        view = self._plex_view()
        if view.get("selected_libraries"):
            return view["selected_libraries"]
        return self.get("selected_libraries", [])

    @selected_libraries.setter
    def selected_libraries(self, value: list[str]) -> None:
        self.set("selected_libraries", value)

    @property
    def plex_name(self) -> str | None:
        """Name of the connected Plex server."""
        return self.get("plex_name")

    @plex_name.setter
    def plex_name(self, value: str) -> None:
        self.set("plex_name", value)

    @property
    def processing_paused(self) -> bool:
        """Global processing pause: when True, no new jobs start and dispatch stops (soft)."""
        return bool(self.get("processing_paused", False))

    @processing_paused.setter
    def processing_paused(self, value: bool) -> None:
        self.set("processing_paused", bool(value))

    @property
    def dismissed_notifications(self) -> list[str]:
        """IDs of notifications the user has permanently dismissed.

        Keyed by stable notification ID (e.g. ``"vulkan_software_fallback"``)
        so the warning message can evolve between releases without
        un-suppressing the dismissal.  Session-only dismissals live in
        memory in the notifications module and are not persisted here.
        """
        val = self.get("dismissed_notifications", [])
        if not isinstance(val, list):
            return []
        return [str(entry) for entry in val if isinstance(entry, str)]

    @dismissed_notifications.setter
    def dismissed_notifications(self, value: list[str]) -> None:
        cleaned = [str(entry) for entry in (value or []) if isinstance(entry, str)]
        self.set("dismissed_notifications", cleaned)

    def dismiss_notification_permanent(self, notification_id: str) -> None:
        """Append a notification ID to the persistent dismissal list.

        Idempotent: calling twice with the same ID is a no-op.
        """
        with self._lock:
            current = list(self.dismissed_notifications)
            if notification_id not in current:
                current.append(notification_id)
                self._settings["dismissed_notifications"] = current
                self._save()

    def undismiss_notification(self, notification_id: str) -> None:
        """Remove a notification ID from the persistent dismissal list.

        Used by the "reset dismissed notifications" UI button.  Idempotent.
        """
        with self._lock:
            current = list(self.dismissed_notifications)
            if notification_id in current:
                current = [n for n in current if n != notification_id]
                self._settings["dismissed_notifications"] = current
                self._save()

    def reset_dismissed_notifications(self) -> None:
        """Clear all persistently-dismissed notifications."""
        with self._lock:
            if self._settings.get("dismissed_notifications"):
                self._settings["dismissed_notifications"] = []
                self._save()

    # =========================================================================
    # Configuration Status Methods
    # =========================================================================

    def is_configured(self) -> bool:
        """Check if the application is fully configured.

        Returns True when at least one media server is configured well
        enough to dispatch previews to:

        * Legacy single-Plex install: ``plex_url`` + ``plex_token`` set.
        * Multi-server install: any enabled entry in ``media_servers``
          with a non-empty ``url`` and a vendor-appropriate auth shape:
          - Plex: ``auth.token``
          - Emby / Jellyfin: ``auth.api_key`` (api-key flow) OR
            ``auth.access_token`` (password / QuickConnect flow)

        The Plex fast-path stays so existing pre-multi-server installs
        keep passing without touching the new check; the new path
        unblocks Emby- or Jellyfin-only first-run setups.
        """
        if self.plex_url and self.plex_token:
            return True

        servers = self.get("media_servers") or []
        if not isinstance(servers, list):
            return False

        for entry in servers:
            if not isinstance(entry, dict):
                continue
            if entry.get("enabled") is False:
                continue
            url = (entry.get("url") or "").strip()
            if not url:
                continue
            stype = (entry.get("type") or "").lower()
            auth = entry.get("auth") or {}
            if stype == "plex" and (auth.get("token") or "").strip():
                return True
            if stype in ("emby", "jellyfin"):
                # Emby/Jellyfin support two auth flows — api-key (direct
                # paste) or password/QuickConnect (returns an access_token
                # + user_id at probe time). Either yields a usable
                # credential, so accept either.
                api_key = (auth.get("api_key") or "").strip()
                access_token = (auth.get("access_token") or "").strip()
                if api_key or access_token:
                    return True
        return False

    def is_plex_authenticated(self) -> bool:
        """Check if Plex authentication is configured."""
        return bool(self.plex_token)

    def validate_plex_token(self) -> bool:
        """Validate the Plex token by testing connection.

        Returns True if the token is valid and can connect to plex.tv.
        """
        if not self.plex_token:
            return False

        try:
            import requests

            response = requests.get(
                "https://plex.tv/api/v2/user",
                headers={
                    "X-Plex-Token": self.plex_token,
                    "Accept": "application/json",
                },
                timeout=10,
            )
            return response.status_code == 200
        except Exception as e:
            logger.warning(
                "Could not check whether the saved Plex token is still valid ({}: {}). "
                "If you can't open the dashboard or libraries, re-authenticate via the Setup Wizard.",
                type(e).__name__,
                e,
            )
            return False

    # =========================================================================
    # Client Identifier (for Plex OAuth)
    # =========================================================================

    def get_client_identifier(self) -> str:
        """Get or generate a unique client identifier.

        This ID is used for Plex OAuth and should be consistent
        across app restarts. Format: plex-preview-generator-<uuid>
        """
        if self._client_id:
            return self._client_id

        # Try to load from file
        if self.client_id_file.exists():
            try:
                self._client_id = self.client_id_file.read_text().strip()
                if self._client_id:
                    return self._client_id
            except Exception as e:
                logger.warning(
                    "Could not read the saved client identifier from {} ({}: {}). "
                    "A new one will be generated — Plex will treat this as a fresh client "
                    "the next time you sign in.",
                    self.client_id_file,
                    type(e).__name__,
                    e,
                )

        # Generate new ID
        self._client_id = f"plex-preview-generator-{uuid.uuid4()}"

        # Save to file
        try:
            self.config_dir.mkdir(parents=True, exist_ok=True)
            self.client_id_file.write_text(self._client_id)
            logger.info("Generated new client identifier: {}", self._client_id)
        except Exception as e:
            logger.warning(
                "Could not save the new client identifier to {} ({}: {}). "
                "It'll keep working for this session, but a new one will be generated on next start. "
                "Check the config directory is writable (Docker: confirm volume mount permissions and PUID/PGID).",
                self.client_id_file,
                type(e).__name__,
                e,
            )

        return self._client_id

    # =========================================================================
    # Setup Wizard State
    # =========================================================================

    def get_setup_state(self) -> dict[str, Any]:
        """Get the current setup wizard state."""
        with self._lock:
            return self._setup_state.copy()

    def set_setup_state(self, step: int, data: dict[str, Any]) -> None:
        """Save setup wizard progress.

        Args:
            step: Current step number (1-4)
            data: Step-specific data to save

        """
        with self._lock:
            self._setup_state = {
                "step": step,
                "data": data,
            }
            self._save_setup_state()

    def get_setup_step(self) -> int:
        """Get the current setup wizard step (1-4, or 0 if not started)."""
        with self._lock:
            return self._setup_state.get("step", 0)

    def clear_setup_state(self) -> None:
        """Clear setup wizard state (called when setup is complete)."""
        with self._lock:
            self._setup_state = {}
            if self.setup_state_file.exists():
                try:
                    self.setup_state_file.unlink()
                except Exception as e:
                    logger.warning(
                        "Could not delete the setup-state file at {} ({}: {}). "
                        "Setup is complete and the app will keep working — the leftover file is harmless and "
                        "will be ignored. If it bothers you, remove it manually from the config directory.",
                        self.setup_state_file,
                        type(e).__name__,
                        e,
                    )

    def complete_setup(self) -> None:
        """Mark setup as complete and clear setup state."""
        with self._lock:
            self.set("setup_complete", True)
            self.clear_setup_state()
            logger.info("Setup wizard completed")

    def is_setup_complete(self) -> bool:
        """Check if setup wizard has been completed.

        Returns True when:
        - The setup_complete flag was explicitly set (wizard finished), or
        - The app is configured (plex_url + plex_token) AND the wizard
          is not actively in progress.

        This prevents a partial wizard run (e.g. Step 2 saved plex_url/token
        but user never finished) from being treated as complete.
        """
        with self._lock:
            if self.get("setup_complete", False):
                return True
            if self._setup_state.get("step", 0) > 0:
                return False
            return self.is_configured()


# Global instance
_settings_manager: SettingsManager | None = None
_settings_lock = threading.Lock()


def get_settings_manager(config_dir: str | None = None) -> SettingsManager:
    """Get the global settings manager instance.

    Thread-safe singleton. If config_dir is provided and different from
    current instance, creates a new instance with the new config_dir.
    """
    global _settings_manager
    with _settings_lock:
        if _settings_manager is None:
            _settings_manager = SettingsManager(config_dir)
        elif config_dir is not None and str(_settings_manager.config_dir) != config_dir:
            # Re-initialize with new config_dir
            _settings_manager = SettingsManager(config_dir)
    return _settings_manager


def reset_settings_manager() -> None:
    """Reset the global settings manager. Used for testing."""
    global _settings_manager
    with _settings_lock:
        _settings_manager = None
