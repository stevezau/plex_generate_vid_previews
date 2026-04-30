"""Tests for media_preview_generator.web.scheduler."""

import os
from unittest.mock import MagicMock

import pytest

from media_preview_generator.web.scheduler import (
    ScheduleManager,
    get_schedule_manager,
)


@pytest.fixture
def scheduler_manager(tmp_path, monkeypatch):
    """Create and start a ScheduleManager, set as global singleton, clean up after."""
    config_dir = str(tmp_path / "config")
    os.makedirs(config_dir, exist_ok=True)

    manager = ScheduleManager(config_dir=config_dir, run_job_callback=None)
    # Make this the global singleton so execute_scheduled_job uses it
    monkeypatch.setattr("media_preview_generator.web.scheduler._schedule_manager", manager)
    manager.start()

    yield manager

    manager.stop()


# ========================================================================
# Schedule CRUD
# ========================================================================


class TestScheduleCRUD:
    """Tests for schedule create/read/update/delete operations."""

    def test_create_schedule_with_cron(self, scheduler_manager):
        """Test creating a schedule with a cron expression."""
        schedule = scheduler_manager.create_schedule(
            name="Daily Backup",
            library_id="123",
            library_name="Movies",
            cron_expression="0 2 * * *",
            enabled=True,
        )

        assert schedule["id"] is not None
        assert schedule["name"] == "Daily Backup"
        assert schedule["library_id"] == "123"
        assert schedule["library_name"] == "Movies"
        assert schedule["trigger_type"] == "cron"
        assert schedule["trigger_value"] == "0 2 * * *"
        assert schedule["enabled"] is True
        assert schedule["last_run"] is None
        assert "next_run" in schedule

    def test_create_schedule_with_interval(self, scheduler_manager):
        """Test creating a schedule with an interval trigger."""
        schedule = scheduler_manager.create_schedule(
            name="Hourly Sync",
            library_id="456",
            library_name="TV Shows",
            interval_minutes=60,
            enabled=True,
        )

        assert schedule["trigger_type"] == "interval"
        assert schedule["trigger_value"] == "60"
        assert schedule["enabled"] is True

    def test_create_schedule_disabled(self, scheduler_manager):
        """Test creating a disabled schedule."""
        schedule = scheduler_manager.create_schedule(
            name="Disabled",
            library_id="123",
            library_name="Movies",
            cron_expression="0 2 * * *",
            enabled=False,
        )

        assert schedule["enabled"] is False
        assert schedule["next_run"] is None

    def test_create_schedule_with_config(self, scheduler_manager):
        """Test creating a schedule with custom config."""
        config = {"quality": "high", "threads": 4}
        schedule = scheduler_manager.create_schedule(
            name="Custom",
            library_id="789",
            library_name="Movies",
            cron_expression="0 3 * * *",
            config=config,
        )

        assert schedule["config"] == config

    def test_create_schedule_no_trigger_raises(self, scheduler_manager):
        """Test that creating a schedule without cron or interval raises ValueError."""
        with pytest.raises(ValueError, match="cron_expression or interval_minutes"):
            scheduler_manager.create_schedule(
                name="Bad",
                library_id="123",
                library_name="Movies",
            )

    def test_get_schedule(self, scheduler_manager):
        """Test getting a single schedule by ID."""
        schedule = scheduler_manager.create_schedule(
            name="Test",
            library_id="123",
            library_name="Movies",
            cron_expression="0 2 * * *",
        )

        retrieved = scheduler_manager.get_schedule(schedule["id"])

        assert retrieved is not None
        assert retrieved["id"] == schedule["id"]
        assert retrieved["name"] == "Test"

    def test_get_nonexistent_schedule(self, scheduler_manager):
        """Test that getting a nonexistent schedule returns None."""
        assert scheduler_manager.get_schedule("nonexistent") is None

    def test_get_all_schedules(self, scheduler_manager):
        """Test getting all schedules."""
        scheduler_manager.create_schedule(
            name="S1",
            library_id="1",
            library_name="Movies",
            cron_expression="0 2 * * *",
        )
        scheduler_manager.create_schedule(
            name="S2",
            library_id="2",
            library_name="TV",
            interval_minutes=60,
        )

        schedules = scheduler_manager.get_all_schedules()

        assert len(schedules) == 2
        names = {s["name"] for s in schedules}
        assert names == {"S1", "S2"}

    def test_update_schedule_name(self, scheduler_manager):
        """Test updating a schedule's name."""
        schedule = scheduler_manager.create_schedule(
            name="Old Name",
            library_id="123",
            library_name="Movies",
            cron_expression="0 2 * * *",
        )

        updated = scheduler_manager.update_schedule(schedule["id"], name="New Name")

        assert updated is not None
        assert updated["name"] == "New Name"
        assert updated["library_id"] == "123"

    def test_update_schedule_trigger_cron_to_interval(self, scheduler_manager):
        """Test changing a schedule from cron to interval trigger."""
        schedule = scheduler_manager.create_schedule(
            name="Test",
            library_id="123",
            library_name="Movies",
            cron_expression="0 2 * * *",
        )

        updated = scheduler_manager.update_schedule(schedule["id"], interval_minutes=120)

        assert updated["trigger_type"] == "interval"
        assert updated["trigger_value"] == "120"

    def test_update_schedule_trigger_interval_to_cron(self, scheduler_manager):
        """Test changing a schedule from interval to cron trigger."""
        schedule = scheduler_manager.create_schedule(
            name="Test",
            library_id="123",
            library_name="Movies",
            interval_minutes=60,
        )

        updated = scheduler_manager.update_schedule(schedule["id"], cron_expression="0 3 * * *")

        assert updated["trigger_type"] == "cron"
        assert updated["trigger_value"] == "0 3 * * *"

    def test_update_nonexistent_schedule(self, scheduler_manager):
        """Test that updating a nonexistent schedule returns None."""
        assert scheduler_manager.update_schedule("nonexistent", name="X") is None

    def test_delete_schedule(self, scheduler_manager):
        """Test deleting a schedule."""
        schedule = scheduler_manager.create_schedule(
            name="Delete Me",
            library_id="123",
            library_name="Movies",
            cron_expression="0 2 * * *",
        )

        assert scheduler_manager.delete_schedule(schedule["id"]) is True
        assert scheduler_manager.get_schedule(schedule["id"]) is None

    def test_delete_nonexistent_schedule(self, scheduler_manager):
        """Test that deleting a nonexistent schedule returns False."""
        assert scheduler_manager.delete_schedule("nonexistent") is False


