"""Tests for ScheduleManager's load_status + recover_from_backup flow.

Pre-fix the loader logged the PermissionError and recovery hint to stderr,
but the Schedules page rendered an empty list with no hint that schedules
existed but failed to load. Live regression 2026-05-10: the user's
``/config/schedules.json`` shipped as ``root:root 0600`` while gunicorn
ran as ``abc:abc`` (linuxserver.io PUID/PGID). Empty list, no banner,
user thought their schedules were gone.

Fix surfaces the load failure in two places:
  * ``ScheduleManager.load_status`` carries a structured block the API
    can return alongside the (empty) schedules list.
  * ``recover_schedules_from_backup`` atomically restores from the newest
    backup file and reloads the in-process schedule list.

Matrix coverage per .claude/rules/testing.md:
  * load_status: ok / permission_denied / corrupt_json / load_failed
  * recover: success / no_backup / backup_unreadable / backup_invalid /
    write_failed / chown_failed
"""

from __future__ import annotations

import json
import os
import time
from unittest.mock import patch

import pytest

from media_preview_generator.web.scheduler import ScheduleManager


@pytest.fixture(autouse=True)
def _reset_schedule_singleton():
    import media_preview_generator.web.scheduler as sched_mod

    with sched_mod._schedule_lock:
        if sched_mod._schedule_manager is not None:
            try:
                sched_mod._schedule_manager.stop()
            except Exception:
                pass
        sched_mod._schedule_manager = None
    yield
    with sched_mod._schedule_lock:
        if sched_mod._schedule_manager is not None:
            try:
                sched_mod._schedule_manager.stop()
            except Exception:
                pass
        sched_mod._schedule_manager = None


def _make_manager(tmp_path):
    """Create a ScheduleManager scoped to tmp_path."""
    return ScheduleManager(config_dir=str(tmp_path), run_job_callback=lambda *a, **kw: None)


def _write_schedules_file(tmp_path, payload, mode=0o600):
    path = tmp_path / "schedules.json"
    path.write_text(json.dumps(payload))
    os.chmod(path, mode)
    return path


class TestLoadStatusOk:
    def test_no_file_yields_ok_status(self, tmp_path):
        manager = _make_manager(tmp_path)
        try:
            assert manager.load_status == {"status": "ok"}
        finally:
            manager.stop()

    def test_valid_file_yields_ok_status(self, tmp_path):
        _write_schedules_file(tmp_path, {"schedules": {}})
        manager = _make_manager(tmp_path)
        try:
            assert manager.load_status == {"status": "ok"}
        finally:
            manager.stop()


class TestLoadStatusPermissionDenied:
    def test_permission_error_surfaces_recovery_hint(self, tmp_path):
        # Real PermissionError via mocked open() so the test doesn't
        # depend on the test runner's UID being able to chown around.
        _write_schedules_file(tmp_path, {"schedules": {}})
        with patch("builtins.open", side_effect=PermissionError(13, "Permission denied")):
            manager = _make_manager(tmp_path)
        try:
            status = manager.load_status
            assert status["status"] == "permission_denied"
            assert status["error_type"] == "PermissionError"
            assert "recovery_hint" in status
            assert "chown" in status["recovery_hint"], (
                "Recovery hint must explicitly call out chown — that's the action "
                "the user needs to take when running unprivileged in a container."
            )
            # Surface the process user so the user knows what to chown TO.
            assert "process_user" in status
        finally:
            manager.stop()

    def test_permission_error_without_backup_omits_recover_button_path(self, tmp_path):
        # No .bak file exists.
        _write_schedules_file(tmp_path, {"schedules": {}})
        with patch("builtins.open", side_effect=PermissionError(13, "Permission denied")):
            manager = _make_manager(tmp_path)
        try:
            assert manager.load_status["status"] == "permission_denied"
            assert manager.load_status.get("backup_path") is None, (
                "No .bak in the config dir → backup_path must be None so the UI "
                "renders manual `chown` instructions instead of a Recover button."
            )
        finally:
            manager.stop()

    def test_permission_error_with_backup_includes_backup_path(self, tmp_path):
        _write_schedules_file(tmp_path, {"schedules": {}})
        bak = tmp_path / "schedules.json.bak"
        bak.write_text(json.dumps({"schedules": {"a": {"name": "Old", "enabled": True}}}))
        with patch("builtins.open", side_effect=PermissionError(13, "Permission denied")):
            manager = _make_manager(tmp_path)
        try:
            assert manager.load_status["backup_path"] == str(bak)
            assert "Recover from backup" in manager.load_status["recovery_hint"], (
                "When a .bak exists, the hint MUST mention the recovery action."
            )
        finally:
            manager.stop()


