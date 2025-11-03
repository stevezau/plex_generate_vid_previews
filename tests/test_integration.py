"""
Integration tests for the full processing pipeline.

Tests the complete flow from Plex query through worker pool
to BIF generation with all components working together.
"""

import pytest
from unittest.mock import MagicMock, patch
import xml.etree.ElementTree as ET


class TestFullPipeline:
    """Test complete processing pipeline."""
    
    @patch('plex_generate_previews.media_processing.generate_bif')
    @patch('plex_generate_previews.media_processing.generate_images')
    @patch('os.path.isfile')
    @patch('os.path.isdir')
    @patch('os.makedirs')
    @patch('shutil.rmtree')
    def test_full_pipeline_single_video(self, mock_rmtree, mock_makedirs, mock_isdir, 
                                        mock_isfile, mock_gen_images, mock_gen_bif, 
                                        mock_config, plex_xml_movie_tree):
        """Test processing a single video through the full pipeline."""
        from plex_generate_previews.worker import WorkerPool
        from plex_generate_previews.media_processing import process_item
        
        # Mock Plex
        mock_plex = MagicMock()
        mock_plex.query.return_value = ET.fromstring(plex_xml_movie_tree)
        
        # Mock file system - media file exists but index.bif doesn't
        def isfile_side_effect(path):
            # Media files exist, but not BIF files
            return '.bif' not in path
        
        mock_isfile.side_effect = isfile_side_effect
        mock_isdir.return_value = False  # Directories don't exist yet
        
        mock_config.plex_config_folder = "/config/plex"
        mock_config.tmp_folder = "/tmp"
        mock_config.plex_local_videos_path_mapping = ""
        mock_config.plex_videos_path_mapping = ""
        mock_config.regenerate_thumbnails = False
        
        # Simulate successful image generation
        mock_gen_images.return_value = (True, 3, False, 1.3, "1.0x")
        # Process single item
        process_item("/library/metadata/54321", None, None, mock_config, mock_plex)
        
        # Verify pipeline executed
        assert mock_gen_images.called
        assert mock_gen_bif.called
    
    @patch('plex_generate_previews.worker.process_item')
    def test_full_pipeline_multiple_videos(self, mock_process, mock_config):
        """Test processing multiple videos with worker pool."""
        from plex_generate_previews.worker import WorkerPool
        import time
        
        # Mock process_item to simulate some processing time
        def mock_process_fn(*args, **kwargs):
            time.sleep(0.01)  # Small delay to simulate work
        
        mock_process.side_effect = mock_process_fn
        
        # Create worker pool with CPU workers only (no GPU in CI)
        pool = WorkerPool(gpu_workers=0, cpu_workers=2, selected_gpus=[])
        
        # Mock Plex
        mock_plex = MagicMock()
        
        # Test items
        items = [
            ('/library/metadata/1', 'Movie 1', 'movie'),
            ('/library/metadata/2', 'Movie 2', 'movie'),
            ('/library/metadata/3', 'Movie 3', 'movie'),
            ('/library/metadata/4', 'Movie 4', 'movie'),
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
    
    @patch('plex_generate_previews.worker.process_item')
    def test_full_pipeline_with_errors(self, mock_process, mock_config):
        """Test pipeline with some items failing."""
        from plex_generate_previews.worker import WorkerPool
        import time
        
        # Make some items fail
        call_count = [0]
        def process_with_errors(*args, **kwargs):
            time.sleep(0.01)  # Small delay to simulate work
            call_count[0] += 1
            if call_count[0] % 2 == 0:
                raise Exception("Processing failed")
        
        mock_process.side_effect = process_with_errors
        
        # Create worker pool
        pool = WorkerPool(gpu_workers=0, cpu_workers=2, selected_gpus=[])
        
        mock_plex = MagicMock()
        
        items = [
            ('/library/metadata/1', 'Movie 1', 'movie'),
            ('/library/metadata/2', 'Movie 2', 'movie'),
            ('/library/metadata/3', 'Movie 3', 'movie'),
            ('/library/metadata/4', 'Movie 4', 'movie'),
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
    
    @patch('plex_generate_previews.media_processing.generate_bif')
    @patch('plex_generate_previews.media_processing.generate_images')
    @patch('os.path.isfile')
    @patch('os.path.isdir')
    @patch('os.makedirs')
    @patch('shutil.rmtree')
    def test_worker_pool_integration(self, mock_rmtree, mock_makedirs, mock_isdir, 
                                     mock_isfile, mock_gen_images, mock_gen_bif, 
                                     mock_config, plex_xml_movie_tree):
        """Test worker pool coordinating multiple workers."""
        from plex_generate_previews.worker import WorkerPool
        
        # Mock Plex
        mock_plex = MagicMock()
        mock_plex.query.return_value = ET.fromstring(plex_xml_movie_tree)
        
        # Mock file system - media file exists but index.bif doesn't
        def isfile_side_effect(path):
            # Media files exist, but not BIF files
            return '.bif' not in path
        
        mock_isfile.side_effect = isfile_side_effect
        mock_isdir.return_value = False  # Directories don't exist yet
        
        mock_config.plex_config_folder = "/config/plex"
        mock_config.tmp_folder = "/tmp"
        mock_config.plex_local_videos_path_mapping = ""
        mock_config.plex_videos_path_mapping = ""
        mock_config.regenerate_thumbnails = False
        
        # Create pool with multiple workers
        pool = WorkerPool(gpu_workers=0, cpu_workers=3, selected_gpus=[])
        
        # Test items
        items = [
            ('/library/metadata/1', 'Movie 1', 'movie'),
            ('/library/metadata/2', 'Movie 2', 'movie'),
            ('/library/metadata/3', 'Movie 3', 'movie'),
        ]
        
        main_progress = MagicMock()
        worker_progress = MagicMock()
        worker_progress.add_task = MagicMock(side_effect=list(range(10)))
        
        # Each item simulates successful image generation
        mock_gen_images.return_value = (True, 1, False, 0.8, "1.0x")
        # Process
        pool.process_items(items, mock_config, mock_plex, worker_progress, main_progress)
        
        # Verify all completed
        total_completed = sum(w.completed for w in pool.workers)
        assert total_completed == 3
        
        # Verify images and BIF generation were called
        assert mock_gen_images.call_count == 3
        assert mock_gen_bif.call_count == 3
    
    @patch('plex_generate_previews.worker.process_item')
    def test_worker_pool_load_balancing(self, mock_process, mock_config):
        """Test that work is distributed across workers."""
        from plex_generate_previews.worker import WorkerPool
        import time
        
        # Simulate variable processing times
        def variable_process(*args, **kwargs):
            time.sleep(0.01)  # Small delay to simulate work
        
        mock_process.side_effect = variable_process
        
        # Create pool with multiple workers
        pool = WorkerPool(gpu_workers=0, cpu_workers=3, selected_gpus=[])
        
        mock_plex = MagicMock()
        
        # Many items to ensure distribution
        items = [(f'/library/metadata/{i}', f'Movie {i}', 'movie') for i in range(9)]
        
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

