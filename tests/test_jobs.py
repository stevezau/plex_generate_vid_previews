"""
Tests for JobManager file-backed job logs, retention, and cleanup.

Covers: log persistence to disk, reading logs from file after "restart",
retention message when file was cleared, time-based _enforce_log_retention,
periodic retention timer, and deletion of log files when jobs are deleted
or cleared.
"""

import os
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from plex_generate_previews.web.jobs import (
    LOG_RETENTION_CLEARED_MESSAGE,
    JobManager,
    JobStatus,
    get_job_manager,
)


@pytest.fixture(autouse=True)
def _reset_job_manager():
    """Reset global job manager so tests can create their own with custom config_dir."""
    import plex_generate_previews.web.jobs as jobs_mod

    with jobs_mod._job_lock:
        jobs_mod._job_manager = None
    yield
    with jobs_mod._job_lock:
        jobs_mod._job_manager = None


@pytest.fixture
def config_dir(tmp_path):
    """Temporary config directory for job logs."""
    return str(tmp_path / "config")


class TestJobLogPersistence:
    """File-backed job log persistence and retrieval."""

    def test_add_log_writes_to_file(self, config_dir):
        """add_log appends to both in-memory deque and to job log file."""
        os.makedirs(config_dir, exist_ok=True)
        jm = JobManager(config_dir=config_dir)
        job = jm.create_job(library_name="Test")
        jm.start_job(job.id)

        jm.add_log(job.id, "INFO - first line")
        jm.add_log(job.id, "WARNING - second line")

        log_path = os.path.join(config_dir, "logs", "jobs", f"{job.id}.log")
        assert os.path.isfile(log_path)
        with open(log_path) as f:
            content = f.read()
        assert "first line" in content
        assert "second line" in content
        assert content.count("\n") == 2

    def test_get_logs_reads_from_file_after_restart(self, config_dir):
        """get_logs returns file content when in-memory cache is empty (e.g. after restart)."""
        os.makedirs(config_dir, exist_ok=True)
        jm1 = JobManager(config_dir=config_dir)
        job = jm1.create_job(library_name="Test")
        jm1.start_job(job.id)
        jm1.add_log(job.id, "INFO - persisted line")

        # Simulate restart: new JobManager, same config_dir. In-memory _job_logs is empty.
        jm2 = JobManager(config_dir=config_dir)
        logs = jm2.get_logs(job.id)
        assert len(logs) == 1
        assert "persisted line" in logs[0]

    def test_get_logs_returns_retention_message_when_file_missing_but_job_exists(self, config_dir):
        """get_logs returns retention message when log file is gone but job still in jobs.json."""
        os.makedirs(config_dir, exist_ok=True)
        jm = JobManager(config_dir=config_dir)
        job = jm.create_job(library_name="Test")
        jm.complete_job(job.id)
        logs = jm.get_logs(job.id)
        assert logs == [LOG_RETENTION_CLEARED_MESSAGE]

    def test_get_logs_returns_empty_when_job_does_not_exist(self, config_dir):
        """get_logs returns [] when job_id is unknown."""
        os.makedirs(config_dir, exist_ok=True)
        jm = JobManager(config_dir=config_dir)
        logs = jm.get_logs("nonexistent-job-id")
        assert logs == []


class TestLogRetentionEnforcement:
    """_enforce_log_retention removes jobs + log files older than job_history_days."""

    def test_enforce_retention_removes_expired_jobs(self, config_dir):
        """Terminal jobs older than job_history_days are deleted along with log files."""
        os.makedirs(config_dir, exist_ok=True)
        jm = JobManager(config_dir=config_dir)
        job = jm.create_job(library_name="Old Job")
        jm.start_job(job.id)
        jm.add_log(job.id, "INFO - test")
        jm.complete_job(job.id)

        log_path = os.path.join(config_dir, "logs", "jobs", f"{job.id}.log")
        assert os.path.isfile(log_path)

        # Backdate completed_at to 60 days ago
        old_time = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
        jm._jobs[job.id].completed_at = old_time
        jm._save_jobs()

        with patch("plex_generate_previews.web.jobs.get_settings_manager") as m:
            m.return_value.get.return_value = 30
            jm._enforce_log_retention()

        assert jm.get_job(job.id) is None
        assert not os.path.isfile(log_path)

    def test_enforce_retention_keeps_recent_jobs(self, config_dir):
        """Jobs younger than job_history_days are kept."""
        os.makedirs(config_dir, exist_ok=True)
        jm = JobManager(config_dir=config_dir)
        job = jm.create_job(library_name="New Job")
        jm.start_job(job.id)
        jm.add_log(job.id, "INFO - test")
        jm.complete_job(job.id)

        log_path = os.path.join(config_dir, "logs", "jobs", f"{job.id}.log")

        with patch("plex_generate_previews.web.jobs.get_settings_manager") as m:
            m.return_value.get.return_value = 30
            jm._enforce_log_retention()

        assert jm.get_job(job.id) is not None
        assert os.path.isfile(log_path)

    def test_enforce_retention_keeps_running_jobs(self, config_dir):
        """Running jobs are never removed by retention regardless of age."""
        os.makedirs(config_dir, exist_ok=True)
        jm = JobManager(config_dir=config_dir)
        job = jm.create_job(library_name="Running")
        jm.start_job(job.id)
        jm.add_log(job.id, "INFO - test")

        # Backdate created_at to 90 days ago
        old_time = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
        jm._jobs[job.id].created_at = old_time

        with patch("plex_generate_previews.web.jobs.get_settings_manager") as m:
            m.return_value.get.return_value = 30
            jm._enforce_log_retention()

        assert jm.get_job(job.id) is not None

    def test_enforce_retention_removes_orphaned_log_files(self, config_dir):
        """Log files with no matching job entry are cleaned up."""
        log_dir = os.path.join(config_dir, "logs", "jobs")
        os.makedirs(log_dir, exist_ok=True)
        orphan_path = os.path.join(log_dir, "no-such-job-id.log")
        with open(orphan_path, "w") as f:
            f.write("orphaned\n")

        jm = JobManager(config_dir=config_dir)
        with patch("plex_generate_previews.web.jobs.get_settings_manager") as m:
            m.return_value.get.return_value = 30
            jm._enforce_log_retention()

        assert not os.path.isfile(orphan_path)


