"""
Integration tests for the full processing pipeline.

Tests the complete flow from Plex query through worker pool
to BIF generation with all components working together.
"""

import xml.etree.ElementTree as ET
from unittest.mock import MagicMock, patch

from media_preview_generator.processing import ProcessingResult


class TestFullPipeline:
    """Test complete processing pipeline."""

    @patch("os.path.isfile")
    @patch("media_preview_generator.processing.multi_server.process_canonical_path")
    def test_full_pipeline_single_video(
        self,
        mock_process_canonical,
        mock_isfile,
        mock_config,
        plex_xml_movie_tree,
    ):
        """Single-video processing reaches the unified per-vendor pipeline."""
        from media_preview_generator.processing import process_item
        from media_preview_generator.processing.multi_server import (
            MultiServerResult,
            MultiServerStatus,
        )

        mock_plex = MagicMock()
        mock_plex.query.return_value = ET.fromstring(plex_xml_movie_tree)
        mock_isfile.return_value = True

        mock_config.plex_config_folder = "/config/plex"
        mock_config.tmp_folder = "/tmp"
        mock_config.plex_local_videos_path_mapping = ""
        mock_config.plex_videos_path_mapping = ""
        mock_config.regenerate_thumbnails = False
        mock_process_canonical.return_value = MultiServerResult(
            canonical_path="/data/movies/Test Movie (2024)/Test Movie (2024).mkv",
            status=MultiServerStatus.PUBLISHED,
        )

        process_item("/library/metadata/54321", None, None, mock_config, mock_plex)

        assert mock_process_canonical.called

    @patch("media_preview_generator.jobs.worker.process_item")
    def test_full_pipeline_multiple_videos(self, mock_process, mock_config):
        """Test processing multiple videos with worker pool."""
        import time

        from media_preview_generator.jobs.worker import WorkerPool

        # Mock process_item to simulate some processing time
        def mock_process_fn(*args, **kwargs):
            time.sleep(0.01)  # Small delay to simulate work
            return ProcessingResult.GENERATED

        mock_process.side_effect = mock_process_fn

        # Create worker pool with CPU workers only (no GPU in CI)
        pool = WorkerPool(gpu_workers=0, cpu_workers=2, selected_gpus=[])

        # Mock Plex
        mock_plex = MagicMock()

        # Test items
        items = [
            ("/library/metadata/1", "Movie 1", "movie"),
            ("/library/metadata/2", "Movie 2", "movie"),
            ("/library/metadata/3", "Movie 3", "movie"),
            ("/library/metadata/4", "Movie 4", "movie"),
        ]

        # Mock progress
        main_progress = MagicMock()
        worker_progress = MagicMock()
        worker_progress.add_task = MagicMock(side_effect=list(range(10)))

        # Process items
        pool.process_items(items, mock_config, mock_plex, worker_progress, main_progress)

        # Verify all items were processed
        assert mock_process.call_count == 4
        total_completed = sum(w.completed for w in pool.workers)
        assert total_completed == 4

    @patch("media_preview_generator.jobs.worker.process_item")
    def test_full_pipeline_with_errors(self, mock_process, mock_config):
        """Test pipeline with some items failing."""
        import time

        from media_preview_generator.jobs.worker import WorkerPool

        # Make some items fail
        call_count = [0]

        def process_with_errors(*args, **kwargs):
            time.sleep(0.01)  # Small delay to simulate work
            call_count[0] += 1
            if call_count[0] % 2 == 0:
                raise Exception("Processing failed")
            return ProcessingResult.GENERATED

        mock_process.side_effect = process_with_errors

        # Create worker pool
        pool = WorkerPool(gpu_workers=0, cpu_workers=2, selected_gpus=[])

        mock_plex = MagicMock()

        items = [
            ("/library/metadata/1", "Movie 1", "movie"),
            ("/library/metadata/2", "Movie 2", "movie"),
            ("/library/metadata/3", "Movie 3", "movie"),
            ("/library/metadata/4", "Movie 4", "movie"),
        ]

        main_progress = MagicMock()
        worker_progress = MagicMock()
        worker_progress.add_task = MagicMock(side_effect=list(range(10)))

        # Process items (should handle errors gracefully)
        pool.process_items(items, mock_config, mock_plex, worker_progress, main_progress)

        # Verify some succeeded and some failed
        total_completed = sum(w.completed for w in pool.workers)
        total_failed = sum(w.failed for w in pool.workers)

        assert total_completed > 0  # At least some succeeded
        assert total_failed > 0  # At least some failed
        assert total_completed + total_failed == 4  # All were attempted


