"""Tests for the upgrade/migration system.

Tests env var migration, schema migrations, and GPU config building.
"""

from unittest.mock import patch

import pytest

from media_preview_generator.upgrade import _CURRENT_SCHEMA_VERSION


@pytest.fixture
def settings_manager(tmp_path, monkeypatch):
    """Create a SettingsManager with clean environment."""
    from media_preview_generator.web.settings_manager import SettingsManager

    monkeypatch.delenv("GPU_THREADS", raising=False)
    monkeypatch.delenv("GPU_SELECTION", raising=False)
    monkeypatch.delenv("FFMPEG_THREADS", raising=False)
    monkeypatch.delenv("PLEX_URL", raising=False)
    monkeypatch.delenv("PLEX_TOKEN", raising=False)
    monkeypatch.delenv("PLEX_LIBRARIES", raising=False)
    monkeypatch.delenv("PLEX_VIDEOS_PATH_MAPPING", raising=False)
    monkeypatch.delenv("PLEX_LOCAL_VIDEOS_PATH_MAPPING", raising=False)
    return SettingsManager(config_dir=str(tmp_path))


class TestRunMigrations:
    """Tests for the top-level run_migrations entry point."""

    def test_calls_env_and_schema_migrations(self, settings_manager, monkeypatch):
        """run_migrations runs both env var and schema migrations."""
        from media_preview_generator.upgrade import run_migrations

        monkeypatch.setenv("PLEX_URL", "http://plex:32400")
        run_migrations(settings_manager)

        assert settings_manager.get("_env_migrated") is True
        assert settings_manager.get("_schema_version") == _CURRENT_SCHEMA_VERSION
        assert settings_manager.get("plex_url") == "http://plex:32400"


class TestEnvVarMigration:
    """Tests for environment variable migration."""

    def test_migrates_plex_url(self, settings_manager, monkeypatch):
        from media_preview_generator.upgrade import _migrate_env_vars

        monkeypatch.setenv("PLEX_URL", "http://plex:32400")
        _migrate_env_vars(settings_manager)
        assert settings_manager.get("plex_url") == "http://plex:32400"

    def test_migrates_int_values(self, settings_manager, monkeypatch):
        from media_preview_generator.upgrade import _migrate_env_vars

        monkeypatch.setenv("CPU_THREADS", "4")
        _migrate_env_vars(settings_manager)
        assert settings_manager.get("cpu_threads") == 4

    def test_migrates_bool_values(self, settings_manager, monkeypatch):
        from media_preview_generator.upgrade import _migrate_env_vars

        monkeypatch.setenv("PLEX_VERIFY_SSL", "false")
        _migrate_env_vars(settings_manager)
        assert settings_manager.get("plex_verify_ssl") is False

    def test_skips_existing_keys(self, settings_manager, monkeypatch):
        from media_preview_generator.upgrade import _migrate_env_vars

        settings_manager.set("plex_url", "http://existing:32400")
        monkeypatch.setenv("PLEX_URL", "http://new:32400")
        _migrate_env_vars(settings_manager)
        assert settings_manager.get("plex_url") == "http://existing:32400"

    def test_runs_only_once(self, settings_manager, monkeypatch):
        from media_preview_generator.upgrade import _migrate_env_vars

        monkeypatch.setenv("PLEX_URL", "http://first:32400")
        _migrate_env_vars(settings_manager)
        assert settings_manager.get("plex_url") == "http://first:32400"

        monkeypatch.setenv("PLEX_URL", "http://second:32400")
        _migrate_env_vars(settings_manager)
        assert settings_manager.get("plex_url") == "http://first:32400"

    def test_sets_env_migrated_flag(self, settings_manager, monkeypatch):
        from media_preview_generator.upgrade import _migrate_env_vars

        _migrate_env_vars(settings_manager)
        assert settings_manager.get("_env_migrated") is True

    def test_migrates_libraries(self, settings_manager, monkeypatch):
        from media_preview_generator.upgrade import _migrate_env_vars

        monkeypatch.setenv("PLEX_LIBRARIES", "Movies, TV Shows")
        _migrate_env_vars(settings_manager)
        assert settings_manager.get("selected_libraries") == ["Movies", "TV Shows"]


class TestEnvVarMigrationExtended:
    """Additional coverage for _migrate_env_vars edge cases."""

    def test_invalid_int_env_var_logged(self, settings_manager, monkeypatch):
        """Invalid int env var is skipped with a warning, not a crash."""
        from media_preview_generator.upgrade import _migrate_env_vars

        monkeypatch.setenv("CPU_THREADS", "not_a_number")
        _migrate_env_vars(settings_manager)
        assert settings_manager.get("cpu_threads") is None
        assert settings_manager.get("_env_migrated") is True

    def test_gpu_config_migrated_from_env(self, settings_manager, monkeypatch):
        """gpu_config is built from GPU env vars during env migration."""
        from media_preview_generator.upgrade import _migrate_env_vars

        monkeypatch.setenv("GPU_THREADS", "2")
        detected = [("nvidia", "cuda", {"name": "RTX 4090"})]
        with patch(
            "media_preview_generator.gpu.detect.detect_all_gpus",
            return_value=detected,
        ):
            _migrate_env_vars(settings_manager)
        config = settings_manager.get("gpu_config")
        assert config is not None
        assert len(config) == 1
        assert config[0]["workers"] == 2

    def test_path_mappings_migrated_from_env(self, settings_manager, monkeypatch):
        """path_mappings are built from legacy env vars during env migration."""
        from media_preview_generator.upgrade import _migrate_env_vars

        monkeypatch.setenv("PLEX_VIDEOS_PATH_MAPPING", "/plex/media")
        monkeypatch.setenv("PLEX_LOCAL_VIDEOS_PATH_MAPPING", "/local/media")
        _migrate_env_vars(settings_manager)
        mappings = settings_manager.get("path_mappings")
        assert mappings is not None
        assert len(mappings) == 1
        assert mappings[0]["plex_prefix"] == "/plex/media"

    def test_deprecated_env_var_does_not_crash(self, settings_manager, monkeypatch):
        """Deprecated env vars emit a warning but don't block migration."""
        from media_preview_generator.upgrade import _migrate_env_vars

        monkeypatch.setenv("GPU_SELECTION", "0")
        monkeypatch.setenv("SORT_BY", "name")
        _migrate_env_vars(settings_manager)
        assert settings_manager.get("_env_migrated") is True


