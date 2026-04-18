"""
Tests for the SettingsManager class.

Tests persistent settings storage and configuration status.
"""

import json
from pathlib import Path

import pytest


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


class TestPreviewSettingsAfterUpdate:
    """preview_settings_after_update matches SettingsManager.update for gpu_threads."""

    def test_gpu_threads_distribution_matches_update(self, tmp_path):
        from plex_generate_previews.config import validate_processing_thread_totals
        from plex_generate_previews.web.settings_manager import (
            SettingsManager,
            preview_settings_after_update,
        )

        sm = SettingsManager(config_dir=str(tmp_path))
        sm.gpu_config = [
            {"device": "/a", "enabled": True, "workers": 1, "ffmpeg_threads": 2},
            {"device": "/b", "enabled": True, "workers": 1, "ffmpeg_threads": 2},
        ]
        sm.set("cpu_threads", 1)
        base = sm.get_all()
        merged = preview_settings_after_update(base, {"gpu_threads": 3})
        assert validate_processing_thread_totals(merged)[0] is True
        total = sum(
            e["workers"]
            for e in merged["gpu_config"]
            if isinstance(e, dict) and e.get("enabled", True)
        )
        assert total == 3


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
        """Test gpu_threads computed from gpu_config and distributed by setter."""
        settings_manager.gpu_config = [
            {
                "device": "/dev/dri/renderD128",
                "name": "GPU 0",
                "type": "vaapi",
                "enabled": True,
                "workers": 1,
                "ffmpeg_threads": 2,
            },
            {
                "device": "/dev/dri/renderD129",
                "name": "GPU 1",
                "type": "vaapi",
                "enabled": True,
                "workers": 1,
                "ffmpeg_threads": 2,
            },
        ]
        settings_manager.gpu_threads = 4
        assert settings_manager.gpu_threads == 4

    def test_plex_verify_ssl_property(self, settings_manager):
        """Test plex_verify_ssl property with bool conversion."""
        settings_manager.plex_verify_ssl = False
        assert settings_manager.plex_verify_ssl is False

    def test_plex_verify_ssl_defaults_true(self, settings_manager, monkeypatch):
        """Test plex_verify_ssl defaults to True when unset."""
        monkeypatch.delenv("PLEX_VERIFY_SSL", raising=False)
        assert settings_manager.plex_verify_ssl is True

    def test_plex_verify_ssl_saved_true(self, settings_manager):
        """Test plex_verify_ssl persists True value."""
        settings_manager.plex_verify_ssl = True
        assert settings_manager.plex_verify_ssl is True

    def test_plex_verify_ssl_saved_false(self, settings_manager):
        """Test plex_verify_ssl persists False value."""
        settings_manager.plex_verify_ssl = False
        assert settings_manager.plex_verify_ssl is False

    def test_cpu_threads_default_when_missing(self, settings_manager, monkeypatch):
        """Test cpu_threads defaults to 1 when key is not set."""
        monkeypatch.delenv("CPU_THREADS", raising=False)
        assert settings_manager.cpu_threads == 1

    def test_gpu_threads_default_when_missing(self, settings_manager, monkeypatch):
        """Test gpu_threads defaults to 0 when gpu_config is empty."""
        monkeypatch.delenv("GPU_THREADS", raising=False)
        assert settings_manager.gpu_threads == 0

    def test_cpu_threads_zero_preserved(self, settings_manager):
        """Test cpu_threads=0 is preserved (issue #142)."""
        settings_manager.cpu_threads = 0
        assert settings_manager.cpu_threads == 0
        from plex_generate_previews.web.settings_manager import SettingsManager

        sm2 = SettingsManager(config_dir=str(settings_manager.config_dir))
        assert sm2.cpu_threads == 0

    def test_gpu_threads_zero_preserved(self, settings_manager):
        """Test gpu_threads=0 is preserved."""
        settings_manager.gpu_threads = 0
        assert settings_manager.gpu_threads == 0

    def test_thumbnail_interval_property(self, settings_manager):
        """Test thumbnail_interval property."""
        settings_manager.thumbnail_interval = 5
        assert settings_manager.thumbnail_interval == 5


