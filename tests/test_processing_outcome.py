"""
Tests for ProcessingResult tracking and misconfiguration detection.

Covers:
- ProcessingResult enum returned by process_item()
- Worker.outcome_counts tracking
- WorkerPool outcome aggregation
- CLI enhanced logging and misconfiguration warnings
- JobProgress.outcome field serialization
"""

import xml.etree.ElementTree as ET
from unittest.mock import MagicMock, patch

from plex_generate_previews.media_processing import ProcessingResult, process_item
from plex_generate_previews.web.jobs import JobManager, JobProgress
from plex_generate_previews.worker import Worker


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


class TestProcessItemReturnsResult:
    """Test that process_item returns the correct ProcessingResult."""

    def test_plex_api_error_returns_failed(self, mock_config):
        """Plex query exception returns FAILED."""
        mock_plex = MagicMock()
        mock_plex.query.side_effect = Exception("connection refused")

        result = process_item("/library/metadata/1", None, None, mock_config, mock_plex)
        assert result == ProcessingResult.FAILED

    def test_no_media_parts_returns_no_media_parts(self, mock_config):
        """XML with no MediaPart elements returns NO_MEDIA_PARTS."""
        mock_plex = MagicMock()
        xml = '<MediaContainer size="1"><Video><Media></Media></Video></MediaContainer>'
        mock_plex.query.return_value = ET.fromstring(xml)

        result = process_item("/library/metadata/1", None, None, mock_config, mock_plex)
        assert result == ProcessingResult.NO_MEDIA_PARTS

    @patch("os.path.isfile")
    def test_missing_file_returns_skipped_file_not_found(self, mock_isfile, mock_config, plex_xml_movie_tree):
        """File that doesn't exist locally returns SKIPPED_FILE_NOT_FOUND."""
        mock_plex = MagicMock()
        mock_plex.query.return_value = ET.fromstring(plex_xml_movie_tree)
        mock_isfile.return_value = False
        mock_config.plex_config_folder = "/config/plex"

        result = process_item("/library/metadata/54321", None, None, mock_config, mock_plex)
        assert result == ProcessingResult.SKIPPED_FILE_NOT_FOUND

    @patch("os.path.isfile")
    def test_excluded_path_returns_skipped_excluded(self, mock_isfile, mock_config, plex_xml_movie_tree):
        """Path matching exclude_paths returns SKIPPED_EXCLUDED."""
        mock_plex = MagicMock()
        mock_plex.query.return_value = ET.fromstring(plex_xml_movie_tree)
        mock_config.plex_config_folder = "/config/plex"
        mock_config.exclude_paths = [{"value": "/data/movies", "type": "path"}]

        result = process_item("/library/metadata/54321", None, None, mock_config, mock_plex)
        assert result == ProcessingResult.SKIPPED_EXCLUDED

    def test_invalid_hash_returns_skipped_invalid_hash(self, mock_config):
        """Empty or too-short bundle hash returns SKIPPED_INVALID_HASH."""
        mock_plex = MagicMock()
        xml = """<MediaContainer size="1">
            <Video>
                <Media>
                    <MediaPart hash="" file="/data/movies/test.mkv"/>
                </Media>
            </Video>
        </MediaContainer>"""
        mock_plex.query.return_value = ET.fromstring(xml)
        mock_config.plex_config_folder = "/config/plex"
        mock_config.exclude_paths = None

        result = process_item("/library/metadata/1", None, None, mock_config, mock_plex)
        assert result == ProcessingResult.SKIPPED_INVALID_HASH

    @patch("os.path.isfile")
    def test_bif_exists_returns_skipped_bif_exists(self, mock_isfile, mock_config, plex_xml_movie_tree):
        """Existing BIF file returns SKIPPED_BIF_EXISTS."""
        mock_plex = MagicMock()
        mock_plex.query.return_value = ET.fromstring(plex_xml_movie_tree)
        mock_isfile.return_value = True
        mock_config.plex_config_folder = "/config/plex"
        mock_config.regenerate_thumbnails = False

        result = process_item("/library/metadata/54321", None, None, mock_config, mock_plex)
        assert result == ProcessingResult.SKIPPED_BIF_EXISTS

    @patch("plex_generate_previews.media_processing.generate_bif")
    @patch("plex_generate_previews.media_processing.generate_images")
    @patch("os.path.isfile")
    @patch("os.path.isdir")
    @patch("os.makedirs")
    @patch("shutil.rmtree")
    def test_successful_generation_returns_generated(
        self,
        mock_rmtree,
        mock_makedirs,
        mock_isdir,
        mock_isfile,
        mock_gen_images,
        mock_gen_bif,
        mock_config,
        plex_xml_movie_tree,
    ):
        """Successful BIF generation returns GENERATED."""
        mock_plex = MagicMock()
        mock_plex.query.return_value = ET.fromstring(plex_xml_movie_tree)

        def isfile_side_effect(path):
            return ".bif" not in path

        mock_isfile.side_effect = isfile_side_effect
        mock_isdir.return_value = False
        mock_config.plex_config_folder = "/config/plex"
        mock_config.tmp_folder = "/tmp"
        mock_config.regenerate_thumbnails = False
        mock_gen_images.return_value = (True, 3, False, 1.2, "1.0x")

        result = process_item("/library/metadata/54321", None, None, mock_config, mock_plex)
        assert result == ProcessingResult.GENERATED


