"""
Integration tests for the full processing pipeline.

Tests the complete flow from Plex query through worker pool
to BIF generation with all components working together.
"""

import xml.etree.ElementTree as ET
from unittest.mock import MagicMock, patch


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


def _pi_list_or_passthrough(items):
    """Pass through a list of ProcessableItems untouched, or convert tuples on the fly."""
    from media_preview_generator.processing.types import ProcessableItem

    if not items:
        return []
    if isinstance(items[0], ProcessableItem):
        return items
    return _pi_list(items)


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


class TestFullPipeline:
    """Test complete processing pipeline."""

    @patch("media_preview_generator.processing.multi_server.process_canonical_path")
    def test_full_pipeline_single_video(self, mock_process, mock_config):
        """A single ProcessableItem flows through the worker pool to the unified pipeline."""
        from media_preview_generator.jobs.worker import WorkerPool

        mock_process.return_value = _ms("generated")

        pool = WorkerPool(gpu_workers=0, cpu_workers=1, selected_gpus=[])
        items = [_pi("k1", title="Movie 1")]

        main_progress = MagicMock()
        worker_progress = MagicMock()
        worker_progress.add_task = MagicMock(side_effect=list(range(10)))

        pool.process_items(items, mock_config, MagicMock(), worker_progress, main_progress)

        assert mock_process.call_count == 1
        assert sum(w.completed for w in pool.workers) == 1

    @patch("media_preview_generator.processing.multi_server.process_canonical_path")
    def test_full_pipeline_multiple_videos(self, mock_process, mock_config):
        """Test processing multiple videos with worker pool."""
        import time

        from media_preview_generator.jobs.worker import WorkerPool

        # Mock process_item to simulate some processing time
        def mock_process_fn(*args, **kwargs):
            time.sleep(0.01)  # Small delay to simulate work
            return _ms("generated")

        mock_process.side_effect = mock_process_fn

        # Create worker pool with CPU workers only (no GPU in CI)
        pool = WorkerPool(gpu_workers=0, cpu_workers=2, selected_gpus=[])

        # Mock Plex
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
        pool.process_items(_pi_list_or_passthrough(items), mock_config, MagicMock(), worker_progress, main_progress)

        # Verify all items were processed
        assert mock_process.call_count == 4
        total_completed = sum(w.completed for w in pool.workers)
        assert total_completed == 4

    @patch("media_preview_generator.processing.multi_server.process_canonical_path")
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
            return _ms("generated")

        mock_process.side_effect = process_with_errors

        # Create worker pool
        pool = WorkerPool(gpu_workers=0, cpu_workers=2, selected_gpus=[])
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
        pool.process_items(_pi_list_or_passthrough(items), mock_config, MagicMock(), worker_progress, main_progress)

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

        # Plex XML payload is unused now (canonical_path drives the worker
        # directly); keep the parse to validate fixture shape.
        ET.fromstring(plex_xml_movie_tree)
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

        pool.process_items(_pi_list_or_passthrough(items), mock_config, MagicMock(), worker_progress, main_progress)

        total_completed = sum(w.completed for w in pool.workers)
        assert total_completed == 3
        # Each of the 3 items should have funneled into process_canonical_path.
        assert mock_process_canonical.call_count == 3

    @patch("media_preview_generator.processing.multi_server.process_canonical_path")
    def test_worker_pool_load_balancing(self, mock_process, mock_config):
        """Test that work is distributed across workers."""
        import time

        from media_preview_generator.jobs.worker import WorkerPool

        # Simulate variable processing times
        def variable_process(*args, **kwargs):
            time.sleep(0.01)  # Small delay to simulate work
            return _ms("generated")

        mock_process.side_effect = variable_process

        # Create pool with multiple workers
        pool = WorkerPool(gpu_workers=0, cpu_workers=3, selected_gpus=[])
        # Many items to ensure distribution
        items = [(f"/library/metadata/{i}", f"Movie {i}", "movie") for i in range(9)]

        main_progress = MagicMock()
        worker_progress = MagicMock()
        worker_progress.add_task = MagicMock(side_effect=list(range(20)))

        # Process
        pool.process_items(_pi_list_or_passthrough(items), mock_config, MagicMock(), worker_progress, main_progress)

        # Verify work was distributed (each worker should have processed some items)
        for worker in pool.workers:
            assert worker.completed > 0, f"Worker {worker.worker_id} did no work"

        # Total should equal input
        total = sum(w.completed for w in pool.workers)
        assert total == 9
