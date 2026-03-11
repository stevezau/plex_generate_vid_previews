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

    def test_get_logs_returns_retention_message_when_file_missing_but_job_exists(
        self, config_dir
    ):
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

        with patch(
            "plex_generate_previews.web.settings_manager.get_settings_manager"
        ) as m:
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

        with patch(
            "plex_generate_previews.web.settings_manager.get_settings_manager"
        ) as m:
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

        with patch(
            "plex_generate_previews.web.settings_manager.get_settings_manager"
        ) as m:
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
        with patch(
            "plex_generate_previews.web.settings_manager.get_settings_manager"
        ) as m:
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


class TestCompleteJobWarning:
    """complete_job warning= parameter produces COMPLETED status with error message."""

    def test_complete_with_warning_sets_completed_status(self, config_dir):
        """warning= marks the job as COMPLETED (not FAILED)."""
        os.makedirs(config_dir, exist_ok=True)
        jm = JobManager(config_dir=config_dir)
        job = jm.create_job(library_name="Test")
        jm.start_job(job.id)
        jm.complete_job(job.id, warning="2/3 processed; 1 sent for retry")

        result = jm.get_job(job.id)
        assert result.status == JobStatus.COMPLETED
        assert result.error == "2/3 processed; 1 sent for retry"

    def test_complete_with_error_sets_failed_status(self, config_dir):
        """error= marks the job as FAILED."""
        os.makedirs(config_dir, exist_ok=True)
        jm = JobManager(config_dir=config_dir)
        job = jm.create_job(library_name="Test")
        jm.start_job(job.id)
        jm.complete_job(job.id, error="Could not find in Plex after 3 attempt(s)")

        result = jm.get_job(job.id)
        assert result.status == JobStatus.FAILED
        assert result.error == "Could not find in Plex after 3 attempt(s)"

    def test_complete_without_args_sets_completed_no_error(self, config_dir):
        """No args marks the job as COMPLETED with no error."""
        os.makedirs(config_dir, exist_ok=True)
        jm = JobManager(config_dir=config_dir)
        job = jm.create_job(library_name="Test")
        jm.start_job(job.id)
        jm.complete_job(job.id)

        result = jm.get_job(job.id)
        assert result.status == JobStatus.COMPLETED
        assert result.error is None

    def test_error_takes_precedence_over_warning(self, config_dir):
        """If both error= and warning= are given, error= wins (FAILED)."""
        os.makedirs(config_dir, exist_ok=True)
        jm = JobManager(config_dir=config_dir)
        job = jm.create_job(library_name="Test")
        jm.start_job(job.id)
        jm.complete_job(job.id, error="hard fail", warning="soft warning")

        result = jm.get_job(job.id)
        assert result.status == JobStatus.FAILED
        assert result.error == "hard fail"

    def test_warning_emits_job_completed_event(self, config_dir):
        """warning= emits job_completed (not job_failed) SocketIO event."""
        os.makedirs(config_dir, exist_ok=True)
        jm = JobManager(config_dir=config_dir)
        emitted = []
        jm._emit_event = lambda event, data: emitted.append((event, data))

        job = jm.create_job(library_name="Test")
        jm.start_job(job.id)
        jm.complete_job(job.id, warning="1 sent for retry")

        assert len(emitted) >= 1
        event_names = [e[0] for e in emitted]
        assert "job_completed" in event_names
        assert "job_failed" not in event_names
        completed_data = next(d for e, d in emitted if e == "job_completed")
        assert completed_data["status"] == "completed"
        assert completed_data["error"] == "1 sent for retry"

    def test_error_emits_job_failed_event(self, config_dir):
        """error= emits job_failed (not job_completed) SocketIO event."""
        os.makedirs(config_dir, exist_ok=True)
        jm = JobManager(config_dir=config_dir)
        emitted = []
        jm._emit_event = lambda event, data: emitted.append((event, data))

        job = jm.create_job(library_name="Test")
        jm.start_job(job.id)
        jm.complete_job(job.id, error="processing failed")

        event_names = [e[0] for e in emitted]
        assert "job_failed" in event_names
        assert "job_completed" not in event_names