class TestBuildGpuConfigFromEnv:
    """Tests for _build_gpu_config_from_env helper."""

    def test_returns_none_when_no_env_vars(self, monkeypatch):
        from media_preview_generator.upgrade import _build_gpu_config_from_env

        monkeypatch.delenv("GPU_THREADS", raising=False)
        monkeypatch.delenv("GPU_SELECTION", raising=False)
        monkeypatch.delenv("FFMPEG_THREADS", raising=False)
        result = _build_gpu_config_from_env()
        assert result is None

    def test_builds_config_with_gpu_threads(self, monkeypatch):
        from media_preview_generator.upgrade import _build_gpu_config_from_env

        monkeypatch.setenv("GPU_THREADS", "2")
        detected = [
            ("vaapi", "/dev/dri/renderD128", {"name": "Intel GPU"}),
            ("vaapi", "/dev/dri/renderD129", {"name": "AMD GPU"}),
        ]
        with patch(
            "media_preview_generator.gpu.detect.detect_all_gpus",
            return_value=detected,
        ):
            result = _build_gpu_config_from_env()
        assert result is not None
        assert len(result) == 2
        assert all(entry["enabled"] for entry in result)
        total_workers = sum(e["workers"] for e in result)
        assert total_workers == 2

    def test_gpu_selection_disables_unselected(self, monkeypatch):
        from media_preview_generator.upgrade import _build_gpu_config_from_env

        monkeypatch.setenv("GPU_THREADS", "1")
        monkeypatch.setenv("GPU_SELECTION", "0")
        detected = [
            ("vaapi", "/dev/dri/renderD128", {"name": "Intel GPU"}),
            ("vaapi", "/dev/dri/renderD129", {"name": "AMD GPU"}),
        ]
        with patch(
            "media_preview_generator.gpu.detect.detect_all_gpus",
            return_value=detected,
        ):
            result = _build_gpu_config_from_env()
        assert result is not None
        assert result[0]["enabled"] is True
        assert result[1]["enabled"] is False

    def test_ffmpeg_threads_propagated(self, monkeypatch):
        from media_preview_generator.upgrade import _build_gpu_config_from_env

        monkeypatch.setenv("GPU_THREADS", "1")
        monkeypatch.setenv("FFMPEG_THREADS", "4")
        detected = [("nvidia", "/dev/nvidia0", {"name": "RTX 4090"})]
        with patch(
            "media_preview_generator.gpu.detect.detect_all_gpus",
            return_value=detected,
        ):
            result = _build_gpu_config_from_env()
        assert result[0]["ffmpeg_threads"] == 4

    def test_returns_empty_when_no_gpus_detected(self, monkeypatch):
        from media_preview_generator.upgrade import _build_gpu_config_from_env

        monkeypatch.setenv("GPU_THREADS", "2")
        with patch(
            "media_preview_generator.gpu.detect.detect_all_gpus",
            return_value=[],
        ):
            result = _build_gpu_config_from_env()
        assert result == []


class TestMigrateSchema:
    """Tests for settings schema migration (gpu_threads -> gpu_config)."""

    def test_noop_when_already_at_current_version(self, settings_manager):
        """Migration is skipped when _schema_version is current."""
        from media_preview_generator.upgrade import _migrate_schema

        settings_manager.set("_schema_version", 2)
        settings_manager.set("gpu_threads", 4)
        _migrate_schema(settings_manager)
        assert settings_manager.get("gpu_threads") == 4

    def test_refuses_when_disk_schema_is_newer_than_binary(self, settings_manager):
        """J3: a settings.json from a newer release must refuse to start.

        Silent acceptance would drop unknown fields on the next save — the
        exact failure mode that wiped jobs.json on the tag-drift incident.
        """
        from media_preview_generator.upgrade import (
            _CURRENT_SCHEMA_VERSION,
            SchemaDowngradeError,
            _migrate_schema,
        )

        settings_manager.set("_schema_version", _CURRENT_SCHEMA_VERSION + 5)
        with pytest.raises(SchemaDowngradeError) as exc_info:
            _migrate_schema(settings_manager)
        msg = str(exc_info.value)
        assert "Refusing to start" in msg
        assert ".bak" in msg  # always points users at the recovery file

    def test_builds_gpu_config_from_flat_gpu_threads(self, settings_manager):
        """Flat gpu_threads is converted to per-GPU gpu_config."""
        from media_preview_generator.upgrade import _migrate_schema

        settings_manager.set("gpu_threads", 3)
        settings_manager.set("ffmpeg_threads", 4)

        detected = [
            ("nvidia", "/dev/nvidia0", {"name": "RTX 4090"}),
            ("nvidia", "/dev/nvidia1", {"name": "RTX 3090"}),
        ]
        with patch(
            "media_preview_generator.gpu.detect.detect_all_gpus",
            return_value=detected,
        ):
            _migrate_schema(settings_manager)

        config = settings_manager.gpu_config
        assert len(config) == 2
        assert config[0]["name"] == "RTX 4090"
        assert config[1]["name"] == "RTX 3090"
        assert all(e["enabled"] for e in config)
        assert sum(e["workers"] for e in config) == 3
        assert all(e["ffmpeg_threads"] == 4 for e in config)

        assert settings_manager.get("gpu_threads") is None
        assert settings_manager.get("ffmpeg_threads") is None
        assert settings_manager.get("_schema_version") == _CURRENT_SCHEMA_VERSION

    def test_removes_stale_keys_without_gpu_config(self, settings_manager):
        """Stale flat keys are removed even when no GPUs are detected."""
        from media_preview_generator.upgrade import _migrate_schema

        settings_manager.set("gpu_threads", 2)
        settings_manager.set("ffmpeg_threads", 3)

        with patch(
            "media_preview_generator.gpu.detect.detect_all_gpus",
            return_value=[],
        ):
            _migrate_schema(settings_manager)

        assert settings_manager.get("gpu_threads") is None
        assert settings_manager.get("ffmpeg_threads") is None
        assert settings_manager.gpu_config == []
        assert settings_manager.get("_schema_version") == _CURRENT_SCHEMA_VERSION

    def test_preserves_existing_gpu_config(self, settings_manager):
        """Existing gpu_config is not overwritten by migration."""
        from media_preview_generator.upgrade import _migrate_schema

        existing = [
            {
                "device": "/dev/nvidia0",
                "name": "RTX 4090",
                "type": "nvidia",
                "enabled": True,
                "workers": 2,
                "ffmpeg_threads": 3,
            }
        ]
        settings_manager.set("gpu_config", existing)
        settings_manager.set("gpu_threads", 99)

        _migrate_schema(settings_manager)

        config = settings_manager.gpu_config
        assert len(config) == 1
        assert config[0]["workers"] == 2
        assert settings_manager.get("gpu_threads") is None

    def test_no_flat_keys_noop(self, settings_manager):
        """Migration is a no-op when no flat keys or gpu_config exist."""
        from media_preview_generator.upgrade import _migrate_schema

        _migrate_schema(settings_manager)

        assert settings_manager.gpu_config == []
        assert settings_manager.get("_schema_version") == _CURRENT_SCHEMA_VERSION

    def test_idempotent(self, settings_manager):
        """Running migration twice does not change the result."""
        from media_preview_generator.upgrade import _migrate_schema

        settings_manager.set("gpu_threads", 2)
        detected = [("nvidia", "/dev/nvidia0", {"name": "GPU"})]

        with patch(
            "media_preview_generator.gpu.detect.detect_all_gpus",
            return_value=detected,
        ):
            _migrate_schema(settings_manager)

        config_after_first = settings_manager.gpu_config.copy()
        _migrate_schema(settings_manager)

        assert settings_manager.gpu_config == config_after_first