# ========================================================================
# Enable / Disable
# ========================================================================


class TestScheduleEnableDisable:
    """Tests for toggling schedule enabled state."""

    def test_enable_schedule(self, scheduler_manager):
        """Test enabling a disabled schedule."""
        schedule = scheduler_manager.create_schedule(
            name="Test",
            library_id="123",
            library_name="Movies",
            cron_expression="0 2 * * *",
            enabled=False,
        )

        updated = scheduler_manager.enable_schedule(schedule["id"])

        assert updated is not None
        assert updated["enabled"] is True

    def test_disable_schedule(self, scheduler_manager):
        """Test disabling an enabled schedule."""
        schedule = scheduler_manager.create_schedule(
            name="Test",
            library_id="123",
            library_name="Movies",
            cron_expression="0 2 * * *",
            enabled=True,
        )

        updated = scheduler_manager.disable_schedule(schedule["id"])

        assert updated is not None
        assert updated["enabled"] is False

    def test_enable_nonexistent(self, scheduler_manager):
        """Test that enabling a nonexistent schedule returns None."""
        assert scheduler_manager.enable_schedule("nonexistent") is None

    def test_disable_nonexistent(self, scheduler_manager):
        """Test that disabling a nonexistent schedule returns None."""
        assert scheduler_manager.disable_schedule("nonexistent") is None


# ========================================================================
# Run Now
# ========================================================================


