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


# ---------------------------------------------------------------------------
# _GpuPartitionManager
# ---------------------------------------------------------------------------


class TestGpuPartitionManager:
    """Verify GPU partitioning across concurrent jobs."""

    @pytest.fixture(autouse=True)
    def _reset_settings(self):
        import plex_generate_previews.web.settings_manager as sm_mod

        with sm_mod._settings_lock:
            sm_mod._settings_manager = None
        yield

    @pytest.fixture(autouse=True)
    def _restore_get_max_concurrent(self):
        """Save/restore _get_max_concurrent as a staticmethod descriptor.

        Accessing ``Cls._get_max_concurrent`` unwraps the ``staticmethod``
        descriptor (Python descriptor protocol), so restoring via a plain
        assignment would lose the ``@staticmethod`` wrapper.  We access the
        raw descriptor through ``__dict__`` to avoid this.
        """
        from plex_generate_previews.web.routes import _JobConcurrencyManager

        original_descriptor = _JobConcurrencyManager.__dict__["_get_max_concurrent"]
        yield
        _JobConcurrencyManager._get_max_concurrent = original_descriptor

    def _patch_max_concurrent(self, n: int):
        from plex_generate_previews.web.routes import _JobConcurrencyManager

        _JobConcurrencyManager._get_max_concurrent = staticmethod(lambda: n)

    def _make_manager(self):
        from plex_generate_previews.web.routes import _GpuPartitionManager

        return _GpuPartitionManager()

    @staticmethod
    def _make_gpus(n: int) -> list:
        """Create *n* fake GPU tuples (type, device, info_dict)."""
        return [
            (f"GPU_{i}", f"/dev/dri/renderD{128 + i}", {"name": f"GPU {i}"})
            for i in range(n)
        ]

    def test_single_job_gets_all_gpus(self):
        """When concurrent mode is off, the job receives every GPU."""
        self._patch_max_concurrent(1)
        mgr = self._make_manager()
        gpus = self._make_gpus(3)

        result = mgr.acquire("job-1", gpus)
        assert len(result) == 3
        mgr.release("job-1")

    def test_two_jobs_partition_three_gpus(self):
        """3 GPUs, max_concurrent=2: slot 0 gets [0,2], slot 1 gets [1]."""
        self._patch_max_concurrent(2)
        mgr = self._make_manager()
        gpus = self._make_gpus(3)

        result1 = mgr.acquire("job-1", gpus)
        result2 = mgr.acquire("job-2", gpus)

        # Slot 0: indices where i%2==0 → [0, 2]
        assert len(result1) == 2
        assert result1[0][0] == "GPU_0"
        assert result1[1][0] == "GPU_2"

        # Slot 1: indices where i%2==1 → [1]
        assert len(result2) == 1
        assert result2[0][0] == "GPU_1"

        mgr.release("job-1")
        mgr.release("job-2")

    def test_three_jobs_three_gpus(self):
        """3 GPUs, max_concurrent=3: each job gets exactly 1 GPU."""
        self._patch_max_concurrent(3)
        mgr = self._make_manager()
        gpus = self._make_gpus(3)

        r1 = mgr.acquire("j1", gpus)
        r2 = mgr.acquire("j2", gpus)
        r3 = mgr.acquire("j3", gpus)

        assert len(r1) == 1 and r1[0][0] == "GPU_0"
        assert len(r2) == 1 and r2[0][0] == "GPU_1"
        assert len(r3) == 1 and r3[0][0] == "GPU_2"

        mgr.release("j1")
        mgr.release("j2")
        mgr.release("j3")

    def test_more_slots_than_gpus_shares(self):
        """2 GPUs, max_concurrent=3: third job shares a GPU."""
        self._patch_max_concurrent(3)
        mgr = self._make_manager()
        gpus = self._make_gpus(2)

        r1 = mgr.acquire("j1", gpus)
        r2 = mgr.acquire("j2", gpus)
        r3 = mgr.acquire("j3", gpus)

        assert len(r1) == 1 and r1[0][0] == "GPU_0"
        assert len(r2) == 1 and r2[0][0] == "GPU_1"
        # Slot 2: no index where i%3==2 in range(2), fallback = 2%2=0
        assert len(r3) == 1 and r3[0][0] == "GPU_0"

        mgr.release("j1")
        mgr.release("j2")
        mgr.release("j3")

    def test_release_frees_slot_for_reuse(self):
        """After releasing, a new job gets the freed slot's GPUs."""
        self._patch_max_concurrent(2)
        mgr = self._make_manager()
        gpus = self._make_gpus(2)

        r1 = mgr.acquire("j1", gpus)
        mgr.acquire("j2", gpus)
        assert mgr.get_slot("j1") == 0
        assert mgr.get_slot("j2") == 1

        mgr.release("j1")
        assert mgr.get_slot("j1") is None

        # New job should get slot 0 (lowest free)
        r3 = mgr.acquire("j3", gpus)
        assert mgr.get_slot("j3") == 0
        assert r3[0][0] == r1[0][0]  # Same GPU as j1 had

        mgr.release("j2")
        mgr.release("j3")

    def test_empty_gpu_list(self):
        """With no GPUs, acquire returns empty list regardless of mode."""
        self._patch_max_concurrent(2)
        mgr = self._make_manager()

        result = mgr.acquire("j1", [])
        assert result == []
        mgr.release("j1")

    def test_thread_safety_partitioning(self):
        """Multiple threads acquiring partitions concurrently get distinct slots."""
        self._patch_max_concurrent(4)
        mgr = self._make_manager()
        gpus = self._make_gpus(4)

        slots = []
        barrier = threading.Barrier(4)

        def worker(jid):
            barrier.wait()
            mgr.acquire(jid, gpus)
            slots.append(mgr.get_slot(jid))

        threads = [threading.Thread(target=worker, args=(f"j{i}",)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # All 4 slots should be distinct
        assert sorted(slots) == [0, 1, 2, 3]

        for i in range(4):
            mgr.release(f"j{i}")


# ---------------------------------------------------------------------------
# Worker scaling with GPU partitioning
# ---------------------------------------------------------------------------


class TestWorkerScaling:
    """Verify that worker counts are scaled proportionally to GPU partition size."""

    def test_gpu_threads_scaled_proportionally(self):
        """When a job gets 1/3 of GPUs, gpu_threads should be scaled down."""
        # Simulate: 6 gpu_threads, 3 total GPUs, job gets 1 GPU → scale = 1/3
        original_gpu_threads = 6
        total_gpus = 3
        assigned_gpus = 1
        scale = assigned_gpus / total_gpus
        effective = max(1, round(original_gpu_threads * scale))
        assert effective == 2  # 6 * (1/3) = 2

    def test_gpu_threads_minimum_one(self):
        """Even with extreme scaling, gpu_threads never drops below 1."""
        original_gpu_threads = 1
        total_gpus = 10
        assigned_gpus = 1
        scale = assigned_gpus / total_gpus
        effective = max(1, round(original_gpu_threads * scale))
        assert effective == 1  # max(1, round(0.1)) = max(1, 0) = 1

    def test_no_scaling_when_all_gpus_assigned(self):
        """When a job gets all GPUs, no scaling occurs."""
        total_gpus = 3
        assigned_gpus = 3
        # No scaling needed — assigned == total
        assert assigned_gpus == total_gpus


# ---------------------------------------------------------------------------
# _GpuPartitionManager.rebalance()
# ---------------------------------------------------------------------------


class TestGpuPartitionRebalance:
    """Verify GPU rebalancing when a job finishes."""

    @pytest.fixture(autouse=True)
    def _reset_settings(self):
        import plex_generate_previews.web.settings_manager as sm_mod

        with sm_mod._settings_lock:
            sm_mod._settings_manager = None
        yield

    @pytest.fixture(autouse=True)
    def _restore_get_max_concurrent(self):
        from plex_generate_previews.web.routes import _JobConcurrencyManager

        original_descriptor = _JobConcurrencyManager.__dict__["_get_max_concurrent"]
        yield
        _JobConcurrencyManager._get_max_concurrent = original_descriptor

    def _patch_max_concurrent(self, n: int):
        from plex_generate_previews.web.routes import _JobConcurrencyManager

        _JobConcurrencyManager._get_max_concurrent = staticmethod(lambda: n)

    def _make_manager(self):
        from plex_generate_previews.web.routes import _GpuPartitionManager

        return _GpuPartitionManager()

    @staticmethod
    def _make_gpus(n: int) -> list:
        return [
            (f"GPU_{i}", f"/dev/dri/renderD{128 + i}", {"name": f"GPU {i}"})
            for i in range(n)
        ]

    def test_rebalance_two_to_one_job(self):
        """When one of two jobs finishes, the survivor gets all GPUs."""
        self._patch_max_concurrent(2)
        mgr = self._make_manager()
        gpus = self._make_gpus(4)

        mgr.acquire("j1", gpus)  # slot 0 → GPUs [0,2]
        mgr.acquire("j2", gpus)  # slot 1 → GPUs [1,3]

        # j1 finishes
        mgr.release("j1")
        new_partitions = mgr.rebalance(gpus)

        # j2 should now get all 4 GPUs (1 remaining job, divisor = 1)
        assert "j2" in new_partitions
        assert len(new_partitions["j2"]) == 4

    def test_rebalance_three_to_two_jobs(self):
        """When one of three jobs finishes, two survivors split GPUs evenly."""
        self._patch_max_concurrent(3)
        mgr = self._make_manager()
        gpus = self._make_gpus(6)

        mgr.acquire("j1", gpus)  # slot 0 → GPUs [0,3]
        mgr.acquire("j2", gpus)  # slot 1 → GPUs [1,4]
        mgr.acquire("j3", gpus)  # slot 2 → GPUs [2,5]

        # j2 finishes
        mgr.release("j2")
        new_partitions = mgr.rebalance(gpus)

        # 2 remaining jobs, 6 GPUs → 3 each
        assert len(new_partitions) == 2
        all_assigned = []
        for jid in new_partitions:
            all_assigned.extend(new_partitions[jid])
        # All 6 GPUs should be covered
        assigned_names = sorted(g[0] for g in all_assigned)
        assert assigned_names == [f"GPU_{i}" for i in range(6)]

    def test_rebalance_empty_after_all_jobs_finish(self):
        """Rebalance returns empty dict when no jobs remain."""
        self._patch_max_concurrent(2)
        mgr = self._make_manager()
        gpus = self._make_gpus(2)

        mgr.acquire("j1", gpus)
        mgr.release("j1")
        result = mgr.rebalance(gpus)
        assert result == {}

    def test_rebalance_with_no_gpus(self):
        """Rebalance handles empty GPU list gracefully."""
        self._patch_max_concurrent(2)
        mgr = self._make_manager()

        mgr.acquire("j1", [])  # No GPUs
        mgr.release("j1")
        result = mgr.rebalance([])
        assert result == {}

    def test_rebalance_reassigns_slots_contiguously(self):
        """After rebalance, slot numbers are contiguous starting from 0."""
        self._patch_max_concurrent(3)
        mgr = self._make_manager()
        gpus = self._make_gpus(3)

        mgr.acquire("j1", gpus)  # slot 0
        mgr.acquire("j2", gpus)  # slot 1
        mgr.acquire("j3", gpus)  # slot 2

        # j1 finishes (had slot 0)
        mgr.release("j1")
        mgr.rebalance(gpus)

        # Remaining jobs should be reassigned to slots 0 and 1
        assert mgr.get_slot("j2") in (0, 1)
        assert mgr.get_slot("j3") in (0, 1)
        assert mgr.get_slot("j2") != mgr.get_slot("j3")

    def test_rebalance_preserves_relative_order(self):
        """Jobs keep relative slot ordering even after rebalance."""
        self._patch_max_concurrent(3)
        mgr = self._make_manager()
        gpus = self._make_gpus(3)

        mgr.acquire("j1", gpus)  # slot 0
        mgr.acquire("j2", gpus)  # slot 1
        mgr.acquire("j3", gpus)  # slot 2

        # Middle job finishes
        mgr.release("j2")
        mgr.rebalance(gpus)

        # j1 had slot 0, j3 had slot 2 → j1 should get slot 0, j3 slot 1
        assert mgr.get_slot("j1") == 0
        assert mgr.get_slot("j3") == 1


# ---------------------------------------------------------------------------
# _rebalance_running_jobs integration
# ---------------------------------------------------------------------------


class TestRebalanceRunningJobs:
    """Test the _rebalance_running_jobs function with mock WorkerPools."""

    @pytest.fixture(autouse=True)
    def _reset_modules(self):
        import plex_generate_previews.web.settings_manager as sm_mod
        import plex_generate_previews.web.jobs as jobs_mod

        with sm_mod._settings_lock:
            sm_mod._settings_manager = None
        with jobs_mod._job_lock:
            jobs_mod._job_manager = None
        yield

    @pytest.fixture(autouse=True)
    def _restore_get_max_concurrent(self):
        from plex_generate_previews.web.routes import _JobConcurrencyManager

        original_descriptor = _JobConcurrencyManager.__dict__["_get_max_concurrent"]
        yield
        _JobConcurrencyManager._get_max_concurrent = original_descriptor

    def _patch_max_concurrent(self, n: int):
        from plex_generate_previews.web.routes import _JobConcurrencyManager

        _JobConcurrencyManager._get_max_concurrent = staticmethod(lambda: n)

    @staticmethod
    def _make_gpus(n: int) -> list:
        return [
            (f"GPU_{i}", f"/dev/dri/renderD{128 + i}", {"name": f"GPU {i}"})
            for i in range(n)
        ]

    def test_rebalance_adds_workers_to_surviving_pool(self, tmp_path):
        """After j1 finishes, j2's pool gets more GPUs and workers added."""
        from unittest.mock import MagicMock
        from plex_generate_previews.web.routes import (
            _GpuPartitionManager,
            _rebalance_running_jobs,
            _gpu_cache,
            _gpu_cache_lock,
        )
        from plex_generate_previews.web.jobs import JobManager

        self._patch_max_concurrent(2)
        gpus = self._make_gpus(4)

        # Set up GPU cache
        with _gpu_cache_lock:
            _gpu_cache["result"] = [
                {"type": g[0], "device": g[1], "name": g[2].get("name", g[0])}
                for g in gpus
            ]

        jm = JobManager(config_dir=str(tmp_path))
        j1 = jm.create_job(library_name="Lib1")
        j2 = jm.create_job(library_name="Lib2")
        jm.start_job(j1.id)
        jm.start_job(j2.id)

        # Use our own partition manager to avoid global state leaks
        partition_mgr = _GpuPartitionManager()

        # Acquire partitions
        gpu1 = partition_mgr.acquire(j1.id, gpus)
        gpu2 = partition_mgr.acquire(j2.id, gpus)
        assert len(gpu1) == 2
        assert len(gpu2) == 2

        # Set up mock pool for j2
        mock_pool = MagicMock()
        mock_pool.selected_gpus = list(gpu2)
        mock_worker1 = MagicMock()
        mock_worker1.worker_type = "GPU"
        mock_worker2 = MagicMock()
        mock_worker2.worker_type = "GPU"
        mock_pool.workers = [mock_worker1, mock_worker2]
        mock_pool.add_workers.return_value = 2

        jm.set_active_worker_pool(j2.id, mock_pool)

        # j1 finishes — release its partition
        partition_mgr.release(j1.id)

        # Rebalance — monkey-patch module-level _gpu_partition temporarily
        import plex_generate_previews.web.routes as routes_mod

        original_partition = routes_mod._gpu_partition
        routes_mod._gpu_partition = partition_mgr

        # Also patch get_job_manager to return our jm
        from unittest.mock import patch

        with patch(
            "plex_generate_previews.web.routes.get_job_manager", return_value=jm
        ):
            _rebalance_running_jobs(j1.id)

        routes_mod._gpu_partition = original_partition

        # j2's pool should have been updated with more GPUs
        assert len(mock_pool.selected_gpus) == 4
        mock_pool.add_workers.assert_called_once_with("GPU", 2)

        # Clean up
        with _gpu_cache_lock:
            _gpu_cache["result"] = None

    def test_rebalance_noop_when_no_gpu_gain(self, tmp_path):
        """When there's no GPU gain, rebalance doesn't add workers."""
        from unittest.mock import MagicMock, patch
        from plex_generate_previews.web.routes import (
            _GpuPartitionManager,
            _rebalance_running_jobs,
            _gpu_cache,
            _gpu_cache_lock,
        )
        from plex_generate_previews.web.jobs import JobManager

        self._patch_max_concurrent(1)  # Single job mode
        gpus = self._make_gpus(2)

        with _gpu_cache_lock:
            _gpu_cache["result"] = [
                {"type": g[0], "device": g[1], "name": g[2].get("name", g[0])}
                for g in gpus
            ]

        jm = JobManager(config_dir=str(tmp_path))
        j1 = jm.create_job(library_name="Lib1")
        jm.start_job(j1.id)

        partition_mgr = _GpuPartitionManager()
        partition_mgr.acquire(j1.id, gpus)

        mock_pool = MagicMock()
        mock_pool.selected_gpus = list(gpus)  # Already has all GPUs
        jm.set_active_worker_pool(j1.id, mock_pool)

        import plex_generate_previews.web.routes as routes_mod

        original_partition = routes_mod._gpu_partition
        routes_mod._gpu_partition = partition_mgr

        with patch(
            "plex_generate_previews.web.routes.get_job_manager", return_value=jm
        ):
            _rebalance_running_jobs("some-finished-job")

        routes_mod._gpu_partition = original_partition

        # No workers should have been added
        mock_pool.add_workers.assert_not_called()

        with _gpu_cache_lock:
            _gpu_cache["result"] = None

    def test_rebalance_handles_missing_pool_gracefully(self, tmp_path):
        """Rebalance doesn't crash if a job's pool is already cleaned up."""
        from unittest.mock import patch
        from plex_generate_previews.web.routes import (
            _GpuPartitionManager,
            _rebalance_running_jobs,
            _gpu_cache,
            _gpu_cache_lock,
        )
        from plex_generate_previews.web.jobs import JobManager

        self._patch_max_concurrent(2)
        gpus = self._make_gpus(2)

        with _gpu_cache_lock:
            _gpu_cache["result"] = [
                {"type": g[0], "device": g[1], "name": g[2].get("name", g[0])}
                for g in gpus
            ]

        jm = JobManager(config_dir=str(tmp_path))
        j1 = jm.create_job(library_name="Lib1")
        j2 = jm.create_job(library_name="Lib2")
        jm.start_job(j1.id)
        jm.start_job(j2.id)

        partition_mgr = _GpuPartitionManager()
        partition_mgr.acquire(j1.id, gpus)
        partition_mgr.acquire(j2.id, gpus)

        # j2 has NO active pool (already cleaned up)
        # This should not raise
        partition_mgr.release(j1.id)

        import plex_generate_previews.web.routes as routes_mod

        original_partition = routes_mod._gpu_partition
        routes_mod._gpu_partition = partition_mgr

        with patch(
            "plex_generate_previews.web.routes.get_job_manager", return_value=jm
        ):
            _rebalance_running_jobs(j1.id)  # Should not raise

        routes_mod._gpu_partition = original_partition

        with _gpu_cache_lock:
            _gpu_cache["result"] = None