class TestMigrateSchemaEdgeCases:
    """Edge-case tests for schema migration."""

    def test_invalid_gpu_threads_value_handled(self, settings_manager):
        """Non-numeric gpu_threads in settings doesn't crash migration."""
        from media_preview_generator.upgrade import _migrate_schema

        settings_manager.set("gpu_threads", "abc")
        settings_manager.set("ffmpeg_threads", 2)
        _migrate_schema(settings_manager)
        assert settings_manager.get("gpu_threads") is None
        assert settings_manager.get("_schema_version") == _CURRENT_SCHEMA_VERSION

    def test_invalid_ffmpeg_threads_value_handled(self, settings_manager):
        """Non-numeric ffmpeg_threads in settings doesn't crash migration."""
        from media_preview_generator.upgrade import _migrate_schema

        settings_manager.set("gpu_threads", 2)
        settings_manager.set("ffmpeg_threads", "not_a_number")
        _migrate_schema(settings_manager)
        assert settings_manager.get("ffmpeg_threads") is None
        assert settings_manager.get("_schema_version") == _CURRENT_SCHEMA_VERSION

    def test_gpu_config_empty_list_not_overwritten(self, settings_manager):
        """gpu_config=[] is not overwritten (it means user cleared config)."""
        from media_preview_generator.upgrade import _migrate_schema

        settings_manager.set("gpu_config", [])
        settings_manager.set("gpu_threads", 4)
        _migrate_schema(settings_manager)
        assert settings_manager.gpu_config == []
        assert settings_manager.get("gpu_threads") is None

    def test_gpu_threads_zero_skips_config_build(self, settings_manager):
        """gpu_threads=0 removes stale keys but doesn't build gpu_config."""
        from media_preview_generator.upgrade import _migrate_schema

        settings_manager.set("gpu_threads", 0)
        _migrate_schema(settings_manager)
        assert settings_manager.gpu_config == []
        assert settings_manager.get("gpu_threads") is None


class TestBuildGpuConfigFromEnvEdgeCases:
    """Edge-case tests for _build_gpu_config_from_env."""

    def test_invalid_gpu_threads_env_uses_default(self, monkeypatch):
        from media_preview_generator.upgrade import _build_gpu_config_from_env

        monkeypatch.setenv("GPU_THREADS", "not_a_number")
        detected = [("nvidia", "cuda", {"name": "GPU"})]
        with patch(
            "media_preview_generator.gpu.detect.detect_all_gpus",
            return_value=detected,
        ):
            result = _build_gpu_config_from_env()
        assert result is not None
        assert result[0]["workers"] == 1

    def test_invalid_ffmpeg_threads_env_uses_default(self, monkeypatch):
        from media_preview_generator.upgrade import _build_gpu_config_from_env

        monkeypatch.setenv("GPU_THREADS", "1")
        monkeypatch.setenv("FFMPEG_THREADS", "xyz")
        detected = [("nvidia", "cuda", {"name": "GPU"})]
        with patch(
            "media_preview_generator.gpu.detect.detect_all_gpus",
            return_value=detected,
        ):
            result = _build_gpu_config_from_env()
        assert result is not None
        assert result[0]["ffmpeg_threads"] == 2

    def test_gpu_selection_out_of_range_indices(self, monkeypatch):
        """GPU_SELECTION with out-of-range index doesn't crash."""
        from media_preview_generator.upgrade import _build_gpu_config_from_env

        monkeypatch.setenv("GPU_THREADS", "1")
        monkeypatch.setenv("GPU_SELECTION", "0,999")
        detected = [("nvidia", "cuda", {"name": "GPU"})]
        with patch(
            "media_preview_generator.gpu.detect.detect_all_gpus",
            return_value=detected,
        ):
            result = _build_gpu_config_from_env()
        assert result is not None
        assert result[0]["enabled"] is True

    def test_gpu_detection_exception_returns_empty(self, monkeypatch):
        """When detect_all_gpus raises, returns empty list."""
        from media_preview_generator.upgrade import _build_gpu_config_from_env

        monkeypatch.setenv("GPU_THREADS", "1")
        with patch(
            "media_preview_generator.gpu.detect.detect_all_gpus",
            side_effect=RuntimeError("detection failed"),
        ):
            result = _build_gpu_config_from_env()
        assert result == []

    def test_gpu_threads_zero_disables_all(self, monkeypatch):
        """GPU_THREADS=0 sets all GPUs to enabled=False with workers=0."""
        from media_preview_generator.upgrade import _build_gpu_config_from_env

        monkeypatch.setenv("GPU_THREADS", "0")
        detected = [("nvidia", "cuda", {"name": "GPU"})]
        with patch(
            "media_preview_generator.gpu.detect.detect_all_gpus",
            return_value=detected,
        ):
            result = _build_gpu_config_from_env()
        assert result[0]["enabled"] is False
        assert result[0]["workers"] == 0

    def test_gpu_selection_non_numeric_falls_back_to_all(self, monkeypatch):
        """Non-numeric GPU_SELECTION enables all GPUs."""
        from media_preview_generator.upgrade import _build_gpu_config_from_env

        monkeypatch.setenv("GPU_THREADS", "1")
        monkeypatch.setenv("GPU_SELECTION", "first,second")
        detected = [
            ("nvidia", "cuda:0", {"name": "GPU A"}),
            ("nvidia", "cuda:1", {"name": "GPU B"}),
        ]
        with patch(
            "media_preview_generator.gpu.detect.detect_all_gpus",
            return_value=detected,
        ):
            result = _build_gpu_config_from_env()
        assert all(e["enabled"] for e in result)

    def test_gpu_selection_no_matching_indices_falls_back(self, monkeypatch):
        """GPU_SELECTION with only out-of-range indices enables all GPUs."""
        from media_preview_generator.upgrade import _build_gpu_config_from_env

        monkeypatch.setenv("GPU_THREADS", "2")
        monkeypatch.setenv("GPU_SELECTION", "99,100")
        detected = [("nvidia", "cuda", {"name": "GPU"})]
        with patch(
            "media_preview_generator.gpu.detect.detect_all_gpus",
            return_value=detected,
        ):
            result = _build_gpu_config_from_env()
        assert result[0]["enabled"] is True
        assert result[0]["workers"] == 2

    def test_remainder_distributed_across_gpus(self, monkeypatch):
        """5 threads across 2 GPUs: first gets 3, second gets 2."""
        from media_preview_generator.upgrade import _build_gpu_config_from_env

        monkeypatch.setenv("GPU_THREADS", "5")
        detected = [
            ("nvidia", "cuda:0", {"name": "GPU A"}),
            ("nvidia", "cuda:1", {"name": "GPU B"}),
        ]
        with patch(
            "media_preview_generator.gpu.detect.detect_all_gpus",
            return_value=detected,
        ):
            result = _build_gpu_config_from_env()
        assert result[0]["workers"] == 3
        assert result[1]["workers"] == 2
        assert sum(e["workers"] for e in result) == 5

    def test_only_ffmpeg_threads_set_triggers_migration(self, monkeypatch):
        """Setting only FFMPEG_THREADS triggers GPU config build."""
        from media_preview_generator.upgrade import _build_gpu_config_from_env

        monkeypatch.delenv("GPU_THREADS", raising=False)
        monkeypatch.delenv("GPU_SELECTION", raising=False)
        monkeypatch.setenv("FFMPEG_THREADS", "4")
        detected = [("nvidia", "cuda", {"name": "GPU"})]
        with patch(
            "media_preview_generator.gpu.detect.detect_all_gpus",
            return_value=detected,
        ):
            result = _build_gpu_config_from_env()
        assert result is not None
        assert result[0]["ffmpeg_threads"] == 4
        assert result[0]["workers"] == 1


