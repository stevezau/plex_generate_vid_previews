"""Tests for ``_run_full_scan_multi_server`` (Phase D).

Verifies the multi-server full-library scan dispatcher:
* enumerates items via the per-vendor :class:`VendorProcessor`
* fans them out through ``process_canonical_path`` in parallel
* respects ``server_id_filter`` (single server) and the no-filter case
  (every enabled server)
* aggregates per-publisher outcomes back into the legacy
  ProcessingResult counts shape
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from media_preview_generator.jobs.orchestrator import _run_full_scan_multi_server
from media_preview_generator.processing import ProcessingResult
from media_preview_generator.processing.types import ProcessableItem
from media_preview_generator.servers.base import ServerConfig, ServerType

MODULE = "media_preview_generator.jobs.orchestrator"


def _config():
    return SimpleNamespace(
        gpu_threads=0,
        cpu_threads=1,
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


class TestMultiServerFullScan:
    def test_no_servers_configured_returns_zero_counts(self, tmp_path):
        with (
            patch("media_preview_generator.web.settings_manager.get_settings_manager") as mock_sm,
            patch(f"{MODULE}.ProcessingResult", ProcessingResult),
        ):
            mock_sm.return_value.get.return_value = []
            counts = _run_full_scan_multi_server(_config(), selected_gpus=[])
        assert all(v == 0 for v in counts.values())

    def test_pinned_to_server_only_walks_that_server(self, tmp_path):
        cfg_a = _server_config("srv-a", ServerType.JELLYFIN)
        cfg_b = _server_config("srv-b", ServerType.EMBY)

        registry_mock = MagicMock()
        registry_mock.configs.return_value = [cfg_a, cfg_b]

        proc_a = MagicMock()
        proc_a.list_canonical_paths.return_value = iter(
            [
                ProcessableItem(canonical_path="/data/a.mkv", server_id="srv-a"),
            ]
        )
        proc_b = MagicMock()
        proc_b.list_canonical_paths.return_value = iter([])

        def _get_proc(stype):
            return {ServerType.JELLYFIN: proc_a, ServerType.EMBY: proc_b}[stype]

        with (
            patch("media_preview_generator.web.settings_manager.get_settings_manager") as mock_sm,
            patch("media_preview_generator.servers.ServerRegistry") as mock_registry,
            patch("media_preview_generator.processing.get_processor_for", side_effect=_get_proc),
            patch("media_preview_generator.processing.multi_server.process_canonical_path") as mock_process,
        ):
            mock_sm.return_value.get.return_value = [
                {"id": "srv-a", "type": "jellyfin", "enabled": True},
                {"id": "srv-b", "type": "emby", "enabled": True},
            ]
            mock_registry.from_settings.return_value = registry_mock
            mock_process.return_value = MagicMock(publishers=[])

            _run_full_scan_multi_server(
                _config(),
                selected_gpus=[],
                server_id_filter="srv-a",
            )

        # Only the pinned server's processor was used; the other one's
        # list_canonical_paths must never have been called.
        proc_a.list_canonical_paths.assert_called_once()
        proc_b.list_canonical_paths.assert_not_called()
        # process_canonical_path fired exactly once for the single item.
        mock_process.assert_called_once()
        kwargs = mock_process.call_args.kwargs
        assert kwargs["canonical_path"] == "/data/a.mkv"

    def test_no_pin_walks_every_enabled_server(self, tmp_path):
        cfg_a = _server_config("srv-a", ServerType.JELLYFIN)
        cfg_b = _server_config("srv-b", ServerType.EMBY)
        cfg_c = _server_config("srv-c", ServerType.JELLYFIN)
        cfg_c = ServerConfig(
            id=cfg_c.id, type=cfg_c.type, name=cfg_c.name, enabled=False, url=cfg_c.url, auth=cfg_c.auth
        )

        registry_mock = MagicMock()
        registry_mock.configs.return_value = [cfg_a, cfg_b, cfg_c]

        items_a = [ProcessableItem(canonical_path="/a/1.mkv", server_id="srv-a")]
        items_b = [ProcessableItem(canonical_path="/b/2.mkv", server_id="srv-b")]
        proc_a = MagicMock()
        proc_a.list_canonical_paths.return_value = iter(items_a)
        proc_b = MagicMock()
        proc_b.list_canonical_paths.return_value = iter(items_b)

        def _get_proc(stype):
            return {ServerType.JELLYFIN: proc_a, ServerType.EMBY: proc_b}[stype]

        with (
            patch("media_preview_generator.web.settings_manager.get_settings_manager") as mock_sm,
            patch("media_preview_generator.servers.ServerRegistry") as mock_registry,
            patch("media_preview_generator.processing.get_processor_for", side_effect=_get_proc),
            patch("media_preview_generator.processing.multi_server.process_canonical_path") as mock_process,
        ):
            mock_sm.return_value.get.return_value = [
                {"id": "srv-a", "type": "jellyfin", "enabled": True},
                {"id": "srv-b", "type": "emby", "enabled": True},
                {"id": "srv-c", "type": "jellyfin", "enabled": False},
            ]
            mock_registry.from_settings.return_value = registry_mock
            mock_process.return_value = MagicMock(publishers=[])

            _run_full_scan_multi_server(_config(), selected_gpus=[])

        # Both enabled servers walked; the disabled one was skipped at
        # the candidate-collection step (so its processor was never
        # invoked at all).
        proc_a.list_canonical_paths.assert_called_once()
        proc_b.list_canonical_paths.assert_called_once()
        # process_canonical_path fired for both items.
        assert mock_process.call_count == 2

    def test_aggregates_per_publisher_outcomes_into_processing_result_counts(self, tmp_path):
        cfg = _server_config("srv-a", ServerType.JELLYFIN)
        registry_mock = MagicMock()
        registry_mock.configs.return_value = [cfg]

        proc = MagicMock()
        proc.list_canonical_paths.return_value = iter(
            [
                ProcessableItem(canonical_path="/x.mkv", server_id="srv-a"),
                ProcessableItem(canonical_path="/y.mkv", server_id="srv-a"),
            ]
        )

        # Two items, each producing different publish outcomes.
        result_x = MagicMock(publishers=[MagicMock(status=MagicMock(value="published"))])
        result_y = MagicMock(publishers=[MagicMock(status=MagicMock(value="failed"))])

        with (
            patch("media_preview_generator.web.settings_manager.get_settings_manager") as mock_sm,
            patch("media_preview_generator.servers.ServerRegistry") as mock_registry,
            patch("media_preview_generator.processing.get_processor_for", return_value=proc),
            patch(
                "media_preview_generator.processing.multi_server.process_canonical_path",
                side_effect=[result_x, result_y],
            ),
        ):
            mock_sm.return_value.get.return_value = [
                {"id": "srv-a", "type": "jellyfin", "enabled": True},
            ]
            mock_registry.from_settings.return_value = registry_mock

            counts = _run_full_scan_multi_server(_config(), selected_gpus=[])

        assert counts.get("published", 0) == 1
        assert counts.get("failed", 0) == 1

    def test_zero_items_logs_warning_not_info(self, tmp_path):
        """When the scan walks the server but enumerates zero items, the user
        must see a WARN-level message — silent INFO leaves them puzzled why
        a "successful" job did no work.

        Regression: the previous behaviour was a single INFO line that didn't
        survive the user's typical log filter (which sits at WARN by default).
        Real causes (bad library_ids, scoped auth, vendor mid-index) all
        ended up looking like a green job that did nothing — exactly the
        symptom the user reported.

        Project uses loguru — sink-attach to capture, not pytest's caplog.
        """
        from loguru import logger as _logger

        cfg = _server_config("srv-a", ServerType.JELLYFIN)
        registry_mock = MagicMock()
        registry_mock.configs.return_value = [cfg]

        proc = MagicMock()
        proc.list_canonical_paths.return_value = iter([])  # 0 items

        captured: list[tuple[str, str]] = []

        sink_id = _logger.add(lambda m: captured.append((m.record["level"].name, m.record["message"])), level="WARNING")
        try:
            with (
                patch("media_preview_generator.web.settings_manager.get_settings_manager") as mock_sm,
                patch("media_preview_generator.servers.ServerRegistry") as mock_registry,
                patch("media_preview_generator.processing.get_processor_for", return_value=proc),
            ):
                mock_sm.return_value.get.return_value = [
                    {"id": "srv-a", "type": "jellyfin", "enabled": True},
                ]
                mock_registry.from_settings.return_value = registry_mock

                counts = _run_full_scan_multi_server(
                    _config(),
                    selected_gpus=[],
                    library_ids=["bogus-library-id"],
                )
        finally:
            _logger.remove(sink_id)

        # Counts still all zero (no items to process)
        assert all(v == 0 for v in counts.values())
        # The warning must mention the library_ids the caller passed so the
        # user can match it to what they ticked in the UI.
        warns = [(lvl, msg) for lvl, msg in captured if lvl in ("WARNING", "ERROR", "CRITICAL")]
        assert any("bogus-library-id" in msg for _, msg in warns), (
            f"Expected WARN mentioning 'bogus-library-id'; got: {warns}"
        )


class TestPinnedFilterForwardedToProcessCanonicalPath:
    """Regression for the cross-server contamination bug where a Plex-pinned
    full scan still attempted to publish to Jellyfin/Emby siblings.

    Live reproducer: job d9918149 was configured with
    ``server_id: "plex-default"`` (the user picked "scan Movies on plex"
    from the UI). Enumeration correctly walked only the Plex library, but
    every per-item dispatch into ``process_canonical_path`` fanned out to
    every owning server because the Plex-originator branch in
    ``_dispatch_processable_items._process_one`` unconditionally dropped
    the caller's ``server_id_filter``. Result: every Plex success row was
    paired with a JellyTest "failed" row (no item_id available), and
    FFmpeg was re-extracted for the doomed Jellyfin attempt despite Plex
    already having the BIF.

    The previous test suite had:
      * a "pinned to Jellyfin/Emby" test — passes either way because the
        non-Plex branch happened to forward the filter.
      * no test asserting the filter is *forwarded* to
        ``process_canonical_path`` — it only checked which server's
        enumerator was called.

    Both gaps are fixed here. Every test below asserts the kwargs reaching
    ``process_canonical_path`` so a future regression in the per-item
    pin precedence is caught at the boundary.
    """

    def test_plex_pinned_forwards_filter_to_process_canonical_path(self, tmp_path):
        """The exact d9918149 reproducer: Plex-pinned full scan must
        publish ONLY to Plex — never fan out to Jellyfin/Emby siblings."""
        cfg_plex = _server_config("plex-default", ServerType.PLEX)

        registry_mock = MagicMock()
        registry_mock.configs.return_value = [cfg_plex]

        proc = MagicMock()
        proc.list_canonical_paths.return_value = iter(
            [
                ProcessableItem(canonical_path="/data/movies/x.mkv", server_id="plex-default"),
                ProcessableItem(canonical_path="/data/movies/y.mkv", server_id="plex-default"),
            ]
        )

        with (
            patch("media_preview_generator.web.settings_manager.get_settings_manager") as mock_sm,
            patch("media_preview_generator.servers.ServerRegistry") as mock_registry,
            patch("media_preview_generator.processing.get_processor_for", return_value=proc),
            patch("media_preview_generator.processing.multi_server.process_canonical_path") as mock_process,
        ):
            mock_sm.return_value.get.return_value = [
                {"id": "plex-default", "type": "plex", "enabled": True},
                {"id": "jelly-test", "type": "jellyfin", "enabled": True},
            ]
            mock_registry.from_settings.return_value = registry_mock
            mock_process.return_value = MagicMock(publishers=[])

            _run_full_scan_multi_server(
                _config(),
                selected_gpus=[],
                server_id_filter="plex-default",
            )

        assert mock_process.call_count == 2
        # Both items must have the pin forwarded — without this fix the
        # Plex-originator branch dropped the filter to None and the
        # downstream resolver fanned out to Jellyfin too.
        for call in mock_process.call_args_list:
            assert call.kwargs["server_id_filter"] == "plex-default", (
                "Plex-pinned dispatch leaked into other servers — "
                f"got server_id_filter={call.kwargs.get('server_id_filter')!r}"
            )

    def test_no_pin_plex_originator_fans_out(self, tmp_path):
        """When no pin is set and the originator is Plex, the dispatcher
        forwards ``server_id_filter=None`` so every owning server gets a
        publisher (the multi-vendor fan-out path)."""
        cfg_plex = _server_config("plex-default", ServerType.PLEX)

        registry_mock = MagicMock()
        registry_mock.configs.return_value = [cfg_plex]

        proc = MagicMock()
        proc.list_canonical_paths.return_value = iter(
            [ProcessableItem(canonical_path="/data/x.mkv", server_id="plex-default")]
        )

        with (
            patch("media_preview_generator.web.settings_manager.get_settings_manager") as mock_sm,
            patch("media_preview_generator.servers.ServerRegistry") as mock_registry,
            patch("media_preview_generator.processing.get_processor_for", return_value=proc),
            patch("media_preview_generator.processing.multi_server.process_canonical_path") as mock_process,
        ):
            mock_sm.return_value.get.return_value = [
                {"id": "plex-default", "type": "plex", "enabled": True},
            ]
            mock_registry.from_settings.return_value = registry_mock
            mock_process.return_value = MagicMock(publishers=[])

            _run_full_scan_multi_server(_config(), selected_gpus=[])

        mock_process.assert_called_once()
        assert mock_process.call_args.kwargs["server_id_filter"] is None

    def test_no_pin_non_plex_originator_scopes_to_originator(self, tmp_path):
        """No pin + Jellyfin originator → dispatcher pins to that
        originator so single-server installs don't try to publish to
        unrelated servers that happen to own the path."""
        cfg = _server_config("jelly-only", ServerType.JELLYFIN)

        registry_mock = MagicMock()
        registry_mock.configs.return_value = [cfg]

        proc = MagicMock()
        proc.list_canonical_paths.return_value = iter(
            [ProcessableItem(canonical_path="/data/x.mkv", server_id="jelly-only")]
        )

        with (
            patch("media_preview_generator.web.settings_manager.get_settings_manager") as mock_sm,
            patch("media_preview_generator.servers.ServerRegistry") as mock_registry,
            patch("media_preview_generator.processing.get_processor_for", return_value=proc),
            patch("media_preview_generator.processing.multi_server.process_canonical_path") as mock_process,
        ):
            mock_sm.return_value.get.return_value = [
                {"id": "jelly-only", "type": "jellyfin", "enabled": True},
            ]
            mock_registry.from_settings.return_value = registry_mock
            mock_process.return_value = MagicMock(publishers=[])

            _run_full_scan_multi_server(_config(), selected_gpus=[])

        mock_process.assert_called_once()
        assert mock_process.call_args.kwargs["server_id_filter"] == "jelly-only"


class TestEnumerationStatusBanner:
    """D28 — surface a "Querying {server} library…" status BEFORE each
    server's enumeration walk and a "Dispatching N item(s)…" status the
    moment enumeration finishes. Without these the progress bar sits at
    0/0 with no message for the 10–60s the Emby/Jellyfin library walk
    takes; users assume the job is stuck and cancel."""

    def test_querying_banner_emitted_per_server_before_enumeration(self, tmp_path):
        cfg_a = _server_config("srv-a", ServerType.JELLYFIN)
        cfg_b = _server_config("srv-b", ServerType.EMBY)
        registry_mock = MagicMock()
        registry_mock.configs.return_value = [cfg_a, cfg_b]

        proc_a = MagicMock()
        proc_a.list_canonical_paths.return_value = iter([])
        proc_b = MagicMock()
        proc_b.list_canonical_paths.return_value = iter([])

        def _get_proc(stype):
            return {ServerType.JELLYFIN: proc_a, ServerType.EMBY: proc_b}[stype]

        progress_calls: list[tuple[int, int, str]] = []

        with (
            patch("media_preview_generator.web.settings_manager.get_settings_manager") as mock_sm,
            patch("media_preview_generator.servers.ServerRegistry") as mock_registry,
            patch("media_preview_generator.processing.get_processor_for", side_effect=_get_proc),
        ):
            mock_sm.return_value.get.return_value = [
                {"id": "srv-a", "type": "jellyfin", "enabled": True},
                {"id": "srv-b", "type": "emby", "enabled": True},
            ]
            mock_registry.from_settings.return_value = registry_mock

            _run_full_scan_multi_server(
                _config(),
                selected_gpus=[],
                progress_callback=lambda p, t, m: progress_calls.append((p, t, m)),
            )

        querying_messages = [m for _, _, m in progress_calls if m.startswith("Querying ")]
        assert any("Test jellyfin" in m for m in querying_messages), (
            f"missing per-server Querying banner; got {querying_messages!r}"
        )
        assert any("Test emby" in m for m in querying_messages), (
            f"missing per-server Querying banner; got {querying_messages!r}"
        )

    def test_dispatch_banner_emitted_with_total_after_enumeration(self, tmp_path):
        from media_preview_generator.jobs.orchestrator import _dispatch_processable_items

        cfg = _server_config("srv-a", ServerType.JELLYFIN)
        items = [
            (
                cfg,
                ProcessableItem(
                    canonical_path=f"/data/m{i}.mkv",
                    server_id="srv-a",
                    item_id_by_server={"srv-a": str(i)},
                ),
            )
            for i in range(4)
        ]

        progress_calls: list[tuple[int, int, str]] = []

        with patch(
            "media_preview_generator.processing.multi_server.process_canonical_path",
            return_value=SimpleNamespace(publishers=[], canonical_path="/x"),
        ):
            _dispatch_processable_items(
                items=items,
                config=_config(),
                registry=MagicMock(),
                selected_gpus=[],
                progress_callback=lambda p, t, m: progress_calls.append((p, t, m)),
                label="full scan",
            )

        # First emit must be the up-front Dispatching banner with (0, total).
        assert progress_calls, "no progress messages emitted"
        first = progress_calls[0]
        assert first[0] == 0
        assert first[1] == 4
        assert "Dispatching" in first[2]


class TestFileResultEmissionFromMultiServerDispatch:
    """D34 — the multi-server dispatch path bypasses
    Worker → _persist → _notify_file_result, so without an explicit
    notify call the Files panel stays empty for the entire run despite
    continuous activity in app.log. Live reproducer: job d9918149's
    Files panel showed 0 rows mid-run; the user thought the job was
    stuck even though items were completing.

    These tests assert that ``_dispatch_processable_items`` invokes the
    file-result callback for every item — both happy path and the
    exception-swallowed branch — so the panel populates as work
    progresses.
    """

    def _items(self, n: int):
        cfg = _server_config("srv-a", ServerType.JELLYFIN)
        return [(cfg, ProcessableItem(canonical_path=f"/data/m{i}.mkv", server_id="srv-a")) for i in range(n)]

    def test_emits_file_result_per_completed_item(self, tmp_path):
        from media_preview_generator.jobs.orchestrator import _dispatch_processable_items
        from media_preview_generator.processing.generator import set_file_result_callback
        from media_preview_generator.processing.multi_server import MultiServerStatus

        recorded: list[tuple] = []

        def _cb(file_path, outcome_str, reason, worker, servers=None):
            recorded.append((file_path, outcome_str, reason, worker, servers or []))

        set_file_result_callback(_cb)
        try:
            with patch(
                "media_preview_generator.processing.multi_server.process_canonical_path",
                side_effect=[
                    SimpleNamespace(
                        publishers=[],
                        canonical_path="/data/m0.mkv",
                        status=MultiServerStatus.PUBLISHED,
                        message="",
                    ),
                    SimpleNamespace(
                        publishers=[],
                        canonical_path="/data/m1.mkv",
                        status=MultiServerStatus.SKIPPED,
                        message="cached",
                    ),
                ],
            ):
                _dispatch_processable_items(
                    items=self._items(2),
                    config=_config(),
                    registry=MagicMock(),
                    selected_gpus=[],
                    label="full scan",
                )
        finally:
            set_file_result_callback(None)

        assert len(recorded) == 2, f"Expected 2 file rows, got {len(recorded)}: {recorded!r}"
        assert recorded[0][0] == "/data/m0.mkv"
        assert recorded[0][1] == "generated"
        assert recorded[1][0] == "/data/m1.mkv"
        assert recorded[1][1] == "skipped_bif_exists"
        # Worker label is present and non-empty so the Files panel column
        # never shows a row with no attribution at all.
        for r in recorded:
            assert r[3], f"empty worker label on row {r!r}"

    def test_emits_failed_file_result_when_process_canonical_path_raises(self, tmp_path):
        """The exception-swallowed branch (process_canonical_path raised)
        used to count toward the failed counter but never produce a
        Files-panel row, so the user could see "5 failed" on the chip
        but had no way to know which 5 files."""
        from media_preview_generator.jobs.orchestrator import _dispatch_processable_items
        from media_preview_generator.processing.generator import set_file_result_callback

        recorded: list[tuple] = []

        def _cb(file_path, outcome_str, reason, worker, servers=None):
            recorded.append((file_path, outcome_str, reason, worker, servers or []))

        set_file_result_callback(_cb)
        try:
            with patch(
                "media_preview_generator.processing.multi_server.process_canonical_path",
                side_effect=RuntimeError("FFmpeg crashed"),
            ):
                _dispatch_processable_items(
                    items=self._items(1),
                    config=_config(),
                    registry=MagicMock(),
                    selected_gpus=[],
                    label="full scan",
                )
        finally:
            set_file_result_callback(None)

        assert len(recorded) == 1
        path, outcome, _reason, _worker, _servers = recorded[0]
        assert path == "/data/m0.mkv"
        assert outcome == "failed", (
            "exception-swallowed branch must still produce a per-file row so the user "
            "can see which file blew up — without this the Files panel was just a counter."
        )


class TestWorkerCallbackEmissionFromMultiServerDispatch:
    """D34 — multi-server dispatch path used to bypass the WorkerPool's
    worker_callback so the Workers panel stayed empty for the entire run.
    Synthetic worker rows derived from the executor's threads now flow
    through the same callback the legacy path uses.
    """

    def test_worker_callback_fires_with_active_workers_during_run(self, tmp_path):
        from media_preview_generator.jobs.orchestrator import _dispatch_processable_items
        from media_preview_generator.processing.multi_server import MultiServerStatus

        cfg = _server_config("srv-a", ServerType.JELLYFIN)
        items = [
            (
                cfg,
                ProcessableItem(
                    canonical_path=f"/data/m{i}.mkv",
                    server_id="srv-a",
                    title=f"Movie {i}",
                ),
            )
            for i in range(3)
        ]

        snapshots: list[list[dict]] = []

        def _wcb(workers_list):
            snapshots.append(list(workers_list))

        with patch(
            "media_preview_generator.processing.multi_server.process_canonical_path",
            return_value=SimpleNamespace(
                publishers=[],
                canonical_path="/data/m.mkv",
                status=MultiServerStatus.PUBLISHED,
                message="",
            ),
        ):
            _dispatch_processable_items(
                items=items,
                config=_config(),
                registry=MagicMock(),
                selected_gpus=[],
                label="full scan",
                worker_callback=_wcb,
            )

        # At least one snapshot must show a "processing" row — without
        # the synthetic worker entries the panel would never have any
        # rows at all during a multi-server scan.
        assert any(any(w.get("status") == "processing" for w in snap) for snap in snapshots), (
            f"no 'processing' worker entry in any snapshot: {snapshots!r}"
        )
        # Final state: every active row drained on completion.
        assert snapshots[-1] == [], f"workers leaked into the final snapshot: {snapshots[-1]!r}"

    def test_worker_callback_carries_current_title(self, tmp_path):
        from media_preview_generator.jobs.orchestrator import _dispatch_processable_items
        from media_preview_generator.processing.multi_server import MultiServerStatus

        cfg = _server_config("srv-a", ServerType.JELLYFIN)
        items = [
            (
                cfg,
                ProcessableItem(
                    canonical_path="/data/named.mkv",
                    server_id="srv-a",
                    title="The Named Movie",
                ),
            )
        ]

        captured_titles: list[str] = []

        def _wcb(workers_list):
            for w in workers_list:
                if w.get("current_title"):
                    captured_titles.append(w["current_title"])

        with patch(
            "media_preview_generator.processing.multi_server.process_canonical_path",
            return_value=SimpleNamespace(
                publishers=[],
                canonical_path="/data/named.mkv",
                status=MultiServerStatus.PUBLISHED,
                message="",
            ),
        ):
            _dispatch_processable_items(
                items=items,
                config=_config(),
                registry=MagicMock(),
                selected_gpus=[],
                label="full scan",
                worker_callback=_wcb,
            )

        assert "The Named Movie" in captured_titles, f"item title missing from worker snapshots: {captured_titles!r}"


class TestPerJobLogCapture:
    """D27 — every executor-pool worker thread MUST register itself
    under the active job's id so the per-job log handler captures the
    per-file Dispatch / FFmpeg / Publisher lines.

    Without this, the Emby/Jellyfin full-scan path (which uses a raw
    ThreadPoolExecutor instead of the JobDispatcher → Worker chain that
    Plex uses) leaves its worker threads anonymous. The per-job log
    handler's filter is_job_thread_for(record.thread.id, job_id) drops
    every record from those threads. Result: per-job log file shows
    only the lifecycle markers ("Started job", "dispatching N items")
    while app.log shows continuous activity.
    """

    def test_dispatch_processable_items_registers_executor_threads(self, tmp_path):
        from media_preview_generator.jobs.orchestrator import _dispatch_processable_items
        from media_preview_generator.jobs.worker import (
            _job_thread_to_job_id,
            register_job_thread,
            unregister_job_thread,
        )

        cfg = _server_config("srv-a", ServerType.JELLYFIN)
        items = [
            (
                cfg,
                ProcessableItem(
                    canonical_path=f"/data/m{i}.mkv",
                    server_id="srv-a",
                    item_id_by_server={"srv-a": str(i)},
                    title=f"M{i}",
                    library_id="lib1",
                ),
            )
            for i in range(3)
        ]

        # Reset registry. The dispatch-pool's threads should populate it
        # with the test job_id; without the D27 fix they'd remain absent
        # (or registered to "" if a previous task on a recycled thread had
        # left a different mapping).
        _job_thread_to_job_id.clear()
        register_job_thread("MAIN")  # the test thread itself

        captured_thread_to_job: dict = {}

        def fake_pcp(canonical_path, **_):
            import threading as _t

            from media_preview_generator.jobs.worker import _job_thread_to_job_id as _r

            captured_thread_to_job[_t.get_ident()] = _r.get(_t.get_ident())
            return SimpleNamespace(publishers=[], canonical_path=canonical_path)

        try:
            with patch(
                "media_preview_generator.processing.multi_server.process_canonical_path",
                side_effect=fake_pcp,
            ):
                _dispatch_processable_items(
                    items=items,
                    config=_config(),
                    registry=MagicMock(),
                    selected_gpus=[],
                    job_id="job-D27-test",
                    label="full scan",
                )
        finally:
            unregister_job_thread()

        # Every executor thread that ran an item must have been registered
        # under the test job's id. Empty-string registration (the bug) or
        # missing registration both fail this.
        assert captured_thread_to_job, "no items processed?"
        for tid, mapped_job in captured_thread_to_job.items():
            assert mapped_job == "job-D27-test", (
                f"thread {tid} was registered to {mapped_job!r}, expected 'job-D27-test' — "
                "per-job log filter would drop this thread's logs"
            )


class TestMultiPlexDeduping:
    """Phase P4: when the same canonical_path appears on more than one
    enabled server (e.g. two Plex servers sharing storage, or Plex+Jellyfin
    over the same media), the dispatcher dedups by canonical_path and
    merges every server's vendor item-id into a single ProcessableItem
    so each adapter still finds its target."""

    def test_two_plex_servers_sharing_media_dispatches_once_per_path(self, tmp_path):
        cfg_plex_a = _server_config("plex-a", ServerType.PLEX)
        cfg_plex_b = _server_config("plex-b", ServerType.PLEX)
        registry_mock = MagicMock()
        registry_mock.configs.return_value = [cfg_plex_a, cfg_plex_b]

        # Same canonical_path enumerated by both Plex servers, each with
        # its own vendor item-id.
        proc_a = MagicMock()
        proc_a.list_canonical_paths.return_value = iter(
            [
                ProcessableItem(
                    canonical_path="/data/Movies/Foo.mkv",
                    server_id="plex-a",
                    item_id_by_server={"plex-a": "rk-A"},
                ),
            ]
        )
        proc_b = MagicMock()
        proc_b.list_canonical_paths.return_value = iter(
            [
                ProcessableItem(
                    canonical_path="/data/Movies/Foo.mkv",
                    server_id="plex-b",
                    item_id_by_server={"plex-b": "rk-B"},
                ),
            ]
        )

        with (
            patch("media_preview_generator.web.settings_manager.get_settings_manager") as mock_sm,
            patch("media_preview_generator.servers.ServerRegistry") as mock_registry,
            patch("media_preview_generator.processing.get_processor_for", return_value=proc_a),
            patch("media_preview_generator.processing.multi_server.process_canonical_path") as mock_process,
        ):
            mock_sm.return_value.get.return_value = [
                {"id": "plex-a", "type": "plex", "enabled": True},
                {"id": "plex-b", "type": "plex", "enabled": True},
            ]
            mock_registry.from_settings.return_value = registry_mock

            # Both processors return the same path — proc_a is returned
            # by get_processor_for(plex), so we hand-feed proc_b's items
            # by chaining the iterator.
            chained_iter = iter(
                [
                    *list(proc_a.list_canonical_paths.return_value),
                    *list(proc_b.list_canonical_paths.return_value),
                ]
            )
            proc_a.list_canonical_paths.return_value = chained_iter
            mock_process.return_value = MagicMock(publishers=[])

            _run_full_scan_multi_server(_config(), selected_gpus=[])

        # process_canonical_path fired EXACTLY once despite the path
        # appearing twice in enumeration — Phase P4 dedup.
        assert mock_process.call_count == 1
        # And the merged hint includes BOTH servers' item-ids so each
        # PlexBundleAdapter call (plex-a's and plex-b's) can resolve
        # its own bundle hash.
        kwargs = mock_process.call_args.kwargs
        merged = kwargs.get("item_id_by_server") or {}
        assert merged.get("plex-a") == "rk-A", merged
        assert merged.get("plex-b") == "rk-B", merged

    def test_plex_plus_jellyfin_sharing_media_dispatches_once(self, tmp_path):
        """Cross-vendor variant of the above — Plex + Jellyfin both own
        the same canonical_path. Single dispatch, item-id hints carry
        both vendor identifiers."""
        cfg_plex = _server_config("plex-1", ServerType.PLEX)
        cfg_jf = _server_config("jf-1", ServerType.JELLYFIN)
        registry_mock = MagicMock()
        registry_mock.configs.return_value = [cfg_plex, cfg_jf]

        proc_plex = MagicMock()
        proc_plex.list_canonical_paths.return_value = iter(
            [
                ProcessableItem(
                    canonical_path="/data/Show/S01E01.mkv",
                    server_id="plex-1",
                    item_id_by_server={"plex-1": "rk-1"},
                ),
            ]
        )
        proc_jf = MagicMock()
        proc_jf.list_canonical_paths.return_value = iter(
            [
                ProcessableItem(
                    canonical_path="/data/Show/S01E01.mkv",
                    server_id="jf-1",
                    item_id_by_server={"jf-1": "jf-id"},
                ),
            ]
        )

        def _get_proc(stype):
            return {ServerType.PLEX: proc_plex, ServerType.JELLYFIN: proc_jf}[stype]

        with (
            patch("media_preview_generator.web.settings_manager.get_settings_manager") as mock_sm,
            patch("media_preview_generator.servers.ServerRegistry") as mock_registry,
            patch("media_preview_generator.processing.get_processor_for", side_effect=_get_proc),
            patch("media_preview_generator.processing.multi_server.process_canonical_path") as mock_process,
        ):
            mock_sm.return_value.get.return_value = [
                {"id": "plex-1", "type": "plex", "enabled": True},
                {"id": "jf-1", "type": "jellyfin", "enabled": True},
            ]
            mock_registry.from_settings.return_value = registry_mock
            mock_process.return_value = MagicMock(publishers=[])

            _run_full_scan_multi_server(_config(), selected_gpus=[])

        assert mock_process.call_count == 1
        merged = mock_process.call_args.kwargs.get("item_id_by_server") or {}
        assert merged.get("plex-1") == "rk-1", merged
        assert merged.get("jf-1") == "jf-id", merged


class TestRealEndToEndMultiServerFullScan:
    """Boundary-only end-to-end test: real ``_run_full_scan_multi_server`` →
    real ``_dispatch_processable_items`` → real ``process_canonical_path`` →
    real ``_publish_one``, with mocks at only:

      * the per-vendor processor's enumeration (replaces network-backed
        library walk),
      * ``generate_images`` (FFmpeg subprocess),
      * the adapter's ``compute_output_paths`` / ``publish`` so we don't
        write to a real Plex bundle directory but DO capture every call
        the real publisher path makes,
      * ``os.path.isfile`` / ``os.listdir`` filesystem boundary.

    The point: D31 shipped because every test stubbed the function with
    the bug. This test is the canary — if any future change reshapes the
    canonical-path or item-id flow, this assertion fires.
    """

    def test_full_scan_drives_real_publish_with_well_formed_inputs(self, tmp_path):
        cfg = _server_config("srv-real", ServerType.JELLYFIN)
        registry_real = MagicMock()
        registry_real.configs.return_value = [cfg]

        # The real _resolve_publishers (in multi_server.py) calls
        # registry.find_owning_servers(canonical_path) — wire that up to
        # return the same server + adapter the dispatcher must publish to.
        server_obj = MagicMock(id="srv-real", name="Test jellyfin")
        adapter = MagicMock()
        adapter.name = "test_adapter"
        adapter.compute_output_paths.return_value = [tmp_path / "out" / "trickplay.bif"]
        adapter.publish.return_value = None

        # Capture every call the real publisher path makes — these are
        # the "URLs hit" the audit asks for. The adapter is the boundary
        # where we'd see a doubled prefix or a leaked URL-form id.
        captured_compute_calls: list = []
        captured_publish_calls: list = []

        def _capture_compute(bundle, srv, item_id):
            captured_compute_calls.append({"canonical_path": bundle.canonical_path, "item_id": item_id, "server": srv})
            return [tmp_path / "out" / "trickplay.bif"]

        def _capture_publish(bundle, output_paths, item_id=None):
            captured_publish_calls.append(
                {
                    "canonical_path": bundle.canonical_path,
                    "frame_count": bundle.frame_count,
                    "item_id": item_id,
                    "output_paths": output_paths,
                }
            )

        adapter.compute_output_paths.side_effect = _capture_compute
        adapter.publish.side_effect = _capture_publish

        # The vendor processor enumerates one item; we drive it with a
        # bare ratingKey-style id (the production canonical form).
        proc = MagicMock()
        proc.list_canonical_paths.return_value = iter(
            [
                ProcessableItem(
                    canonical_path="/data/movies/Real (2024)/Real (2024).mkv",
                    server_id="srv-real",
                    item_id_by_server={"srv-real": "12345"},
                    title="Real (2024)",
                    library_id="lib-real",
                )
            ]
        )

        cfg_obj = SimpleNamespace(
            gpu_threads=0,
            cpu_threads=1,
            working_tmp_folder=str(tmp_path / "work"),
            tmp_folder=str(tmp_path / "frames"),
            plex_url="",
            plex_token="",
            webhook_paths=None,
            server_id_filter=None,
            plex_bif_frame_interval=5,
            thumbnail_interval=5,
            server_display_name="srv-real",
        )

        with (
            patch("media_preview_generator.web.settings_manager.get_settings_manager") as mock_sm,
            patch("media_preview_generator.servers.ServerRegistry") as mock_registry,
            patch("media_preview_generator.processing.get_processor_for", return_value=proc),
            # Only the *boundary* mocks — the real process_canonical_path
            # body runs from here down.
            patch(
                "media_preview_generator.processing.multi_server._resolve_publishers",
                return_value=[(server_obj, adapter, "12345")],
            ),
            patch(
                "media_preview_generator.processing.multi_server._resolve_item_id_for",
                return_value="12345",
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
                return_value=(True, 8, "h264", 320, 30.0, 320),
            ),
            patch(
                "media_preview_generator.processing.multi_server.os.listdir",
                return_value=[f"{i:05d}.jpg" for i in range(1, 9)],
            ),
            patch(
                "media_preview_generator.processing.multi_server.write_meta",
            ),
        ):
            mock_sm.return_value.get.return_value = [{"id": "srv-real", "type": "jellyfin", "enabled": True}]
            mock_registry.from_settings.return_value = registry_real

            counts = _run_full_scan_multi_server(cfg_obj, selected_gpus=[])

        # The real pipeline ran end-to-end and recorded one publish.
        assert counts.get("published", 0) == 1, counts
        # compute_output_paths is called twice in the real flow: once by
        # the pre-FFmpeg "are outputs fresh?" probe, once inside
        # _publish_one for the publish path. Both must see the same
        # canonical path and the same bare item id — that's the D31 contract.
        assert len(captured_compute_calls) == 2
        assert len(captured_publish_calls) == 1

        for call in captured_compute_calls:
            assert call["canonical_path"] == "/data/movies/Real (2024)/Real (2024).mkv"
            assert call["item_id"] == "12345"
            assert not str(call["item_id"]).startswith("/library/metadata/"), (
                f"D31 regression: URL-form item id leaked to compute_output_paths: {call['item_id']!r}"
            )

        publish_call = captured_publish_calls[0]
        assert publish_call["canonical_path"] == "/data/movies/Real (2024)/Real (2024).mkv"
        assert publish_call["item_id"] == "12345"
        assert not str(publish_call["item_id"]).startswith("/library/metadata/"), (
            f"D31 regression: URL-form item id leaked to publish: {publish_call['item_id']!r}"
        )
        # Frame count from generate_images flowed through to the bundle.
        assert publish_call["frame_count"] == 8
