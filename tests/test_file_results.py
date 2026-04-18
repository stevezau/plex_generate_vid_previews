"""
Tests for per-file job result tracking.

Covers: JobManager.record_file_result / get_file_results round-trip,
JSONL persistence, filtering, retention cleanup, deletion cleanup,
and the GET /api/jobs/{id}/files API endpoint.
"""

import json
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from plex_generate_previews.web.jobs import JobManager


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
    """Temporary config directory."""
    return str(tmp_path / "config")


class TestFileResultRecording:
    """record_file_result writes JSONL and get_file_results reads it back."""

    def test_record_and_read_round_trip(self, config_dir):
        os.makedirs(config_dir, exist_ok=True)
        jm = JobManager(config_dir=config_dir)
        job = jm.create_job(library_name="Test")

        jm.record_file_result(job.id, "/media/video1.mkv", "generated", "", "GPU 1")
        jm.record_file_result(job.id, "/media/video2.mkv", "failed", "FFmpeg exit 183", "GPU 2")
        jm.record_file_result(job.id, "/media/video3.mkv", "skipped_bif_exists", "BIF exists", "")

        results = jm.get_file_results(job.id)
        assert len(results) == 3
        assert results[0]["file"] == "/media/video1.mkv"
        assert results[0]["outcome"] == "generated"
        assert results[0]["worker"] == "GPU 1"
        assert results[1]["outcome"] == "failed"
        assert results[1]["reason"] == "FFmpeg exit 183"
        assert results[2]["outcome"] == "skipped_bif_exists"

    def test_jsonl_file_created(self, config_dir):
        os.makedirs(config_dir, exist_ok=True)
        jm = JobManager(config_dir=config_dir)
        job = jm.create_job(library_name="Test")
        jm.record_file_result(job.id, "/media/a.mkv", "generated")

        path = os.path.join(config_dir, "logs", "job_file_results", f"{job.id}.jsonl")
        assert os.path.isfile(path)
        with open(path) as f:
            lines = [line.strip() for line in f if line.strip()]
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["file"] == "/media/a.mkv"
        assert record["outcome"] == "generated"
        assert "ts" in record

    def test_get_file_results_empty_when_no_records(self, config_dir):
        os.makedirs(config_dir, exist_ok=True)
        jm = JobManager(config_dir=config_dir)
        assert jm.get_file_results("nonexistent") == []

    def test_timestamp_present(self, config_dir):
        os.makedirs(config_dir, exist_ok=True)
        jm = JobManager(config_dir=config_dir)
        job = jm.create_job(library_name="Test")
        jm.record_file_result(job.id, "/media/a.mkv", "generated")
        results = jm.get_file_results(job.id)
        assert results[0]["ts"]

    def test_malformed_jsonl_lines_skipped(self, config_dir):
        """Corrupt lines in the JSONL file are silently skipped."""
        os.makedirs(config_dir, exist_ok=True)
        jm = JobManager(config_dir=config_dir)
        job = jm.create_job(library_name="Test")
        jm.record_file_result(job.id, "/media/good.mkv", "generated")

        path = jm._file_results_path(job.id)
        with open(path, "a") as f:
            f.write("NOT VALID JSON\n")
            f.write("\n")
            f.write('{"file":"/media/also_good.mkv","outcome":"failed","reason":"","worker":"","ts":"00:00:00"}\n')

        results = jm.get_file_results(job.id)
        assert len(results) == 2
        assert results[0]["file"] == "/media/good.mkv"
        assert results[1]["file"] == "/media/also_good.mkv"