class TestScheduleRunNow:
    """Tests for immediate schedule execution."""

    def test_run_now_with_callback(self, scheduler_manager):
        """Test run_now invokes the run_job_callback."""
        mock_callback = MagicMock()
        scheduler_manager.set_run_job_callback(mock_callback)

        schedule = scheduler_manager.create_schedule(
            name="Test",
            library_id="123",
            library_name="Movies",
            cron_expression="0 2 * * *",
        )

        result = scheduler_manager.run_now(schedule["id"])

        assert result is True
        mock_callback.assert_called_once_with(library_id="123", library_name="Movies", config={})

    def test_run_now_nonexistent(self, scheduler_manager):
        """Test that run_now on a nonexistent schedule returns False."""
        assert scheduler_manager.run_now("nonexistent") is False

    def test_run_now_updates_last_run(self, scheduler_manager):
        """Test that run_now sets the last_run timestamp."""
        mock_callback = MagicMock()
        scheduler_manager.set_run_job_callback(mock_callback)

        schedule = scheduler_manager.create_schedule(
            name="Test",
            library_id="123",
            library_name="Movies",
            cron_expression="0 2 * * *",
        )
        assert schedule["last_run"] is None

        scheduler_manager.run_now(schedule["id"])

        updated = scheduler_manager.get_schedule(schedule["id"])
        assert updated["last_run"] is not None


# ========================================================================
# Persistence
# ========================================================================


class TestSchedulePersistence:
    """Tests for schedule file persistence."""

    def test_schedules_survive_restart(self, tmp_path, monkeypatch):
        """Test that schedules persist after manager restart."""
        config_dir = str(tmp_path / "config")
        os.makedirs(config_dir, exist_ok=True)

        manager1 = ScheduleManager(config_dir=config_dir)
        monkeypatch.setattr("media_preview_generator.web.scheduler._schedule_manager", manager1)
        manager1.start()
        schedule = manager1.create_schedule(
            name="Persistent",
            library_id="123",
            library_name="Movies",
            cron_expression="0 2 * * *",
        )
        sid = schedule["id"]
        manager1.stop()

        manager2 = ScheduleManager(config_dir=config_dir)
        monkeypatch.setattr("media_preview_generator.web.scheduler._schedule_manager", manager2)
        manager2.start()

        retrieved = manager2.get_schedule(sid)
        assert retrieved is not None
        assert retrieved["name"] == "Persistent"
        assert retrieved["library_id"] == "123"

        manager2.stop()

    def test_handles_missing_file(self, tmp_path):
        """Test graceful handling of missing schedules.json."""
        config_dir = str(tmp_path / "config")
        os.makedirs(config_dir, exist_ok=True)

        manager = ScheduleManager(config_dir=config_dir)
        manager.start()

        assert manager.get_all_schedules() == []

        manager.stop()

    def test_handles_corrupt_file(self, tmp_path):
        """Test graceful handling of corrupt schedules.json."""
        config_dir = str(tmp_path / "config")
        os.makedirs(config_dir, exist_ok=True)

        with open(os.path.join(config_dir, "schedules.json"), "w") as f:
            f.write("{invalid json")

        manager = ScheduleManager(config_dir=config_dir)
        manager.start()

        assert manager.get_all_schedules() == []

        manager.stop()


# ========================================================================
# get_schedule_manager singleton
# ========================================================================


class TestGetScheduleManager:
    """Tests for the get_schedule_manager singleton factory."""

    def test_returns_singleton(self, tmp_path, monkeypatch):
        """Test that get_schedule_manager returns the same instance."""
        monkeypatch.setattr("media_preview_generator.web.scheduler._schedule_manager", None)

        config_dir = str(tmp_path / "config")
        os.makedirs(config_dir, exist_ok=True)

        m1 = get_schedule_manager(config_dir=config_dir)
        m2 = get_schedule_manager(config_dir=config_dir)

        assert m1 is m2
        m1.stop()

    def test_sets_callback_on_existing(self, tmp_path, monkeypatch):
        """Test that a callback can be set on an existing singleton."""
        monkeypatch.setattr("media_preview_generator.web.scheduler._schedule_manager", None)

        config_dir = str(tmp_path / "config")
        os.makedirs(config_dir, exist_ok=True)

        mock_cb = MagicMock()
        m1 = get_schedule_manager(config_dir=config_dir)
        m2 = get_schedule_manager(run_job_callback=mock_cb)

        assert m1 is m2
        assert m2.run_job_callback is mock_cb


# ========================================================================
# Scheduled job dispatch (full_library vs recently_added)
# ========================================================================


