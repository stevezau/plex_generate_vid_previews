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

from media_preview_generator.web.jobs import (
    LOG_RETENTION_CLEARED_MESSAGE,
    JobManager,
    JobStatus,
)


@pytest.fixture(autouse=True)
def _reset_job_manager():
    """Reset global job manager so tests can create their own with custom config_dir."""
    import media_preview_generator.web.jobs as jobs_mod

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
        jm._persist_job(jm._jobs[job.id])

        with patch("media_preview_generator.web.settings_manager.get_settings_manager") as m:
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

        with patch("media_preview_generator.web.settings_manager.get_settings_manager") as m:
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

        with patch("media_preview_generator.web.settings_manager.get_settings_manager") as m:
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
        with patch("media_preview_generator.web.settings_manager.get_settings_manager") as m:
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
    """Interrupted job recovery revives the original job in place."""

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

    def test_old_created_at_but_recent_started_at_revived(self, config_dir):
        """A long-running job with old created_at but recent started_at is revived."""
        os.makedirs(config_dir, exist_ok=True)
        jm = JobManager(config_dir=config_dir)
        job = jm.create_job(library_name="Long Runner")
        job.created_at = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
        job.started_at = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        job.status = JobStatus.FAILED
        job.error = "Job was interrupted by server restart"
        jm._interrupted_jobs = [job]

        result = jm.requeue_interrupted_jobs(max_age_minutes=60)

        assert len(result) == 1
        assert result[0] is job
        assert job.status == JobStatus.PENDING
        assert job.error is None

    def test_stale_started_at_also_skipped(self, config_dir):
        """Jobs whose started_at is also past the max age are skipped."""
        os.makedirs(config_dir, exist_ok=True)
        jm = JobManager(config_dir=config_dir)
        job = jm.create_job(library_name="Very Old Runner")
        job.created_at = (datetime.now(timezone.utc) - timedelta(hours=10)).isoformat()
        job.started_at = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
        job.status = JobStatus.FAILED
        jm._interrupted_jobs = [job]

        result = jm.requeue_interrupted_jobs(max_age_minutes=60)

        assert result == []

    def test_pending_job_revived_in_place(self, config_dir):
        """Pending jobs are returned as-is, still pending."""
        os.makedirs(config_dir, exist_ok=True)
        jm = JobManager(config_dir=config_dir)
        job = jm.create_job(library_name="Pending Job", config={"selected_libraries": ["1"]})
        jm._interrupted_jobs = [job]

        result = jm.requeue_interrupted_jobs()

        assert len(result) == 1
        assert result[0] is job
        assert job.status == JobStatus.PENDING
        assert job.error is None
        assert job.library_name == "Pending Job"
        assert job.config["selected_libraries"] == ["1"]

    def test_failed_job_revived_in_place(self, config_dir):
        """Interrupted running jobs (marked failed) are revived to pending."""
        os.makedirs(config_dir, exist_ok=True)
        jm = JobManager(config_dir=config_dir)
        job = jm.create_job(library_name="Interrupted Job")
        original_id = job.id
        original_created = job.created_at
        job.status = JobStatus.FAILED
        job.error = "Job was interrupted by server restart"
        job.completed_at = datetime.now(timezone.utc).isoformat()
        jm._interrupted_jobs = [job]

        result = jm.requeue_interrupted_jobs()

        assert len(result) == 1
        assert result[0] is job
        assert job.id == original_id
        assert job.created_at == original_created
        assert job.status == JobStatus.PENDING
        assert job.error is None
        assert job.completed_at is None
        assert job.paused is False
        assert job.progress.percent == 0.0

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

    def test_list_cleared_after_revive(self, config_dir):
        """Interrupted list is cleared after processing so it only runs once."""
        os.makedirs(config_dir, exist_ok=True)
        jm = JobManager(config_dir=config_dir)
        job = jm.create_job(library_name="One Shot")
        jm._interrupted_jobs = [job]

        first = jm.requeue_interrupted_jobs()
        second = jm.requeue_interrupted_jobs()

        assert len(first) == 1
        assert first[0] is job
        assert second == []
        assert jm._interrupted_jobs == []
        assert len(jm.get_all_jobs()) == 1