class TestBuildPathMappingsFromEnv:
    """Tests for _build_path_mappings_from_env helper."""

    def test_returns_none_when_no_env_vars(self, monkeypatch):
        from media_preview_generator.upgrade import _build_path_mappings_from_env

        monkeypatch.delenv("PLEX_VIDEOS_PATH_MAPPING", raising=False)
        monkeypatch.delenv("PLEX_LOCAL_VIDEOS_PATH_MAPPING", raising=False)
        result = _build_path_mappings_from_env()
        assert result is None

    def test_builds_mappings(self, monkeypatch):
        from media_preview_generator.upgrade import _build_path_mappings_from_env

        monkeypatch.setenv("PLEX_VIDEOS_PATH_MAPPING", "/plex/media")
        monkeypatch.setenv("PLEX_LOCAL_VIDEOS_PATH_MAPPING", "/local/media")
        result = _build_path_mappings_from_env()
        assert result is not None
        assert len(result) == 1
        assert result[0]["plex_prefix"] == "/plex/media"
        assert result[0]["local_prefix"] == "/local/media"
        assert result[0]["webhook_prefixes"] == []

    def test_returns_none_when_only_one_var_set(self, monkeypatch):
        from media_preview_generator.upgrade import _build_path_mappings_from_env

        monkeypatch.setenv("PLEX_VIDEOS_PATH_MAPPING", "/plex/media")
        monkeypatch.delenv("PLEX_LOCAL_VIDEOS_PATH_MAPPING", raising=False)
        result = _build_path_mappings_from_env()
        assert result is None

    def test_returns_none_when_get_path_mapping_pairs_raises(self, monkeypatch):
        """Exception in get_path_mapping_pairs returns None gracefully."""
        from media_preview_generator.upgrade import _build_path_mappings_from_env

        monkeypatch.setenv("PLEX_VIDEOS_PATH_MAPPING", "/plex/media")
        monkeypatch.setenv("PLEX_LOCAL_VIDEOS_PATH_MAPPING", "/local/media")
        with patch(
            "media_preview_generator.config.get_path_mapping_pairs",
            side_effect=RuntimeError("parsing failed"),
        ):
            result = _build_path_mappings_from_env()
        assert result is None

    def test_returns_none_when_pairs_empty(self, monkeypatch):
        """Empty pairs from get_path_mapping_pairs returns None."""
        from media_preview_generator.upgrade import _build_path_mappings_from_env

        monkeypatch.setenv("PLEX_VIDEOS_PATH_MAPPING", "/plex/media")
        monkeypatch.setenv("PLEX_LOCAL_VIDEOS_PATH_MAPPING", "/local/media")
        with patch(
            "media_preview_generator.config.get_path_mapping_pairs",
            return_value=[],
        ):
            result = _build_path_mappings_from_env()
        assert result is None


class TestMigrateToV2Extended:
    """Additional edge-case tests for _migrate_to_v2."""

    def test_gpu_detection_exception_still_removes_stale_keys(self, settings_manager):
        """If GPU detection fails, stale keys are still cleaned up."""
        from media_preview_generator.upgrade import _migrate_to_v2

        settings_manager.set("gpu_threads", 4)
        settings_manager.set("ffmpeg_threads", 2)
        with patch(
            "media_preview_generator.gpu.detect.detect_all_gpus",
            side_effect=RuntimeError("no driver"),
        ):
            notes = _migrate_to_v2(settings_manager)
        assert settings_manager.get("gpu_threads") is None
        assert settings_manager.get("ffmpeg_threads") is None
        assert any("removed stale keys" in n for n in notes)

    def test_worker_remainder_distribution(self, settings_manager):
        """5 threads across 3 GPUs: 2+2+1 with remainder going to first GPUs."""
        from media_preview_generator.upgrade import _migrate_to_v2

        settings_manager.set("gpu_threads", 5)
        settings_manager.set("ffmpeg_threads", 2)
        detected = [
            ("nvidia", "/dev/nvidia0", {"name": "GPU A"}),
            ("nvidia", "/dev/nvidia1", {"name": "GPU B"}),
            ("nvidia", "/dev/nvidia2", {"name": "GPU C"}),
        ]
        with patch(
            "media_preview_generator.gpu.detect.detect_all_gpus",
            return_value=detected,
        ):
            _migrate_to_v2(settings_manager)
        config = settings_manager.gpu_config
        workers = [e["workers"] for e in config]
        assert sum(workers) == 5
        assert workers == [2, 2, 1]

    def test_gpu_name_fallback(self, settings_manager):
        """GPU without a 'name' in gpu_info uses type-based fallback."""
        from media_preview_generator.upgrade import _migrate_to_v2

        settings_manager.set("gpu_threads", 1)
        detected = [("vaapi", "/dev/dri/renderD128", {})]
        with patch(
            "media_preview_generator.gpu.detect.detect_all_gpus",
            return_value=detected,
        ):
            _migrate_to_v2(settings_manager)
        config = settings_manager.gpu_config
        assert config[0]["name"] == "vaapi GPU"


# ============================================================================
# v4: Recently Added scanner → schedule-type migration
# ============================================================================


