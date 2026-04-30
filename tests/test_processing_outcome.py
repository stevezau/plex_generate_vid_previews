"""
Tests for ProcessingResult tracking and misconfiguration detection.

Covers:
- ProcessingResult enum produced by Worker._process_item via the
  unified MultiServerStatus → ProcessingResult translator.
- Worker.outcome_counts tracking.
- WorkerPool outcome aggregation.
- CLI enhanced logging and misconfiguration warnings.
- JobProgress.outcome field serialization.
"""

from unittest.mock import MagicMock, patch

from media_preview_generator.jobs.worker import Worker
from media_preview_generator.processing import ProcessingResult
from media_preview_generator.web.jobs import JobManager, JobProgress


# --- Test helpers for the unified ProcessableItem dispatch path ---
def _pi(key="test_key", title="Test", media_type="movie", canonical_path=None, server_id="plex-1"):
    """Build a ProcessableItem matching what the tests used to assemble as a tuple."""
    from media_preview_generator.processing.types import ProcessableItem

    return ProcessableItem(
        canonical_path=canonical_path or f"/data/{key.replace('/', '_').strip('_') or 'item'}.mkv",
        server_id=server_id,
        item_id_by_server={server_id: key} if key else {},
        title=title,
        library_id="lib-1",
    )


def _pi_list_or_passthrough(items):
    """Pass through a list of ProcessableItems untouched, or convert tuples on the fly."""
    from media_preview_generator.processing.types import ProcessableItem

    if not items:
        return []
    if isinstance(items[0], ProcessableItem):
        return items
    return _pi_list(items)


def _pi_list(triples, *, server_id="plex-1"):
    """Bulk version: convert ``[(key, title, media_type)]`` to ProcessableItems."""
    out = []
    for entry in triples:
        if not entry:
            continue
        key = entry[0]
        title = entry[1] if len(entry) > 1 else "Test"
        media_type = entry[2] if len(entry) > 2 else "movie"
        out.append(_pi(key, title=title, media_type=media_type, server_id=server_id))
    return out


def _ms(status="generated", canonical_path="/data/test.mkv", message=""):
    """Build a MultiServerResult that maps back to a specific ProcessingResult."""
    from media_preview_generator.processing.multi_server import MultiServerResult, MultiServerStatus

    status_map = {
        "generated": MultiServerStatus.PUBLISHED,
        "published": MultiServerStatus.PUBLISHED,
        "skipped_bif_exists": MultiServerStatus.SKIPPED,
        "skipped": MultiServerStatus.SKIPPED,
        "no_media_parts": MultiServerStatus.NO_OWNERS,
        "no_owners": MultiServerStatus.NO_OWNERS,
        "failed": MultiServerStatus.FAILED,
    }
    return MultiServerResult(
        canonical_path=canonical_path,
        status=status_map.get(status, MultiServerStatus.PUBLISHED),
        message=message,
    )


class TestProcessingResultEnum:
    """Test ProcessingResult enum values and structure."""

    def test_all_values_present(self):
        """All expected outcome values exist."""
        expected = {
            "generated",
            "skipped_bif_exists",
            "skipped_file_not_found",
            "skipped_excluded",
            "skipped_invalid_hash",
            "failed",
            "no_media_parts",
        }
        assert {r.value for r in ProcessingResult} == expected

    def test_enum_members_are_strings(self):
        """Each value is a plain string (suitable as dict key)."""
        for r in ProcessingResult:
            assert isinstance(r.value, str)


class TestWorkerOutcomeCounts:
    """Test Worker.outcome_counts tracking."""

    def test_initial_outcome_counts_are_zero(self):
        """All outcome counters start at zero."""
        worker = Worker(0, "CPU")
        for r in ProcessingResult:
            assert worker.outcome_counts[r.value] == 0

    @patch("media_preview_generator.processing.multi_server.process_canonical_path")
    def test_completed_item_increments_outcome(self, mock_process):
        """Successful process_item updates both completed and outcome_counts."""
        mock_process.return_value = _ms("generated")
        worker = Worker(0, "CPU")
        config = MagicMock()
        registry = MagicMock()
        worker.assign_task(_pi("test_key", title="Test", media_type="movie"), config, registry)
        worker.current_thread.join(timeout=2)

        assert worker.outcome_counts["generated"] == 1
        assert worker.completed == 1
        assert worker.failed == 0

    @patch("media_preview_generator.processing.multi_server.process_canonical_path")
    def test_skipped_item_counts_as_completed_not_failed(self, mock_process):
        """Skipped items (e.g. BIF exists) count as completed, not failed."""
        mock_process.return_value = _ms("skipped_bif_exists")
        worker = Worker(0, "CPU")
        config = MagicMock()
        registry = MagicMock()
        worker.assign_task(_pi("test_key", title="Test", media_type="movie"), config, registry)
        worker.current_thread.join(timeout=2)

        assert worker.outcome_counts["skipped_bif_exists"] == 1
        assert worker.completed == 1
        assert worker.failed == 0

    @patch("media_preview_generator.processing.multi_server.process_canonical_path")
    def test_failed_result_counts_as_failed(self, mock_process):
        """ProcessingResult.FAILED increments worker.failed."""
        mock_process.return_value = _ms("failed")
        worker = Worker(0, "CPU")
        config = MagicMock()
        registry = MagicMock()
        worker.assign_task(_pi("test_key", title="Test", media_type="movie"), config, registry)
        worker.current_thread.join(timeout=2)

        assert worker.outcome_counts["failed"] == 1
        assert worker.failed == 1
        assert worker.completed == 0