class TestLogFileCleanup:
    """Log files are removed when jobs are deleted or cleared."""

    def test_delete_job_removes_log_file(self, config_dir):
        """delete_job removes the job's log file from disk."""
        os.makedirs(config_dir, exist_ok=True)
        jm = JobManager(config_dir=config_dir)
        job = jm.create_job(library_name="Test")
        jm.start_job(job.id)
        jm.add_log(job.id, "INFO - test")
        jm.complete_job(job.id)

        log_path = os.path.join(config_dir, "logs", "jobs", f"{job.id}.log")
        assert os.path.isfile(log_path)

        ok = jm.delete_job(job.id)
        assert ok is True
        assert not os.path.isfile(log_path)

    def test_clear_completed_jobs_removes_log_files(self, config_dir):
        """clear_completed_jobs deletes log files for cleared jobs."""
        os.makedirs(config_dir, exist_ok=True)
        jm = JobManager(config_dir=config_dir)
        job = jm.create_job(library_name="Test")
        jm.start_job(job.id)
        jm.add_log(job.id, "INFO - test")
        jm.complete_job(job.id)
        job_id = job.id

        log_path = os.path.join(config_dir, "logs", "jobs", f"{job_id}.log")
        assert os.path.isfile(log_path)

        n = jm.clear_completed_jobs()
        assert n == 1
        assert not os.path.isfile(log_path)

    def test_prune_terminal_jobs_removes_log_files(self, config_dir):
        """Pruning old terminal jobs removes their log files."""
        os.makedirs(config_dir, exist_ok=True)
        jm = JobManager(config_dir=config_dir)
        created = []
        for i in range(52):
            job = jm.create_job(library_name=f"Lib{i}")
            jm.start_job(job.id)
            jm.add_log(job.id, "INFO - x")
            jm.complete_job(job.id)
            created.append(job.id)

        log_dir = os.path.join(config_dir, "logs", "jobs")
        assert len([f for f in os.listdir(log_dir) if f.endswith(".log")]) == 52

        jm.create_job(library_name="New")
        remaining_logs = [f for f in os.listdir(log_dir) if f.endswith(".log")]
        assert len(remaining_logs) == 50

    def test_clear_logs_removes_file(self, config_dir):
        """clear_logs deletes the job log file."""
        os.makedirs(config_dir, exist_ok=True)
        jm = JobManager(config_dir=config_dir)
        job = jm.create_job(library_name="Test")
        jm.start_job(job.id)
        jm.add_log(job.id, "INFO - test")

        log_path = os.path.join(config_dir, "logs", "jobs", f"{job.id}.log")
        assert os.path.isfile(log_path)

        jm.clear_logs(job.id)
        assert not os.path.isfile(log_path)


class TestRetentionTimer:
    """Background retention timer starts and can be stopped."""

    def test_timer_starts_on_init(self, config_dir):
        """JobManager starts a daemon retention timer on init."""
        os.makedirs(config_dir, exist_ok=True)
        jm = JobManager(config_dir=config_dir)
        assert jm._retention_timer is not None
        assert jm._retention_timer.daemon is True
        jm._stop_retention_timer()

    def test_timer_can_be_stopped(self, config_dir):
        """_stop_retention_timer cancels the timer."""
        os.makedirs(config_dir, exist_ok=True)
        jm = JobManager(config_dir=config_dir)
        jm._stop_retention_timer()
        assert jm._retention_timer is None