@pytest.fixture
def _fresh_schedule_manager(settings_manager):
    """Reset the schedule-manager singleton and scope it to ``settings_manager.config_dir``.

    The v4 migration calls ``get_schedule_manager(config_dir=str(sm.config_dir))``
    to create schedules.  Tests must null the singleton between runs so each
    test gets a fresh ScheduleManager pointed at its own pytest ``tmp_path``
    — otherwise the singleton from a previous test leaks schedules into the
    next one.
    """
    import media_preview_generator.web.scheduler as sched_mod

    def _reset():
        with sched_mod._schedule_lock:
            if sched_mod._schedule_manager is not None:
                try:
                    sched_mod._schedule_manager.stop()
                except Exception:
                    pass
            sched_mod._schedule_manager = None

    _reset()
    yield str(settings_manager.config_dir)
    _reset()


class TestMigrateToV4:
    """Tests for the v4 legacy-settings → schedule-entry migration."""

    def test_no_op_when_legacy_keys_absent(self, settings_manager, _fresh_schedule_manager):
        """Fresh installs with no legacy keys should not create any schedules."""
        from media_preview_generator.upgrade import _migrate_to_v4
        from media_preview_generator.web.scheduler import get_schedule_manager

        notes = _migrate_to_v4(settings_manager)
        assert notes == []
        manager = get_schedule_manager(config_dir=_fresh_schedule_manager)
        assert manager.get_all_schedules() == []

    def test_converts_enabled_scanner_to_schedule(self, settings_manager, _fresh_schedule_manager):
        """Legacy recently_added_enabled=True creates an equivalent schedule."""
        from media_preview_generator.upgrade import _migrate_to_v4
        from media_preview_generator.web.scheduler import get_schedule_manager

        settings_manager.apply_changes(
            updates={
                "recently_added_enabled": True,
                "recently_added_interval_minutes": 5,
                "recently_added_lookback_hours": 12,
                "recently_added_libraries": [],
            }
        )

        notes = _migrate_to_v4(settings_manager)

        manager = get_schedule_manager(config_dir=_fresh_schedule_manager)
        schedules = manager.get_all_schedules()
        assert len(schedules) == 1
        sched = schedules[0]
        assert sched["name"] == "Recently Added Scanner"
        assert sched["trigger_type"] == "interval"
        assert sched["trigger_value"] == "5"
        assert sched["library_id"] is None
        assert sched["config"]["job_type"] == "recently_added"
        assert sched["config"]["lookback_hours"] == 12
        # Legacy keys removed
        for key in [
            "recently_added_enabled",
            "recently_added_interval_minutes",
            "recently_added_lookback_hours",
            "recently_added_libraries",
        ]:
            assert settings_manager.get(key) is None
        assert any("v4: created" in n for n in notes)

    def test_no_schedule_when_scanner_was_disabled(self, settings_manager, _fresh_schedule_manager):
        """Legacy keys present but disabled → legacy keys still cleaned up, no schedule."""
        from media_preview_generator.upgrade import _migrate_to_v4
        from media_preview_generator.web.scheduler import get_schedule_manager

        settings_manager.apply_changes(
            updates={
                "recently_added_enabled": False,
                "recently_added_interval_minutes": 15,
                "recently_added_lookback_hours": 24,
                "recently_added_libraries": [],
            }
        )

        _migrate_to_v4(settings_manager)

        manager = get_schedule_manager(config_dir=_fresh_schedule_manager)
        assert manager.get_all_schedules() == []
        assert settings_manager.get("recently_added_enabled") is None
        assert settings_manager.get("recently_added_interval_minutes") is None

    def test_creates_one_schedule_per_library_override(self, settings_manager, _fresh_schedule_manager):
        """A non-empty recently_added_libraries list creates one schedule per entry."""
        from media_preview_generator.upgrade import _migrate_to_v4
        from media_preview_generator.web.scheduler import get_schedule_manager

        settings_manager.apply_changes(
            updates={
                "recently_added_enabled": True,
                "recently_added_interval_minutes": 30,
                "recently_added_lookback_hours": 6,
                "recently_added_libraries": ["1", "2"],
            }
        )

        _migrate_to_v4(settings_manager)

        manager = get_schedule_manager(config_dir=_fresh_schedule_manager)
        schedules = manager.get_all_schedules()
        assert len(schedules) == 2
        for sched in schedules:
            assert sched["config"]["job_type"] == "recently_added"
            assert sched["config"]["lookback_hours"] == 6
            assert sched["trigger_value"] == "30"
        library_ids = {s["library_id"] for s in schedules}
        assert library_ids == {"1", "2"}


class TestMigrateToV6:
    """Tests for the v6 migration that strips stale generic 'cuda' gpu_config entries."""

    def test_no_op_when_gpu_config_missing(self, settings_manager):
        from media_preview_generator.upgrade import _migrate_to_v6

        assert _migrate_to_v6(settings_manager) == []

    def test_no_op_when_no_stale_cuda_entry(self, settings_manager):
        from media_preview_generator.upgrade import _migrate_to_v6

        settings_manager.set(
            "gpu_config",
            [
                {"device": "cuda:0", "name": "NVIDIA", "type": "NVIDIA", "enabled": True, "workers": 1},
                {"device": "/dev/dri/renderD128", "name": "AMD", "type": "AMD", "enabled": True, "workers": 1},
            ],
        )

        notes = _migrate_to_v6(settings_manager)
        assert notes == []
        assert len(settings_manager.get("gpu_config")) == 2

    def test_strips_legacy_cuda_entry(self, settings_manager):
        from media_preview_generator.upgrade import _migrate_to_v6

        settings_manager.set(
            "gpu_config",
            [
                {"device": "cuda", "name": "NVIDIA GeForce RTX 3090", "type": "NVIDIA", "enabled": True, "workers": 2},
                {"device": "/dev/dri/renderD128", "name": "AMD", "type": "AMD", "enabled": True, "workers": 1},
            ],
        )

        notes = _migrate_to_v6(settings_manager)

        assert len(notes) == 1
        assert "removed 1" in notes[0]
        remaining = settings_manager.get("gpu_config")
        assert len(remaining) == 1
        assert remaining[0]["device"] == "/dev/dri/renderD128"

    def test_leaves_indexed_cuda_entries_untouched(self, settings_manager):
        from media_preview_generator.upgrade import _migrate_to_v6

        settings_manager.set(
            "gpu_config",
            [
                {"device": "cuda:0", "type": "NVIDIA", "enabled": True, "workers": 1},
                {"device": "cuda:1", "type": "NVIDIA", "enabled": True, "workers": 1},
            ],
        )

        notes = _migrate_to_v6(settings_manager)
        assert notes == []
        devices = [e["device"] for e in settings_manager.get("gpu_config")]
        assert devices == ["cuda:0", "cuda:1"]


# ============================================================================
# v7: Synthesise media_servers[] from legacy plex_* keys
# ============================================================================