class TestPublishersAttribution:
    """Phase H5: per-publisher rows persisted on Job for the Jobs UI."""

    def test_default_publishers_is_empty_list(self, config_dir):
        os.makedirs(config_dir, exist_ok=True)
        jm = JobManager(config_dir=config_dir)
        job = jm.create_job(library_name="X")
        assert job.publishers == []
        assert job.to_dict()["publishers"] == []

    def test_append_publishers_persists_through_restart(self, config_dir):
        os.makedirs(config_dir, exist_ok=True)
        jm = JobManager(config_dir=config_dir)
        job = jm.create_job(library_name="X")

        rows = [
            {
                "server_id": "p1",
                "server_name": "Plex Home",
                "server_type": "plex",
                "adapter_name": "plex_bundle",
                "status": "published",
                "message": "",
                "canonical_path": "/data/movies/Foo.mkv",
            },
            {
                "server_id": "e1",
                "server_name": "Emby Den",
                "server_type": "emby",
                "adapter_name": "emby_sidecar",
                "status": "failed",
                "message": "sidecar dir not writable",
                "canonical_path": "/data/movies/Foo.mkv",
            },
        ]
        jm.append_publishers(job.id, rows)

        # Same in-memory state.
        assert len(jm.get_job(job.id).publishers) == 2
        assert jm.get_job(job.id).publishers[1]["status"] == "failed"

        # Round-trip through disk via a fresh JobManager.
        jm2 = JobManager(config_dir=config_dir)
        revived = jm2.get_job(job.id)
        assert revived is not None
        assert len(revived.publishers) == 2
        assert revived.publishers[0]["server_name"] == "Plex Home"
        assert revived.publishers[1]["message"] == "sidecar dir not writable"

    def test_append_publishers_noop_when_unknown_job(self, config_dir):
        """append_publishers must silently no-op for an unknown job id and
        not insert a phantom row. A regression that "creates the job on
        first publish" would corrupt the dashboard's job count + leave
        zombie rows in the jobs.db; assert that didn't happen."""
        os.makedirs(config_dir, exist_ok=True)
        jm = JobManager(config_dir=config_dir)
        before = len(jm.get_all_jobs())

        jm.append_publishers("missing-id", [{"server_id": "x"}])

        assert len(jm.get_all_jobs()) == before, "append_publishers created a phantom job for an unknown id"
        assert jm.get_job("missing-id") is None

    def test_append_publishers_noop_when_rows_empty(self, config_dir):
        os.makedirs(config_dir, exist_ok=True)
        jm = JobManager(config_dir=config_dir)
        job = jm.create_job(library_name="X")
        jm.append_publishers(job.id, [])
        assert jm.get_job(job.id).publishers == []

    def test_set_publishers_replaces_existing_rows(self, config_dir):
        """D12 — set_publishers overwrites (replace), not appends. The
        dispatcher rebuilds a per-server aggregate every task and
        mirrors it onto the job; append semantics would re-introduce
        the unbounded-row bug this method exists to fix."""
        os.makedirs(config_dir, exist_ok=True)
        jm = JobManager(config_dir=config_dir)
        job = jm.create_job(library_name="X")
        jm.set_publishers(
            job.id,
            [
                {
                    "server_id": "p1",
                    "server_name": "Plex Home",
                    "server_type": "plex",
                    "counts": {"published": 10},
                }
            ],
        )
        jm.set_publishers(
            job.id,
            [
                {
                    "server_id": "p1",
                    "server_name": "Plex Home",
                    "server_type": "plex",
                    "counts": {"published": 50, "failed": 2},
                }
            ],
        )
        revived = jm.get_job(job.id)
        assert len(revived.publishers) == 1
        assert revived.publishers[0]["counts"] == {"published": 50, "failed": 2}

    def test_set_publishers_noop_when_unknown_job(self, config_dir):
        """Same contract as append_publishers: silently no-op for an unknown
        id, MUST NOT create a phantom job. A regression that auto-creates
        jobs on first publish would corrupt the dashboard count."""
        os.makedirs(config_dir, exist_ok=True)
        jm = JobManager(config_dir=config_dir)
        before = len(jm.get_all_jobs())

        jm.set_publishers("missing-id", [{"server_id": "x", "counts": {}}])

        assert len(jm.get_all_jobs()) == before
        assert jm.get_job("missing-id") is None

    def test_set_publishers_persists_through_restart(self, config_dir):
        os.makedirs(config_dir, exist_ok=True)
        jm = JobManager(config_dir=config_dir)
        job = jm.create_job(library_name="X")
        jm.set_publishers(
            job.id,
            [
                {
                    "server_id": "p1",
                    "server_name": "Plex Home",
                    "server_type": "plex",
                    "counts": {"published": 200, "skipped_output_exists": 50},
                },
                {
                    "server_id": "e1",
                    "server_name": "Emby",
                    "server_type": "emby",
                    "counts": {"published": 200},
                },
            ],
        )
        jm2 = JobManager(config_dir=config_dir)
        revived = jm2.get_job(job.id)
        assert revived is not None
        assert len(revived.publishers) == 2
        names = {p["server_name"] for p in revived.publishers}
        assert names == {"Plex Home", "Emby"}