class TestExecuteScheduledJobDispatch:
    """Verify that execute_scheduled_job branches correctly on config.job_type."""

    def test_dispatches_full_library_by_default(self, scheduler_manager):
        """A schedule with no job_type in config goes through run_job_callback."""
        from media_preview_generator.web.scheduler import execute_scheduled_job

        mock_callback = MagicMock()
        scheduler_manager.set_run_job_callback(mock_callback)

        schedule = scheduler_manager.create_schedule(
            name="Test",
            library_id="123",
            library_name="Movies",
            cron_expression="0 2 * * *",
        )

        execute_scheduled_job(
            schedule["id"],
            library_id="123",
            library_name="Movies",
            config={},
        )

        mock_callback.assert_called_once()
        kwargs = mock_callback.call_args.kwargs
        assert kwargs["library_id"] == "123"
        assert kwargs["library_name"] == "Movies"

    def test_dispatches_recently_added_calls_multi_server_scan(self, scheduler_manager, monkeypatch):
        """A schedule with job_type='recently_added' invokes the multi-server
        recently-added dispatcher (Phase E — works for any vendor)."""
        from media_preview_generator.web import scheduler as sched_mod

        mock_scan = MagicMock(return_value={})
        monkeypatch.setattr(
            "media_preview_generator.jobs.orchestrator._run_recently_added_multi_server",
            mock_scan,
        )
        # Stub the heavy load_config / build_selected_gpus paths so the test
        # stays focused on the dispatch contract.
        monkeypatch.setattr("media_preview_generator.config.load_config", MagicMock(return_value=MagicMock()))
        monkeypatch.setattr(
            "media_preview_generator.web.routes.job_runner._build_selected_gpus",
            MagicMock(return_value=[]),
        )

        schedule = scheduler_manager.create_schedule(
            name="Scanner",
            library_id="2",
            library_name="TV",
            interval_minutes=15,
            config={"job_type": "recently_added", "lookback_hours": 2},
        )

        sched_mod.execute_scheduled_job(
            schedule["id"],
            library_id="2",
            library_name="TV",
            config={"job_type": "recently_added", "lookback_hours": 2},
        )

        mock_scan.assert_called_once()
        kwargs = mock_scan.call_args.kwargs
        assert kwargs["library_ids"] == ["2"]
        assert kwargs["lookback_hours"] == 2.0

    def test_dispatches_recently_added_with_no_library_passes_none(self, scheduler_manager, monkeypatch):
        """No library_id = library_ids=None reaches the multi-server scan."""
        from media_preview_generator.web import scheduler as sched_mod

        mock_scan = MagicMock(return_value={})
        monkeypatch.setattr(
            "media_preview_generator.jobs.orchestrator._run_recently_added_multi_server",
            mock_scan,
        )
        monkeypatch.setattr("media_preview_generator.config.load_config", MagicMock(return_value=MagicMock()))
        monkeypatch.setattr(
            "media_preview_generator.web.routes.job_runner._build_selected_gpus",
            MagicMock(return_value=[]),
        )

        schedule = scheduler_manager.create_schedule(
            name="Scanner",
            library_id=None,
            library_name="",
            interval_minutes=15,
            config={"job_type": "recently_added", "lookback_hours": 1},
        )

        sched_mod.execute_scheduled_job(
            schedule["id"],
            library_id=None,
            library_name="",
            config={"job_type": "recently_added", "lookback_hours": 1},
        )

        mock_scan.assert_called_once()
        assert mock_scan.call_args.kwargs["library_ids"] is None
        assert mock_scan.call_args.kwargs["lookback_hours"] == 1.0

    def test_recently_added_dispatch_clamps_invalid_lookback(self, scheduler_manager, monkeypatch):
        """Garbage lookback_hours values are coerced to a safe default."""
        from media_preview_generator.web import scheduler as sched_mod

        mock_scan = MagicMock(return_value={})
        monkeypatch.setattr(
            "media_preview_generator.jobs.orchestrator._run_recently_added_multi_server",
            mock_scan,
        )
        monkeypatch.setattr("media_preview_generator.config.load_config", MagicMock(return_value=MagicMock()))
        monkeypatch.setattr(
            "media_preview_generator.web.routes.job_runner._build_selected_gpus",
            MagicMock(return_value=[]),
        )

        schedule = scheduler_manager.create_schedule(
            name="Scanner",
            interval_minutes=15,
            config={"job_type": "recently_added", "lookback_hours": "nope"},
        )

        sched_mod.execute_scheduled_job(
            schedule["id"],
            library_id=None,
            library_name="",
            config={"job_type": "recently_added", "lookback_hours": "nope"},
        )

        # Falls back to 1.0 hour default
        mock_scan.assert_called_once()
        assert mock_scan.call_args.kwargs["lookback_hours"] == 1.0

    def test_recently_added_dispatch_updates_last_run(self, scheduler_manager, monkeypatch):
        """After dispatching a recently_added scan the schedule's last_run updates."""
        from media_preview_generator.web import scheduler as sched_mod

        monkeypatch.setattr(
            "media_preview_generator.jobs.orchestrator._run_recently_added_multi_server",
            MagicMock(return_value={}),
        )
        monkeypatch.setattr("media_preview_generator.config.load_config", MagicMock(return_value=MagicMock()))
        monkeypatch.setattr(
            "media_preview_generator.web.routes.job_runner._build_selected_gpus",
            MagicMock(return_value=[]),
        )

        schedule = scheduler_manager.create_schedule(
            name="Scanner",
            interval_minutes=15,
            config={"job_type": "recently_added", "lookback_hours": 1},
        )
        assert schedule["last_run"] is None

        sched_mod.execute_scheduled_job(
            schedule["id"],
            library_id=None,
            library_name="",
            config={"job_type": "recently_added", "lookback_hours": 1},
        )

        updated = scheduler_manager.get_schedule(schedule["id"])
        assert updated["last_run"] is not None