class TestLoadStatusCorruptJson:
    def test_corrupt_json_yields_corrupt_status(self, tmp_path):
        path = tmp_path / "schedules.json"
        path.write_text("{not valid json")
        manager = _make_manager(tmp_path)
        try:
            status = manager.load_status
            assert status["status"] == "corrupt_json"
            assert status["error_type"] == "JSONDecodeError"
        finally:
            manager.stop()


class TestRecoverFromBackup:
    def test_recover_when_no_backup_returns_no_backup(self, tmp_path):
        manager = _make_manager(tmp_path)
        try:
            result = manager.recover_schedules_from_backup()
            assert result["status"] == "no_backup"
        finally:
            manager.stop()

    def test_recover_with_valid_backup_restores_schedules(self, tmp_path):
        # Set up a corrupt live file + a valid .bak so the loader fails
        # initially, then recovery brings the schedules back.
        live = tmp_path / "schedules.json"
        live.write_text("{not valid json")
        bak_payload = {
            "schedules": {
                "abc-123": {
                    "id": "abc-123",
                    "name": "Nightly Movies Scan",
                    "enabled": True,
                    "trigger_type": "cron",
                    "trigger_value": "0 3 * * *",
                    "library_ids": [],
                    "config": {},
                }
            }
        }
        bak = tmp_path / "schedules.json.bak"
        bak.write_text(json.dumps(bak_payload))

        manager = _make_manager(tmp_path)
        try:
            # Pre-recover: load failed.
            assert manager.load_status["status"] == "corrupt_json"
            assert len(manager.get_all_schedules()) == 0

            result = manager.recover_schedules_from_backup()
            assert result["status"] == "ok", f"Expected ok, got {result}"
            assert result["restored_count"] == 1
            assert result["backup_path"] == str(bak)
            # After recover, load_status should be ok again.
            assert manager.load_status["status"] == "ok"
            # And the actual schedules should be loaded in memory.
            schedules = manager.get_all_schedules()
            assert len(schedules) == 1
            assert schedules[0]["name"] == "Nightly Movies Scan"
        finally:
            manager.stop()

    def test_recover_picks_newest_backup_by_mtime(self, tmp_path):
        """When multiple .bak files exist, the newest (by mtime) wins.

        Catches the bug where alphabetical sort would prefer
        ``schedules.json.20260101.bak`` over the more recent
        ``schedules.json.bak``.
        """
        live = tmp_path / "schedules.json"
        live.write_text("{not valid json")

        old_bak = tmp_path / "schedules.json.20260101-000000.bak"
        old_bak.write_text(json.dumps({"schedules": {"old-1": {"name": "Old", "enabled": True}}}))
        os.utime(old_bak, (time.time() - 86400, time.time() - 86400))  # 1 day old

        new_bak = tmp_path / "schedules.json.bak"
        new_bak.write_text(json.dumps({"schedules": {"new-1": {"name": "New", "enabled": True}}}))
        # mtime defaults to now → newer than old_bak.

        manager = _make_manager(tmp_path)
        try:
            result = manager.recover_schedules_from_backup()
            assert result["status"] == "ok"
            assert result["backup_path"] == str(new_bak), (
                f"Expected newest .bak ({new_bak}); got {result['backup_path']}"
            )
            assert manager.get_all_schedules()[0]["name"] == "New"
        finally:
            manager.stop()

    def test_recover_with_only_unreadable_backups_returns_no_backup_with_attempts(self, tmp_path):
        """When EVERY backup is unreadable (typical when a host-side
        chown hit both the live file AND the most recent .bak), the
        recovery surfaces a helpful 'no_backup' result that lists the
        attempts + a chown hint covering all backups.
        """
        live = tmp_path / "schedules.json"
        live.write_text("{not valid json")
        bak = tmp_path / "schedules.json.bak"
        bak.write_text(json.dumps({"schedules": {}}))

        manager = _make_manager(tmp_path)
        try:
            with patch("builtins.open", side_effect=PermissionError(13, "Permission denied")):
                result = manager.recover_schedules_from_backup()
            assert result["status"] == "no_backup", (
                "All-backups-unreadable must surface 'no_backup', not 'backup_unreadable' — "
                "the latter implies a single specific file failed; here we tried everything."
            )
            assert "attempts" in result and len(result["attempts"]) >= 1
            assert "PermissionError" in result["primary_error"]["error"]
            assert "chown" in result["recovery_hint"], (
                "Hint MUST mention chown — that's the only fix when every backup is owned "
                "by a UID this process can't read."
            )
        finally:
            manager.stop()

    def test_recover_falls_through_to_next_readable_backup(self, tmp_path):
        """Live regression 2026-05-10: the user's NEWEST .bak was also
        owned root:root 0600 (same chown event broke both). The
        recovery MUST iterate newest-first, skip unreadable candidates,
        and restore from the next-readable one.
        """
        live = tmp_path / "schedules.json"
        live.write_text("{not valid json")

        # NEWEST .bak — simulated unreadable. We patch builtins.open to
        # raise PermissionError when this specific path is opened, while
        # still allowing the older (readable) .bak through.
        bad_newest = tmp_path / "schedules.json.20260510-070000.bak"
        bad_newest.write_text(json.dumps({"schedules": {"x": {"name": "Bad", "enabled": True}}}))
        # Set its mtime so it sorts as newest.
        os.utime(bad_newest, (time.time(), time.time()))

        # OLDER .bak — readable and valid.
        good_older = tmp_path / "schedules.json.20260101-000000.bak"
        good_older.write_text(json.dumps({"schedules": {"good-1": {"name": "Older Good", "enabled": True}}}))
        os.utime(good_older, (time.time() - 86400, time.time() - 86400))

        real_open = open

        def selective_open(path, *args, **kwargs):
            if str(path) == str(bad_newest):
                raise PermissionError(13, "Permission denied")
            return real_open(path, *args, **kwargs)

        manager = _make_manager(tmp_path)
        try:
            with patch("builtins.open", side_effect=selective_open):
                result = manager.recover_schedules_from_backup()
            assert result["status"] == "ok", f"Expected ok, got {result}"
            assert result["backup_path"] == str(good_older), (
                f"Should have fallen through to the older readable backup; got {result['backup_path']}"
            )
            assert result["restored_count"] == 1
            # Verify the in-memory state matches the older backup.
            assert manager.get_all_schedules()[0]["name"] == "Older Good"
        finally:
            manager.stop()

    def test_recover_with_invalid_backup_shape_falls_through(self, tmp_path):
        """When the newest .bak is malformed (missing 'schedules' key),
        skip it and try the next one.
        """
        live = tmp_path / "schedules.json"
        live.write_text("{not valid json")
        # Newest .bak is malformed.
        bad = tmp_path / "schedules.json.20260510-070000.bak"
        bad.write_text(json.dumps({"junk": "data"}))
        os.utime(bad, (time.time(), time.time()))
        # Older .bak is valid.
        good = tmp_path / "schedules.json.20260101-000000.bak"
        good.write_text(json.dumps({"schedules": {"a": {"name": "Recovered", "enabled": True}}}))
        os.utime(good, (time.time() - 86400, time.time() - 86400))

        manager = _make_manager(tmp_path)
        try:
            result = manager.recover_schedules_from_backup()
            assert result["status"] == "ok"
            assert result["backup_path"] == str(good)
            assert manager.get_all_schedules()[0]["name"] == "Recovered"
        finally:
            manager.stop()