class TestParentScheduleId:
    """D20 — Jobs spawned by a schedule carry parent_schedule_id so the
    schedule's stop cron can later find them to pause, and the next
    start tick can resume them instead of spawning a fresh job."""

    def test_create_job_persists_parent_schedule_id(self, config_dir):
        os.makedirs(config_dir, exist_ok=True)
        jm = JobManager(config_dir=config_dir)
        job = jm.create_job(library_name="Movies", parent_schedule_id="sched-xyz")
        assert job.parent_schedule_id == "sched-xyz"
        # Disk round-trip via fresh manager
        jm2 = JobManager(config_dir=config_dir)
        revived = jm2.get_job(job.id)
        assert revived is not None
        assert revived.parent_schedule_id == "sched-xyz"

    def test_create_job_defaults_parent_schedule_id_to_empty(self, config_dir):
        os.makedirs(config_dir, exist_ok=True)
        jm = JobManager(config_dir=config_dir)
        job = jm.create_job(library_name="Movies")
        assert job.parent_schedule_id == ""
        assert job.to_dict()["parent_schedule_id"] == ""


class TestJobUnknownFieldTolerance:
    """Phase H Fix-5: jobs.json with future / removed fields must still load.

    Without the kwarg-filtering safeguard, adding/removing any field on the
    Job dataclass would silently drop every persisted job at startup (the
    surrounding ``except (TypeError, ...)`` swallows the failure)."""

    def test_load_skips_unknown_kwarg_fields(self, config_dir):
        """A persisted job with an extra field (e.g. one we removed since)
        must still load — the unknown field is silently dropped."""
        import json as _json

        os.makedirs(config_dir, exist_ok=True)
        jobs_file = os.path.join(config_dir, "jobs.json")
        with open(jobs_file, "w") as f:
            _json.dump(
                {
                    "jobs": [
                        {
                            "id": "j1",
                            "library_name": "Movies",
                            "future_field_we_dont_know_about": 42,
                            "another_unknown": "ignore me",
                        }
                    ]
                },
                f,
            )
        jm = JobManager(config_dir=config_dir)
        loaded = jm.get_job("j1")
        assert loaded is not None
        assert loaded.library_name == "Movies"