class TestMultiLibrarySchedules:
    """Phase H7: Schedule.library_ids list + back-compat migration."""

    def test_create_schedule_with_multiple_libraries(self, scheduler_manager):
        schedule = scheduler_manager.create_schedule(
            name="Multi",
            interval_minutes=60,
            library_ids=["1", "2", "3"],
            library_name="Movies, TV, Anime",
        )
        assert schedule["library_ids"] == ["1", "2", "3"]
        # library_id stays None when there's more than one library.
        assert schedule["library_id"] is None

    def test_create_schedule_with_single_library_keeps_back_compat(self, scheduler_manager):
        schedule = scheduler_manager.create_schedule(
            name="Single",
            interval_minutes=60,
            library_ids=["7"],
            library_name="Movies",
        )
        assert schedule["library_ids"] == ["7"]
        assert schedule["library_id"] == "7"  # mirrored for legacy readers

    def test_legacy_library_id_arg_still_works(self, scheduler_manager):
        # Older callers that still pass library_id= keep working.
        schedule = scheduler_manager.create_schedule(
            name="Legacy",
            interval_minutes=60,
            library_id="42",
            library_name="Movies",
        )
        assert schedule["library_id"] == "42"
        assert schedule["library_ids"] == ["42"]

    def test_load_migrates_legacy_library_id_to_library_ids(self, tmp_path):
        """Schedules persisted in pre-H7 shape (only library_id) should migrate
        to library_ids on read so the rest of the codebase stays uniform."""
        import json

        config_dir = str(tmp_path / "config")
        os.makedirs(config_dir, exist_ok=True)
        legacy = {
            "schedules": {
                "abc": {
                    "id": "abc",
                    "name": "Legacy",
                    "trigger_type": "interval",
                    "trigger_value": "60",
                    "library_id": "5",
                    "library_name": "Movies",
                    "config": {},
                    "enabled": True,
                    "created_at": "2025-01-01T00:00:00+00:00",
                    "last_run": None,
                    "next_run": None,
                    "priority": None,
                }
            }
        }
        with open(os.path.join(config_dir, "schedules.json"), "w") as f:
            json.dump(legacy, f)

        manager = ScheduleManager(config_dir=config_dir, run_job_callback=None)
        sched = manager.get_schedule("abc")
        assert sched["library_ids"] == ["5"]  # promoted on load
        assert sched["library_id"] == "5"  # original kept

    def test_update_schedule_with_library_ids(self, scheduler_manager):
        sched = scheduler_manager.create_schedule(
            name="X",
            interval_minutes=60,
            library_id="1",
            library_name="Movies",
        )
        updated = scheduler_manager.update_schedule(
            sched["id"],
            library_ids=["1", "2", "3"],
            library_name="Movies, TV, Anime",
        )
        assert updated["library_ids"] == ["1", "2", "3"]
        assert updated["library_id"] is None  # cleared when multi
