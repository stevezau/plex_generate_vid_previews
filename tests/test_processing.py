"""Tests for the core processing workflow in processing.py.

Covers run_processing() with mocked Plex, WorkerPool, and config
to exercise the library scan flow, webhook flow, cancellation,
error handling, callbacks, and cleanup paths.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from media_preview_generator.jobs.orchestrator import run_processing
from media_preview_generator.processing import ProcessingResult

MODULE = "media_preview_generator.jobs.orchestrator"


def _make_config(tmp_path, **overrides):
    """Build a minimal mock config with sane defaults."""
    defaults = {
        "gpu_threads": 0,
        "cpu_threads": 1,
        "working_tmp_folder": str(tmp_path / "work"),
        "webhook_paths": None,
        "sort_by": None,
        "plex_url": "http://plex:32400",
        "plex_token": "tok",
        "server_id_filter": None,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _pool_result(completed=0, failed=0, cancelled=False, outcome=None):
    """Build a dict matching WorkerPool.process_items_headless return value."""
    if outcome is None:
        outcome = {r.value: 0 for r in ProcessingResult}
        outcome["generated"] = completed
        outcome["failed"] = failed
    return {
        "completed": completed,
        "failed": failed,
        "cancelled": cancelled,
        "outcome": outcome,
    }


def _make_section(title):
    return SimpleNamespace(title=title)


def _processable(key, title, *, canonical_path=None, server_id="plex-1", library_id="lib-1"):
    """Build a :class:`ProcessableItem` for tests.

    Mirrors what ``PlexProcessor.list_canonical_paths`` would yield.
    ``canonical_path`` defaults to a unique path derived from ``title`` so
    dedup-by-path doesn't accidentally collapse distinct items.
    """
    from media_preview_generator.processing.types import ProcessableItem

    return ProcessableItem(
        canonical_path=canonical_path or f"/data/{title.replace(' ', '_')}_{key}.mkv",
        server_id=server_id,
        item_id_by_server={server_id: key} if key else {},
        title=title,
        library_id=library_id,
    )


def _processables(items, *, server_id="plex-1", library_id="lib-1"):
    """Bulk version of :func:`_processable` — converts ``[(key, title, _media)]``."""
    return [_processable(key, title, server_id=server_id, library_id=library_id) for key, title, *_ in items]


def _webhook_resolution_payload(items=None, unresolved=None, skipped=None, path_hints=None):
    """Build a webhook-resolution stand-in that includes ``items_with_locations``.

    ``run_processing`` reads ``items_with_locations`` (4-tuples with the Plex
    side's locations) to build ProcessableItems for dispatch, while the
    legacy ``items`` field stays the 3-tuple shape callers historically used.
    """
    items = items or []
    items_with_locations = [
        (key, [f"/data/{title.replace(' ', '_')}_{key}.mkv"], title, mt) for key, title, mt in items
    ]
    return SimpleNamespace(
        items=list(items),
        unresolved_paths=unresolved or [],
        skipped_paths=skipped or [],
        path_hints=path_hints or [],
        items_with_locations=items_with_locations,
    )


# ---------------------------------------------------------------------------
# Patches applied to every test
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate(tmp_path):
    """Prevent real Plex/FFmpeg/GPU calls during tests."""
    with (
        patch(f"{MODULE}.plex_server", return_value=MagicMock()) as _ps,
        patch(f"{MODULE}.clear_failures"),
        patch(f"{MODULE}.log_failure_summary"),
    ):
        yield _ps


# ---------------------------------------------------------------------------
# Happy path: library scan flow
# ---------------------------------------------------------------------------


class TestMultiServerGuards:
    """Tests for the multi-server full-scan path (Phase D of the multi-server
    completion).

    Originally these were "no-op skip" guards that bailed when no Plex was
    configured; Phase D wires up the per-vendor processor + ThreadPoolExecutor
    so non-Plex full-scans actually run instead. The tests now assert the
    multi-server path is invoked.
    """

    def test_no_plex_full_scan_routes_through_multi_server_scan(self, tmp_path):
        config = _make_config(tmp_path, plex_url="", plex_token="")
        with patch(f"{MODULE}._run_full_scan_multi_server") as mock_scan:
            mock_scan.return_value = {r.value: 0 for r in ProcessingResult}
            result = run_processing(config, selected_gpus=[])
        # Exactly one multi-server scan dispatched, with the *same* config
        # the caller passed in (no silent reshape). The "selected_gpus=[]"
        # is the bare empty list we provided — if a wrapper accidentally
        # mutated it into None we'd see it here.
        mock_scan.assert_called_once()
        call_args = mock_scan.call_args
        assert call_args.args[0] is config
        assert call_args.kwargs.get("selected_gpus") == []
        assert result is not None
        assert "outcome" in result
        # No skip path — the multi-server scan ran (possibly producing zero
        # output) instead of returning a skipped_reason.
        assert "skipped_reason" not in result

    def test_pinned_to_non_plex_full_scan_routes_through_multi_server_scan(self, tmp_path):
        config = _make_config(tmp_path, server_id_filter="emby-1")
        with (
            patch("media_preview_generator.web.settings_manager.get_settings_manager") as mock_sm,
            patch(f"{MODULE}._run_full_scan_multi_server") as mock_scan,
        ):
            mock_sm.return_value.get.return_value = [
                {"id": "emby-1", "type": "emby", "name": "My Emby"},
            ]
            mock_scan.return_value = {r.value: 0 for r in ProcessingResult}
            result = run_processing(config, selected_gpus=[])
        mock_scan.assert_called_once()
        # The pin must be passed through so the scan only walks that server.
        assert mock_scan.call_args.kwargs.get("server_id_filter") == "emby-1"
        assert result is not None
        assert "outcome" in result

    def test_no_plex_with_webhook_paths_dispatches_via_worker_pool(self, tmp_path):
        """No-Plex install routes webhook_paths through the worker pool
        (audit fix #1). The legacy direct-call to
        ``_dispatch_webhook_paths_multi_server`` ran synchronously on the
        orchestrator thread — no GPU, no worker UI rows. The fix makes it
        fall through to ``_run_webhook_paths_phase`` which builds
        ProcessableItems and runs them through ``WorkerPool``.
        """
        config = _make_config(
            tmp_path,
            plex_url="",
            plex_token="",
            webhook_paths=["/data/movies/Foo.mkv"],
        )
        with (
            patch(f"{MODULE}.WorkerPool") as MockPool,
            patch("media_preview_generator.web.settings_manager.get_settings_manager") as mock_sm,
        ):
            MockPool.return_value.process_items_headless.return_value = _pool_result(completed=1)
            mock_sm.return_value.get.return_value = [
                {"id": "emby-1", "type": "emby", "enabled": True, "libraries": []},
            ]
            result = run_processing(config, selected_gpus=[])

        # WorkerPool.process_items_headless ran exactly once for the
        # single webhook path — proving the worker pool is engaged.
        MockPool.return_value.process_items_headless.assert_called_once()
        items_arg = MockPool.return_value.process_items_headless.call_args.args[0]
        assert len(items_arg) == 1
        assert items_arg[0].canonical_path == "/data/movies/Foo.mkv"
        assert result is not None
        assert "outcome" in result

    def test_k4_unresolved_paths_fall_back_to_worker_pool_when_emby_present(self, tmp_path):
        """K4: when Plex is configured AND has at least one Emby/Jellyfin
        sibling, paths Plex couldn't resolve flow through the worker
        pool (audit fix #5). The legacy direct-call to
        ``_dispatch_webhook_paths_multi_server`` ran synchronously on the
        orchestrator thread — no GPU, no worker UI rows. The fix builds
        ProcessableItems for the unresolved subset and runs them through
        ``WorkerPool``.
        """
        from media_preview_generator.plex_client import WebhookResolutionResult

        config = _make_config(
            tmp_path,
            webhook_paths=["/data/a.mkv", "/data/b.mkv", "/data/c.mkv"],
        )

        # Plex resolves only "a.mkv"; b + c are unresolved → K4 fallback
        # should dispatch only those two through the worker pool.
        resolution = WebhookResolutionResult(
            items=[(MagicMock(name="MediaPart-A"), MagicMock())],
            unresolved_paths=["/data/b.mkv", "/data/c.mkv"],
            skipped_paths=[],
            path_hints={},
        )

        with (
            patch(f"{MODULE}.get_media_items_by_paths", return_value=resolution),
            patch(f"{MODULE}.plex_server"),
            patch(f"{MODULE}.WorkerPool") as MockPool,
            patch("media_preview_generator.web.settings_manager.get_settings_manager") as mock_sm,
        ):
            MockPool.return_value.process_items_headless.return_value = _pool_result(completed=1)
            mock_sm.return_value.get.return_value = [
                {"id": "plex-a", "type": "plex", "enabled": True},
                {"id": "emby-1", "type": "emby", "enabled": True},
            ]
            run_processing(config, selected_gpus=[])

        # The K4 fallback call (last one) carries only b + c — not a.
        # The MagicMock'd Plex resolution may or may not produce a
        # dispatchable item depending on what its locations look like;
        # the assertion that matters is that K4 received the right subset.
        all_calls = MockPool.return_value.process_items_headless.call_args_list
        assert len(all_calls) >= 1, "expected at least one K4 worker-pool dispatch"
        k4_items = all_calls[-1].args[0]
        k4_paths = sorted(i.canonical_path for i in k4_items)
        assert k4_paths == ["/data/b.mkv", "/data/c.mkv"]

    def test_owning_servers_breadcrumb_logged_before_resolver(self, tmp_path):
        """Before the resolver runs, an info-level breadcrumb names the
        owning server(s) for the webhook paths so an operator reading the
        log top-down sees the routing decision before per-server work
        starts. Single-Plex install: the message names Plex.
        """
        from loguru import logger

        from media_preview_generator.plex_client import WebhookResolutionResult

        config = _make_config(tmp_path, webhook_paths=["/data_16tb/Movies/x.mkv"])
        resolution = WebhookResolutionResult(
            items=[(MagicMock(), MagicMock())],
            unresolved_paths=[],
            skipped_paths=[],
            path_hints={},
        )

        captured: list[str] = []
        sink_id = logger.add(lambda msg: captured.append(str(msg)), level="INFO")
        try:
            with (
                patch(f"{MODULE}.get_media_items_by_paths", return_value=resolution),
                patch(f"{MODULE}.plex_server"),
                patch(f"{MODULE}.WorkerPool") as MockPool,
                patch("media_preview_generator.web.settings_manager.get_settings_manager") as mock_sm,
            ):
                MockPool.return_value.process_items_headless.return_value = _pool_result(completed=1)
                mock_sm.return_value.get.return_value = [
                    {
                        "id": "plex-a",
                        "type": "plex",
                        "name": "Plex",
                        "enabled": True,
                        "url": "http://x",
                        "auth": {"method": "token", "token": "t"},
                        "libraries": [
                            {
                                "id": "1",
                                "name": "Movies",
                                "remote_paths": ["/data_16tb/Movies"],
                                "enabled": True,
                            }
                        ],
                        "path_mappings": [],
                    }
                ]
                run_processing(config, selected_gpus=[])
        finally:
            logger.remove(sink_id)

        assert any("owning server" in line.lower() and "plex" in line.lower() for line in captured), captured

    def test_hint_short_circuit_skips_plex_resolution_when_hints_present(self, tmp_path):
        """Audit L5 — hint short-circuit doesn't touch Plex.

        When ``Config.webhook_item_id_hints`` is set (vendor webhook), the
        orchestrator must build ProcessableItems directly from the hints
        and dispatch without calling ``get_media_items_by_paths`` (the
        slow Plex resolution roundtrip the hint exists to skip).
        """
        config = _make_config(
            tmp_path,
            webhook_paths=["/data/movies/Foo.mkv"],
        )
        config.webhook_item_id_hints = {"/data/movies/Foo.mkv": {"plex-1": "k1"}}

        with (
            patch(f"{MODULE}.get_media_items_by_paths") as mock_resolve,
            patch(f"{MODULE}.plex_server"),
            patch(f"{MODULE}.WorkerPool") as MockPool,
            patch("media_preview_generator.web.settings_manager.get_settings_manager") as mock_sm,
        ):
            MockPool.return_value.process_items_headless.return_value = _pool_result(completed=1)
            mock_sm.return_value.get.return_value = [
                {"id": "plex-1", "type": "plex", "enabled": True, "libraries": []},
            ]
            run_processing(config, selected_gpus=[])

        # Plex resolution must NOT be called — that's the whole point.
        mock_resolve.assert_not_called()
        # Worker pool DOES dispatch the path.
        MockPool.return_value.process_items_headless.assert_called_once()
        items = MockPool.return_value.process_items_headless.call_args.args[0]
        assert len(items) == 1
        assert items[0].canonical_path == "/data/movies/Foo.mkv"
        assert items[0].item_id_by_server == {"plex-1": "k1"}

    def test_k4_does_not_cascade_when_pinned_to_plex(self, tmp_path):
        """Audit M4 — a Plex-pinned webhook must NOT fall through to Emby/Jellyfin
        K4 fallback when Plex resolution comes up empty. Pinning means
        "publish to Plex only".
        """
        from media_preview_generator.plex_client import WebhookResolutionResult

        config = _make_config(tmp_path, webhook_paths=["/data/x.mkv"])
        config.server_id_filter = "plex-a"
        resolution = WebhookResolutionResult(
            items=[],
            unresolved_paths=["/data/x.mkv"],
            skipped_paths=[],
            path_hints={},
        )

        with (
            patch(f"{MODULE}.get_media_items_by_paths", return_value=resolution),
            patch(f"{MODULE}.plex_server"),
            patch(f"{MODULE}.WorkerPool") as MockPool,
            patch("media_preview_generator.web.settings_manager.get_settings_manager") as mock_sm,
        ):
            MockPool.return_value.process_items_headless.return_value = _pool_result(completed=0)
            mock_sm.return_value.get.return_value = [
                {"id": "plex-a", "type": "plex", "enabled": True},
                {"id": "emby-1", "type": "emby", "enabled": True},
            ]
            run_processing(config, selected_gpus=[])

        # No K4 dispatch for the unresolved path — pin contract held.
        for call in MockPool.return_value.process_items_headless.call_args_list:
            for item in call.args[0]:
                assert item.canonical_path != "/data/x.mkv", (
                    "K4 fallback fired despite Plex pin — would silently publish to Emby"
                )

    def test_k4_no_fallback_when_only_plex_configured(self, tmp_path):
        """K4: don't churn — when only Plex is configured, the unresolved
        paths go to the existing retry queue, not through the worker pool
        as a fallback."""
        from media_preview_generator.plex_client import WebhookResolutionResult

        config = _make_config(tmp_path, webhook_paths=["/data/x.mkv"])
        resolution = WebhookResolutionResult(
            items=[],
            unresolved_paths=["/data/x.mkv"],
            skipped_paths=[],
            path_hints={},
        )

        with (
            patch(f"{MODULE}.get_media_items_by_paths", return_value=resolution),
            patch(f"{MODULE}.plex_server"),
            patch(f"{MODULE}.WorkerPool") as MockPool,
            patch("media_preview_generator.web.settings_manager.get_settings_manager") as mock_sm,
        ):
            MockPool.return_value.process_items_headless.return_value = _pool_result(completed=0)
            # Only Plex configured → no Emby/Jellyfin → K4 fallback should NOT fire.
            mock_sm.return_value.get.return_value = [
                {"id": "plex-a", "type": "plex", "enabled": True},
            ]
            run_processing(config, selected_gpus=[])

        # No K4 worker-pool dispatch — the resolved (empty) items branch
        # may still call process_items_headless once for the empty Plex
        # resolution result, but the K4 fallback for unresolved_paths
        # must not fire when only Plex is configured.
        all_calls = MockPool.return_value.process_items_headless.call_args_list
        for call in all_calls:
            items_arg = call.args[0]
            for item in items_arg:
                assert item.canonical_path != "/data/x.mkv", (
                    "K4 fallback fired for the unresolved path despite no Emby/Jellyfin sibling"
                )


class TestLibraryScanFlow:
    """Tests for the normal library enumeration + dispatch path."""

    def test_processes_multiple_libraries(self, tmp_path):
        """Items from multiple libraries are merged and dispatched together."""
        config = _make_config(tmp_path)
        items_a = _processables([("k1", "Movie 1", "movie"), ("k2", "Movie 2", "movie")])
        items_b = _processables([("k3", "Show 1", "episode")])

        with (
            patch(
                f"{MODULE}._enumerate_plex_full_scan_items",
                return_value=iter(items_a + items_b),
            ),
            patch(f"{MODULE}.WorkerPool") as MockPool,
        ):
            pool_inst = MockPool.return_value
            pool_inst.process_items_headless.return_value = _pool_result(completed=3)

            result = run_processing(config, selected_gpus=[])

        assert result is not None
        assert result["outcome"]["generated"] == 3
        pool_inst.process_items_headless.assert_called_once()
        dispatched_items = pool_inst.process_items_headless.call_args[0][0]
        assert len(dispatched_items) == 3

    def test_skips_empty_library(self, tmp_path):
        """When the enumerator yields nothing, no dispatch occurs."""
        config = _make_config(tmp_path)

        with (
            patch(
                f"{MODULE}._enumerate_plex_full_scan_items",
                return_value=iter([]),
            ),
            patch(f"{MODULE}.WorkerPool") as MockPool,
        ):
            result = run_processing(config, selected_gpus=[])

        assert result is not None
        MockPool.return_value.process_items_headless.assert_not_called()

    def test_no_libraries_returns_empty_outcome(self, tmp_path):
        """When get_library_sections yields nothing, result is still returned."""
        config = _make_config(tmp_path)

        with (
            patch(f"{MODULE}._enumerate_plex_full_scan_items", return_value=iter([])),
            patch(f"{MODULE}.WorkerPool"),
        ):
            result = run_processing(config, selected_gpus=[])

        assert result is not None
        assert "outcome" in result

    def test_sort_by_random_shuffles_combined_items(self, tmp_path):
        """sort_by='random' reorders the combined cross-library list before dispatch."""
        config = _make_config(tmp_path, sort_by="random")
        items_a = _processables([(f"k{i}", f"Movie {i}", "movie") for i in range(5)])
        items_b = _processables([(f"kt{i}", f"Show {i}", "episode") for i in range(5)])
        original_order = items_a + items_b

        # Deterministic stand-in for random.Random(): shuffle reverses the list
        class _RevShuffler:
            def shuffle(self, lst):
                lst.reverse()

        with (
            patch(
                f"{MODULE}._enumerate_plex_full_scan_items",
                return_value=iter(items_a + items_b),
            ),
            patch(f"{MODULE}.random.Random", return_value=_RevShuffler()),
            patch(f"{MODULE}.WorkerPool") as MockPool,
        ):
            MockPool.return_value.process_items_headless.return_value = _pool_result(completed=10)
            run_processing(config, selected_gpus=[])

        dispatched = MockPool.return_value.process_items_headless.call_args[0][0]
        assert dispatched == list(reversed(original_order))

    def test_sort_by_non_random_preserves_order(self, tmp_path):
        """Without sort_by='random', item order is preserved as yielded."""
        config = _make_config(tmp_path, sort_by="newest")
        items = _processables([("k1", "Movie A", "movie"), ("k2", "Movie B", "movie"), ("k3", "Movie C", "movie")])

        with (
            patch(
                f"{MODULE}._enumerate_plex_full_scan_items",
                return_value=iter(items),
            ),
            patch(f"{MODULE}.WorkerPool") as MockPool,
        ):
            MockPool.return_value.process_items_headless.return_value = _pool_result(completed=3)
            run_processing(config, selected_gpus=[])

        dispatched = MockPool.return_value.process_items_headless.call_args[0][0]
        assert dispatched == items

    def test_progress_callback_invoked(self, tmp_path):
        """progress_callback reports pre-dispatch stages + the dispatch tick."""
        config = _make_config(tmp_path)
        items = _processables([("k1", "M1", "movie"), ("k2", "M2", "movie")])
        progress = MagicMock()

        with (
            patch(
                f"{MODULE}._enumerate_plex_full_scan_items",
                return_value=iter(items),
            ),
            patch(f"{MODULE}.WorkerPool") as MockPool,
        ):
            MockPool.return_value.process_items_headless.return_value = _pool_result(completed=2)
            run_processing(config, selected_gpus=[], progress_callback=progress)

        messages = [call.args[2] for call in progress.call_args_list if call.args]
        assert any("Connecting to Plex" in m for m in messages)
        # Dispatch tick carries the total item count.
        dispatch_calls = [
            call for call in progress.call_args_list if call.args and call.args[1] == 2 and "Starting" in call.args[2]
        ]
        assert dispatch_calls, "expected a 'Starting <library>' progress call with total=2"


# ---------------------------------------------------------------------------
# Webhook flow
# ---------------------------------------------------------------------------


class TestWebhookFlow:
    """Tests for the webhook-targeted processing path."""

    def _webhook_resolution(self, items=None, unresolved=None, skipped=None, path_hints=None):
        return _webhook_resolution_payload(
            items=items,
            unresolved=unresolved,
            skipped=skipped,
            path_hints=path_hints,
        )

    def test_webhook_with_resolved_items(self, tmp_path):
        """Webhook paths that resolve to Plex items are dispatched."""
        config = _make_config(tmp_path, webhook_paths=["/data/movie.mkv"])
        resolved_items = [("k1", "Movie", "movie")]

        with (
            patch(
                f"{MODULE}.get_media_items_by_paths",
                return_value=self._webhook_resolution(items=resolved_items, unresolved=["/data/missing.mkv"]),
            ),
            patch(f"{MODULE}.WorkerPool") as MockPool,
        ):
            MockPool.return_value.process_items_headless.return_value = _pool_result(completed=1)
            result = run_processing(config, selected_gpus=[])

        assert result is not None
        assert "webhook_resolution" in result
        assert result["webhook_resolution"]["resolved_count"] == 1
        assert result["webhook_resolution"]["unresolved_paths"] == ["/data/missing.mkv"]
        assert result["outcome"]["generated"] == 1

    def test_webhook_no_matches_skips_dispatch(self, tmp_path):
        """When no webhook paths resolve, no dispatch occurs."""
        config = _make_config(tmp_path, webhook_paths=["/data/no_match.mkv"])

        with (
            patch(
                f"{MODULE}.get_media_items_by_paths",
                return_value=self._webhook_resolution(items=[]),
            ),
            patch(f"{MODULE}.WorkerPool") as MockPool,
        ):
            result = run_processing(config, selected_gpus=[])

        MockPool.return_value.process_items_headless.assert_not_called()
        assert result is not None
        assert result["webhook_resolution"]["resolved_count"] == 0

    def test_webhook_progress_callback(self, tmp_path):
        """Progress callback reports pre-resolution stages + dispatch tick."""
        config = _make_config(tmp_path, webhook_paths=["/data/movie.mkv"])
        items = [("k1", "Movie", "movie")]
        progress = MagicMock()

        with (
            patch(
                f"{MODULE}.get_media_items_by_paths",
                return_value=self._webhook_resolution(items=items),
            ),
            patch(f"{MODULE}.WorkerPool") as MockPool,
        ):
            MockPool.return_value.process_items_headless.return_value = _pool_result(completed=1)
            run_processing(config, selected_gpus=[], progress_callback=progress)

        messages = [call.args[2] for call in progress.call_args_list if call.args]
        assert any("Connecting to Plex" in m for m in messages)
        assert any("Looking up 1 file path" in m for m in messages)
        dispatch_calls = [
            call for call in progress.call_args_list if call.args and call.args[1] == 1 and "Starting" in call.args[2]
        ]
        assert dispatch_calls, "expected a 'Starting Webhook Targets' dispatch tick"


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------


class TestCancellation:
    """Tests for cancel_check during enumeration and before dispatch."""

    def test_cancel_during_enumeration(self, tmp_path):
        """Cancellation during library enumeration stops further scanning."""
        config = _make_config(tmp_path)
        section = _make_section("Movies")
        items = [("k1", "M1", "movie")]
        call_count = 0

        def cancel_after_first():
            nonlocal call_count
            call_count += 1
            return call_count > 1

        with (
            patch(
                f"{MODULE}._enumerate_plex_full_scan_items",
                return_value=iter([(section, items)]),
            ),
            patch(f"{MODULE}.WorkerPool") as MockPool,
        ):
            result = run_processing(config, selected_gpus=[], cancel_check=cancel_after_first)

        assert result is not None
        MockPool.return_value.process_items_headless.assert_not_called()

    def test_cancel_before_dispatch(self, tmp_path):
        """When cancel_check returns True before dispatch, nothing is dispatched."""
        config = _make_config(tmp_path)

        with (
            patch(f"{MODULE}._enumerate_plex_full_scan_items", return_value=iter([])),
            patch(f"{MODULE}.WorkerPool") as MockPool,
        ):
            result = run_processing(
                config,
                selected_gpus=[],
                cancel_check=lambda: True,
            )

        assert result is not None
        MockPool.return_value.process_items_headless.assert_not_called()


# ---------------------------------------------------------------------------
# Summary and path mapping warning
# ---------------------------------------------------------------------------


class TestSummaryAndWarnings:
    """Tests for the outcome summary and path-mapping warning."""

    def test_summary_includes_all_outcome_types(self, tmp_path):
        """All non-zero outcome types appear in the returned data."""
        config = _make_config(tmp_path)
        section = _make_section("Movies")
        items = [("k1", "M1", "movie")]

        outcome = {r.value: 0 for r in ProcessingResult}
        outcome["generated"] = 2
        outcome["skipped_bif_exists"] = 1
        outcome["failed"] = 1

        with (
            patch(
                f"{MODULE}._enumerate_plex_full_scan_items",
                return_value=iter([(section, items)]),
            ),
            patch(f"{MODULE}.WorkerPool") as MockPool,
        ):
            MockPool.return_value.process_items_headless.return_value = _pool_result(
                completed=3, failed=1, outcome=outcome
            )
            result = run_processing(config, selected_gpus=[])

        assert result["outcome"]["generated"] == 2
        assert result["outcome"]["skipped_bif_exists"] == 1
        assert result["outcome"]["failed"] == 1

    def test_path_mapping_warning_on_all_not_found(self, tmp_path):
        """Warning is logged when every item is skipped_file_not_found."""
        config = _make_config(tmp_path)
        section = _make_section("Movies")
        items = [("k1", "M1", "movie")]

        outcome = {r.value: 0 for r in ProcessingResult}
        outcome["skipped_file_not_found"] = 3

        captured = []

        with (
            patch(
                f"{MODULE}._enumerate_plex_full_scan_items",
                return_value=iter([(section, items)]),
            ),
            patch(f"{MODULE}.WorkerPool") as MockPool,
            patch(f"{MODULE}.logger") as mock_logger,
        ):
            mock_logger.warning = lambda msg, *a, **kw: captured.append(msg)
            mock_logger.info = MagicMock()
            MockPool.return_value.process_items_headless.return_value = {
                "completed": 0,
                "failed": 3,
                "cancelled": False,
                "outcome": outcome,
            }
            run_processing(config, selected_gpus=[])

        assert any("path mapping" in msg.lower() for msg in captured)

    def test_cancellation_noted_in_summary(self, tmp_path):
        """When dispatch reports cancellation, outcome is still returned."""
        config = _make_config(tmp_path)
        section = _make_section("Movies")
        items = [("k1", "M1", "movie")]

        with (
            patch(
                f"{MODULE}._enumerate_plex_full_scan_items",
                return_value=iter([(section, items)]),
            ),
            patch(f"{MODULE}.WorkerPool") as MockPool,
        ):
            MockPool.return_value.process_items_headless.return_value = _pool_result(completed=0, cancelled=True)
            result = run_processing(config, selected_gpus=[])

        assert result is not None
        assert "outcome" in result


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestErrorHandling:
    """Tests for exception paths in run_processing."""

    def test_connection_error_returns_none(self, tmp_path):
        """ConnectionError from plex_server returns None."""
        config = _make_config(tmp_path)
        with patch(f"{MODULE}.plex_server", side_effect=ConnectionError("refused")):
            result = run_processing(config, selected_gpus=[])

        assert result is None

    def test_unexpected_exception_re_raised(self, tmp_path):
        """Unexpected exceptions propagate after logging."""
        config = _make_config(tmp_path)
        with patch(f"{MODULE}.plex_server", side_effect=RuntimeError("boom")):
            with pytest.raises(RuntimeError, match="boom"):
                run_processing(config, selected_gpus=[])

    def test_keyboard_interrupt_returns_none(self, tmp_path):
        """KeyboardInterrupt is caught and returns None (implicit)."""
        config = _make_config(tmp_path)
        with patch(f"{MODULE}.plex_server", side_effect=KeyboardInterrupt):
            result = run_processing(config, selected_gpus=[])

        assert result is None


# ---------------------------------------------------------------------------
# Cleanup: worker pool shutdown and temp folder removal
# ---------------------------------------------------------------------------


class TestCleanup:
    """Tests for the finally block: pool shutdown, callback, temp cleanup."""

    def test_worker_pool_shutdown_called(self, tmp_path):
        """Worker pool is shut down in the finally block when no job_id."""
        config = _make_config(tmp_path)
        section = _make_section("Movies")
        items = [("k1", "M1", "movie")]

        with (
            patch(
                f"{MODULE}._enumerate_plex_full_scan_items",
                return_value=iter([(section, items)]),
            ),
            patch(f"{MODULE}.WorkerPool") as MockPool,
        ):
            pool_inst = MockPool.return_value
            pool_inst.process_items_headless.return_value = _pool_result(completed=1)
            run_processing(config, selected_gpus=[])

        pool_inst.shutdown.assert_called_once()

    def test_worker_pool_callback_receives_pool_and_none(self, tmp_path):
        """worker_pool_callback is called with the pool on create and None on cleanup."""
        config = _make_config(tmp_path)
        section = _make_section("Movies")
        items = [("k1", "M1", "movie")]
        wp_callback = MagicMock()

        with (
            patch(
                f"{MODULE}._enumerate_plex_full_scan_items",
                return_value=iter([(section, items)]),
            ),
            patch(f"{MODULE}.WorkerPool") as MockPool,
        ):
            pool_inst = MockPool.return_value
            pool_inst.process_items_headless.return_value = _pool_result(completed=1)
            run_processing(
                config,
                selected_gpus=[],
                worker_pool_callback=wp_callback,
            )

        assert wp_callback.call_count == 2
        wp_callback.assert_any_call(pool_inst)
        wp_callback.assert_any_call(None)

    def test_temp_folder_cleaned_up(self, tmp_path):
        """working_tmp_folder is removed in the finally block."""
        work_dir = tmp_path / "work"
        work_dir.mkdir()
        (work_dir / "temp_file.jpg").touch()
        config = _make_config(tmp_path, working_tmp_folder=str(work_dir))

        with (
            patch(f"{MODULE}._enumerate_plex_full_scan_items", return_value=iter([])),
            patch(f"{MODULE}.WorkerPool"),
        ):
            run_processing(config, selected_gpus=[])

        assert not work_dir.exists()

    def test_cleanup_on_error(self, tmp_path):
        """Temp folder is cleaned even when plex_server raises."""
        work_dir = tmp_path / "work"
        work_dir.mkdir()
        config = _make_config(tmp_path, working_tmp_folder=str(work_dir))

        with patch(f"{MODULE}.plex_server", side_effect=ConnectionError("fail")):
            run_processing(config, selected_gpus=[])

        assert not work_dir.exists()

    def test_no_shutdown_when_job_id_set(self, tmp_path):
        """With job_id, worker pool shutdown is skipped (dispatcher owns it)."""
        config = _make_config(tmp_path)

        with (
            patch(f"{MODULE}._enumerate_plex_full_scan_items", return_value=iter([])),
            patch(f"{MODULE}.WorkerPool") as MockPool,
            patch(f"{MODULE}.get_dispatcher", create=True),
        ):
            run_processing(config, selected_gpus=[], job_id="job-123")

        MockPool.return_value.shutdown.assert_not_called()


# ---------------------------------------------------------------------------
# Job dispatcher branch (job_id path)
# ---------------------------------------------------------------------------


class TestJobDispatcherPath:
    """Tests for the job_id/dispatcher-based dispatch path."""

    def test_dispatcher_existing_pool(self, tmp_path):
        """When dispatcher already exists, reuses its worker_pool."""
        config = _make_config(tmp_path)
        section = _make_section("Movies")
        items = [("k1", "M1", "movie")]
        on_start = MagicMock()

        mock_tracker = MagicMock()
        mock_tracker.get_result.return_value = _pool_result(completed=1)

        mock_existing_pool = MagicMock()
        mock_dispatcher = MagicMock()
        mock_dispatcher.worker_pool = mock_existing_pool
        mock_dispatcher.submit_items.return_value = mock_tracker

        with (
            patch(
                f"{MODULE}._enumerate_plex_full_scan_items",
                return_value=iter([(section, items)]),
            ),
            patch(
                "media_preview_generator.jobs.orchestrator.get_dispatcher",
                side_effect=[mock_dispatcher, mock_dispatcher],
                create=True,
            ) as mock_get_disp,
            patch("media_preview_generator.web.jobs.PRIORITY_NORMAL", 2, create=True),
        ):
            # Patch the import inside _dispatch_items
            with (
                patch.dict(
                    "sys.modules",
                    {
                        "media_preview_generator.jobs.dispatcher": MagicMock(get_dispatcher=mock_get_disp),
                        "media_preview_generator.web.jobs": MagicMock(PRIORITY_NORMAL=2),
                    },
                ),
            ):
                result = run_processing(
                    config,
                    selected_gpus=[],
                    job_id="job-1",
                    on_dispatch_start=on_start,
                )

        assert result is not None
        on_start.assert_called_once()

    def test_dispatcher_creates_new_pool(self, tmp_path):
        """When no existing dispatcher, a new worker_pool is created."""
        config = _make_config(tmp_path)
        section = _make_section("Movies")
        items = [("k1", "M1", "movie")]

        mock_tracker = MagicMock()
        mock_tracker.get_result.return_value = _pool_result(completed=1)

        mock_dispatcher = MagicMock()
        mock_dispatcher.submit_items.return_value = mock_tracker

        call_count = 0

        def fake_get_dispatcher(pool=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return None  # First call: no existing dispatcher
            return mock_dispatcher

        with (
            patch(
                f"{MODULE}._enumerate_plex_full_scan_items",
                return_value=iter([(section, items)]),
            ),
            patch(f"{MODULE}.WorkerPool"),
        ):
            with patch.dict(
                "sys.modules",
                {
                    "media_preview_generator.jobs.dispatcher": MagicMock(get_dispatcher=fake_get_dispatcher),
                    "media_preview_generator.web.jobs": MagicMock(PRIORITY_NORMAL=2),
                },
            ):
                result = run_processing(
                    config,
                    selected_gpus=[],
                    job_id="job-2",
                    priority=1,
                )

        assert result is not None


# ---------------------------------------------------------------------------
# Additional summary branches (excluded, invalid_hash, no_media_parts)
# ---------------------------------------------------------------------------


class TestSummaryBranches:
    """Cover uncovered summary-building branches for excluded, invalid_hash, no_parts."""

    def test_excluded_and_invalid_hash_in_outcome(self, tmp_path):
        """excluded and invalid_hash outcomes appear in result."""
        config = _make_config(tmp_path)
        section = _make_section("Movies")
        items = [("k1", "M1", "movie")]

        outcome = {r.value: 0 for r in ProcessingResult}
        outcome["skipped_excluded"] = 4
        outcome["skipped_invalid_hash"] = 2
        outcome["no_media_parts"] = 1

        with (
            patch(
                f"{MODULE}._enumerate_plex_full_scan_items",
                return_value=iter([(section, items)]),
            ),
            patch(f"{MODULE}.WorkerPool") as MockPool,
        ):
            MockPool.return_value.process_items_headless.return_value = _pool_result(
                completed=0, failed=0, outcome=outcome
            )
            result = run_processing(config, selected_gpus=[])

        assert result["outcome"]["skipped_excluded"] == 4
        assert result["outcome"]["skipped_invalid_hash"] == 2
        assert result["outcome"]["no_media_parts"] == 1


# ---------------------------------------------------------------------------
# Cleanup edge cases
# ---------------------------------------------------------------------------


class TestCleanupEdgeCases:
    """Cover error handling within the finally block."""

    def test_shutdown_error_is_logged(self, tmp_path):
        """Error during worker_pool.shutdown() is caught and logged."""
        config = _make_config(tmp_path)
        section = _make_section("Movies")
        items = [("k1", "M1", "movie")]

        with (
            patch(
                f"{MODULE}._enumerate_plex_full_scan_items",
                return_value=iter([(section, items)]),
            ),
            patch(f"{MODULE}.WorkerPool") as MockPool,
        ):
            pool_inst = MockPool.return_value
            pool_inst.process_items_headless.return_value = _pool_result(completed=1)
            pool_inst.shutdown.side_effect = RuntimeError("shutdown failed")

            # Should not raise despite shutdown error
            result = run_processing(config, selected_gpus=[])

        assert result is not None

    def test_temp_cleanup_error_is_logged(self, tmp_path):
        """Error removing temp folder is caught and logged."""
        work_dir = tmp_path / "work"
        work_dir.mkdir()
        config = _make_config(tmp_path, working_tmp_folder=str(work_dir))

        with (
            patch(f"{MODULE}._enumerate_plex_full_scan_items", return_value=iter([])),
            patch(f"{MODULE}.WorkerPool"),
            patch(f"{MODULE}.shutil.rmtree", side_effect=OSError("perm denied")),
        ):
            # Should not raise despite cleanup error
            result = run_processing(config, selected_gpus=[])

        assert result is not None

    def test_cancel_during_enumeration_with_items_queued(self, tmp_path):
        """Cancel fires after first lib is queued; second lib is skipped."""
        config = _make_config(tmp_path)
        section_a = _make_section("Movies")
        section_b = _make_section("TV Shows")
        items_a = [("k1", "M1", "movie")]
        items_b = [("k2", "S1", "episode")]

        calls = []

        def cancel_on_second_check():
            calls.append(1)
            # Cancel on the 2nd check (after first library yielded)
            return len(calls) >= 2

        with (
            patch(
                f"{MODULE}._enumerate_plex_full_scan_items",
                return_value=iter([(section_a, items_a), (section_b, items_b)]),
            ),
            patch(f"{MODULE}.WorkerPool") as MockPool,
        ):
            result = run_processing(
                config,
                selected_gpus=[],
                cancel_check=cancel_on_second_check,
            )

        assert result is not None
        MockPool.return_value.process_items_headless.assert_not_called()