class TestGpuConfig:
    """Tests for gpu_config property and computed gpu_threads."""

    @pytest.fixture
    def settings_manager(self, tmp_path, monkeypatch):
        from plex_generate_previews.web.settings_manager import SettingsManager

        monkeypatch.delenv("GPU_THREADS", raising=False)
        monkeypatch.delenv("GPU_SELECTION", raising=False)
        monkeypatch.delenv("FFMPEG_THREADS", raising=False)
        return SettingsManager(config_dir=str(tmp_path))

    def test_gpu_config_getter_setter_roundtrip(self, settings_manager):
        """Test gpu_config persists through getter/setter."""
        config = [
            {
                "device": "/dev/dri/renderD128",
                "name": "GPU 0",
                "type": "vaapi",
                "enabled": True,
                "workers": 2,
                "ffmpeg_threads": 4,
            },
        ]
        settings_manager.gpu_config = config
        assert settings_manager.gpu_config == config

    def test_gpu_threads_computed_from_gpu_config(self, settings_manager):
        """Test gpu_threads is the sum of workers across enabled GPUs."""
        settings_manager.gpu_config = [
            {
                "device": "/dev/gpu0",
                "name": "GPU 0",
                "type": "vaapi",
                "enabled": True,
                "workers": 3,
                "ffmpeg_threads": 2,
            },
            {
                "device": "/dev/gpu1",
                "name": "GPU 1",
                "type": "vaapi",
                "enabled": True,
                "workers": 2,
                "ffmpeg_threads": 2,
            },
            {
                "device": "/dev/gpu2",
                "name": "GPU 2",
                "type": "vaapi",
                "enabled": False,
                "workers": 5,
                "ffmpeg_threads": 2,
            },
        ]
        assert settings_manager.gpu_threads == 5

    def test_gpu_threads_setter_distributes_across_enabled(self, settings_manager):
        """Test gpu_threads setter distributes workers evenly across enabled GPUs."""
        settings_manager.gpu_config = [
            {
                "device": "/dev/gpu0",
                "name": "GPU 0",
                "type": "vaapi",
                "enabled": True,
                "workers": 0,
                "ffmpeg_threads": 2,
            },
            {
                "device": "/dev/gpu1",
                "name": "GPU 1",
                "type": "vaapi",
                "enabled": True,
                "workers": 0,
                "ffmpeg_threads": 2,
            },
            {
                "device": "/dev/gpu2",
                "name": "GPU 2",
                "type": "vaapi",
                "enabled": False,
                "workers": 0,
                "ffmpeg_threads": 2,
            },
        ]
        settings_manager.gpu_threads = 5
        config = settings_manager.gpu_config
        enabled_workers = [e["workers"] for e in config if e["enabled"]]
        assert sum(enabled_workers) == 5
        assert enabled_workers == [3, 2]
        assert config[2]["workers"] == 0

    def test_gpu_threads_setter_noop_when_no_enabled(self, settings_manager):
        """Test gpu_threads setter does nothing when no enabled GPUs."""
        settings_manager.gpu_config = [
            {
                "device": "/dev/gpu0",
                "name": "GPU 0",
                "type": "vaapi",
                "enabled": False,
                "workers": 0,
                "ffmpeg_threads": 2,
            },
        ]
        settings_manager.gpu_threads = 4
        assert settings_manager.gpu_threads == 0

    def test_gpu_threads_zero_with_empty_config(self, settings_manager):
        """Test gpu_threads returns 0 when gpu_config is empty."""
        assert settings_manager.gpu_config == []
        assert settings_manager.gpu_threads == 0

    def test_update_routes_gpu_threads_through_setter(self, settings_manager):
        """Test that update() distributes gpu_threads via setter logic."""
        settings_manager.gpu_config = [
            {
                "device": "/dev/gpu0",
                "name": "GPU 0",
                "type": "vaapi",
                "enabled": True,
                "workers": 1,
                "ffmpeg_threads": 2,
            },
        ]
        settings_manager.update({"gpu_threads": 3, "cpu_threads": 2})
        assert settings_manager.gpu_threads == 3
        assert settings_manager.cpu_threads == 2


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


class TestApplyChanges:
    """Tests for the apply_changes batch update method."""

    @pytest.fixture
    def settings_manager(self, tmp_path):
        from plex_generate_previews.web.settings_manager import SettingsManager

        return SettingsManager(config_dir=str(tmp_path))

    def test_apply_updates_only(self, settings_manager):
        settings_manager.apply_changes(updates={"a": 1, "b": 2})
        assert settings_manager.get("a") == 1
        assert settings_manager.get("b") == 2

    def test_apply_deletes_only(self, settings_manager):
        settings_manager.set("x", 10)
        settings_manager.apply_changes(deletes=["x"])
        assert settings_manager.get("x") is None

    def test_apply_updates_and_deletes(self, settings_manager):
        settings_manager.set("old_key", "old_val")
        settings_manager.apply_changes(
            updates={"new_key": "new_val"},
            deletes=["old_key"],
        )
        assert settings_manager.get("new_key") == "new_val"
        assert settings_manager.get("old_key") is None

    def test_apply_noop(self, settings_manager):
        """apply_changes with no args saves without error."""
        settings_manager.set("keep", True)
        settings_manager.apply_changes()
        assert settings_manager.get("keep") is True

    def test_deleting_nonexistent_key_is_safe(self, settings_manager):
        settings_manager.apply_changes(deletes=["no_such_key"])
        assert settings_manager.get("no_such_key") is None


