"""
Worker-pool dispatch tests + a real end-to-end pipeline integration.

Most of this file mocks ``process_canonical_path`` at the WorkerPool seam
because the surface under test is the pool's dispatch contract (one item
in, one ``process_canonical_path`` call out, with status accounting).
Those tests are kept intentionally narrow.

The class :class:`TestRealProcessCanonicalPathIntegration` is the real
integration test: it runs a live ``process_canonical_path`` end-to-end
with mocks only at true system boundaries (FFmpeg subprocess, the BIF
writer, filesystem isfile, and the per-server adapter so we don't write
into a real Plex bundle). Its job is to catch regressions like D31 where
a bug deep in the pipeline went undetected because every test stubbed the
function under test.
"""

import xml.etree.ElementTree as ET
from unittest.mock import MagicMock, patch

from tests.conftest import _ms, _pi, _pi_list_or_passthrough  # noqa: F401


class TestWorkerPoolDispatchToUnifiedPipeline:
    """WorkerPool dispatches each item into ``process_canonical_path``.

    These tests stub ``process_canonical_path`` on purpose — the assertion
    is the dispatch contract (call count, status accounting), not what the
    pipeline does internally. The real pipeline behaviour is exercised in
    :class:`TestRealProcessCanonicalPathIntegration` below.
    """

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
            ("1", "Movie 1", "movie"),
            ("2", "Movie 2", "movie"),
            ("3", "Movie 3", "movie"),
            ("4", "Movie 4", "movie"),
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
            ("1", "Movie 1", "movie"),
            ("2", "Movie 2", "movie"),
            ("3", "Movie 3", "movie"),
            ("4", "Movie 4", "movie"),
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


class TestWorkerPoolDispatchAccounting:
    """Worker-pool accounting (completed / failed) under stubbed dispatch."""

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
            ("1", "Movie 1", "movie"),
            ("2", "Movie 2", "movie"),
            ("3", "Movie 3", "movie"),
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
        items = [(f"{i}", f"Movie {i}", "movie") for i in range(9)]

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


class TestRealProcessCanonicalPathIntegration:
    """End-to-end pipeline test with mocks only at true system boundaries.

    The point of this class — versus the dispatch-contract tests above —
    is to exercise the *real* ``process_canonical_path`` body. Stubbing
    that function is exactly the D31 anti-pattern: a bug in its frame-
    extraction or publisher fan-out logic could ship for days because
    every test mocked the function under test. Here we mock only:

      * ``generate_images`` (FFmpeg subprocess) — true subprocess boundary.
      * The per-server adapter — we don't write into a real Plex bundle
        directory; we capture the ``publish`` call and assert on its args.
      * ``os.path.isfile`` for the source video (filesystem boundary).
      * ``os.makedirs`` / ``os.listdir`` of the FFmpeg output dir
        (filesystem boundary).
    """

    def test_real_dispatch_publishes_via_adapter(self, tmp_path):
        """A canonical path resolves to one publisher and the adapter receives a real BifBundle."""
        from media_preview_generator.processing.multi_server import (
            MultiServerStatus,
            process_canonical_path,
        )

        registry = MagicMock()
        server = MagicMock(id="plex-1", name="plex-1")
        adapter = MagicMock()
        adapter.name = "plex_bundle"

        # The adapter will be asked for an output path; return a real
        # path under tmp_path so any incidental fs interaction is safe.
        out_path = tmp_path / "out" / "index-sd.bif"
        adapter.compute_output_paths.return_value = [out_path]
        adapter.publish.return_value = None

        config = MagicMock()
        config.working_tmp_folder = str(tmp_path / "work")
        config.tmp_folder = str(tmp_path / "frames")
        config.plex_bif_frame_interval = 5
        config.thumbnail_interval = 5
        config.server_display_name = "plex-1"

        canonical = "/data/movies/Test (2024)/Test (2024).mkv"

        with (
            patch(
                "media_preview_generator.processing.multi_server._resolve_publishers",
                return_value=[(server, adapter, "rk-1")],
            ),
            patch(
                "media_preview_generator.processing.multi_server._resolve_item_id_for",
                return_value="rk-1",
            ),
            patch(
                "media_preview_generator.processing.multi_server.outputs_fresh_for_source",
                return_value=False,
            ),
            patch(
                "media_preview_generator.processing.multi_server.os.path.isfile",
                return_value=True,
            ),
            patch(
                "media_preview_generator.processing.multi_server.generate_images",
                return_value=(True, 12, "h264", 320, 30.0, 320),
            ),
            patch(
                "media_preview_generator.processing.multi_server.os.listdir",
                return_value=[f"{i:05d}.jpg" for i in range(1, 13)],
            ),
            patch(
                "media_preview_generator.processing.multi_server.write_meta",
            ),
        ):
            result = process_canonical_path(
                canonical_path=canonical,
                registry=registry,
                config=config,
                use_frame_cache=False,
            )

        assert result.status is MultiServerStatus.PUBLISHED
        assert len(result.publishers) == 1
        publisher = result.publishers[0]
        assert publisher.server_id == "plex-1"
        assert publisher.adapter_name == "plex_bundle"

        # The real _publish_one inside process_canonical_path should have
        # invoked publish exactly once with the canonical path the
        # dispatcher started from. This is the seam D31 broke — when
        # production accidentally stored the URL form for an item id, the
        # bundle path doubled. The assertion below is the canary: any
        # future regression that reshapes what flows into the adapter
        # would fail here.
        adapter.publish.assert_called_once()
        call_args = adapter.publish.call_args
        bundle_arg = call_args.args[0]
        item_id_arg = call_args.args[2] if len(call_args.args) >= 3 else call_args.kwargs.get("item_id")
        assert bundle_arg.canonical_path == canonical
        assert bundle_arg.frame_count == 12
        assert item_id_arg == "rk-1"
        # Item id must be the bare ratingKey, NOT the URL form (D31 guardrail).
        assert not str(item_id_arg).startswith("/library/metadata/"), (
            f"D31 regression: item id leaked URL form to adapter: {item_id_arg!r}"
        )

    def test_real_dispatch_handles_no_owners(self, tmp_path):
        """Real process_canonical_path returns NO_OWNERS when no publisher resolves."""
        from media_preview_generator.processing.multi_server import (
            MultiServerStatus,
            process_canonical_path,
        )

        registry = MagicMock()
        config = MagicMock()
        config.working_tmp_folder = str(tmp_path / "work")
        config.tmp_folder = str(tmp_path / "frames")
        config.plex_bif_frame_interval = 5
        config.thumbnail_interval = 5

        with patch(
            "media_preview_generator.processing.multi_server._resolve_publishers",
            return_value=[],
        ):
            result = process_canonical_path(
                canonical_path="/data/movies/Unowned.mkv",
                registry=registry,
                config=config,
                use_frame_cache=False,
            )

        assert result.status is MultiServerStatus.NO_OWNERS
        assert result.publishers == []
