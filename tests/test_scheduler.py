"""Tests for plex_generate_previews.web.scheduler."""

import os

import pytest
from unittest.mock import MagicMock

from plex_generate_previews.web.scheduler import (
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
    monkeypatch.setattr(
        "plex_generate_previews.web.scheduler._schedule_manager", manager
    )
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

        updated = scheduler_manager.update_schedule(
            schedule["id"], interval_minutes=120
        )

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

        updated = scheduler_manager.update_schedule(
            schedule["id"], cron_expression="0 3 * * *"
        )

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
        mock_callback.assert_called_once_with(
            library_id="123", library_name="Movies", config={}
        )

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
        monkeypatch.setattr(
            "plex_generate_previews.web.scheduler._schedule_manager", manager1
        )
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
        monkeypatch.setattr(
            "plex_generate_previews.web.scheduler._schedule_manager", manager2
        )
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
        monkeypatch.setattr(
            "plex_generate_previews.web.scheduler._schedule_manager", None
        )

        config_dir = str(tmp_path / "config")
        os.makedirs(config_dir, exist_ok=True)

        m1 = get_schedule_manager(config_dir=config_dir)
        m2 = get_schedule_manager(config_dir=config_dir)

        assert m1 is m2
        m1.stop()

    def test_sets_callback_on_existing(self, tmp_path, monkeypatch):
        """Test that a callback can be set on an existing singleton."""
        monkeypatch.setattr(
            "plex_generate_previews.web.scheduler._schedule_manager", None
        )

        config_dir = str(tmp_path / "config")
        os.makedirs(config_dir, exist_ok=True)

        mock_cb = MagicMock()
        m1 = get_schedule_manager(config_dir=config_dir)
        m2 = get_schedule_manager(run_job_callback=mock_cb)

        assert m1 is m2
        assert m2.run_job_callback is mock_cb
        m1.stop()
