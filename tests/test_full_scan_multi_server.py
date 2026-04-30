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