class TestMigrateToV7:
    """Tests for the v7 multi-media-server schema migration."""

    def test_fresh_install_writes_empty_array(self, settings_manager):
        """No plex_url/token → media_servers initialised to []."""
        from media_preview_generator.upgrade import _migrate_to_v7

        notes = _migrate_to_v7(settings_manager)

        assert settings_manager.get("media_servers") == []
        assert any("empty media_servers" in n for n in notes)

    def test_synthesises_single_plex_entry_from_legacy_settings(self, settings_manager):
        """Existing single-Plex deployment → one media_servers entry."""
        from media_preview_generator.upgrade import _migrate_to_v7

        settings_manager.apply_changes(
            updates={
                "plex_url": "http://plex:32400",
                "plex_token": "secret-token",
                "plex_verify_ssl": False,
                "plex_timeout": 90,
                "plex_libraries": ["Movies", "TV Shows"],
                "plex_config_folder": "/config/plex",
                "plex_bif_frame_interval": 5,
                "path_mappings": [{"plex_prefix": "/media", "local_prefix": "/data"}],
            }
        )

        notes = _migrate_to_v7(settings_manager)

        servers = settings_manager.get("media_servers")
        assert isinstance(servers, list) and len(servers) == 1
        entry = servers[0]
        assert entry["id"] == "plex-default"
        assert entry["type"] == "plex"
        assert entry["enabled"] is True
        assert entry["url"] == "http://plex:32400"
        assert entry["verify_ssl"] is False
        assert entry["timeout"] == 90
        assert entry["auth"] == {"method": "token", "token": "secret-token"}
        assert entry["output"]["adapter"] == "plex_bundle"
        assert entry["output"]["plex_config_folder"] == "/config/plex"
        assert entry["output"]["frame_interval"] == 5
        assert entry["path_mappings"] == [{"plex_prefix": "/media", "local_prefix": "/data"}]
        # Two enabled libraries derived from plex_libraries
        names = [lib["name"] for lib in entry["libraries"]]
        assert names == ["Movies", "TV Shows"]
        assert all(lib["enabled"] is True for lib in entry["libraries"])
        assert any("synthesised" in n for n in notes)

    def test_prefers_plex_library_ids_over_titles(self, settings_manager):
        """When both id and title lists are set, ids win (matches existing filter logic)."""
        from media_preview_generator.upgrade import _migrate_to_v7

        settings_manager.apply_changes(
            updates={
                "plex_url": "http://plex:32400",
                "plex_token": "t",
                "plex_libraries": ["Movies"],
                "plex_library_ids": ["1", "2"],
            }
        )

        _migrate_to_v7(settings_manager)
        servers = settings_manager.get("media_servers")
        ids = [lib["id"] for lib in servers[0]["libraries"]]
        assert ids == ["1", "2"]

    def test_no_op_when_media_servers_already_present(self, settings_manager):
        """Re-running the migration must not overwrite an existing array."""
        from media_preview_generator.upgrade import _migrate_to_v7

        original = [{"id": "custom-id", "type": "plex", "name": "Custom"}]
        settings_manager.set("media_servers", original)

        notes = _migrate_to_v7(settings_manager)

        assert notes == []
        assert settings_manager.get("media_servers") == original

    def test_legacy_plex_keys_remain_after_migration(self, settings_manager):
        """v7 is additive — legacy plex_* keys must keep working for now."""
        from media_preview_generator.upgrade import _migrate_to_v7

        settings_manager.apply_changes(
            updates={
                "plex_url": "http://plex:32400",
                "plex_token": "t",
            }
        )

        _migrate_to_v7(settings_manager)

        assert settings_manager.get("plex_url") == "http://plex:32400"
        assert settings_manager.get("plex_token") == "t"

    def test_run_migrations_includes_v7(self, settings_manager, monkeypatch):
        """End-to-end check: run_migrations bumps schema_version to 7."""
        from media_preview_generator.upgrade import _CURRENT_SCHEMA_VERSION, run_migrations

        monkeypatch.setenv("PLEX_URL", "http://plex:32400")
        monkeypatch.setenv("PLEX_TOKEN", "t")

        run_migrations(settings_manager)

        assert settings_manager.get("_schema_version") == _CURRENT_SCHEMA_VERSION
        assert _CURRENT_SCHEMA_VERSION >= 7
        servers = settings_manager.get("media_servers")
        assert servers is not None
        assert len(servers) == 1
        assert servers[0]["url"] == "http://plex:32400"


