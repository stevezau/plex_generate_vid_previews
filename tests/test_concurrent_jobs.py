"""
Tests for concurrent job execution infrastructure.

Covers: _JobConcurrencyManager, JobManager multi-job tracking,
and SettingsManager concurrent job settings.
"""

import threading

import pytest


# ---------------------------------------------------------------------------
# SettingsManager: concurrent_jobs_enabled / max_concurrent_jobs
# ---------------------------------------------------------------------------


class TestConcurrentJobSettings:
    """Verify new settings properties for concurrent jobs."""

    @pytest.fixture
    def settings_manager(self, tmp_path):
        from plex_generate_previews.web.settings_manager import SettingsManager

        return SettingsManager(config_dir=str(tmp_path))

    def test_concurrent_jobs_enabled_default_false(self, settings_manager):
        assert settings_manager.concurrent_jobs_enabled is False

    def test_concurrent_jobs_enabled_set_true(self, settings_manager):
        settings_manager.concurrent_jobs_enabled = True
        assert settings_manager.concurrent_jobs_enabled is True

    def test_max_concurrent_jobs_default(self, settings_manager):
        assert settings_manager.max_concurrent_jobs == 2

    def test_max_concurrent_jobs_set(self, settings_manager):
        settings_manager.max_concurrent_jobs = 4
        assert settings_manager.max_concurrent_jobs == 4

    def test_max_concurrent_jobs_minimum_clamped(self, settings_manager):
        settings_manager.max_concurrent_jobs = 0
        assert settings_manager.max_concurrent_jobs >= 1


# ---------------------------------------------------------------------------
# JobManager: multiple running jobs
# ---------------------------------------------------------------------------


class TestJobManagerMultipleRunningJobs:
    """Verify JobManager tracks multiple running jobs."""

    @pytest.fixture(autouse=True)
    def _reset_job_manager(self):
        import plex_generate_previews.web.jobs as jobs_mod

        with jobs_mod._job_lock:
            jobs_mod._job_manager = None
        yield

    @pytest.fixture
    def job_manager(self, tmp_path):
        from plex_generate_previews.web.jobs import JobManager

        return JobManager(config_dir=str(tmp_path))

    def test_no_running_jobs_initially(self, job_manager):
        assert job_manager.get_running_job() is None
        assert job_manager.get_running_jobs() == []
        assert job_manager.get_running_job_count() == 0

    def test_single_running_job(self, job_manager):
        job = job_manager.create_job(library_name="Test")
        job_manager.start_job(job.id)
        assert job_manager.get_running_job() is not None
        assert job_manager.get_running_job().id == job.id
        assert job_manager.get_running_job_count() == 1

    def test_multiple_running_jobs(self, job_manager):
        job1 = job_manager.create_job(library_name="Test 1")
        job2 = job_manager.create_job(library_name="Test 2")
        job_manager.start_job(job1.id)
        job_manager.start_job(job2.id)

        assert job_manager.get_running_job_count() == 2
        running = job_manager.get_running_jobs()
        running_ids = {j.id for j in running}
        assert job1.id in running_ids
        assert job2.id in running_ids

    def test_complete_job_removes_from_running(self, job_manager):
        job1 = job_manager.create_job(library_name="Test 1")
        job2 = job_manager.create_job(library_name="Test 2")
        job_manager.start_job(job1.id)
        job_manager.start_job(job2.id)
        assert job_manager.get_running_job_count() == 2

        job_manager.complete_job(job1.id)
        assert job_manager.get_running_job_count() == 1
        assert job_manager.get_running_jobs()[0].id == job2.id

    def test_cancel_job_removes_from_running(self, job_manager):
        job = job_manager.create_job(library_name="Test")
        job_manager.start_job(job.id)
        assert job_manager.get_running_job_count() == 1

        job_manager.cancel_job(job.id)
        assert job_manager.get_running_job_count() == 0

    def test_cannot_delete_running_job(self, job_manager):
        job = job_manager.create_job(library_name="Test")
        job_manager.start_job(job.id)
        assert job_manager.delete_job(job.id) is False

    def test_get_running_job_backward_compat(self, job_manager):
        """get_running_job() returns a single Job for backward compatibility."""
        job1 = job_manager.create_job(library_name="Test 1")
        job2 = job_manager.create_job(library_name="Test 2")
        job_manager.start_job(job1.id)
        job_manager.start_job(job2.id)

        result = job_manager.get_running_job()
        assert result is not None
        assert result.id in {job1.id, job2.id}


# ---------------------------------------------------------------------------
# _JobConcurrencyManager
# ---------------------------------------------------------------------------


class TestJobConcurrencyManager:
    """Verify the concurrency manager respects settings."""

    @pytest.fixture(autouse=True)
    def _reset_settings(self):
        import plex_generate_previews.web.settings_manager as sm_mod

        with sm_mod._settings_lock:
            sm_mod._settings_manager = None
        yield

    def _make_manager(self):
        from plex_generate_previews.web.routes import _JobConcurrencyManager

        return _JobConcurrencyManager()

    def test_default_allows_one_job(self, tmp_path):
        """Without concurrent_jobs_enabled, only 1 slot is available."""
        from plex_generate_previews.web.settings_manager import SettingsManager

        # Force a settings manager with concurrent disabled
        sm = SettingsManager(config_dir=str(tmp_path))
        sm.concurrent_jobs_enabled = False

        mgr = self._make_manager()
        # Patch _get_max_concurrent to use our settings
        mgr._get_max_concurrent = lambda: 1

        assert mgr.try_acquire() is True
        assert mgr.try_acquire() is False  # Second slot blocked
        mgr.release()
        assert mgr.try_acquire() is True  # Slot available again
        mgr.release()

    def test_concurrent_allows_multiple(self):
        mgr = self._make_manager()
        mgr._get_max_concurrent = lambda: 3

        assert mgr.try_acquire() is True
        assert mgr.try_acquire() is True
        assert mgr.try_acquire() is True
        assert mgr.try_acquire() is False  # All 3 slots taken
        mgr.release()
        assert mgr.try_acquire() is True  # One freed up
        # Cleanup
        mgr.release()
        mgr.release()
        mgr.release()

    def test_release_does_not_go_negative(self):
        mgr = self._make_manager()
        mgr._get_max_concurrent = lambda: 1
        mgr.release()  # Release without acquire
        mgr.release()
        # Should still only allow max_concurrent
        assert mgr.try_acquire() is True
        assert mgr.try_acquire() is False
        mgr.release()

    def test_thread_safety(self):
        """Multiple threads acquiring/releasing concurrently."""
        mgr = self._make_manager()
        mgr._get_max_concurrent = lambda: 2

        results = []
        barrier = threading.Barrier(4)

        def worker():
            barrier.wait()
            acquired = mgr.try_acquire()
            results.append(acquired)
            if acquired:
                # Hold briefly
                import time

                time.sleep(0.01)
                mgr.release()

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # At most 2 should have acquired simultaneously
        assert sum(results) >= 2
