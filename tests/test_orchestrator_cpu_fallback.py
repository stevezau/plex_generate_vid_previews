"""Regression: orchestrator full-scan path must honour CPU fallback.

Live failure (2026-05-14, job ``a90c9b87`` TV Shows full scan): four
Re:ZERO episodes hit ``CodecNotSupportedError`` from the GPU mjpeg encoder
(exit code 218, ``vost#0:0/mjpeg ... Task finished with error code: -22``).
The user-facing logs reported:

    WARNING - GPU processing failed for ... — automatically handing off to a CPU worker.
    INFO    - Hardware acceleration could not handle the codec ... — retrying on CPU automatically.
    WARNING - Multi-server full scan: per-item processing failed (CodecNotSupportedError: ...)

…but no actual CPU retry happened. None of the 4 files have a
``"completed CPU fallback"`` log line. The orchestrator's per-item
``except Exception`` arm at ``jobs/orchestrator.py:1079`` swallowed
``CodecNotSupportedError`` and gave up — even though
``jobs/worker.py:557-613`` (the webhook/JobDispatcher path) has the
in-place CPU retry that mirrors what the announcement promised.

This test drives the full-scan dispatcher with a mocked
``process_canonical_path`` that raises ``CodecNotSupportedError`` on the
first call (GPU attempt) and returns success on the second (CPU
fallback). Pre-fix the second call never happened. Post-fix the
fallback runs with ``gpu=None`` and the result counts as success.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from media_preview_generator.jobs.orchestrator import _run_full_scan_multi_server
from media_preview_generator.processing import ProcessingResult
from media_preview_generator.processing.generator import CodecNotSupportedError
from media_preview_generator.processing.multi_server import MultiServerStatus
from media_preview_generator.processing.types import ProcessableItem
from media_preview_generator.servers.base import ServerConfig, ServerType

MODULE = "media_preview_generator.jobs.orchestrator"


def _config(cpu_threads: int = 1):
    return SimpleNamespace(
        gpu_threads=0,
        cpu_threads=cpu_threads,
        working_tmp_folder="/tmp/work",
        plex_url="",
        plex_token="",
        webhook_paths=None,
        server_id_filter=None,
    )


def _server_config(server_id, server_type=ServerType.JELLYFIN):
    return ServerConfig(
        id=server_id,
        type=server_type,
        name=f"Test {server_type.value}",
        enabled=True,
        url="http://test",
        auth={"access_token": "t"},
    )


def _success_result():
    """A successful per-item dispatch result with one published publisher.

    Mirrors the shape ``test_aggregates_per_publisher_outcomes_into_processing_result_counts``
    uses in ``test_full_scan_multi_server.py`` — the orchestrator folds
    per-publisher ``status.value`` into ``counts``, so a non-empty
    ``publishers`` list with ``status.value="published"`` is what surfaces
    as a ``published`` count (the success column on a multi-server scan).
    """
    return MagicMock(
        status=MultiServerStatus.PUBLISHED,
        publishers=[MagicMock(status=MagicMock(value="published"))],
    )


class TestOrchestratorCpuFallback:
    """Cover the GPU→CPU fallback matrix on the full-scan dispatcher.

    Branching variable: outcome of the first ``process_canonical_path``
    call. Three live cells:

    1. GPU succeeds → no fallback, no CPU call.
    2. GPU raises CodecNotSupportedError, CPU succeeds → fallback works.
    3. GPU raises CodecNotSupportedError, CPU also fails → file is
       marked failed but other items keep processing.

    Pre-fix only cell 1 worked; cells 2 and 3 collapsed into the bare
    ``except Exception`` and never tried CPU.
    """

    def _setup_one_item_dispatch(self):
        """Common scaffolding: 1 server, 1 item, mocked enumeration."""
        cfg = _server_config("srv-a", ServerType.JELLYFIN)
        registry_mock = MagicMock()
        registry_mock.configs.return_value = [cfg]
        proc = MagicMock()
        proc.list_canonical_paths.return_value = iter(
            [ProcessableItem(canonical_path="/data/anime.mkv", server_id="srv-a")]
        )
        return cfg, registry_mock, proc

    def test_gpu_succeeds_does_not_invoke_cpu_fallback(self, tmp_path):
        """Sanity: a normal GPU success must not trigger any CPU re-run.

        Without this row, a refactor that always re-runs on CPU would
        silently double the workload on healthy items.
        """
        cfg, registry_mock, proc = self._setup_one_item_dispatch()

        with (
            patch("media_preview_generator.web.settings_manager.get_settings_manager") as mock_sm,
            patch("media_preview_generator.servers.ServerRegistry") as mock_registry,
            patch("media_preview_generator.processing.get_processor_for", return_value=proc),
            patch("media_preview_generator.processing.multi_server.process_canonical_path") as mock_pcp,
        ):
            mock_sm.return_value.get.return_value = [{"id": "srv-a", "type": "jellyfin", "enabled": True}]
            mock_registry.from_settings.return_value = registry_mock
            mock_pcp.return_value = _success_result()

            _run_full_scan_multi_server(_config(), selected_gpus=[("NVIDIA", "/dev/nvidia0", {})])

        assert mock_pcp.call_count == 1, (
            f"GPU success path must call process_canonical_path exactly once, got {mock_pcp.call_count}"
        )

    def test_gpu_codec_error_falls_back_to_cpu_with_gpu_none(self, tmp_path):
        """GPU raises CodecNotSupportedError → second call must run with gpu=None.

        This is the live regression: pre-fix the orchestrator caught
        the error in its bare ``except Exception`` and gave up, so the
        announced "retrying on CPU automatically" log line was a lie.
        """
        cfg, registry_mock, proc = self._setup_one_item_dispatch()

        # Fail on first call (GPU), succeed on second (CPU fallback).
        # Asserting the kwargs of BOTH calls ensures the fix forwards
        # the right gpu= value — per .claude/rules/testing.md the test
        # must lock in the contract the SUT controls (gpu=None on retry),
        # not just that fallback was attempted.
        outcomes = [
            CodecNotSupportedError("GPU processing failed (hardware accelerator runtime error) for /data/anime.mkv"),
            _success_result(),
        ]
        mock_pcp = MagicMock(side_effect=outcomes)

        with (
            patch("media_preview_generator.web.settings_manager.get_settings_manager") as mock_sm,
            patch("media_preview_generator.servers.ServerRegistry") as mock_registry,
            patch("media_preview_generator.processing.get_processor_for", return_value=proc),
            patch("media_preview_generator.processing.multi_server.process_canonical_path", mock_pcp),
        ):
            mock_sm.return_value.get.return_value = [{"id": "srv-a", "type": "jellyfin", "enabled": True}]
            mock_registry.from_settings.return_value = registry_mock

            counts = _run_full_scan_multi_server(_config(), selected_gpus=[("NVIDIA", "/dev/nvidia0", {})])

        assert mock_pcp.call_count == 2, (
            f"GPU CodecNotSupportedError must trigger one CPU retry (2 total calls), "
            f"got {mock_pcp.call_count}. Pre-fix this was 1 — the fallback never ran."
        )

        # First call: GPU attempt — must include the GPU type/device the orchestrator was given.
        first_kwargs = mock_pcp.call_args_list[0].kwargs
        assert first_kwargs["gpu"] == "NVIDIA", f"first call gpu kwarg should be 'NVIDIA', got {first_kwargs['gpu']!r}"
        assert first_kwargs["gpu_device_path"] == "/dev/nvidia0"

        # Second call: CPU fallback — must explicitly pass gpu=None / gpu_device_path=None.
        # If the fix forwards the original gpu= here by mistake, FFmpeg would
        # re-attempt on the same GPU and fail with the same error.
        second_kwargs = mock_pcp.call_args_list[1].kwargs
        assert second_kwargs["gpu"] is None, (
            f"CPU fallback call must pass gpu=None to disable hardware acceleration; got {second_kwargs['gpu']!r}"
        )
        assert second_kwargs["gpu_device_path"] is None, (
            f"CPU fallback call must pass gpu_device_path=None; got {second_kwargs['gpu_device_path']!r}"
        )

        # Other kwargs should be preserved across the retry — same canonical_path,
        # same server_id_filter, same regenerate flag. A fix that fat-fingered
        # one of these would silently change behaviour on the retry.
        assert second_kwargs["canonical_path"] == first_kwargs["canonical_path"]
        assert second_kwargs["server_id_filter"] == first_kwargs["server_id_filter"]
        assert second_kwargs["regenerate"] == first_kwargs["regenerate"]

        # Counts: CPU fallback succeeded → the per-publisher ``published``
        # status is folded into ``counts`` (orchestrator behaviour locked
        # in by ``test_aggregates_per_publisher_outcomes_into_processing_result_counts``).
        # Pre-fix this would have been 0 because no result was returned.
        assert counts.get(ProcessingResult.FAILED.value, 0) == 0, (
            f"successful CPU fallback must NOT be counted as failed; counts={counts}"
        )
        assert counts.get("published", 0) == 1, (
            f"successful CPU fallback must surface as a published item; counts={counts}"
        )

    def test_cpu_fallback_failure_is_marked_failed_and_does_not_propagate(self, tmp_path):
        """If CPU also fails, the item is marked failed but other items keep going.

        The live regression's bare ``except Exception`` already had this
        property for the GPU-attempt case; the new fallback arm must
        preserve it (re-raising would tank the rest of the dispatch).
        """
        cfg, registry_mock, proc = self._setup_one_item_dispatch()
        proc.list_canonical_paths.return_value = iter(
            [
                ProcessableItem(canonical_path="/data/a.mkv", server_id="srv-a"),
                ProcessableItem(canonical_path="/data/b.mkv", server_id="srv-a"),
            ]
        )

        # First item: GPU fails → CPU fails too. Second item: GPU succeeds.
        # Per-item CodecNotSupportedError on item 1 must NOT block item 2.
        cpu_failure = RuntimeError("CPU also failed: out of memory")

        def pcp_side_effect(**kwargs):
            if kwargs["canonical_path"] == "/data/a.mkv":
                if kwargs["gpu"] is not None:
                    raise CodecNotSupportedError("GPU processing failed for /data/a.mkv")
                # CPU fallback for /data/a.mkv also fails
                raise cpu_failure
            # /data/b.mkv on GPU: success
            return _success_result()

        mock_pcp = MagicMock(side_effect=pcp_side_effect)

        with (
            patch("media_preview_generator.web.settings_manager.get_settings_manager") as mock_sm,
            patch("media_preview_generator.servers.ServerRegistry") as mock_registry,
            patch("media_preview_generator.processing.get_processor_for", return_value=proc),
            patch("media_preview_generator.processing.multi_server.process_canonical_path", mock_pcp),
        ):
            mock_sm.return_value.get.return_value = [{"id": "srv-a", "type": "jellyfin", "enabled": True}]
            mock_registry.from_settings.return_value = registry_mock

            # Must not raise — bare except below the new arm should still
            # catch the secondary failure and the dispatch should complete.
            _run_full_scan_multi_server(_config(), selected_gpus=[("NVIDIA", "/dev/nvidia0", {})])

        # 3 calls total: a.mkv GPU (raises CodecError), a.mkv CPU (raises RuntimeError), b.mkv GPU (succeeds).
        assert mock_pcp.call_count == 3, (
            f"expected GPU-attempt + CPU-fallback for a.mkv plus GPU for b.mkv (3 calls), got {mock_pcp.call_count}"
        )

        # b.mkv must have been processed despite a.mkv failing on both attempts.
        b_calls = [c for c in mock_pcp.call_args_list if c.kwargs.get("canonical_path") == "/data/b.mkv"]
        assert len(b_calls) == 1, (
            "/data/b.mkv must still get its turn even when /data/a.mkv exhausted both GPU and CPU attempts"
        )