class TestWorkerPoolIntegration:
    """Test worker pool integration with processing."""

    @patch("os.path.isfile")
    @patch("media_preview_generator.processing.multi_server.process_canonical_path")
    def test_worker_pool_integration(
        self,
        mock_process_canonical,
        mock_isfile,
        mock_config,
        plex_xml_movie_tree,
    ):
        """Worker pool coordinates multiple workers; each item dispatches into the unified pipeline."""
        from media_preview_generator.jobs.worker import WorkerPool
        from media_preview_generator.processing.multi_server import (
            MultiServerResult,
            MultiServerStatus,
        )

        mock_plex = MagicMock()
        mock_plex.query.return_value = ET.fromstring(plex_xml_movie_tree)
        mock_isfile.return_value = True

        mock_config.plex_config_folder = "/config/plex"
        mock_config.tmp_folder = "/tmp"
        mock_config.plex_local_videos_path_mapping = ""
        mock_config.plex_videos_path_mapping = ""
        mock_config.regenerate_thumbnails = False
        mock_process_canonical.return_value = MultiServerResult(
            canonical_path="/data/movies/Test Movie (2024)/Test Movie (2024).mkv",
            status=MultiServerStatus.PUBLISHED,
        )

        pool = WorkerPool(gpu_workers=0, cpu_workers=3, selected_gpus=[])

        items = [
            ("/library/metadata/1", "Movie 1", "movie"),
            ("/library/metadata/2", "Movie 2", "movie"),
            ("/library/metadata/3", "Movie 3", "movie"),
        ]

        main_progress = MagicMock()
        worker_progress = MagicMock()
        worker_progress.add_task = MagicMock(side_effect=list(range(10)))

        pool.process_items(items, mock_config, mock_plex, worker_progress, main_progress)

        total_completed = sum(w.completed for w in pool.workers)
        assert total_completed == 3
        # Each of the 3 items should have funneled into process_canonical_path.
        assert mock_process_canonical.call_count == 3

    @patch("media_preview_generator.jobs.worker.process_item")
    def test_worker_pool_load_balancing(self, mock_process, mock_config):
        """Test that work is distributed across workers."""
        import time

        from media_preview_generator.jobs.worker import WorkerPool

        # Simulate variable processing times
        def variable_process(*args, **kwargs):
            time.sleep(0.01)  # Small delay to simulate work
            return ProcessingResult.GENERATED

        mock_process.side_effect = variable_process

        # Create pool with multiple workers
        pool = WorkerPool(gpu_workers=0, cpu_workers=3, selected_gpus=[])

        mock_plex = MagicMock()

        # Many items to ensure distribution
        items = [(f"/library/metadata/{i}", f"Movie {i}", "movie") for i in range(9)]

        main_progress = MagicMock()
        worker_progress = MagicMock()
        worker_progress.add_task = MagicMock(side_effect=list(range(20)))

        # Process
        pool.process_items(items, mock_config, mock_plex, worker_progress, main_progress)

        # Verify work was distributed (each worker should have processed some items)
        for worker in pool.workers:
            assert worker.completed > 0, f"Worker {worker.worker_id} did no work"

        # Total should equal input
        total = sum(w.completed for w in pool.workers)
        assert total == 9