class TestSqliteJobsBackend:
    """Phase J8: jobs persistence moved from jobs.json to jobs.db."""

    def test_creates_jobs_db_on_first_start(self, config_dir):
        os.makedirs(config_dir, exist_ok=True)
        jm = JobManager(config_dir=config_dir)
        assert os.path.isfile(os.path.join(config_dir, "jobs.db"))
        assert jm._storage is not None
        assert jm._storage.row_count() == 0

    def test_create_job_persists_one_row_not_a_full_file(self, config_dir):
        """The backup helper rewrites whole files; SQLite writes one row.

        Crucially, jobs.json should never appear after a fresh start — only
        jobs.db. This is what makes a future schema-drift incident structurally
        impossible.
        """
        os.makedirs(config_dir, exist_ok=True)
        jm = JobManager(config_dir=config_dir)
        jm.create_job(library_name="Movies")
        jm.create_job(library_name="TV")
        assert jm._storage.row_count() == 2
        assert not os.path.exists(os.path.join(config_dir, "jobs.json"))

    def test_state_survives_manager_recreation(self, config_dir):
        """Recreate the JobManager (simulating a restart) and verify history persists."""
        os.makedirs(config_dir, exist_ok=True)
        jm = JobManager(config_dir=config_dir)
        job = jm.create_job(library_name="Persistent")
        jm.start_job(job.id)
        jm.complete_job(job.id)

        # Force the singleton reset so a "fresh" manager is rebuilt.
        import media_preview_generator.web.jobs as jobs_mod

        with jobs_mod._job_lock:
            jobs_mod._job_manager = None
        jm._storage.close()

        jm2 = JobManager(config_dir=config_dir)
        recovered = jm2.get_job(job.id)
        assert recovered is not None
        assert recovered.status == JobStatus.COMPLETED
        assert recovered.library_name == "Persistent"

    def test_legacy_json_imports_then_renames(self, config_dir):
        """A pre-J8 jobs.json gets imported once, then renamed to .imported.bak."""
        import json as _json

        os.makedirs(config_dir, exist_ok=True)
        legacy = os.path.join(config_dir, "jobs.json")
        with open(legacy, "w") as f:
            _json.dump(
                {
                    "jobs": [
                        {"id": "good-1", "library_name": "Movies", "status": "completed"},
                        {"id": "good-2", "library_name": "TV", "status": "failed"},
                    ]
                },
                f,
            )

        jm = JobManager(config_dir=config_dir)
        assert jm.get_job("good-1") is not None
        assert jm.get_job("good-2") is not None
        assert jm.get_job("good-2").status == JobStatus.FAILED
        # Legacy file is preserved, just out of the way
        assert not os.path.exists(legacy)
        assert os.path.isfile(legacy + ".imported.bak")

    def test_legacy_import_skips_corrupt_records(self, config_dir):
        """One bad record never blocks the others (Fix-5 pattern, J4 mirror).

        ``progress`` must be a dict / JobProgress shape; a bare string trips
        ``to_dict()`` inside ``upsert()`` and the importer must catch that
        without dropping the surrounding good rows. A separate bad row with
        a ``status`` value that isn't a JobStatus enum member trips at
        ``__post_init__`` time and exercises the constructor failure path.
        """
        import json as _json

        os.makedirs(config_dir, exist_ok=True)
        with open(os.path.join(config_dir, "jobs.json"), "w") as f:
            _json.dump(
                {
                    "jobs": [
                        {"id": "ok-1", "library_name": "Movies"},
                        # status is parsed by JobStatus(...) which raises ValueError
                        {"id": "bad-status", "library_name": "X", "status": "not-a-status"},
                        {"id": "ok-2", "library_name": "TV"},
                        # progress must be a dict / JobProgress; bare string trips to_dict()
                        {"id": "bad-progress", "library_name": "Y", "progress": "not-a-dict"},
                        {"id": "ok-3", "library_name": "Anime"},
                    ]
                },
                f,
            )

        jm = JobManager(config_dir=config_dir)
        assert jm.get_job("ok-1") is not None
        assert jm.get_job("ok-2") is not None
        assert jm.get_job("ok-3") is not None
        # Both bad rows should have been skipped, not stored.
        assert jm.get_job("bad-status") is None
        assert jm.get_job("bad-progress") is None

    def test_does_not_reimport_when_db_already_populated(self, config_dir):
        """Restoring jobs.json on top of a populated DB must not duplicate."""
        import json as _json

        os.makedirs(config_dir, exist_ok=True)
        jm = JobManager(config_dir=config_dir)
        jm.create_job(library_name="Direct")
        baseline = jm._storage.row_count()
        jm._storage.close()

        # Drop a stale jobs.json next to the now-populated jobs.db.
        with open(os.path.join(config_dir, "jobs.json"), "w") as f:
            _json.dump({"jobs": [{"id": "stale", "library_name": "Should not appear"}]}, f)

        # Reset singleton + reopen.
        import media_preview_generator.web.jobs as jobs_mod

        with jobs_mod._job_lock:
            jobs_mod._job_manager = None
        jm2 = JobManager(config_dir=config_dir)
        assert jm2._storage.row_count() == baseline
        assert jm2.get_job("stale") is None
        # The legacy file is left in place — we did NOT consume it because the
        # DB was already populated. This is intentional: don't silently merge.
        assert os.path.isfile(os.path.join(config_dir, "jobs.json"))

    def test_delete_removes_row(self, config_dir):
        os.makedirs(config_dir, exist_ok=True)
        jm = JobManager(config_dir=config_dir)
        job = jm.create_job(library_name="Doomed")
        assert jm._storage.row_count() == 1
        jm.delete_job(job.id)
        assert jm._storage.row_count() == 0
        assert jm.get_job(job.id) is None