class TestGpuConfigEdgeCases:
    """Edge-case tests for GPU config handling."""

    @pytest.fixture
    def settings_manager(self, tmp_path):
        from plex_generate_previews.web.settings_manager import SettingsManager

        return SettingsManager(config_dir=str(tmp_path))

    def test_gpu_config_none_returns_empty_list(self, settings_manager):
        """gpu_config returns [] when stored value is None."""
        settings_manager.set("gpu_config", None)
        assert settings_manager.gpu_config == []

    def test_gpu_config_non_list_returns_empty_list(self, settings_manager):
        """gpu_config returns [] when stored value is a string or int."""
        settings_manager.set("gpu_config", "invalid")
        assert settings_manager.gpu_config == []
        settings_manager.set("gpu_config", 42)
        assert settings_manager.gpu_config == []

    def test_gpu_config_filters_non_dict_entries(self, settings_manager):
        """gpu_config filters out non-dict entries in the list."""
        settings_manager.set(
            "gpu_config",
            [
                {"device": "cuda", "enabled": True, "workers": 1},
                "bad_entry",
                None,
                42,
                {"device": "vaapi", "enabled": False, "workers": 0},
            ],
        )
        result = settings_manager.gpu_config
        assert len(result) == 2
        assert result[0]["device"] == "cuda"
        assert result[1]["device"] == "vaapi"

    def test_gpu_threads_with_none_gpu_config(self, settings_manager):
        """gpu_threads returns 0 when gpu_config is None."""
        settings_manager.set("gpu_config", None)
        assert settings_manager.gpu_threads == 0

    def test_gpu_threads_with_malformed_entries(self, settings_manager):
        """gpu_threads ignores non-dict entries in gpu_config."""
        settings_manager.set(
            "gpu_config",
            [
                {"device": "cuda", "enabled": True, "workers": 3},
                "not_a_dict",
                None,
            ],
        )
        assert settings_manager.gpu_threads == 3

    def test_gpu_threads_missing_workers_key(self, settings_manager):
        """gpu_threads defaults workers to 0 when key is missing."""
        settings_manager.set(
            "gpu_config",
            [{"device": "cuda", "enabled": True}],
        )
        assert settings_manager.gpu_threads == 0

    def test_distribute_gpu_threads_with_none_config(self, settings_manager):
        """_distribute_gpu_threads does nothing when gpu_config is None."""
        settings_manager.set("gpu_config", None)
        settings_manager.gpu_threads = 5
        assert settings_manager.get("gpu_config") is None

    def test_distribute_gpu_threads_with_non_list_config(self, settings_manager):
        """_distribute_gpu_threads does nothing when gpu_config is not a list."""
        settings_manager.set("gpu_config", "invalid")
        settings_manager.gpu_threads = 5
        assert settings_manager.get("gpu_config") == "invalid"

    def test_distribute_gpu_threads_with_malformed_entries(self, settings_manager):
        """_distribute_gpu_threads filters out non-dict entries."""
        settings_manager.set(
            "gpu_config",
            [
                {"device": "cuda", "enabled": True, "workers": 0},
                "not_a_dict",
                None,
            ],
        )
        settings_manager.gpu_threads = 3
        config = settings_manager.gpu_config
        assert len(config) == 1
        assert config[0]["workers"] == 3

    def test_distribute_fewer_threads_than_gpus(self, settings_manager):
        """2 threads across 3 enabled GPUs: first 2 get 1 each, third gets 0."""
        settings_manager.gpu_config = [
            {
                "device": "g0",
                "type": "t",
                "name": "G0",
                "enabled": True,
                "workers": 0,
                "ffmpeg_threads": 2,
            },
            {
                "device": "g1",
                "type": "t",
                "name": "G1",
                "enabled": True,
                "workers": 0,
                "ffmpeg_threads": 2,
            },
            {
                "device": "g2",
                "type": "t",
                "name": "G2",
                "enabled": True,
                "workers": 0,
                "ffmpeg_threads": 2,
            },
        ]
        settings_manager.gpu_threads = 2
        config = settings_manager.gpu_config
        workers = [e["workers"] for e in config]
        assert sum(workers) == 2
        assert workers == [1, 1, 0]

    def test_update_does_not_mutate_caller_dict(self, settings_manager):
        """update() should not modify the dict passed to it."""
        settings_manager.gpu_config = [
            {
                "device": "cuda",
                "type": "nvidia",
                "name": "GPU",
                "enabled": True,
                "workers": 0,
                "ffmpeg_threads": 2,
            },
        ]
        payload = {"gpu_threads": 3, "cpu_threads": 2}
        settings_manager.update(payload)
        assert "gpu_threads" in payload
        assert payload["gpu_threads"] == 3
