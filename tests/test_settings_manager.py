"""
Tests for the SettingsManager class.

Tests persistent settings storage and configuration status.
"""

import json
import pytest
from pathlib import Path


class TestSettingsManager:
    """Tests for SettingsManager."""

    @pytest.fixture
    def temp_config_dir(self, tmp_path):
        """Create a temporary config directory."""
        return str(tmp_path)

    @pytest.fixture
    def settings_manager(self, temp_config_dir):
        """Create a SettingsManager with temporary directory."""
        from plex_generate_previews.web.settings_manager import SettingsManager

        return SettingsManager(config_dir=temp_config_dir)

    def test_init_creates_empty_settings(self, settings_manager, temp_config_dir):
        """Test that init creates empty settings when no file exists."""
        assert settings_manager.get_all() == {}

    def test_set_and_get(self, settings_manager):
        """Test setting and getting a value."""
        settings_manager.set("test_key", "test_value")
        assert settings_manager.get("test_key") == "test_value"

    def test_get_with_default(self, settings_manager):
        """Test get with default value."""
        assert settings_manager.get("nonexistent", "default") == "default"

    def test_update_multiple(self, settings_manager):
        """Test updating multiple settings at once."""
        settings_manager.update({"key1": "value1", "key2": "value2", "key3": 123})
        assert settings_manager.get("key1") == "value1"
        assert settings_manager.get("key2") == "value2"
        assert settings_manager.get("key3") == 123

    def test_delete(self, settings_manager):
        """Test deleting a setting."""
        settings_manager.set("to_delete", "value")
        assert settings_manager.get("to_delete") == "value"
        settings_manager.delete("to_delete")
        assert settings_manager.get("to_delete") is None

    def test_persistence(self, temp_config_dir):
        """Test that settings persist across instances."""
        from plex_generate_previews.web.settings_manager import SettingsManager

        # Create manager and set value
        manager1 = SettingsManager(config_dir=temp_config_dir)
        manager1.set("persistent_key", "persistent_value")

        # Create new manager and verify value persists
        manager2 = SettingsManager(config_dir=temp_config_dir)
        assert manager2.get("persistent_key") == "persistent_value"

    def test_settings_file_created(self, settings_manager, temp_config_dir):
        """Test that settings file is created on save."""
        settings_manager.set("test", "value")
        settings_file = Path(temp_config_dir) / "settings.json"
        assert settings_file.exists()

        with open(settings_file) as f:
            data = json.load(f)
        assert data["test"] == "value"


class TestSettingsManagerProperties:
    """Tests for SettingsManager property methods."""

    @pytest.fixture
    def settings_manager(self, tmp_path):
        """Create a SettingsManager with temporary directory."""
        from plex_generate_previews.web.settings_manager import SettingsManager

        return SettingsManager(config_dir=str(tmp_path))

    def test_plex_url_property(self, settings_manager):
        """Test plex_url property."""
        settings_manager.plex_url = "http://localhost:32400"
        assert settings_manager.plex_url == "http://localhost:32400"

    def test_plex_token_property(self, settings_manager):
        """Test plex_token property."""
        settings_manager.plex_token = "test-token-123"
        assert settings_manager.plex_token == "test-token-123"

    def test_gpu_threads_property(self, settings_manager):
        """Test gpu_threads property with int conversion."""
        settings_manager.gpu_threads = 4
        assert settings_manager.gpu_threads == 4

    def test_thumbnail_interval_property(self, settings_manager):
        """Test thumbnail_interval property."""
        settings_manager.thumbnail_interval = 5
        assert settings_manager.thumbnail_interval == 5


class TestSettingsManagerConfigStatus:
    """Tests for configuration status methods."""

    @pytest.fixture
    def settings_manager(self, tmp_path, monkeypatch):
        """Create a SettingsManager with clean environment."""
        from plex_generate_previews.web.settings_manager import SettingsManager

        # Clear environment variables
        monkeypatch.delenv("PLEX_URL", raising=False)
        monkeypatch.delenv("PLEX_TOKEN", raising=False)
        return SettingsManager(config_dir=str(tmp_path))

    def test_is_configured_false_when_empty(self, settings_manager):
        """Test is_configured returns False when not configured."""
        assert settings_manager.is_configured() is False

    def test_is_configured_true_when_set(self, settings_manager):
        """Test is_configured returns True when plex_url and plex_token are set."""
        settings_manager.set("plex_url", "http://localhost:32400")
        settings_manager.set("plex_token", "test-token")
        assert settings_manager.is_configured() is True

    def test_is_plex_authenticated_false(self, settings_manager):
        """Test is_plex_authenticated returns False when no token."""
        assert settings_manager.is_plex_authenticated() is False

    def test_is_plex_authenticated_true(self, settings_manager):
        """Test is_plex_authenticated returns True when token set."""
        settings_manager.set("plex_token", "test-token")
        assert settings_manager.is_plex_authenticated() is True


class TestClientIdentifier:
    """Tests for client identifier management."""

    def test_get_client_identifier_generates_id(self, tmp_path):
        """Test that get_client_identifier generates a new ID."""
        from plex_generate_previews.web.settings_manager import SettingsManager

        manager = SettingsManager(config_dir=str(tmp_path))

        client_id = manager.get_client_identifier()
        assert client_id.startswith("plex-preview-generator-")

    def test_client_identifier_persists(self, tmp_path):
        """Test that client identifier persists across instances."""
        from plex_generate_previews.web.settings_manager import SettingsManager

        manager1 = SettingsManager(config_dir=str(tmp_path))
        client_id1 = manager1.get_client_identifier()

        manager2 = SettingsManager(config_dir=str(tmp_path))
        client_id2 = manager2.get_client_identifier()

        assert client_id1 == client_id2


class TestSetupState:
    """Tests for setup wizard state management."""

    @pytest.fixture
    def settings_manager(self, tmp_path):
        """Create a SettingsManager with temporary directory."""
        from plex_generate_previews.web.settings_manager import SettingsManager

        return SettingsManager(config_dir=str(tmp_path))

    def test_get_setup_state_empty(self, settings_manager):
        """Test get_setup_state returns empty dict initially."""
        state = settings_manager.get_setup_state()
        assert state == {}

    def test_set_setup_state(self, settings_manager):
        """Test set_setup_state saves state."""
        settings_manager.set_setup_state(2, {"server": "test-server"})
        state = settings_manager.get_setup_state()
        assert state["step"] == 2
        assert state["data"]["server"] == "test-server"

    def test_get_setup_step(self, settings_manager):
        """Test get_setup_step returns current step."""
        assert settings_manager.get_setup_step() == 0  # Not started
        settings_manager.set_setup_state(3, {})
        assert settings_manager.get_setup_step() == 3

    def test_clear_setup_state(self, settings_manager, tmp_path):
        """Test clear_setup_state removes state."""
        settings_manager.set_setup_state(2, {"data": "test"})
        settings_manager.clear_setup_state()

        assert settings_manager.get_setup_state() == {}
        setup_file = Path(tmp_path) / "setup_state.json"
        assert not setup_file.exists()

    def test_complete_setup(self, settings_manager):
        """Test complete_setup marks setup as complete."""
        settings_manager.set_setup_state(4, {"final": "data"})
        settings_manager.complete_setup()

        assert settings_manager.is_setup_complete() is True
        assert settings_manager.get_setup_state() == {}
