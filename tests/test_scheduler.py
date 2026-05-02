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
# Per-schedule stop_time (D20)
# ========================================================================


class TestScheduleStopTime:
    """D20 — optional stop_time pauses jobs at a daily time-of-day."""

    def test_create_with_stop_time_registers_stop_cron(self, scheduler_manager):
        """create_schedule with stop_time MUST register both the start and
        the {id}__stop APScheduler jobs so the daily pause actually fires."""
        schedule = scheduler_manager.create_schedule(
            name="Overnight",
            library_id="1",
            library_name="Movies",
            cron_expression="0 1 * * *",
            stop_time="06:00",
        )
        sid = schedule["id"]
        assert schedule["stop_time"] == "06:00"

        all_ids = {j.id for j in scheduler_manager.scheduler.get_jobs()}
        assert sid in all_ids
        assert f"{sid}__stop" in all_ids

    def test_create_without_stop_time_only_registers_start_cron(self, scheduler_manager):
        schedule = scheduler_manager.create_schedule(
            name="Plain",
            library_id="1",
            library_name="Movies",
            cron_expression="0 1 * * *",
        )
        sid = schedule["id"]
        all_ids = {j.id for j in scheduler_manager.scheduler.get_jobs()}
        assert sid in all_ids
        assert f"{sid}__stop" not in all_ids
        assert schedule.get("stop_time", "") == ""

    def test_update_clearing_stop_time_removes_stop_cron(self, scheduler_manager):
        schedule = scheduler_manager.create_schedule(
            name="Overnight",
            library_id="1",
            library_name="Movies",
            cron_expression="0 1 * * *",
            stop_time="06:00",
        )
        sid = schedule["id"]
        assert f"{sid}__stop" in {j.id for j in scheduler_manager.scheduler.get_jobs()}

        scheduler_manager.update_schedule(schedule_id=sid, stop_time="")
        ids_after = {j.id for j in scheduler_manager.scheduler.get_jobs()}
        assert f"{sid}__stop" not in ids_after
        assert scheduler_manager.get_schedule(sid)["stop_time"] == ""

    def test_update_setting_stop_time_adds_stop_cron(self, scheduler_manager):
        schedule = scheduler_manager.create_schedule(
            name="Overnight",
            library_id="1",
            library_name="Movies",
            cron_expression="0 1 * * *",
        )
        sid = schedule["id"]
        scheduler_manager.update_schedule(schedule_id=sid, stop_time="06:00")
        all_ids = {j.id for j in scheduler_manager.scheduler.get_jobs()}
        assert f"{sid}__stop" in all_ids
        assert scheduler_manager.get_schedule(sid)["stop_time"] == "06:00"

    def test_delete_removes_stop_cron(self, scheduler_manager):
        schedule = scheduler_manager.create_schedule(
            name="Overnight",
            library_id="1",
            library_name="Movies",
            cron_expression="0 1 * * *",
            stop_time="06:00",
        )
        sid = schedule["id"]
        scheduler_manager.delete_schedule(sid)
        ids_after = {j.id for j in scheduler_manager.scheduler.get_jobs()}
        assert sid not in ids_after
        assert f"{sid}__stop" not in ids_after

    def test_interval_trigger_silently_drops_stop_time(self, scheduler_manager):
        """stop_time is meaningless for interval triggers (every X minutes
        has no time-of-day to stop at). create_schedule clears it
        automatically rather than rejecting the request."""
        schedule = scheduler_manager.create_schedule(
            name="Periodic",
            library_id="1",
            library_name="Movies",
            interval_minutes=30,
            stop_time="06:00",
        )
        assert schedule.get("stop_time", "") == ""
        sid = schedule["id"]
        assert f"{sid}__stop" not in {j.id for j in scheduler_manager.scheduler.get_jobs()}

    def test_invalid_stop_time_raises(self, scheduler_manager):
        with pytest.raises(ValueError):
            scheduler_manager.create_schedule(
                name="Bad",
                library_id="1",
                library_name="Movies",
                cron_expression="0 1 * * *",
                stop_time="25:99",
            )

    def test_parse_hhmm_helper_edge_cases(self):
        from media_preview_generator.web.scheduler import _parse_hhmm

        assert _parse_hhmm("") is None
        assert _parse_hhmm(None) is None
        assert _parse_hhmm("06:00") == (6, 0)
        assert _parse_hhmm("23:59") == (23, 59)
        assert _parse_hhmm("0:0") == (0, 0)
        with pytest.raises(ValueError):
            _parse_hhmm("24:00")
        with pytest.raises(ValueError):
            _parse_hhmm("12:60")
        with pytest.raises(ValueError):
            _parse_hhmm("abc")
        with pytest.raises(ValueError):
            _parse_hhmm("12")