class TestMigrateToV8:
    """Tests for the v8 schema migration: move global path_mappings/exclude_paths into media_servers[0]."""

    def test_no_globals_no_op(self, settings_manager):
        """Nothing to migrate → empty notes list, schema versions unchanged otherwise."""
        from media_preview_generator.upgrade import _migrate_to_v8

        settings_manager.apply_changes(updates={"media_servers": [{"id": "plex-default", "type": "plex"}]})
        notes = _migrate_to_v8(settings_manager)
        assert notes == []

    def test_empty_media_servers_keeps_globals_at_top_level(self, settings_manager):
        """Plex-less install (media_servers: []) — keep globals; warn so user knows to assign once a server is added."""
        from media_preview_generator.upgrade import _migrate_to_v8

        settings_manager.apply_changes(
            updates={
                "media_servers": [],
                "path_mappings": [{"plex_prefix": "/media", "local_prefix": "/data"}],
                "exclude_paths": [{"value": "/x", "type": "path"}],
            }
        )
        notes = _migrate_to_v8(settings_manager)
        assert any("no media_servers configured yet" in n for n in notes)
        assert settings_manager.get("path_mappings") == [{"plex_prefix": "/media", "local_prefix": "/data"}]
        assert settings_manager.get("exclude_paths") == [{"value": "/x", "type": "path"}]

    def test_multiple_servers_keeps_globals_with_warning(self, settings_manager):
        """When >1 server is configured, ambiguity → keep at top level + warn user to assign explicitly."""
        from media_preview_generator.upgrade import _migrate_to_v8

        settings_manager.apply_changes(
            updates={
                "media_servers": [
                    {"id": "plex", "type": "plex", "name": "Plex"},
                    {"id": "emby", "type": "emby", "name": "Emby"},
                ],
                "path_mappings": [{"plex_prefix": "/m", "local_prefix": "/l"}],
                "exclude_paths": [{"value": "/x", "type": "path"}],
            }
        )
        notes = _migrate_to_v8(settings_manager)
        assert any("2 servers configured" in n for n in notes)
        assert settings_manager.get("path_mappings") == [{"plex_prefix": "/m", "local_prefix": "/l"}]
        assert settings_manager.get("exclude_paths") == [{"value": "/x", "type": "path"}]
        # Per-server lists left untouched.
        servers = settings_manager.get("media_servers")
        assert servers[0].get("path_mappings", []) == []
        assert servers[1].get("path_mappings", []) == []

    def test_single_plex_server_inherits_globals(self, settings_manager):
        """The common case: single-Plex install — both lists move into media_servers[0]."""
        from media_preview_generator.upgrade import _migrate_to_v8

        settings_manager.apply_changes(
            updates={
                "media_servers": [
                    {"id": "plex-default", "type": "plex", "name": "Plex", "path_mappings": [], "exclude_paths": []}
                ],
                "path_mappings": [{"plex_prefix": "/media", "local_prefix": "/data"}],
                "exclude_paths": [{"value": "/data/Trailers/", "type": "path"}],
            }
        )
        notes = _migrate_to_v8(settings_manager)

        servers = settings_manager.get("media_servers")
        assert servers[0]["path_mappings"] == [{"plex_prefix": "/media", "local_prefix": "/data"}]
        assert servers[0]["exclude_paths"] == [{"value": "/data/Trailers/", "type": "path"}]
        # Top-level keys deleted (no dual state).
        assert "path_mappings" not in settings_manager.get_all()
        assert "exclude_paths" not in settings_manager.get_all()
        assert any("moved global" in n for n in notes)

    def test_single_non_plex_server_also_inherits(self, settings_manager):
        """If Plex was deleted and only Emby remains, the rules go to that single server."""
        from media_preview_generator.upgrade import _migrate_to_v8

        settings_manager.apply_changes(
            updates={
                "media_servers": [
                    {"id": "emby", "type": "emby", "name": "Emby", "path_mappings": [], "exclude_paths": []}
                ],
                "path_mappings": [{"plex_prefix": "/m", "local_prefix": "/l"}],
                "exclude_paths": [{"value": "/x", "type": "path"}],
            }
        )
        _migrate_to_v8(settings_manager)
        servers = settings_manager.get("media_servers")
        assert servers[0]["path_mappings"] == [{"plex_prefix": "/m", "local_prefix": "/l"}]
        assert servers[0]["exclude_paths"] == [{"value": "/x", "type": "path"}]

    def test_existing_per_server_rules_are_preserved_and_appended(self, settings_manager):
        """If media_servers[0] already has per-server rules, the global ones append to them (no overwrite)."""
        from media_preview_generator.upgrade import _migrate_to_v8

        settings_manager.apply_changes(
            updates={
                "media_servers": [
                    {
                        "id": "plex",
                        "type": "plex",
                        "name": "Plex",
                        "path_mappings": [{"plex_prefix": "/old", "local_prefix": "/local-old"}],
                        "exclude_paths": [{"value": "/old-excl", "type": "path"}],
                    }
                ],
                "path_mappings": [{"plex_prefix": "/new", "local_prefix": "/local-new"}],
                "exclude_paths": [{"value": "/new-excl", "type": "path"}],
            }
        )
        _migrate_to_v8(settings_manager)
        servers = settings_manager.get("media_servers")
        assert len(servers[0]["path_mappings"]) == 2
        assert len(servers[0]["exclude_paths"]) == 2

    def test_pre_v6_legacy_keys_cleaned_up(self, settings_manager):
        """plex_videos_path_mapping / plex_local_videos_path_mapping vestigial keys are dropped."""
        from media_preview_generator.upgrade import _migrate_to_v8

        settings_manager.apply_changes(
            updates={
                "media_servers": [{"id": "plex", "type": "plex", "name": "Plex"}],
                "plex_videos_path_mapping": "/p",
                "plex_local_videos_path_mapping": "/l",
            }
        )
        notes = _migrate_to_v8(settings_manager)
        assert "plex_videos_path_mapping" not in settings_manager.get_all()
        assert "plex_local_videos_path_mapping" not in settings_manager.get_all()
        assert any("pre-v6" in n for n in notes)

    def test_idempotent(self, settings_manager):
        """Re-running v8 on already-migrated settings is a no-op (no double-append)."""
        from media_preview_generator.upgrade import _migrate_to_v8

        settings_manager.apply_changes(
            updates={
                "media_servers": [
                    {
                        "id": "plex",
                        "type": "plex",
                        "name": "Plex",
                        "path_mappings": [{"plex_prefix": "/m", "local_prefix": "/l"}],
                        "exclude_paths": [{"value": "/x", "type": "path"}],
                    }
                ]
            }
        )
        # No global keys present.
        _migrate_to_v8(settings_manager)
        _migrate_to_v8(settings_manager)
        servers = settings_manager.get("media_servers")
        assert len(servers[0]["path_mappings"]) == 1
        assert len(servers[0]["exclude_paths"]) == 1


class TestMigrateToV9:
    """v9 dedupes per-server path_mappings + exclude_paths, cleaning up
    rows that the v7+v8 chain double-copied during the legacy → per-server
    migration.
    """

    def test_no_op_when_no_servers(self, settings_manager):
        from media_preview_generator.upgrade import _migrate_to_v9

        notes = _migrate_to_v9(settings_manager)
        assert notes == []

    def test_dedupes_path_mappings_left_by_v7_v8_chain(self, settings_manager):
        """Real-world scenario: v7 copied 3 global rows into media_servers[0],
        v8 then concatenated the same 3 rows again. v9 collapses the 6 back
        down to 3, preserving order.
        """
        from media_preview_generator.upgrade import _migrate_to_v9

        rows = [
            {"plex_prefix": "/data_16tb", "local_prefix": "/data_16tb", "webhook_prefixes": []},
            {"plex_prefix": "/data_16tb2", "local_prefix": "/data_16tb2", "webhook_prefixes": []},
            {"plex_prefix": "/data_16tb3", "local_prefix": "/data_16tb3", "webhook_prefixes": []},
        ]
        settings_manager.apply_changes(
            updates={
                "media_servers": [{"id": "plex-default", "type": "plex", "name": "Plex", "path_mappings": rows + rows}]
            }
        )

        notes = _migrate_to_v9(settings_manager)

        servers = settings_manager.get("media_servers")
        assert len(servers[0]["path_mappings"]) == 3
        assert servers[0]["path_mappings"] == rows
        assert any("3 duplicate path_mapping" in n for n in notes)

    def test_dedupe_treats_different_webhook_aliases_as_distinct(self, settings_manager):
        """Two rows with the same plex/local prefixes but different
        webhook_prefixes are NOT duplicates — each one expands a different
        alias.
        """
        from media_preview_generator.upgrade import _migrate_to_v9

        rows = [
            {"plex_prefix": "/m", "local_prefix": "/l", "webhook_prefixes": ["/data"]},
            {"plex_prefix": "/m", "local_prefix": "/l", "webhook_prefixes": ["/mnt"]},
        ]
        settings_manager.apply_changes(
            updates={"media_servers": [{"id": "plex", "type": "plex", "path_mappings": rows}]}
        )

        _migrate_to_v9(settings_manager)
        servers = settings_manager.get("media_servers")
        assert len(servers[0]["path_mappings"]) == 2

    def test_dedupes_exclude_paths(self, settings_manager):
        from media_preview_generator.upgrade import _migrate_to_v9

        ep = [
            {"value": "/data/Trailers/", "type": "path"},
            {"value": "/data/Trailers/", "type": "path"},
            {"value": "/data/Bonus/", "type": "path"},
        ]
        settings_manager.apply_changes(updates={"media_servers": [{"id": "plex", "type": "plex", "exclude_paths": ep}]})

        _migrate_to_v9(settings_manager)
        servers = settings_manager.get("media_servers")
        assert len(servers[0]["exclude_paths"]) == 2

    def test_idempotent(self, settings_manager):
        """Re-running v9 on a clean file is a no-op."""
        from media_preview_generator.upgrade import _migrate_to_v9

        rows = [{"plex_prefix": "/m", "local_prefix": "/l", "webhook_prefixes": []}]
        settings_manager.apply_changes(
            updates={"media_servers": [{"id": "plex", "type": "plex", "path_mappings": rows}]}
        )
        _migrate_to_v9(settings_manager)
        notes = _migrate_to_v9(settings_manager)
        assert notes == []
        servers = settings_manager.get("media_servers")
        assert len(servers[0]["path_mappings"]) == 1


