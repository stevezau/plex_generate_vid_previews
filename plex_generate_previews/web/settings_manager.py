"""
Settings manager for persistent configuration.

Manages user-configurable settings stored in /config/settings.json.
These settings override environment variables when set.
"""

import json
import os
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Optional, Dict, Any, List
from loguru import logger


class SettingsManager:
    """Manages persistent settings stored in a JSON file."""

    def __init__(self, config_dir: str = None):
        if config_dir is None:
            config_dir = os.environ.get("CONFIG_DIR", "/config")
        self.config_dir = Path(config_dir)
        self.settings_file = self.config_dir / "settings.json"
        self.client_id_file = self.config_dir / "client_id"
        self.setup_state_file = self.config_dir / "setup_state.json"
        self._settings: Dict[str, Any] = {}
        self._setup_state: Dict[str, Any] = {}
        self._client_id: Optional[str] = None
        self._lock = threading.RLock()
        self._load()
        self._load_setup_state()

    def _load(self) -> None:
        """Load settings from file."""
        if self.settings_file.exists():
            try:
                with open(self.settings_file, "r") as f:
                    self._settings = json.load(f)
                logger.debug(f"Loaded settings from {self.settings_file}")
            except Exception as e:
                logger.error(f"Failed to load settings: {e}")
                self._settings = {}
        else:
            self._settings = {}

    def _save(self) -> None:
        """Save settings to file atomically.

        Writes to a temporary file first, then replaces the target to
        avoid corruption if the process is killed mid-write.
        """
        try:
            self.config_dir.mkdir(parents=True, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(dir=str(self.config_dir), suffix=".tmp")
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump(self._settings, f, indent=2)
                os.replace(tmp_path, str(self.settings_file))
            except BaseException:
                os.unlink(tmp_path)
                raise
            try:
                self.settings_file.chmod(0o600)
            except OSError:
                pass
            logger.debug(f"Saved settings to {self.settings_file}")
        except Exception as e:
            logger.error(f"Failed to save settings: {e}")
            raise

    def _load_setup_state(self) -> None:
        """Load setup wizard state from file."""
        if self.setup_state_file.exists():
            try:
                with open(self.setup_state_file, "r") as f:
                    self._setup_state = json.load(f)
                logger.debug(f"Loaded setup state from {self.setup_state_file}")
            except Exception as e:
                logger.error(f"Failed to load setup state: {e}")
                self._setup_state = {}
        else:
            self._setup_state = {}

    def _save_setup_state(self) -> None:
        """Save setup wizard state to file atomically."""
        try:
            self.config_dir.mkdir(parents=True, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(dir=str(self.config_dir), suffix=".tmp")
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump(self._setup_state, f, indent=2)
                os.replace(tmp_path, str(self.setup_state_file))
            except BaseException:
                os.unlink(tmp_path)
                raise
            logger.debug(f"Saved setup state to {self.setup_state_file}")
        except Exception as e:
            logger.error(f"Failed to save setup state: {e}")
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

    def get_all(self) -> Dict[str, Any]:
        """Get all settings."""
        with self._lock:
            return self._settings.copy()

    def update(self, settings: Dict[str, Any]) -> None:
        """Update multiple settings at once."""
        with self._lock:
            self._settings.update(settings)
            self._save()

    def delete(self, key: str) -> None:
        """Delete a setting."""
        with self._lock:
            if key in self._settings:
                del self._settings[key]
                self._save()

    # Convenience methods for common settings
    @property
    def plex_url(self) -> Optional[str]:
        return self.get("plex_url") or os.environ.get("PLEX_URL")

    @plex_url.setter
    def plex_url(self, value: str) -> None:
        self.set("plex_url", value)

    @property
    def plex_token(self) -> Optional[str]:
        return self.get("plex_token") or os.environ.get("PLEX_TOKEN")

    @plex_token.setter
    def plex_token(self, value: str) -> None:
        self.set("plex_token", value)

    @property
    def plex_config_folder(self) -> Optional[str]:
        return self.get("plex_config_folder") or os.environ.get(
            "PLEX_CONFIG_FOLDER", "/plex"
        )

    @plex_config_folder.setter
    def plex_config_folder(self, value: str) -> None:
        self.set("plex_config_folder", value)

    @property
    def media_path(self) -> Optional[str]:
        return self.get("media_path") or os.environ.get("MEDIA_PATH")

    @media_path.setter
    def media_path(self, value: str) -> None:
        self.set("media_path", value)

    @property
    def plex_videos_path_mapping(self) -> Optional[str]:
        return self.get("plex_videos_path_mapping") or os.environ.get(
            "PLEX_VIDEOS_PATH_MAPPING"
        )

    @plex_videos_path_mapping.setter
    def plex_videos_path_mapping(self, value: str) -> None:
        self.set("plex_videos_path_mapping", value)

    @property
    def plex_local_videos_path_mapping(self) -> Optional[str]:
        return self.get("plex_local_videos_path_mapping") or os.environ.get(
            "PLEX_LOCAL_VIDEOS_PATH_MAPPING"
        )

    @plex_local_videos_path_mapping.setter
    def plex_local_videos_path_mapping(self, value: str) -> None:
        self.set("plex_local_videos_path_mapping", value)

    @property
    def thumbnail_interval(self) -> int:
        return int(
            self.get("thumbnail_interval") or os.environ.get("THUMBNAIL_INTERVAL", "2")
        )

    @thumbnail_interval.setter
    def thumbnail_interval(self, value: int) -> None:
        self.set("thumbnail_interval", value)

    @property
    def gpu_threads(self) -> int:
        return int(self.get("gpu_threads") or os.environ.get("GPU_THREADS", "1"))

    @gpu_threads.setter
    def gpu_threads(self, value: int) -> None:
        self.set("gpu_threads", value)

    @property
    def cpu_threads(self) -> int:
        return int(self.get("cpu_threads") or os.environ.get("CPU_THREADS", "1"))

    @cpu_threads.setter
    def cpu_threads(self, value: int) -> None:
        self.set("cpu_threads", value)

    @property
    def thumbnail_quality(self) -> int:
        """Thumbnail quality (1-10, default 4)."""
        return int(
            self.get("thumbnail_quality") or os.environ.get("THUMBNAIL_QUALITY", "4")
        )

    @thumbnail_quality.setter
    def thumbnail_quality(self, value: int) -> None:
        self.set("thumbnail_quality", value)

    @property
    def selected_libraries(self) -> List[str]:
        """List of selected library IDs."""
        return self.get("selected_libraries", [])

    @selected_libraries.setter
    def selected_libraries(self, value: List[str]) -> None:
        self.set("selected_libraries", value)

    @property
    def plex_name(self) -> Optional[str]:
        """Name of the connected Plex server."""
        return self.get("plex_name")

    @plex_name.setter
    def plex_name(self, value: str) -> None:
        self.set("plex_name", value)

    # =========================================================================
    # Configuration Status Methods
    # =========================================================================

    def is_configured(self) -> bool:
        """Check if the application is fully configured.

        Returns True if at least plex_url and plex_token are set
        (either in settings or environment variables).
        """
        return bool(self.plex_url and self.plex_token)

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
            logger.warning(f"Failed to validate Plex token: {e}")
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
                logger.warning(f"Failed to load client ID: {e}")

        # Generate new ID
        self._client_id = f"plex-preview-generator-{uuid.uuid4()}"

        # Save to file
        try:
            self.config_dir.mkdir(parents=True, exist_ok=True)
            self.client_id_file.write_text(self._client_id)
            logger.info(f"Generated new client identifier: {self._client_id}")
        except Exception as e:
            logger.warning(f"Failed to save client ID: {e}")

        return self._client_id

    # =========================================================================
    # Setup Wizard State
    # =========================================================================

    def get_setup_state(self) -> Dict[str, Any]:
        """Get the current setup wizard state."""
        with self._lock:
            return self._setup_state.copy()

    def set_setup_state(self, step: int, data: Dict[str, Any]) -> None:
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
                    logger.warning(f"Failed to delete setup state file: {e}")

    def complete_setup(self) -> None:
        """Mark setup as complete and clear setup state."""
        with self._lock:
            self.set("setup_complete", True)
            self.clear_setup_state()
            logger.info("Setup wizard completed")

    def is_setup_complete(self) -> bool:
        """Check if setup wizard has been completed."""
        with self._lock:
            return self.get("setup_complete", False) or self.is_configured()


# Global instance
_settings_manager: Optional[SettingsManager] = None
_settings_lock = threading.Lock()


def get_settings_manager(config_dir: str = None) -> SettingsManager:
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