class TestQuietHours:
    """D21 — global queue pause/resume schedule."""

    def test_is_in_quiet_window_equal_times_disables(self):
        from media_preview_generator.web.scheduler import is_in_quiet_window

        assert is_in_quiet_window((10, 0), (8, 0), (8, 0)) is False
        assert is_in_quiet_window((0, 0), (0, 0), (0, 0)) is False

    def test_is_in_quiet_window_same_day_window(self):
        from media_preview_generator.web.scheduler import is_in_quiet_window

        # Window is 09:00–17:00. start inclusive, end exclusive.
        assert is_in_quiet_window((9, 0), (9, 0), (17, 0)) is True
        assert is_in_quiet_window((10, 30), (9, 0), (17, 0)) is True
        assert is_in_quiet_window((17, 0), (9, 0), (17, 0)) is False
        assert is_in_quiet_window((8, 59), (9, 0), (17, 0)) is False
        assert is_in_quiet_window((22, 0), (9, 0), (17, 0)) is False

    def test_is_in_quiet_window_cross_midnight(self):
        from media_preview_generator.web.scheduler import is_in_quiet_window

        # Window is 22:00–06:00 (overnight). Pause = 22:00, resume = 06:00.
        assert is_in_quiet_window((22, 0), (22, 0), (6, 0)) is True
        assert is_in_quiet_window((23, 30), (22, 0), (6, 0)) is True
        assert is_in_quiet_window((0, 0), (22, 0), (6, 0)) is True
        assert is_in_quiet_window((5, 59), (22, 0), (6, 0)) is True
        assert is_in_quiet_window((6, 0), (22, 0), (6, 0)) is False
        assert is_in_quiet_window((12, 0), (22, 0), (6, 0)) is False

    def test_apply_quiet_hours_enabled_registers_both_crons(self, scheduler_manager):
        # D21 legacy single-window body — accepted via normalise_quiet_hours
        # and persisted as window #0; registered IDs use the D26 per-window
        # prefix.
        scheduler_manager.apply_quiet_hours({"enabled": True, "start": "08:00", "end": "01:00"})
        ids = {j.id for j in scheduler_manager.scheduler.get_jobs()}
        assert "__qh_pause_0" in ids
        assert "__qh_resume_0" in ids

    def test_apply_quiet_hours_disabled_removes_both_crons(self, scheduler_manager):
        scheduler_manager.apply_quiet_hours({"enabled": True, "start": "08:00", "end": "01:00"})
        scheduler_manager.apply_quiet_hours({"enabled": False, "start": "08:00", "end": "01:00"})
        ids = {j.id for j in scheduler_manager.scheduler.get_jobs()}
        assert not any(jid.startswith("__qh_pause_") for jid in ids)
        assert not any(jid.startswith("__qh_resume_") for jid in ids)
        assert "__quiet_hours_pause" not in ids
        assert "__quiet_hours_resume" not in ids

    def test_apply_quiet_hours_equal_times_treated_as_disabled(self, scheduler_manager):
        scheduler_manager.apply_quiet_hours({"enabled": True, "start": "08:00", "end": "08:00"})
        ids = {j.id for j in scheduler_manager.scheduler.get_jobs()}
        assert not any(jid.startswith("__qh_pause_") for jid in ids)
        assert not any(jid.startswith("__qh_resume_") for jid in ids)

    def test_apply_quiet_hours_malformed_times_skipped(self, scheduler_manager):
        # Malformed times should NOT raise; just skip cron registration.
        scheduler_manager.apply_quiet_hours({"enabled": True, "start": "25:00", "end": "01:00"})
        ids = {j.id for j in scheduler_manager.scheduler.get_jobs()}
        assert not any(jid.startswith("__qh_pause_") for jid in ids)
        assert not any(jid.startswith("__qh_resume_") for jid in ids)

    def test_execute_scheduled_job_skipped_when_processing_paused(self, scheduler_manager, monkeypatch):
        """D21 — when the global queue is paused (manual or quiet hours),
        a scheduled trigger MUST NOT spawn a new Job. Otherwise an
        every-15-min recently_added schedule would balloon the queue."""
        from media_preview_generator.web import scheduler as sched_mod

        called = []
        scheduler_manager.set_run_job_callback(lambda **kw: called.append(kw))

        schedule = scheduler_manager.create_schedule(
            name="GatedSchedule",
            library_id="1",
            library_name="Movies",
            cron_expression="0 2 * * *",
        )
        # Stub settings_manager.processing_paused = True
        fake_sm = MagicMock()
        fake_sm.processing_paused = True
        monkeypatch.setattr(
            "media_preview_generator.web.settings_manager.get_settings_manager",
            lambda: fake_sm,
        )
        sched_mod.execute_scheduled_job(
            schedule["id"],
            ["1"],
            "Movies",
            {},
            None,
            None,
        )
        assert called == [], "callback fired despite processing_paused=True"