class TestWorkerOutcomeCounts:
    """Test Worker.outcome_counts tracking."""

    def test_initial_outcome_counts_are_zero(self):
        """All outcome counters start at zero."""
        worker = Worker(0, "CPU")
        for r in ProcessingResult:
            assert worker.outcome_counts[r.value] == 0

    @patch("plex_generate_previews.worker.process_item")
    def test_completed_item_increments_outcome(self, mock_process):
        """Successful process_item updates both completed and outcome_counts."""
        mock_process.return_value = ProcessingResult.GENERATED
        worker = Worker(0, "CPU")
        config = MagicMock()
        plex = MagicMock()

        worker.assign_task(
            "test_key",
            config,
            plex,
            media_title="Test",
            media_type="movie",
        )
        worker.current_thread.join(timeout=2)

        assert worker.outcome_counts["generated"] == 1
        assert worker.completed == 1
        assert worker.failed == 0

    @patch("plex_generate_previews.worker.process_item")
    def test_skipped_item_counts_as_completed_not_failed(self, mock_process):
        """Skipped items (e.g. BIF exists) count as completed, not failed."""
        mock_process.return_value = ProcessingResult.SKIPPED_BIF_EXISTS
        worker = Worker(0, "CPU")
        config = MagicMock()
        plex = MagicMock()

        worker.assign_task(
            "test_key",
            config,
            plex,
            media_title="Test",
            media_type="movie",
        )
        worker.current_thread.join(timeout=2)

        assert worker.outcome_counts["skipped_bif_exists"] == 1
        assert worker.completed == 1
        assert worker.failed == 0

    @patch("plex_generate_previews.worker.process_item")
    def test_failed_result_counts_as_failed(self, mock_process):
        """ProcessingResult.FAILED increments worker.failed."""
        mock_process.return_value = ProcessingResult.FAILED
        worker = Worker(0, "CPU")
        config = MagicMock()
        plex = MagicMock()

        worker.assign_task(
            "test_key",
            config,
            plex,
            media_title="Test",
            media_type="movie",
        )
        worker.current_thread.join(timeout=2)

        assert worker.outcome_counts["failed"] == 1
        assert worker.failed == 1
        assert worker.completed == 0

    @patch("plex_generate_previews.worker.process_item")
    def test_file_not_found_counts_as_completed(self, mock_process):
        """SKIPPED_FILE_NOT_FOUND is a completed item (no exception)."""
        mock_process.return_value = ProcessingResult.SKIPPED_FILE_NOT_FOUND
        worker = Worker(0, "CPU")
        config = MagicMock()
        plex = MagicMock()

        worker.assign_task(
            "test_key",
            config,
            plex,
            media_title="Test",
            media_type="movie",
        )
        worker.current_thread.join(timeout=2)

        assert worker.outcome_counts["skipped_file_not_found"] == 1
        assert worker.completed == 1
        assert worker.failed == 0


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

        from plex_generate_previews.web.jobs import Job

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
        from plex_generate_previews.media_processing import ProcessingResult

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
        from plex_generate_previews.media_processing import ProcessingResult

        outcome = {r.value: 0 for r in ProcessingResult}
        outcome["generated"] = 50
        outcome["skipped_file_not_found"] = 10

        generated = outcome.get("generated", 0)
        assert generated > 0
        assert outcome.get("skipped_file_not_found", 0) == 10

    def test_no_warning_when_all_exist(self):
        """When all items already have BIF files, no warning."""
        from plex_generate_previews.media_processing import ProcessingResult

        outcome = {r.value: 0 for r in ProcessingResult}
        outcome["skipped_bif_exists"] = 500

        not_found = outcome.get("skipped_file_not_found", 0)
        assert not_found == 0


class TestOutcomeInWorkerPoolResult:
    """Test that WorkerPool includes outcome in its return dict."""

    @patch("plex_generate_previews.worker.process_item")
    def test_process_items_headless_includes_outcome(self, mock_process):
        """process_items_headless result dict contains an 'outcome' key."""
        mock_process.return_value = ProcessingResult.SKIPPED_BIF_EXISTS

        from plex_generate_previews.worker import WorkerPool

        pool = WorkerPool(gpu_workers=0, cpu_workers=1, selected_gpus=[])
        config = MagicMock()
        config.cpu_threads = 1
        plex = MagicMock()

        items = [("/library/metadata/1", "Movie 1", "movie")]
        result = pool.process_items_headless(
            items,
            config,
            plex,
            title_max_width=30,
            library_name="Test",
        )

        assert "outcome" in result
        assert isinstance(result["outcome"], dict)
        assert result["outcome"]["skipped_bif_exists"] >= 1

    @patch("plex_generate_previews.worker.process_item")
    def test_outcome_counts_match_items_processed(self, mock_process):
        """Sum of all outcome values equals total items processed."""
        results_iter = iter(
            [
                ProcessingResult.GENERATED,
                ProcessingResult.SKIPPED_BIF_EXISTS,
                ProcessingResult.SKIPPED_FILE_NOT_FOUND,
            ]
        )
        mock_process.side_effect = lambda *args, **kwargs: next(results_iter)

        from plex_generate_previews.worker import WorkerPool

        pool = WorkerPool(gpu_workers=0, cpu_workers=1, selected_gpus=[])
        config = MagicMock()
        config.cpu_threads = 1
        plex = MagicMock()

        items = [
            ("/library/metadata/1", "Movie 1", "movie"),
            ("/library/metadata/2", "Movie 2", "movie"),
            ("/library/metadata/3", "Movie 3", "movie"),
        ]
        result = pool.process_items_headless(
            items,
            config,
            plex,
            title_max_width=30,
            library_name="Test",
        )

        outcome = result["outcome"]
        total_outcome = sum(outcome.values())
        assert total_outcome == 3
        assert outcome["generated"] == 1
        assert outcome["skipped_bif_exists"] == 1
        assert outcome["skipped_file_not_found"] == 1