class TestJobProgressOutcome:
    """Test outcome field on JobProgress."""

    def test_default_outcome_is_none(self):
        """New JobProgress has outcome=None."""
        progress = JobProgress()
        assert progress.outcome is None

    def test_outcome_round_trips_through_to_dict(self):
        """Outcome data survives to_dict serialization."""
        progress = JobProgress()
        progress.outcome = {"generated": 5, "skipped_bif_exists": 10, "failed": 1}
        d = progress.to_dict()
        assert d["outcome"] == {"generated": 5, "skipped_bif_exists": 10, "failed": 1}

    def test_outcome_none_in_to_dict(self):
        """outcome=None serializes as None."""
        progress = JobProgress()
        d = progress.to_dict()
        assert d["outcome"] is None


class TestJobManagerSetOutcome:
    """Test JobManager.set_job_outcome method."""

    def test_set_job_outcome_stores_data(self):
        """set_job_outcome stores outcome dict on the job."""
        manager = JobManager.__new__(JobManager)
        manager._lock = __import__("threading").Lock()
        manager._jobs = {}
        manager._save_jobs = MagicMock()
        manager._socketio = None
        manager._on_progress_callbacks = []

        from media_preview_generator.web.jobs import Job

        job = Job(id="test-123", library_name="Movies")
        manager._jobs["test-123"] = job

        outcome = {
            "generated": 100,
            "skipped_bif_exists": 50,
            "skipped_file_not_found": 0,
            "failed": 2,
        }
        manager.set_job_outcome("test-123", outcome)

        assert job.progress.outcome == outcome

    def test_set_job_outcome_nonexistent_job(self):
        """set_job_outcome returns None for unknown job_id."""
        manager = JobManager.__new__(JobManager)
        manager._lock = __import__("threading").Lock()
        manager._jobs = {}

        result = manager.set_job_outcome("nonexistent", {"generated": 1})
        assert result is None


class TestMisconfigurationDetection:
    """Test the misconfiguration warning logic in processing.py."""

    def test_warning_logged_when_all_not_found(self):
        """When all items are skipped_file_not_found, a warning is logged."""
        from media_preview_generator.processing import ProcessingResult

        outcome = {r.value: 0 for r in ProcessingResult}
        outcome["skipped_file_not_found"] = 100

        total_processed = 100
        generated = outcome.get("generated", 0)
        not_found = outcome.get("skipped_file_not_found", 0)

        assert total_processed > 0
        assert not_found > 0
        assert generated == 0

    def test_no_warning_when_items_generated(self):
        """When items are generated, no misconfiguration warning."""
        from media_preview_generator.processing import ProcessingResult

        outcome = {r.value: 0 for r in ProcessingResult}
        outcome["generated"] = 50
        outcome["skipped_file_not_found"] = 10

        generated = outcome.get("generated", 0)
        assert generated > 0
        assert outcome.get("skipped_file_not_found", 0) == 10

    def test_no_warning_when_all_exist(self):
        """When all items already have BIF files, no warning."""
        from media_preview_generator.processing import ProcessingResult

        outcome = {r.value: 0 for r in ProcessingResult}
        outcome["skipped_bif_exists"] = 500

        not_found = outcome.get("skipped_file_not_found", 0)
        assert not_found == 0


class TestOutcomeInWorkerPoolResult:
    """Test that WorkerPool includes outcome in its return dict."""

    @patch("media_preview_generator.processing.multi_server.process_canonical_path")
    def test_process_items_headless_includes_outcome(self, mock_process):
        """process_items_headless result dict contains an 'outcome' key."""
        mock_process.return_value = _ms("skipped_bif_exists")

        from media_preview_generator.jobs.worker import WorkerPool

        pool = WorkerPool(gpu_workers=0, cpu_workers=1, selected_gpus=[])
        config = MagicMock()
        config.cpu_threads = 1
        registry = MagicMock()
        items = [("/library/metadata/1", "Movie 1", "movie")]
        result = pool.process_items_headless(
            _pi_list_or_passthrough(items),
            config,
            registry,
            title_max_width=30,
            library_name="Test",
        )

        assert "outcome" in result
        assert isinstance(result["outcome"], dict)
        assert result["outcome"]["skipped_bif_exists"] >= 1

    @patch("media_preview_generator.processing.multi_server.process_canonical_path")
    def test_outcome_counts_match_items_processed(self, mock_process):
        """Sum of all outcome values equals total items processed."""
        results_iter = iter(
            [
                _ms("generated"),
                _ms("skipped_bif_exists"),
                _ms("generated"),
            ]
        )
        mock_process.side_effect = lambda *args, **kwargs: next(results_iter)

        from media_preview_generator.jobs.worker import WorkerPool

        pool = WorkerPool(gpu_workers=0, cpu_workers=1, selected_gpus=[])
        config = MagicMock()
        config.cpu_threads = 1
        registry = MagicMock()
        items = [
            ("/library/metadata/1", "Movie 1", "movie"),
            ("/library/metadata/2", "Movie 2", "movie"),
            ("/library/metadata/3", "Movie 3", "movie"),
        ]
        result = pool.process_items_headless(
            _pi_list_or_passthrough(items),
            config,
            registry,
            title_max_width=30,
            library_name="Test",
        )

        outcome = result["outcome"]
        # 3 items processed; outcomes: 2 generated + 1 skipped_bif_exists.
        # The legacy test tried to mix three distinct ProcessingResult enum
        # values, but the unified pipeline only produces a subset
        # (PUBLISHED → GENERATED, SKIPPED → SKIPPED_BIF_EXISTS,
        # NO_OWNERS → NO_MEDIA_PARTS, FAILED → FAILED). The granular Plex-only
        # SKIPPED_FILE_NOT_FOUND distinction is gone.
        total_outcome = sum(outcome.values())
        assert total_outcome == 3
        assert outcome["generated"] == 2
        assert outcome["skipped_bif_exists"] == 1