class TestRetryPreservesServerIdentity:
    """K1 — retry job spawned by job_runner._spawn_retry_job must inherit the
    parent's server_id/server_name/server_type. Today's bug shows up as
    "Created job ... (server=(all))" instead of "(server=Plex)".

    We can't easily call the closure directly, so this test mirrors the
    pattern: create parent with server triple → simulate retry using the same
    create_job call shape job_runner now uses → verify the child carries it.
    """

    def test_retry_inherits_parent_server_triple(self, config_dir):
        os.makedirs(config_dir, exist_ok=True)
        jm = JobManager(config_dir=config_dir)
        parent = jm.create_job(
            library_name="Sonarr: Show.S01E01",
            server_id="plex-living-room",
            server_name="Living Room Plex",
            server_type="plex",
        )

        # Mirror job_runner.py:_spawn_retry_job's new behaviour.
        retry = jm.create_job(
            library_name="Retry: Sonarr: Show.S01E01",
            config={"is_retry": True, "parent_job_id": parent.id, "retry_attempt": 1},
            priority=parent.priority,
            server_id=parent.server_id,
            server_name=parent.server_name,
            server_type=parent.server_type,
        )

        assert retry.server_id == "plex-living-room"
        assert retry.server_name == "Living Room Plex"
        assert retry.server_type == "plex"

    def test_retry_when_parent_has_no_server_pin(self, config_dir):
        """Webhook with no ?server_id= produces parent with None — retry mirrors None."""
        os.makedirs(config_dir, exist_ok=True)
        jm = JobManager(config_dir=config_dir)
        parent = jm.create_job(library_name="Custom: file.mkv")
        assert parent.server_id is None

        retry = jm.create_job(
            library_name="Retry: Custom: file.mkv",
            config={"is_retry": True, "parent_job_id": parent.id, "retry_attempt": 1},
            priority=parent.priority,
            server_id=parent.server_id,
            server_name=parent.server_name,
            server_type=parent.server_type,
        )
        assert retry.server_id is None