class TestFileResultFiltering:
    """get_file_results with outcome_filter and search parameters."""

    def _seed(self, jm, job_id):
        jm.record_file_result(job_id, "/media/MovieA.mkv", "generated", "", "GPU 1")
        jm.record_file_result(job_id, "/media/MovieB.mkv", "failed", "exit 1", "GPU 2")
        jm.record_file_result(job_id, "/media/ShowC.mkv", "skipped_bif_exists", "exists", "")
        jm.record_file_result(job_id, "/media/ShowD.mkv", "failed", "exit 183", "CPU 1")

    def test_filter_by_outcome(self, config_dir):
        os.makedirs(config_dir, exist_ok=True)
        jm = JobManager(config_dir=config_dir)
        job = jm.create_job(library_name="Test")
        self._seed(jm, job.id)

        failed = jm.get_file_results(job.id, outcome_filter="failed")
        assert len(failed) == 2
        assert all(r["outcome"] == "failed" for r in failed)

    def test_filter_by_search(self, config_dir):
        os.makedirs(config_dir, exist_ok=True)
        jm = JobManager(config_dir=config_dir)
        job = jm.create_job(library_name="Test")
        self._seed(jm, job.id)

        results = jm.get_file_results(job.id, search="Show")
        assert len(results) == 2
        assert all("Show" in r["file"] for r in results)

    def test_filter_by_search_case_insensitive(self, config_dir):
        os.makedirs(config_dir, exist_ok=True)
        jm = JobManager(config_dir=config_dir)
        job = jm.create_job(library_name="Test")
        self._seed(jm, job.id)

        results = jm.get_file_results(job.id, search="movieb")
        assert len(results) == 1
        assert results[0]["file"] == "/media/MovieB.mkv"

    def test_filter_combined(self, config_dir):
        os.makedirs(config_dir, exist_ok=True)
        jm = JobManager(config_dir=config_dir)
        job = jm.create_job(library_name="Test")
        self._seed(jm, job.id)

        results = jm.get_file_results(job.id, outcome_filter="failed", search="Show")
        assert len(results) == 1
        assert results[0]["file"] == "/media/ShowD.mkv"


class TestFileResultRetention:
    """Retention and cleanup of file result JSONL files."""

    def test_retention_removes_file_results_for_expired_jobs(self, config_dir):
        os.makedirs(config_dir, exist_ok=True)
        jm = JobManager(config_dir=config_dir)
        job = jm.create_job(library_name="Old Job")
        jm.start_job(job.id)
        jm.record_file_result(job.id, "/media/a.mkv", "generated")
        jm.complete_job(job.id)

        results_path = jm._file_results_path(job.id)
        assert os.path.isfile(results_path)

        old_time = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
        jm._jobs[job.id].completed_at = old_time
        jm._save_jobs()

        with patch("plex_generate_previews.web.settings_manager.get_settings_manager") as m:
            m.return_value.get.return_value = 30
            jm._enforce_log_retention()

        assert jm.get_job(job.id) is None
        assert not os.path.isfile(results_path)

    def test_delete_job_removes_file_results(self, config_dir):
        os.makedirs(config_dir, exist_ok=True)
        jm = JobManager(config_dir=config_dir)
        job = jm.create_job(library_name="Test")
        jm.record_file_result(job.id, "/media/a.mkv", "generated")
        jm.complete_job(job.id)

        results_path = jm._file_results_path(job.id)
        assert os.path.isfile(results_path)

        jm.delete_job(job.id)
        assert not os.path.isfile(results_path)

    def test_clear_completed_removes_file_results(self, config_dir):
        os.makedirs(config_dir, exist_ok=True)
        jm = JobManager(config_dir=config_dir)
        job = jm.create_job(library_name="Test")
        jm.record_file_result(job.id, "/media/a.mkv", "generated")
        jm.complete_job(job.id)

        results_path = jm._file_results_path(job.id)
        assert os.path.isfile(results_path)

        jm.clear_completed_jobs()
        assert not os.path.isfile(results_path)

    def test_orphaned_file_results_cleaned_up(self, config_dir):
        os.makedirs(config_dir, exist_ok=True)
        jm = JobManager(config_dir=config_dir)

        orphan_path = os.path.join(config_dir, "logs", "job_file_results", "orphan-id.jsonl")
        os.makedirs(os.path.dirname(orphan_path), exist_ok=True)
        with open(orphan_path, "w") as f:
            f.write('{"file":"x","outcome":"generated"}\n')

        with patch("plex_generate_previews.web.settings_manager.get_settings_manager") as m:
            m.return_value.get.return_value = 30
            jm._enforce_log_retention()

        assert not os.path.isfile(orphan_path)