class TestMigrateToV10:
    """v10 migrates legacy /api/webhooks/plex URLs to /api/webhooks/incoming
    and removes per-server output.webhook_secret keys (feature retired).
    """

    def test_no_op_when_no_servers(self, settings_manager):
        from media_preview_generator.upgrade import _migrate_to_v10

        notes = _migrate_to_v10(settings_manager)
        assert notes == []

    def test_rewrites_legacy_plex_url_to_incoming(self, settings_manager):
        from media_preview_generator.upgrade import _migrate_to_v10

        settings_manager.apply_changes(
            updates={
                "media_servers": [
                    {
                        "id": "plex-default",
                        "type": "plex",
                        "name": "Plex",
                        "output": {"webhook_public_url": "https://my-host/api/webhooks/plex"},
                    }
                ]
            }
        )

        notes = _migrate_to_v10(settings_manager)
        servers = settings_manager.get("media_servers")
        assert servers[0]["output"]["webhook_public_url"] == "https://my-host/api/webhooks/incoming"
        assert any("/incoming" in n for n in notes)

    def test_removes_per_server_webhook_secret(self, settings_manager):
        from media_preview_generator.upgrade import _migrate_to_v10

        settings_manager.apply_changes(
            updates={
                "media_servers": [
                    {
                        "id": "plex-a",
                        "type": "plex",
                        "output": {"webhook_secret": "leftover-from-K6"},
                    }
                ]
            }
        )

        notes = _migrate_to_v10(settings_manager)
        servers = settings_manager.get("media_servers")
        assert "webhook_secret" not in (servers[0].get("output") or {})
        assert any("webhook_secret" in n for n in notes)

    def test_url_already_incoming_is_left_alone(self, settings_manager):
        from media_preview_generator.upgrade import _migrate_to_v10

        settings_manager.apply_changes(
            updates={
                "media_servers": [
                    {
                        "id": "plex-a",
                        "type": "plex",
                        "output": {"webhook_public_url": "https://my-host/api/webhooks/incoming"},
                    }
                ]
            }
        )

        notes = _migrate_to_v10(settings_manager)
        assert notes == []
        servers = settings_manager.get("media_servers")
        assert servers[0]["output"]["webhook_public_url"] == "https://my-host/api/webhooks/incoming"

    def test_idempotent(self, settings_manager):
        from media_preview_generator.upgrade import _migrate_to_v10

        settings_manager.apply_changes(
            updates={
                "media_servers": [
                    {
                        "id": "plex-a",
                        "type": "plex",
                        "output": {
                            "webhook_public_url": "https://my-host/api/webhooks/plex",
                            "webhook_secret": "x",
                        },
                    }
                ]
            }
        )
        _migrate_to_v10(settings_manager)
        notes = _migrate_to_v10(settings_manager)
        assert notes == []
        servers = settings_manager.get("media_servers")
        assert servers[0]["output"]["webhook_public_url"] == "https://my-host/api/webhooks/incoming"
        assert "webhook_secret" not in (servers[0].get("output") or {})


class TestLegacyPlexToMediaServer:
    """Tests for the public helper used by the v7 migration."""

    def test_returns_none_when_no_plex_configured(self, settings_manager):
        from media_preview_generator.upgrade import _legacy_plex_to_media_server

        assert _legacy_plex_to_media_server(settings_manager) is None

    def test_handles_token_only_install(self, settings_manager):
        """A token without a URL still produces an entry — user can fix the URL later."""
        from media_preview_generator.upgrade import _legacy_plex_to_media_server

        settings_manager.set("plex_token", "t")
        entry = _legacy_plex_to_media_server(settings_manager)
        assert entry is not None
        assert entry["auth"]["token"] == "t"
        assert entry["url"] == ""

    def test_falls_back_to_selected_libraries_key(self, settings_manager):
        """The env-migration helper writes to ``selected_libraries``; v7 must read it too."""
        from media_preview_generator.upgrade import _legacy_plex_to_media_server

        settings_manager.apply_changes(
            updates={
                "plex_url": "http://x:32400",
                "plex_token": "t",
                "selected_libraries": ["Anime"],
            }
        )

        entry = _legacy_plex_to_media_server(settings_manager)
        assert entry is not None
        names = [lib["name"] for lib in entry["libraries"]]
        assert names == ["Anime"]


class TestMigrateToV11:
    """v11 seeds the ``frame_reuse`` block so cross-server frame reuse is on
    by default with sane TTL + disk cap."""

    def test_seeds_defaults_when_missing(self, settings_manager):
        from media_preview_generator.upgrade import _migrate_to_v11

        notes = _migrate_to_v11(settings_manager)
        assert any("frame_reuse" in n for n in notes)
        block = settings_manager.get("frame_reuse")
        assert block == {
            "enabled": True,
            "ttl_minutes": 60,
            "max_cache_disk_mb": 2048,
        }

    def test_idempotent_when_block_already_present(self, settings_manager):
        """User-customised values must survive re-running the migration."""
        from media_preview_generator.upgrade import _migrate_to_v11

        existing = {"enabled": False, "ttl_minutes": 5, "max_cache_disk_mb": 256}
        settings_manager.apply_changes(updates={"frame_reuse": existing})

        notes = _migrate_to_v11(settings_manager)
        assert notes == []
        assert settings_manager.get("frame_reuse") == existing
