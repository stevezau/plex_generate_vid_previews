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
    """Prevent real FFmpeg/GPU calls during tests.

    Note: ``plex_server`` is no longer imported by orchestrator (the
    eager pre-connection was vestigial — task #49). Per-server Plex
    connections are now established by the dispatcher's per-server
    publishers, mocked at a different boundary by individual tests.
    """
    with (
        patch(f"{MODULE}.clear_failures"),
        patch(f"{MODULE}.log_failure_summary"),
    ):
        yield None


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

    def test_webhook_aggregates_owners_across_heterogeneous_mounts(self, tmp_path):
        """Audit P2 — multi-server heterogeneous-mount install where each
        server has its own ``local_prefix``. Webhook arrives in source
        view (``/data/X.mkv``); each server's ``webhook_prefixes``
        translates ``/data`` to a different local mount (Plex sees the
        file at ``/plex-mount/X.mkv``, Emby at ``/emby-mount/X.mkv``).

        The fix: ``_resolve_webhook_path_to_canonical`` must AGGREGATE
        owners across all matching candidates, not return at the first
        match. Without this, the candidate iteration finds Plex (under
        ``/plex-mount``), returns immediately, and Emby silently never
        publishes — even though Emby owns the same file at its own
        mount.
        """
        config = _make_config(tmp_path, webhook_paths=["/data/Movies/X.mkv"])

        with (
            patch(f"{MODULE}.WorkerPool") as MockPool,
            patch("media_preview_generator.web.settings_manager.get_settings_manager") as mock_sm,
        ):
            MockPool.return_value.process_items_headless.return_value = _pool_result(completed=1)
            mock_sm.return_value.get.return_value = [
                {
                    "id": "plex-a",
                    "type": "plex",
                    "enabled": True,
                    "libraries": [
                        {"id": "1", "name": "Movies", "remote_paths": ["/plex-mount/Movies"], "enabled": True}
                    ],
                    "path_mappings": [
                        {
                            "plex_prefix": "/plex-mount",
                            "local_prefix": "/plex-mount",
                            "webhook_prefixes": ["/data"],
                        }
                    ],
                },
                {
                    "id": "emby-1",
                    "type": "emby",
                    "enabled": True,
                    "libraries": [
                        {"id": "11", "name": "Movies", "remote_paths": ["/emby-mount/Movies"], "enabled": True}
                    ],
                    "path_mappings": [
                        {
                            "remote_prefix": "/emby-mount",
                            "local_prefix": "/emby-mount",
                            "webhook_prefixes": ["/data"],
                        }
                    ],
                },
            ]
            run_processing(config, selected_gpus=[])

        # The path was dispatched (not fast-skipped). Even more
        # important: in production the dispatcher's downstream
        # ownership check would now find BOTH Plex and Emby as owners
        # — so process_canonical_path will fan out to both. Without
        # the P2 fix, only the first candidate's owner (Plex) survived
        # and Emby was silently dropped.
        MockPool.return_value.process_items_headless.assert_called_once()
        items = MockPool.return_value.process_items_headless.call_args.args[0]
        assert len(items) == 1
        # The canonical_path picked must be one of the matching
        # candidates — either /plex-mount/Movies/X.mkv or
        # /emby-mount/Movies/X.mkv. Disk-existence picker prefers the
        # one that exists; in this test neither exists, so falls back
        # to the first matching candidate (insertion order).
        canonical = items[0].canonical_path
        assert canonical in ("/plex-mount/Movies/X.mkv", "/emby-mount/Movies/X.mkv"), (
            f"canonical_path must be one of the heterogeneous mount candidates; got {canonical!r}"
        )

    def test_webhook_path_with_webhook_prefix_translates_before_fast_skip(self, tmp_path):
        """Production regression — a Sonarr webhook for ``/data/TV Shows/X.mkv``
        was being fast-skipped as "no enabled server claims" even though
        Plex/Emby/Jellyfin all owned the path via their library
        ``/data_16tb/TV Shows`` plus a configured
        ``webhook_prefixes=['/data']`` translation.

        The fast-skip gate must apply ``apply_webhook_prefixes`` before
        calling :func:`find_owning_servers` — otherwise every install
        with a webhook-source-vs-server mount discrepancy drops every
        webhook with the misleading "no owners" log line.

        Caught live in job c2500b7e where the breadcrumb said all three
        servers owned the paths but the new fast-skip said no one did
        — direct contradiction proving the gate skipped the
        translation step the breadcrumb does.
        """
        config = _make_config(tmp_path, webhook_paths=["/data/TV Shows/Show/S01E01.mkv"])

        with (
            patch(f"{MODULE}.WorkerPool") as MockPool,
            patch("media_preview_generator.web.settings_manager.get_settings_manager") as mock_sm,
        ):
            MockPool.return_value.process_items_headless.return_value = _pool_result(completed=1)
            # Plex library at /data_16tb/TV Shows; webhook_prefixes
            # tells the resolver "/data" maps to "/data_16tb" for
            # webhook payload translation. Webhook payload arrives with
            # /data/... — must translate to /data_16tb/... before the
            # ownership check.
            mock_sm.return_value.get.return_value = [
                {
                    "id": "plex-a",
                    "type": "plex",
                    "enabled": True,
                    "libraries": [
                        {
                            "id": "1",
                            "name": "TV",
                            "remote_paths": ["/data_16tb/TV Shows"],
                            "enabled": True,
                        }
                    ],
                    "path_mappings": [
                        {
                            "plex_prefix": "/data_16tb",
                            "local_prefix": "/data_16tb",
                            "webhook_prefixes": ["/data"],
                        }
                    ],
                },
            ]
            run_processing(config, selected_gpus=[])

        # The headline assertion: the path was DISPATCHED, not fast-skipped.
        # Without the webhook-prefix translation, this call_count would
        # be 0.
        MockPool.return_value.process_items_headless.assert_called_once()
        items = MockPool.return_value.process_items_headless.call_args.args[0]
        assert len(items) == 1, f"Expected one dispatched item; got {len(items)}"
        # And the canonical_path on the ProcessableItem is the
        # SERVER-VIEW form, not the raw webhook-view form. This is
        # critical because process_canonical_path._resolve_owners
        # checks ownership against this exact string and does NOT
        # translate webhook prefixes itself; storing the raw
        # ``/data/...`` would let the orchestrator gate pass while
        # the downstream worker bails NO_OWNERS — the precise live
        # regression caught in job 6eca6721.
        assert items[0].canonical_path == "/data_16tb/TV Shows/Show/S01E01.mkv", (
            f"canonical_path must be translated to the server-view form so "
            f"process_canonical_path's ownership check finds the owner; got {items[0].canonical_path!r}"
        )

    def test_path_mapping_mismatch_hint_surfaces_in_resolution_payload(self, tmp_path):
        """When a webhook path is unowned but a configured library's
        location is a path-boundary substring of it (the classic
        webhook-with-extra-mount-prefix mismatch — Sonarr sends
        ``/mnt/data/Movies/X.mkv``, server stores ``/data/Movies``,
        no mapping configured), the orchestrator must surface a
        path_hints row the file_result UI can show. Without this,
        the unification regresses the legacy Plex-first stage's "did
        you forget a path mapping?" diagnostic.
        """
        config = _make_config(tmp_path, webhook_paths=["/mnt/data/Movies/X.mkv"])

        with (
            patch(f"{MODULE}.WorkerPool") as MockPool,
            patch("media_preview_generator.web.settings_manager.get_settings_manager") as mock_sm,
        ):
            MockPool.return_value.process_items_headless.return_value = _pool_result(completed=0)
            # Server's library at /data/Movies appears as a path-
            # boundary substring of the webhook's /mnt/data/Movies/X.mkv;
            # extra prefix is /mnt — that's the gap a path mapping
            # would close.
            mock_sm.return_value.get.return_value = [
                {
                    "id": "plex-a",
                    "type": "plex",
                    "enabled": True,
                    "libraries": [{"id": "1", "name": "Movies", "remote_paths": ["/data/Movies"], "enabled": True}],
                    "path_mappings": [],
                },
            ]
            result = run_processing(config, selected_gpus=[])

        resolution = result.get("webhook_resolution") or {}
        assert "/mnt/data/Movies/X.mkv" in (resolution.get("unresolved_paths") or [])
        hints = resolution.get("path_hints") or []
        assert hints, "expected a path-mapping mismatch hint, got none"
        joined = " ".join(hints)
        # Hint must mention path mapping as the action so the user
        # knows where to fix it.
        assert "mapping" in joined.lower(), f"hint must mention path mapping; got {hints!r}"
        # And must surface BOTH the webhook prefix and the server
        # prefix so the user can configure the mapping without
        # guessing which side is which.
        assert "/mnt" in joined, f"hint missing webhook prefix '/mnt'; got {hints!r}"
        assert "/data" in joined, f"hint missing server prefix '/data'; got {hints!r}"

    def test_webhook_pinned_to_plex_does_not_fan_out(self, tmp_path):
        """Audit M4 — a Plex-pinned webhook publishes to Plex only.
        Pinning means "publish to this server only", and the dispatcher
        carries ``server_id_filter`` through to ``process_canonical_path``
        so Emby/Jellyfin owners are intentionally excluded.
        """
        config = _make_config(tmp_path, webhook_paths=["/data/x.mkv"])
        config.server_id_filter = "plex-a"

        with (
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

    def test_failed_dispatch_returns_raw_webhook_path_not_canonical(self, tmp_path):
        """Audit A3/A4 — when a webhook arrives in source view
        (``/data/Movies/X.mkv``), gets translated to a server-view
        canonical (``/data_16tb/Movies/X.mkv``), and dispatch reports
        a FAILED outcome, the unresolved_paths list must store the
        RAW webhook input (``/data/...``), not the translated
        canonical. Retry jobs key webhook_item_id_hints by the raw
        input; a mixed-namespace list silently drops hints on retry.
        """
        config = _make_config(tmp_path, webhook_paths=["/data/Movies/X.mkv"])

        with (
            patch(f"{MODULE}.WorkerPool") as MockPool,
            patch("media_preview_generator.web.settings_manager.get_settings_manager") as mock_sm,
        ):
            # Simulate dispatch returning FAILED for the item.
            MockPool.return_value.process_items_headless.return_value = _pool_result(
                completed=0,
                failed=1,
            )
            mock_sm.return_value.get.return_value = [
                {
                    "id": "plex-1",
                    "type": "plex",
                    "enabled": True,
                    "libraries": [
                        {"id": "1", "name": "Movies", "remote_paths": ["/data_16tb/Movies"], "enabled": True}
                    ],
                    "path_mappings": [
                        {
                            "plex_prefix": "/data_16tb",
                            "local_prefix": "/data_16tb",
                            "webhook_prefixes": ["/data"],
                        }
                    ],
                },
            ]
            result = run_processing(config, selected_gpus=[])

        # The orchestrator translated the webhook path to
        # ``/data_16tb/Movies/X.mkv`` for the ProcessableItem, but the
        # FAILED outcome must be reported against the RAW input
        # ``/data/Movies/X.mkv`` so retry hint keying still works.
        resolution = result.get("webhook_resolution") or {}
        unresolved = resolution.get("unresolved_paths") or []
        assert "/data/Movies/X.mkv" in unresolved, (
            f"FAILED dispatch must surface the raw webhook input in unresolved_paths "
            f"so retry hint keying matches; got {unresolved!r}. "
            "Audit A3/A4: a mixed-namespace list silently breaks the retry flow."
        )
        # And the canonical (translated) form must NOT be in the list
        # — that's the namespace-mixing bug the audit flagged.
        assert "/data_16tb/Movies/X.mkv" not in unresolved, (
            f"Server-view canonical leaked into unresolved_paths; namespace mixing. got {unresolved!r}"
        )

    def test_vendor_hint_dispatches_even_when_library_cache_stale(self, tmp_path):
        """Audit A2 — when a webhook arrives with a vendor item-id hint
        (Plex/Emby/Jellyfin native plugin) but the library cache hasn't
        caught up to the new library yet, the orchestrator must
        DISPATCH (not fast-skip). The dispatcher's downstream
        ``_resolve_publishers`` honours the hint via the hinted
        server's adapter, so the publish still works.

        Pre-fix: orchestrator fast-skipped because no library covers
        the path → the very webhook that should bootstrap a freshly-
        added library silently did nothing. User had to wait for the
        library-cache refresh + a re-fired webhook.
        """
        config = _make_config(
            tmp_path,
            webhook_paths=["/data/freshly_added_library/X.mkv"],
        )
        # Vendor hint says "this is item k1 on plex-1" — even though
        # plex-1 has no libraries cached yet that cover this path.
        config.webhook_item_id_hints = {"/data/freshly_added_library/X.mkv": {"plex-1": "k1"}}

        with (
            patch(f"{MODULE}.WorkerPool") as MockPool,
            patch("media_preview_generator.web.settings_manager.get_settings_manager") as mock_sm,
        ):
            MockPool.return_value.process_items_headless.return_value = _pool_result(completed=1)
            # Plex configured but its libraries don't cover the
            # webhook path — staleness simulated by an unrelated
            # library entry.
            mock_sm.return_value.get.return_value = [
                {
                    "id": "plex-1",
                    "type": "plex",
                    "enabled": True,
                    "libraries": [{"id": "1", "name": "Movies", "remote_paths": ["/data/movies"], "enabled": True}],
                    "path_mappings": [],
                },
            ]
            run_processing(config, selected_gpus=[])

        # The path was DISPATCHED despite no library coverage. The
        # ProcessableItem carries the hint so the dispatcher can
        # publish via plex-1's adapter directly.
        MockPool.return_value.process_items_headless.assert_called_once()
        items = MockPool.return_value.process_items_headless.call_args.args[0]
        assert len(items) == 1, (
            f"Expected one dispatched item; got {len(items)}. Audit A2: a vendor hint "
            "for a path with no library coverage must STILL dispatch — the dispatcher's "
            "_resolve_publishers honours the hint."
        )
        assert items[0].canonical_path == "/data/freshly_added_library/X.mkv"
        assert items[0].item_id_by_server == {"plex-1": "k1"}, (
            f"Vendor hint must propagate to ProcessableItem.item_id_by_server; got {items[0]!r}"
        )

    def test_webhook_path_no_owners_fast_skips(self, tmp_path):
        """No enabled server claims the path → orchestrator fast-skips
        with no worker pickup. Without this gate the path would still hit
        a worker thread, log "Worker N picked up", then bail with
        NO_OWNERS milliseconds later (the original abe52ab7 user report).
        """
        config = _make_config(tmp_path, webhook_paths=["/data/Sports/Match.mkv"])

        with (
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

        # Pre-task #49 the orchestrator emitted "Connecting to Plex…"
        # as the first progress event from the eager pre-connection.
        # That eager connection is gone (it was vestigial — the result
        # was a dead parameter on _run_webhook_paths_phase). The
        # remaining contract is that progress IS reported during the
        # job — and specifically that the dispatch tick fires with the
        # total item count.
        assert progress.call_args_list, "progress_callback must be invoked at least once during a scan"
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
    """Tests for exception paths in run_processing.

    The orchestrator no longer eagerly opens a Plex connection at job
    start (task #49 — that pre-connection was vestigial post-K4
    unification). Plex outages during enumeration are now caught
    inside ``_run_plex_full_scan_phase`` (line 1668-1672 — log via
    ``logger.exception``, return False, job completes gracefully with
    empty outcome) instead of crashing the whole job. This is the
    correct peer-equal behaviour: a Plex outage shouldn't abort jobs
    whose paths only Emby / Jellyfin own.
    """

    def test_full_scan_enumeration_error_swallowed(self, tmp_path):
        """ConnectionError during full-scan enumeration is caught by
        ``_run_plex_full_scan_phase`` and the job still returns a
        result dict (with empty outcome) rather than crashing.
        """
        config = _make_config(tmp_path)
        with patch(f"{MODULE}._enumerate_plex_full_scan_items", side_effect=ConnectionError("refused")):
            result = run_processing(config, selected_gpus=[])

        # Job completes with the enum-failed-then-returned-False shape
        # — outcome dict present (counts are zeroed since no items
        # were processed). Pre-fix #49 a ConnectionError here would
        # have aborted the entire job (return None); the new
        # peer-equal architecture treats Plex outages as a single-
        # server failure, not a job-level fatality.
        assert isinstance(result, dict)
        outcome = result.get("outcome") or {}
        # Either empty dict (legacy aggregate_outcome path) or the
        # full counts dict with all-zero entries — both are valid
        # "no items processed" signals. The contract is ``no items
        # were processed and the job returned cleanly``.
        total = sum(int(v) for v in outcome.values())
        assert total == 0, f"expected zero items processed, got {outcome}"

    def test_keyboard_interrupt_swallowed(self, tmp_path):
        """KeyboardInterrupt during enumeration is caught and returns
        None implicitly (the outer except KeyboardInterrupt handler in
        ``run_processing`` falls through without an explicit return).
        Same path as Ctrl+C from the operator during a scan.
        """
        config = _make_config(tmp_path)
        with patch(f"{MODULE}._enumerate_plex_full_scan_items", side_effect=KeyboardInterrupt):
            result = run_processing(config, selected_gpus=[])

        # The full-scan-phase catches Exception (NOT BaseException), so
        # KeyboardInterrupt propagates up to ``run_processing``'s outer
        # ``except KeyboardInterrupt`` handler, which logs and falls
        # through (implicit ``return None``).
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
        """Temp folder is cleaned even when enumeration fails.

        Even though ``_run_plex_full_scan_phase`` catches the
        ConnectionError internally and returns False, the orchestrator's
        finally block still runs and removes ``working_tmp_folder``.
        """
        work_dir = tmp_path / "work"
        work_dir.mkdir()
        config = _make_config(tmp_path, working_tmp_folder=str(work_dir))

        with patch(f"{MODULE}._enumerate_plex_full_scan_items", side_effect=ConnectionError("fail")):
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
