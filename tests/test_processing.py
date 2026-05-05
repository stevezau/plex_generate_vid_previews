"""Tests for the core processing workflow in processing.py.

Covers run_processing() with mocked Plex, WorkerPool, and config
to exercise the library scan flow, webhook flow, cancellation,
error handling, callbacks, and cleanup paths.
"""

from contextlib import contextmanager
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


@contextmanager
def loguru_lines():
    """Capture loguru log lines as a list. Used by ``test_..._is_logged`` tests.

    The codebase uses ``loguru`` (not stdlib logging), so pytest's ``caplog``
    doesn't see anything. Add a temporary sink, yield the list, remove it
    on exit. Plain (non-underscore-prefixed) name so pytest doesn't try to
    auto-discover it as a fixture.
    """
    from loguru import logger as _loguru_logger

    captured: list[str] = []
    sink_id = _loguru_logger.add(lambda msg: captured.append(str(msg)), level="DEBUG")
    try:
        yield captured
    finally:
        _loguru_logger.remove(sink_id)


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
        """No-Plex install routes webhook_paths through the worker pool.

        Confirms the unified webhook phase builds ProcessableItems and
        runs them through ``WorkerPool`` for any path an enabled
        non-Plex server claims (Emby in this case).
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
                {
                    "id": "emby-1",
                    "type": "emby",
                    "enabled": True,
                    "libraries": [
                        {
                            "id": "1",
                            "name": "Movies",
                            "remote_paths": ["/data/movies"],
                            "enabled": True,
                        }
                    ],
                },
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

    def test_webhook_dispatches_to_all_owning_servers(self, tmp_path):
        """Unified peer-equal dispatch: every webhook path runs through
        a single ``WorkerPool`` call. ``process_canonical_path`` then
        fans out to every owning server (Plex, Emby, Jellyfin) in
        parallel — the orchestrator no longer pre-resolves through Plex
        and falls back to a "K4" stage for the rest.
        """
        config = _make_config(
            tmp_path,
            webhook_paths=["/data/a.mkv", "/data/b.mkv", "/data/c.mkv"],
        )

        with (
            patch(f"{MODULE}.plex_server"),
            patch(f"{MODULE}.WorkerPool") as MockPool,
            patch("media_preview_generator.web.settings_manager.get_settings_manager") as mock_sm,
        ):
            MockPool.return_value.process_items_headless.return_value = _pool_result(completed=3)
            # Both servers own /data/* — unified dispatch builds one
            # ProcessableItem per webhook path and lets process_canonical_path
            # publish to whichever servers own each path.
            mock_sm.return_value.get.return_value = [
                {
                    "id": "plex-a",
                    "type": "plex",
                    "enabled": True,
                    "libraries": [{"id": "1", "name": "Movies", "remote_paths": ["/data"], "enabled": True}],
                },
                {
                    "id": "emby-1",
                    "type": "emby",
                    "enabled": True,
                    "libraries": [{"id": "11", "name": "Movies", "remote_paths": ["/data"], "enabled": True}],
                },
            ]
            run_processing(config, selected_gpus=[])

        # Exactly one dispatch_items call carrying all three webhook
        # paths — there's no K4 fallback split.
        all_calls = MockPool.return_value.process_items_headless.call_args_list
        webhook_calls = [
            c
            for c in all_calls
            if c.args[0]
            and any(getattr(it, "canonical_path", None) and it.canonical_path.startswith("/data/") for it in c.args[0])
        ]
        assert len(webhook_calls) == 1, f"Expected exactly one unified webhook dispatch; got {len(webhook_calls)}"
        items = webhook_calls[0].args[0]
        assert sorted(i.canonical_path for i in items) == ["/data/a.mkv", "/data/b.mkv", "/data/c.mkv"]

    def test_owning_servers_breadcrumb_logged_before_resolver(self, tmp_path):
        """Before dispatch runs, an info-level breadcrumb names the
        owning server(s) for the webhook paths so an operator reading the
        log top-down sees the routing decision before per-server work
        starts. Single-Plex install: the message names Plex.
        """
        from loguru import logger

        config = _make_config(tmp_path, webhook_paths=["/data_16tb/Movies/x.mkv"])

        captured: list[str] = []
        sink_id = logger.add(lambda msg: captured.append(str(msg)), level="INFO")
        try:
            with (
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

    def test_vendor_hints_propagate_to_processable_items(self, tmp_path):
        """Vendor-webhook hints (Plex/Emby/Jellyfin native plugin
        payloads with a known item id) flow through into
        ``ProcessableItem.item_id_by_server`` so the relevant adapter
        skips a slow reverse-lookup. The unified dispatch path treats
        hints as a per-server pre-population, not a Plex-only
        short-circuit.
        """
        config = _make_config(
            tmp_path,
            webhook_paths=["/data/movies/Foo.mkv"],
        )
        config.webhook_item_id_hints = {"/data/movies/Foo.mkv": {"plex-1": "k1"}}

        with (
            patch(f"{MODULE}.plex_server"),
            patch(f"{MODULE}.WorkerPool") as MockPool,
            patch("media_preview_generator.web.settings_manager.get_settings_manager") as mock_sm,
        ):
            MockPool.return_value.process_items_headless.return_value = _pool_result(completed=1)
            mock_sm.return_value.get.return_value = [
                {
                    "id": "plex-1",
                    "type": "plex",
                    "enabled": True,
                    "libraries": [{"id": "1", "name": "Movies", "remote_paths": ["/data/movies"], "enabled": True}],
                },
            ]
            run_processing(config, selected_gpus=[])

        MockPool.return_value.process_items_headless.assert_called_once()
        items = MockPool.return_value.process_items_headless.call_args.args[0]
        assert len(items) == 1
        assert items[0].canonical_path == "/data/movies/Foo.mkv"
        assert items[0].item_id_by_server == {"plex-1": "k1"}

    def test_webhook_pinned_to_plex_does_not_fan_out(self, tmp_path):
        """Audit M4 — a Plex-pinned webhook publishes to Plex only.
        Pinning means "publish to this server only", and the dispatcher
        carries ``server_id_filter`` through to ``process_canonical_path``
        so Emby/Jellyfin owners are intentionally excluded.
        """
        config = _make_config(tmp_path, webhook_paths=["/data/x.mkv"])
        config.server_id_filter = "plex-a"

        with (
            patch(f"{MODULE}.plex_server"),
            patch(f"{MODULE}.WorkerPool") as MockPool,
            patch("media_preview_generator.web.settings_manager.get_settings_manager") as mock_sm,
        ):
            MockPool.return_value.process_items_headless.return_value = _pool_result(completed=1)
            mock_sm.return_value.get.return_value = [
                {
                    "id": "plex-a",
                    "type": "plex",
                    "enabled": True,
                    "libraries": [{"id": "1", "name": "Movies", "remote_paths": ["/data"], "enabled": True}],
                },
                {
                    "id": "emby-1",
                    "type": "emby",
                    "enabled": True,
                    "libraries": [{"id": "11", "name": "Movies", "remote_paths": ["/data"], "enabled": True}],
                },
            ]
            run_processing(config, selected_gpus=[])

        MockPool.return_value.process_items_headless.assert_called_once()
        items = MockPool.return_value.process_items_headless.call_args.args[0]
        assert len(items) == 1
        # ProcessableItem.server_id is empty (no vendor hint) but the
        # Config.server_id_filter pin propagates separately through to
        # process_canonical_path (verified via a downstream contract test
        # in test_jobs.py / test_dispatcher_*).

    def test_webhook_path_no_owners_fast_skips(self, tmp_path):
        """No enabled server claims the path → orchestrator fast-skips
        with no worker pickup. Without this gate the path would still hit
        a worker thread, log "Worker N picked up", then bail with
        NO_OWNERS milliseconds later (the original abe52ab7 user report).
        """
        config = _make_config(tmp_path, webhook_paths=["/data/Sports/Match.mkv"])

        with (
            patch(f"{MODULE}.plex_server"),
            patch(f"{MODULE}.WorkerPool") as MockPool,
            patch("media_preview_generator.web.settings_manager.get_settings_manager") as mock_sm,
        ):
            MockPool.return_value.process_items_headless.return_value = _pool_result(completed=0)
            # Plex configured but with /data/Movies — no library claims
            # /data/Sports/* so the path has no owners.
            mock_sm.return_value.get.return_value = [
                {
                    "id": "plex-a",
                    "type": "plex",
                    "enabled": True,
                    "libraries": [{"id": "1", "name": "Movies", "remote_paths": ["/data/Movies"], "enabled": True}],
                },
            ]
            result = run_processing(config, selected_gpus=[])

        # No worker dispatch for the unowned path.
        for call in MockPool.return_value.process_items_headless.call_args_list:
            for item in call.args[0]:
                assert item.canonical_path != "/data/Sports/Match.mkv", (
                    "Unowned path was dispatched to a worker — should fast-skip with no pickup."
                )
        # Path is reported as unresolved so the file_results UI carries it.
        resolution = result.get("webhook_resolution") or {}
        assert "/data/Sports/Match.mkv" in (resolution.get("unresolved_paths") or [])


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
            patch(f"{MODULE}.WorkerPool") as MockPool,
        ):
            result = run_processing(config, selected_gpus=[])

        assert result is not None
        assert "outcome" in result
        # Audit fix — original test only checked that ``result`` had an
        # "outcome" key. With no items the dispatcher must NEVER call
        # ``process_items_headless`` (no work to dispatch) and every
        # outcome counter must be zero. Without these pins, a regression
        # that dispatched empty lists (wasting a worker round-trip) or
        # leaked outcome counts from a previous run would slip through.
        MockPool.return_value.process_items_headless.assert_not_called()
        outcome = result["outcome"]
        assert all(v == 0 for v in outcome.values()), (
            f"With no libraries, every outcome counter must be 0; got {outcome!r}"
        )

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
    """Tests for the unified webhook-dispatch path."""

    def _webhook_settings(self, *, server_type="plex", server_id=None, lib_root="/data"):
        """Build a settings_manager.get('media_servers') stub that owns
        paths under ``lib_root`` so the unified phase has someone to
        dispatch to.
        """
        return [
            {
                "id": server_id or f"{server_type}-1",
                "type": server_type,
                "enabled": True,
                "libraries": [{"id": "1", "name": "Movies", "remote_paths": [lib_root], "enabled": True}],
            }
        ]

    def test_webhook_with_owned_paths_dispatches(self, tmp_path):
        """Webhook paths owned by an enabled server are dispatched."""
        config = _make_config(tmp_path, webhook_paths=["/data/movie.mkv"])

        with (
            patch(f"{MODULE}.plex_server"),
            patch(f"{MODULE}.WorkerPool") as MockPool,
            patch("media_preview_generator.web.settings_manager.get_settings_manager") as mock_sm,
        ):
            MockPool.return_value.process_items_headless.return_value = _pool_result(completed=1)
            mock_sm.return_value.get.return_value = self._webhook_settings()
            result = run_processing(config, selected_gpus=[])

        assert result is not None
        assert "webhook_resolution" in result
        assert result["webhook_resolution"]["resolved_count"] == 1
        assert result["webhook_resolution"]["unresolved_paths"] == []
        assert result["outcome"]["generated"] == 1

    def test_webhook_no_owners_skips_dispatch(self, tmp_path):
        """When no enabled server owns the webhook path, no dispatch
        occurs and the path lands in unresolved_paths.
        """
        config = _make_config(tmp_path, webhook_paths=["/data/no_match.mkv"])

        with (
            patch(f"{MODULE}.plex_server"),
            patch(f"{MODULE}.WorkerPool") as MockPool,
            patch("media_preview_generator.web.settings_manager.get_settings_manager") as mock_sm,
        ):
            mock_sm.return_value.get.return_value = self._webhook_settings(lib_root="/somewhere/else")
            result = run_processing(config, selected_gpus=[])

        MockPool.return_value.process_items_headless.assert_not_called()
        assert result is not None
        assert result["webhook_resolution"]["resolved_count"] == 0
        assert result["webhook_resolution"]["unresolved_paths"] == ["/data/no_match.mkv"]

    def test_webhook_progress_callback(self, tmp_path):
        """Progress callback reports the unified resolution + dispatch tick."""
        config = _make_config(tmp_path, webhook_paths=["/data/movie.mkv"])
        progress = MagicMock()

        with (
            patch(f"{MODULE}.plex_server"),
            patch(f"{MODULE}.WorkerPool") as MockPool,
            patch("media_preview_generator.web.settings_manager.get_settings_manager") as mock_sm,
        ):
            MockPool.return_value.process_items_headless.return_value = _pool_result(completed=1)
            mock_sm.return_value.get.return_value = self._webhook_settings()
            run_processing(config, selected_gpus=[], progress_callback=progress)

        messages = [call.args[2] for call in progress.call_args_list if call.args]
        assert any("Resolving 1 webhook path" in m for m in messages), messages
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
        """When dispatch reports cancellation, the summary log line names it.

        Audit fix — the test name claimed to verify the summary noted the
        cancellation, but it only asserted ``"outcome" in result`` which
        passes for any return shape. A regression that swallowed the
        cancellation flag (so jobs reported "Processing complete" instead
        of "Processing stopped by cancellation") would have been
        invisible. Production wiring at orchestrator.py:1813:

            if totals["cancelled"]:
                logger.info("Processing stopped by cancellation: {}", summary)
            else:
                logger.info("Processing complete: {}", summary)

        Capture the loguru sink and pin the cancellation phrase.
        """
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
            with loguru_lines() as logs:
                result = run_processing(config, selected_gpus=[])

        assert result is not None
        assert "outcome" in result
        # The summary log MUST name the cancellation; the "complete"
        # branch must NOT have fired.
        assert any("stopped by cancellation" in line.lower() for line in logs), (
            f"Cancellation was reported by the pool but the summary log line did not "
            f"include 'stopped by cancellation'. Captured logs: {logs!r}"
        )
        assert not any("processing complete:" in line.lower() for line in logs), (
            f"When cancellation is reported, the 'Processing complete:' summary must NOT fire — found it in: {logs!r}"
        )


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
            patch(f"{MODULE}.WorkerPool") as MockPool,
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
        # Audit fix — original test only asserted ``result is not None``.
        # When a dispatcher already exists with a worker_pool, the
        # production code reuses it (orchestrator.py:1700-1702) — it must
        # NOT spin up a fresh WorkerPool. A regression that always
        # constructs a new pool would silently double the worker count
        # and waste GPU init. Pin: WorkerPool was never instantiated.
        MockPool.assert_not_called()
        # And the existing pool was actually handed back to get_dispatcher
        # so the dispatcher knows which pool to schedule on.
        mock_dispatcher.submit_items.assert_called_once()

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
        pools_passed: list = []

        def fake_get_dispatcher(pool=None):
            nonlocal call_count
            call_count += 1
            pools_passed.append(pool)
            if call_count == 1:
                return None  # First call: no existing dispatcher
            return mock_dispatcher

        with (
            patch(
                f"{MODULE}._enumerate_plex_full_scan_items",
                return_value=iter([(section, items)]),
            ),
            patch(f"{MODULE}.WorkerPool") as MockPool,
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
        # Audit fix — original test only asserted ``result is not None``.
        # When the first ``get_dispatcher()`` returns None, the
        # orchestrator must construct a new WorkerPool (orchestrator.py:1718)
        # and pass it to ``get_dispatcher(worker_pool)`` on the second
        # call. Without these pins, a regression that quietly returned
        # success without ever creating a pool (or one that called
        # WorkerPool() for both dispatcher branches) would slip through.
        MockPool.assert_called_once()
        new_pool = MockPool.return_value
        # Second get_dispatcher() call must receive the freshly-built pool.
        assert call_count >= 2, f"get_dispatcher must be called twice; got {call_count}"
        # The pool handed to get_dispatcher() on the second call must be
        # the freshly-built WorkerPool instance — proves "creates new pool"
        # rather than passing None or a stray reference.
        assert pools_passed[1] is new_pool, (
            f"On the second get_dispatcher() call, the freshly-constructed WorkerPool must be "
            f"passed in; got {pools_passed[1]!r} vs expected {new_pool!r}. "
            f"Without this pin, a regression that calls get_dispatcher() with no pool would "
            f"leave the dispatcher unbound to the new pool."
        )
        # submit_items ran on the dispatcher returned from the second call.
        mock_dispatcher.submit_items.assert_called_once()


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

    def test_shutdown_error_is_logged(self, tmp_path, caplog):
        """Error during worker_pool.shutdown() is caught AND surfaced in the log.

        Originally this test asserted only ``result is not None`` — proving
        the function didn't crash but NOT that the error was actually
        logged (the test name lied). A regression that silently swallowed
        the exception with no log line would have passed. Audit fix —
        capture the log and assert the failure mode is recorded.
        """
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

            with loguru_lines() as logs:
                result = run_processing(config, selected_gpus=[])

        assert result is not None
        assert any("shutdown failed" in line.lower() or "shutdown" in line.lower() for line in logs), (
            f"shutdown error was swallowed silently — no log line mentions 'shutdown'. logs={logs!r}"
        )

    def test_temp_cleanup_error_is_logged(self, tmp_path):
        """Error removing temp folder is caught AND surfaced in the log.

        Same fix as ``test_shutdown_error_is_logged`` — original asserted
        only "didn't crash"; now also asserts the cleanup-failure log
        line was emitted so an operator can debug a stuck temp folder.
        """
        work_dir = tmp_path / "work"
        work_dir.mkdir()
        config = _make_config(tmp_path, working_tmp_folder=str(work_dir))

        with (
            patch(f"{MODULE}._enumerate_plex_full_scan_items", return_value=iter([])),
            patch(f"{MODULE}.WorkerPool"),
            patch(f"{MODULE}.shutil.rmtree", side_effect=OSError("perm denied")),
        ):
            with loguru_lines() as logs:
                result = run_processing(config, selected_gpus=[])

        assert result is not None
        assert any(
            "perm denied" in line.lower() or "cleanup" in line.lower() or "rmtree" in line.lower() for line in logs
        ), f"temp cleanup error was swallowed silently. logs={logs!r}"

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