class TestRequeueInterruptedJobs:
    """Interrupted job recovery creates clean replacement jobs."""

    def test_no_interrupted_returns_empty(self, config_dir):
        """No interrupted jobs means no new jobs are created."""
        os.makedirs(config_dir, exist_ok=True)
        jm = JobManager(config_dir=config_dir)

        result = jm.requeue_interrupted_jobs()

        assert result == []
        assert jm._interrupted_jobs == []
        assert jm.get_all_jobs() == []

    def test_stale_job_skipped(self, config_dir):
        """Jobs older than the max age are not requeued."""
        os.makedirs(config_dir, exist_ok=True)
        jm = JobManager(config_dir=config_dir)
        job = jm.create_job(library_name="Old Job")
        job.created_at = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        jm._interrupted_jobs = [job]

        result = jm.requeue_interrupted_jobs(max_age_minutes=60)

        assert result == []
        assert jm.get_all_jobs() == [job]

    def test_pending_job_cancelled_and_requeued(self, config_dir):
        """Pending jobs are cancelled and replaced by a fresh pending clone."""
        os.makedirs(config_dir, exist_ok=True)
        jm = JobManager(config_dir=config_dir)
        job = jm.create_job(
            library_name="Pending Job", config={"selected_libraries": ["1"]}
        )
        jm._interrupted_jobs = [job]

        result = jm.requeue_interrupted_jobs()

        assert len(result) == 1
        replacement = result[0]
        assert job.status == JobStatus.CANCELLED
        assert job.error == "Superseded by auto-requeue after restart"
        assert job.completed_at is not None
        assert replacement.id != job.id
        assert replacement.status == JobStatus.PENDING
        assert replacement.library_name == "Pending Job"
        assert replacement.config["selected_libraries"] == ["1"]

    def test_running_job_requeued(self, config_dir):
        """Interrupted running jobs that were marked failed get a fresh clone."""
        os.makedirs(config_dir, exist_ok=True)
        jm = JobManager(config_dir=config_dir)
        job = jm.create_job(library_name="Interrupted Job")
        job.status = JobStatus.FAILED
        job.error = "Job was interrupted by server restart"
        jm._interrupted_jobs = [job]

        result = jm.requeue_interrupted_jobs()

        assert len(result) == 1
        replacement = result[0]
        assert job.status == JobStatus.FAILED
        assert replacement.id != job.id
        assert replacement.library_name == "Interrupted Job"
        assert replacement.status == JobStatus.PENDING

    def test_config_cloned_without_retry_metadata(self, config_dir):
        """Retry metadata is stripped while normal config is preserved."""
        os.makedirs(config_dir, exist_ok=True)
        jm = JobManager(config_dir=config_dir)
        job = jm.create_job(
            library_name="Retry Job",
            config={
                "selected_libraries": ["2"],
                "webhook_paths": ["/data/show/file.mkv"],
                "is_retry": True,
                "retry_delay": 30,
                "retry_attempt": 2,
                "max_retries": 3,
                "parent_job_id": "parent-123",
            },
        )
        jm._interrupted_jobs = [job]

        [replacement] = jm.requeue_interrupted_jobs()

        assert replacement.config["selected_libraries"] == ["2"]
        assert replacement.config["webhook_paths"] == ["/data/show/file.mkv"]
        assert "is_retry" not in replacement.config
        assert "retry_delay" not in replacement.config
        assert "retry_attempt" not in replacement.config
        assert "max_retries" not in replacement.config
        assert "parent_job_id" not in replacement.config

    def test_requeued_from_set(self, config_dir):
        """Replacement jobs record the original job id for traceability."""
        os.makedirs(config_dir, exist_ok=True)
        jm = JobManager(config_dir=config_dir)
        job = jm.create_job(library_name="Traceable Job")
        jm._interrupted_jobs = [job]

        [replacement] = jm.requeue_interrupted_jobs()

        assert replacement.config["requeued_from"] == job.id

    def test_already_requeued_job_skipped(self, config_dir):
        """Jobs that are already auto-requeued are not requeued again."""
        os.makedirs(config_dir, exist_ok=True)
        jm = JobManager(config_dir=config_dir)
        job = jm.create_job(
            library_name="Already Requeued",
            config={"requeued_from": "older-job-id"},
        )
        jm._interrupted_jobs = [job]

        result = jm.requeue_interrupted_jobs()

        assert result == []
        assert len(jm.get_all_jobs()) == 1
        assert jm.get_all_jobs()[0].id == job.id

    def test_unparseable_date_skipped(self, config_dir):
        """Jobs with invalid created_at timestamps are ignored safely."""
        os.makedirs(config_dir, exist_ok=True)
        jm = JobManager(config_dir=config_dir)
        job = jm.create_job(library_name="Bad Date")
        job.created_at = "not-a-real-date"
        jm._interrupted_jobs = [job]

        result = jm.requeue_interrupted_jobs()

        assert result == []
        assert len(jm.get_all_jobs()) == 1
        assert jm.get_all_jobs()[0].id == job.id

    def test_list_cleared_after_requeue(self, config_dir):
        """Interrupted list is cleared after processing so it only runs once."""
        os.makedirs(config_dir, exist_ok=True)
        jm = JobManager(config_dir=config_dir)
        job = jm.create_job(library_name="One Shot")
        jm._interrupted_jobs = [job]

        first = jm.requeue_interrupted_jobs()
        second = jm.requeue_interrupted_jobs()

        assert len(first) == 1
        assert second == []
        assert jm._interrupted_jobs == []