class TestQuietHoursMultiWindow:
    """D26 — multi-window Quiet Hours with per-window day-of-week filter."""

    def test_normalise_legacy_single_window_form(self):
        from media_preview_generator.web.scheduler import normalise_quiet_hours

        out = normalise_quiet_hours({"enabled": True, "start": "08:00", "end": "01:00"})
        assert out["enabled"] is True
        assert len(out["windows"]) == 1
        assert out["windows"][0]["start"] == "08:00"
        assert out["windows"][0]["end"] == "01:00"
        # Legacy form has no day filter — should default to all 7 days.
        assert set(out["windows"][0]["days"]) == {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}

    def test_normalise_multi_window_passes_through(self):
        from media_preview_generator.web.scheduler import normalise_quiet_hours

        raw = {
            "enabled": True,
            "windows": [
                {"start": "08:00", "end": "17:00", "days": ["mon", "tue", "wed", "thu", "fri"]},
                {"start": "22:00", "end": "06:00", "days": ["sat", "sun"]},
            ],
        }
        out = normalise_quiet_hours(raw)
        assert len(out["windows"]) == 2
        assert out["windows"][0]["days"] == ["mon", "tue", "wed", "thu", "fri"]
        assert out["windows"][1]["days"] == ["sat", "sun"]

    def test_normalise_strips_unknown_day_names(self):
        from media_preview_generator.web.scheduler import normalise_quiet_hours

        raw = {"enabled": True, "windows": [{"start": "08:00", "end": "17:00", "days": ["mon", "BOGUS", "fri"]}]}
        out = normalise_quiet_hours(raw)
        assert out["windows"][0]["days"] == ["mon", "fri"]

    def test_normalise_empty_days_list_falls_back_to_all_seven(self):
        from media_preview_generator.web.scheduler import normalise_quiet_hours

        out = normalise_quiet_hours({"enabled": True, "windows": [{"start": "08:00", "end": "17:00", "days": []}]})
        assert len(out["windows"][0]["days"]) == 7

    def test_normalise_handles_none_input(self):
        from media_preview_generator.web.scheduler import normalise_quiet_hours

        out = normalise_quiet_hours(None)
        assert out == {"enabled": False, "windows": []}

    def test_is_now_in_any_quiet_window_respects_day_of_week(self):
        from datetime import datetime as _dt

        from media_preview_generator.web.scheduler import is_now_in_any_quiet_window

        qh = {"enabled": True, "windows": [{"start": "08:00", "end": "17:00", "days": ["mon"]}]}
        # 2026-05-04 is a Monday, 2026-05-05 a Tuesday.
        mon_inside = _dt(2026, 5, 4, 12, 0)
        tue_inside_clock = _dt(2026, 5, 5, 12, 0)
        assert is_now_in_any_quiet_window(qh, mon_inside) is True
        assert is_now_in_any_quiet_window(qh, tue_inside_clock) is False

    def test_is_now_in_any_quiet_window_two_windows_either_active(self):
        from datetime import datetime as _dt

        from media_preview_generator.web.scheduler import is_now_in_any_quiet_window

        qh = {
            "enabled": True,
            "windows": [
                # Weekday daytime
                {"start": "08:00", "end": "17:00", "days": ["mon", "tue", "wed", "thu", "fri"]},
                # Weekend overnight (cross-midnight)
                {"start": "22:00", "end": "06:00", "days": ["sat", "sun"]},
            ],
        }
        # 2026-05-04 Mon 12:00 → first window active
        assert is_now_in_any_quiet_window(qh, _dt(2026, 5, 4, 12, 0)) is True
        # 2026-05-09 Sat 02:00 → second window active (cross-midnight)
        assert is_now_in_any_quiet_window(qh, _dt(2026, 5, 9, 2, 0)) is True
        # 2026-05-04 Mon 22:00 → no active window (weekday-night not covered)
        assert is_now_in_any_quiet_window(qh, _dt(2026, 5, 4, 22, 0)) is False

    def test_is_now_in_any_quiet_window_disabled_returns_false(self):
        from datetime import datetime as _dt

        from media_preview_generator.web.scheduler import is_now_in_any_quiet_window

        qh = {
            "enabled": False,
            "windows": [{"start": "08:00", "end": "17:00", "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]}],
        }
        assert is_now_in_any_quiet_window(qh, _dt(2026, 5, 4, 12, 0)) is False

    def test_apply_quiet_hours_two_windows_registers_two_pairs(self, scheduler_manager):
        scheduler_manager.apply_quiet_hours(
            {
                "enabled": True,
                "windows": [
                    {"start": "08:00", "end": "17:00", "days": ["mon", "tue", "wed", "thu", "fri"]},
                    {"start": "22:00", "end": "06:00", "days": ["sat", "sun"]},
                ],
            }
        )
        ids = {j.id for j in scheduler_manager.scheduler.get_jobs()}
        assert "__qh_pause_0" in ids
        assert "__qh_resume_0" in ids
        assert "__qh_pause_1" in ids
        assert "__qh_resume_1" in ids

    def test_apply_quiet_hours_rebuilds_cleanly_on_reapply(self, scheduler_manager):
        # First apply: 3 windows
        scheduler_manager.apply_quiet_hours(
            {
                "enabled": True,
                "windows": [
                    {"start": "08:00", "end": "10:00", "days": ["mon"]},
                    {"start": "12:00", "end": "13:00", "days": ["tue"]},
                    {"start": "15:00", "end": "16:00", "days": ["wed"]},
                ],
            }
        )
        # Reapply with 1 window — the other two pairs should be removed.
        scheduler_manager.apply_quiet_hours(
            {
                "enabled": True,
                "windows": [{"start": "08:00", "end": "17:00", "days": ["fri"]}],
            }
        )
        ids = {j.id for j in scheduler_manager.scheduler.get_jobs()}
        qh_ids = {jid for jid in ids if jid.startswith("__qh_")}
        assert qh_ids == {"__qh_pause_0", "__qh_resume_0"}

    def test_apply_quiet_hours_window_with_no_valid_days_skipped(self, scheduler_manager):
        scheduler_manager.apply_quiet_hours(
            {
                "enabled": True,
                "windows": [
                    {"start": "08:00", "end": "10:00", "days": ["BOGUS"]},
                    {"start": "12:00", "end": "13:00", "days": ["tue"]},
                ],
            }
        )
        ids = {j.id for j in scheduler_manager.scheduler.get_jobs()}
        # First window had no valid days → normalise_quiet_hours fell back to
        # all 7, so it IS registered as window #0; second window is #1.
        # Both pairs should land.
        assert "__qh_pause_0" in ids
        assert "__qh_pause_1" in ids


class TestExecuteScheduleStop:
    """D20 — the stop handler pauses the right jobs and ignores others."""

    def test_pauses_only_jobs_from_this_schedule(self, scheduler_manager, monkeypatch):
        from media_preview_generator.web import scheduler as sched_mod
        from media_preview_generator.web.jobs import JobStatus

        # Build a fake JobManager whose get_all_jobs returns three jobs:
        # one running from this schedule, one running from a sibling
        # schedule, one already paused. Only the first should be paused.
        sid = "sched-A"
        other_sid = "sched-B"

        target_job = MagicMock(
            id="job-1",
            parent_schedule_id=sid,
            status=JobStatus.RUNNING,
            paused=False,
        )
        sibling_job = MagicMock(
            id="job-2",
            parent_schedule_id=other_sid,
            status=JobStatus.RUNNING,
            paused=False,
        )
        already_paused = MagicMock(
            id="job-3",
            parent_schedule_id=sid,
            status=JobStatus.RUNNING,
            paused=True,
        )

        fake_jm = MagicMock()
        fake_jm.get_all_jobs.return_value = [target_job, sibling_job, already_paused]
        fake_jm.request_pause.return_value = True
        monkeypatch.setattr(
            "media_preview_generator.web.jobs.get_job_manager",
            lambda: fake_jm,
        )
        scheduler_manager._schedules[sid] = {"id": sid, "name": "Overnight", "stop_time": "06:00"}
        monkeypatch.setattr(sched_mod, "_schedule_manager", scheduler_manager)

        sched_mod.execute_schedule_stop(sid)

        fake_jm.request_pause.assert_called_once_with("job-1")
        # Sibling-schedule and already-paused jobs MUST NOT be touched.
        assert all(call.args[0] != "job-2" for call in fake_jm.request_pause.call_args_list)
        assert all(call.args[0] != "job-3" for call in fake_jm.request_pause.call_args_list)


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
        # D20 — every scheduled callback now carries parent_schedule_id so
        # the spawned Job can later be paused by the schedule's stop_time
        # cron and resumed by the next start tick.
        mock_callback.assert_called_once_with(
            library_id="123",
            library_name="Movies",
            config={},
            parent_schedule_id=schedule["id"],
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

    def test_schedules_re_register_with_apscheduler_after_jobstore_wipe(self, tmp_path, monkeypatch):
        """D30 — schedules.json is the source of truth; the SQLAlchemy
        jobstore is just a derived cache. If scheduler.db is wiped (or
        was never persisted because of an earlier bug), the schedules
        should ALL re-register with APScheduler at next load. Without
        this, the canary observed 3 schedules in JSON but 0 jobs in
        apscheduler_jobs and crons silently never fired."""
        config_dir = str(tmp_path / "config")
        os.makedirs(config_dir, exist_ok=True)

        manager1 = ScheduleManager(config_dir=config_dir)
        monkeypatch.setattr("media_preview_generator.web.scheduler._schedule_manager", manager1)
        manager1.start()
        s1 = manager1.create_schedule(
            name="TV Daily",
            library_id="2",
            library_name="TV Shows",
            cron_expression="0 14 * * *",
        )
        s2 = manager1.create_schedule(
            name="Movies Daily",
            library_id="1",
            library_name="Movies",
            cron_expression="0 1 * * *",
        )
        s3 = manager1.create_schedule(
            name="Sports",
            library_id="12",
            library_name="Sports",
            cron_expression="0 2 * * *",
            enabled=False,  # disabled — must NOT register
        )
        manager1.stop()

        # Simulate jobstore wipe — delete scheduler.db so the next manager
        # boots with an empty jobstore. schedules.json is preserved.
        sched_db = os.path.join(config_dir, "scheduler.db")
        if os.path.exists(sched_db):
            os.remove(sched_db)

        manager2 = ScheduleManager(config_dir=config_dir)
        monkeypatch.setattr("media_preview_generator.web.scheduler._schedule_manager", manager2)
        manager2.start()

        # Both enabled schedules must be re-registered with APScheduler.
        registered_ids = {j.id for j in manager2.scheduler.get_jobs()}
        assert s1["id"] in registered_ids, f"Enabled schedule s1 missing after jobstore wipe; got {registered_ids}"
        assert s2["id"] in registered_ids, f"Enabled schedule s2 missing after jobstore wipe; got {registered_ids}"
        # Disabled one must NOT be registered.
        assert s3["id"] not in registered_ids, (
            f"Disabled schedule s3 should not have been re-registered; got {registered_ids}"
        )
        # And each registered cron should have a future next_run_time.
        for j in manager2.scheduler.get_jobs():
            if j.id in (s1["id"], s2["id"]):
                assert j.next_run_time is not None, f"Schedule {j.id} has no next_run_time"

        manager2.stop()

    def test_schedules_re_register_preserves_stop_time_cron(self, tmp_path, monkeypatch):
        """D30 — re-registration must also restore the per-schedule D20
        stop-cron, otherwise schedules with a stop_time would lose their
        nightly pause behaviour after a restart."""
        config_dir = str(tmp_path / "config")
        os.makedirs(config_dir, exist_ok=True)

        manager1 = ScheduleManager(config_dir=config_dir)
        monkeypatch.setattr("media_preview_generator.web.scheduler._schedule_manager", manager1)
        manager1.start()
        s = manager1.create_schedule(
            name="WithStop",
            library_id="2",
            library_name="TV Shows",
            cron_expression="0 1 * * *",
            stop_time="06:00",
        )
        manager1.stop()

        sched_db = os.path.join(config_dir, "scheduler.db")
        if os.path.exists(sched_db):
            os.remove(sched_db)

        manager2 = ScheduleManager(config_dir=config_dir)
        monkeypatch.setattr("media_preview_generator.web.scheduler._schedule_manager", manager2)
        manager2.start()

        registered_ids = {j.id for j in manager2.scheduler.get_jobs()}
        assert s["id"] in registered_ids
        assert f"{s['id']}__stop" in registered_ids, f"D20 stop-cron missing after jobstore wipe; got {registered_ids}"
        manager2.stop()


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