class TestFileResultCallback:
    """set_file_result_callback and _notify_file_result wiring."""

    def test_callback_invoked_for_each_outcome(self):
        from plex_generate_previews.processing import (
            ProcessingResult,
            _notify_file_result,
            set_file_result_callback,
        )

        captured = []

        def cb(file_path, outcome_str, reason, worker):
            captured.append({"file": file_path, "outcome": outcome_str, "reason": reason})

        set_file_result_callback(cb)
        try:
            _notify_file_result("/a.mkv", ProcessingResult.GENERATED, "", "GPU 1")
            _notify_file_result("/b.mkv", ProcessingResult.FAILED, "exit 1", "CPU 1")
        finally:
            set_file_result_callback(None)

        assert len(captured) == 2
        assert captured[0]["outcome"] == "generated"
        assert captured[1]["outcome"] == "failed"

    def test_callback_cleared(self):
        from plex_generate_previews.processing import (
            ProcessingResult,
            _notify_file_result,
            set_file_result_callback,
        )

        captured = []
        set_file_result_callback(lambda *a: captured.append(a))
        set_file_result_callback(None)
        _notify_file_result("/a.mkv", ProcessingResult.GENERATED, "", "")
        assert len(captured) == 0

    def test_callback_exception_does_not_propagate(self):
        """A failing callback must not crash the caller."""
        from plex_generate_previews.processing import (
            ProcessingResult,
            _notify_file_result,
            set_file_result_callback,
        )

        def bad_cb(*_args):
            raise RuntimeError("boom")

        set_file_result_callback(bad_cb)
        try:
            _notify_file_result("/a.mkv", ProcessingResult.GENERATED, "", "GPU 1")
        finally:
            set_file_result_callback(None)


class TestFileResultsAPI:
    """GET /api/jobs/{id}/files API endpoint."""

    @pytest.fixture()
    def app(self, tmp_path):
        from plex_generate_previews.web.app import create_app
        from plex_generate_previews.web.settings_manager import reset_settings_manager

        reset_settings_manager()
        cfg = str(tmp_path / "config")
        os.makedirs(cfg, exist_ok=True)

        auth_file = os.path.join(cfg, "auth.json")
        with open(auth_file, "w") as f:
            json.dump({"token": "test-token-12345678"}, f)

        settings_file = os.path.join(cfg, "settings.json")
        with open(settings_file, "w") as f:
            json.dump({"setup_complete": True}, f)

        with patch.dict(
            os.environ,
            {
                "CONFIG_DIR": cfg,
                "WEB_AUTH_TOKEN": "test-token-12345678",
                "WEB_PORT": "8099",
            },
        ):
            flask_app = create_app(config_dir=cfg)
            flask_app.config["TESTING"] = True
            yield flask_app
        reset_settings_manager()

    @pytest.fixture()
    def client(self, app):
        return app.test_client()

    def _headers(self):
        return {
            "Authorization": "Bearer test-token-12345678",
            "Content-Type": "application/json",
        }

    def test_file_results_endpoint(self, client):
        from plex_generate_previews.web.jobs import get_job_manager

        jm = get_job_manager()
        job = jm.create_job(library_name="API Test")
        jm.record_file_result(job.id, "/media/a.mkv", "generated", "", "GPU 1")
        jm.record_file_result(job.id, "/media/b.mkv", "failed", "exit 1", "CPU 1")

        resp = client.get(f"/api/jobs/{job.id}/files", headers=self._headers())
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["count"] == 2
        assert data["total"] == 2
        assert len(data["files"]) == 2
        assert data["summary"]["generated"] == 1
        assert data["summary"]["failed"] == 1

    def test_file_results_outcome_filter(self, client):
        from plex_generate_previews.web.jobs import get_job_manager

        jm = get_job_manager()
        job = jm.create_job(library_name="API Test")
        jm.record_file_result(job.id, "/media/a.mkv", "generated")
        jm.record_file_result(job.id, "/media/b.mkv", "failed", "exit 1")
        jm.record_file_result(job.id, "/media/c.mkv", "failed", "exit 2")

        resp = client.get(f"/api/jobs/{job.id}/files?outcome=failed", headers=self._headers())
        data = resp.get_json()
        assert data["count"] == 2
        assert data["total"] == 3
        assert all(f["outcome"] == "failed" for f in data["files"])
        # Summary must still contain ALL outcome counts for badge rendering
        assert data["summary"]["generated"] == 1
        assert data["summary"]["failed"] == 2

    def test_file_results_search_filter(self, client):
        from plex_generate_previews.web.jobs import get_job_manager

        jm = get_job_manager()
        job = jm.create_job(library_name="API Test")
        jm.record_file_result(job.id, "/media/NBA/game1.mkv", "generated")
        jm.record_file_result(job.id, "/media/UFC/fight1.mkv", "generated")

        resp = client.get(f"/api/jobs/{job.id}/files?search=NBA", headers=self._headers())
        data = resp.get_json()
        assert data["count"] == 1
        assert "NBA" in data["files"][0]["file"]

    def test_file_results_404_for_missing_job(self, client):
        resp = client.get("/api/jobs/nonexistent/files", headers=self._headers())
        assert resp.status_code == 404
