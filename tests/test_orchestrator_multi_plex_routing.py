"""Regression: a full-library scan pinned to a non-first Plex server in a
multi-Plex install must enumerate the PINNED server, not media_servers[0].

This guards against GitHub issue #244: user had two Plex servers (no
Emby/Jellyfin), pinned a library scan to the second Plex, the job ran
against the first one. Root cause: ``_should_use_multi_server_full_scan``
returned False for "2 Plex, no non-Plex" so dispatch fell through to the
legacy ``_run_plex_full_scan_phase``, whose enumerator picked the first
Plex out of ``registry.configs()`` and ignored ``config.server_id_filter``.

The fix routes multi-Plex installs through ``_run_full_scan_multi_server``
which honors ``server_id_filter`` (orchestrator.py:1552). Single-Plex
installs keep their existing legacy path so the WorkerPool's
``worker_pool_callback`` / ``item_complete_callback`` wiring is preserved.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from media_preview_generator.jobs.orchestrator import (
    _should_use_multi_server_full_scan,
    run_processing,
)


def _full_scan_config(server_id_filter: str | None = None):
    """Bare config that satisfies ``run_processing``'s preconditions for a
    legitimate full library scan (no webhook markers, Plex configured)."""
    return SimpleNamespace(
        webhook_paths=None,
        webhook_source=None,
        server_id_filter=server_id_filter,
        plex_url="http://plex-a.example:32400",
        plex_token="tok-a",
        plex_library_ids=None,
        gpu_threads=0,
        cpu_threads=1,
        working_tmp_folder="/tmp/test-multi-plex-routing-nonexistent",
    )


class TestShouldUseMultiServerFullScan:
    """Pin the gate function itself — the matrix that decides which
    dispatch path handles a full-library scan."""

    def _with_servers(self, entries):
        """Context manager-ish helper returning a patcher already started."""
        patcher = patch("media_preview_generator.web.settings_manager.get_settings_manager")
        mock_sm = patcher.start()
        mock_sm.return_value.get.return_value = entries
        return patcher

    def test_single_plex_uses_legacy_path(self):
        """One enabled Plex, no pin → legacy path (preserves current
        single-Plex behaviour, including WorkerPool callbacks)."""
        patcher = self._with_servers([{"id": "plex-a", "type": "plex", "enabled": True}])
        try:
            config = _full_scan_config(server_id_filter=None)
            assert _should_use_multi_server_full_scan(config, pinned_type="") is False
        finally:
            patcher.stop()

    def test_two_enabled_plex_servers_routes_through_multi_server(self):
        """The #244 bug: two enabled Plex servers MUST route through the
        multi-server path so ``server_id_filter`` is honoured. Without
        this the legacy enumerator picks ``media_servers[0]`` regardless
        of the pin and the second Plex is silently never scanned."""
        patcher = self._with_servers(
            [
                {"id": "plex-a", "type": "plex", "enabled": True},
                {"id": "plex-b", "type": "plex", "enabled": True},
            ]
        )
        try:
            config = _full_scan_config(server_id_filter="plex-b")
            assert _should_use_multi_server_full_scan(config, pinned_type="plex") is True
        finally:
            patcher.stop()

    def test_two_enabled_plex_no_pin_also_routes_multi_server(self):
        """Even without a pin, two enabled Plex installs must use the
        multi-server path — the legacy enumerator's first-Plex-wins
        semantics would silently scan only one of them."""
        patcher = self._with_servers(
            [
                {"id": "plex-a", "type": "plex", "enabled": True},
                {"id": "plex-b", "type": "plex", "enabled": True},
            ]
        )
        try:
            config = _full_scan_config(server_id_filter=None)
            assert _should_use_multi_server_full_scan(config, pinned_type="") is True
        finally:
            patcher.stop()

    def test_disabled_second_plex_keeps_legacy_path(self):
        """One enabled + one disabled Plex → still one effective Plex,
        legacy path is fine. The ``enabled`` flag is the user's intent
        and must be respected."""
        patcher = self._with_servers(
            [
                {"id": "plex-a", "type": "plex", "enabled": True},
                {"id": "plex-b", "type": "plex", "enabled": False},
            ]
        )
        try:
            config = _full_scan_config(server_id_filter=None)
            assert _should_use_multi_server_full_scan(config, pinned_type="") is False
        finally:
            patcher.stop()

    def test_mixed_install_still_routes_multi_server(self):
        """Control: existing behaviour for Plex + Jellyfin must remain
        unchanged — multi-server path."""
        patcher = self._with_servers(
            [
                {"id": "plex-a", "type": "plex", "enabled": True},
                {"id": "jf-1", "type": "jellyfin", "enabled": True},
            ]
        )
        try:
            config = _full_scan_config(server_id_filter=None)
            assert _should_use_multi_server_full_scan(config, pinned_type="") is True
        finally:
            patcher.stop()

    def test_three_enabled_plex_routes_multi_server(self):
        """The ``>= 2`` predicate must hold for any larger N. A user
        consolidating three Plex servers under one runner is an explicit
        operator setup; legacy first-Plex-wins would silently hide two
        of them."""
        patcher = self._with_servers(
            [
                {"id": "plex-a", "type": "plex", "enabled": True},
                {"id": "plex-b", "type": "plex", "enabled": True},
                {"id": "plex-c", "type": "plex", "enabled": True},
            ]
        )
        try:
            config = _full_scan_config(server_id_filter=None)
            assert _should_use_multi_server_full_scan(config, pinned_type="") is True
        finally:
            patcher.stop()

    def test_pin_to_disabled_plex_in_single_enabled_install(self):
        """Pin set to a Plex that exists but is disabled. With only one
        enabled Plex (the *other* one), ``multi_plex`` is False and the
        gate keeps the legacy path — the enumerator then picks the
        enabled Plex (defence in depth at line 437) and the pin is
        effectively ignored. This is a graceful fallback, not silent
        wrong-server scanning — the user's effective Plex is the
        enabled one regardless of pin."""
        patcher = self._with_servers(
            [
                {"id": "plex-a", "type": "plex", "enabled": True},
                {"id": "plex-b", "type": "plex", "enabled": False},
            ]
        )
        try:
            config = _full_scan_config(server_id_filter="plex-b")
            assert _should_use_multi_server_full_scan(config, pinned_type="plex") is False
        finally:
            patcher.stop()

    def test_pin_to_ghost_id_in_multi_plex(self):
        """Pin set to a server-id that doesn't match any configured
        Plex. With 2+ enabled Plex servers the gate routes to the
        multi-server path regardless, where the no-candidates warning
        (orchestrator.py:1555-1558) gives the operator a clean signal
        instead of a silent first-Plex scan."""
        patcher = self._with_servers(
            [
                {"id": "plex-a", "type": "plex", "enabled": True},
                {"id": "plex-b", "type": "plex", "enabled": True},
            ]
        )
        try:
            config = _full_scan_config(server_id_filter="plex-ghost")
            assert _should_use_multi_server_full_scan(config, pinned_type="") is True
        finally:
            patcher.stop()


class TestRunProcessingRoutesMultiPlex:
    """Integration: assert ``run_processing`` actually picks the right
    branch and forwards the pin. This is the high-level contract that
    matters to the user — patching only the boundary makes the test
    bug-blind (the legacy path could be called with the wrong pin and
    the test would still pass)."""

    def test_multi_plex_pin_calls_multi_server_with_pin(self):
        """The full chain: 2-Plex install + Plex pin → dispatch hits
        ``_run_full_scan_multi_server`` with ``server_id_filter`` set to
        the pin. The legacy ``_run_plex_full_scan_phase`` must NOT be
        called — that path's enumerator picks ``media_servers[0]`` and
        the second Plex would never be scanned."""
        config = _full_scan_config(server_id_filter="plex-b")
        with (
            patch("media_preview_generator.web.settings_manager.get_settings_manager") as mock_sm,
            patch(
                "media_preview_generator.jobs.orchestrator._run_full_scan_multi_server",
                return_value={"generated": 0},
            ) as mock_multi,
            patch("media_preview_generator.jobs.orchestrator._run_plex_full_scan_phase") as mock_legacy,
        ):
            mock_sm.return_value.get.return_value = [
                {"id": "plex-a", "type": "plex", "enabled": True},
                {"id": "plex-b", "type": "plex", "enabled": True},
            ]
            run_processing(config, selected_gpus=[])

        mock_legacy.assert_not_called()
        mock_multi.assert_called_once()
        # Pin must be forwarded — without this assertion the test is
        # bug-blind (D34-shape regression: multi-server path called but
        # with the wrong pin). The fix's whole point is that "plex-b"
        # gets all the way down to the enumerator's filter.
        assert mock_multi.call_args.kwargs.get("server_id_filter") == "plex-b"

    def test_single_plex_pin_still_uses_legacy_path(self):
        """Control: single-Plex installs keep using the legacy path so
        no callback wiring is silently lost. The pin matches the only
        Plex configured, so the legacy enumerator's first-Plex-wins
        semantics happen to do the right thing here."""
        config = _full_scan_config(server_id_filter="plex-only")
        with (
            patch("media_preview_generator.web.settings_manager.get_settings_manager") as mock_sm,
            patch("media_preview_generator.jobs.orchestrator._run_full_scan_multi_server") as mock_multi,
            patch(
                "media_preview_generator.jobs.orchestrator._run_plex_full_scan_phase",
                return_value=True,
            ) as mock_legacy,
        ):
            mock_sm.return_value.get.return_value = [
                {"id": "plex-only", "type": "plex", "enabled": True},
            ]
            run_processing(config, selected_gpus=[])

        mock_multi.assert_not_called()
        mock_legacy.assert_called_once()


class TestEnumeratorPicksEnabledPlex:
    """Defence in depth at the legacy enumerator: even if the gate ever
    regresses and lets a "[disabled-first, enabled-second]" layout
    through, the enumerator must pick the *enabled* Plex — never connect
    to a server the user explicitly turned off."""

    def test_picks_enabled_when_first_config_is_disabled(self):
        """Layout: ``media_servers=[disabled Plex-A, enabled Plex-B]``.
        The enumerator must select Plex-B and call its
        ``list_canonical_paths`` — connecting to Plex-A here would be
        the bug shape (disabled server still queried).
        """
        from unittest.mock import MagicMock

        from media_preview_generator.jobs.orchestrator import _enumerate_plex_full_scan_items
        from media_preview_generator.servers.base import ServerType

        disabled_cfg = MagicMock()
        disabled_cfg.type = ServerType.PLEX
        disabled_cfg.enabled = False
        disabled_cfg.id = "plex-a"
        disabled_cfg.name = "Plex-A"

        enabled_cfg = MagicMock()
        enabled_cfg.type = ServerType.PLEX
        enabled_cfg.enabled = True
        enabled_cfg.id = "plex-b"
        enabled_cfg.name = "Plex-B"

        registry = MagicMock()
        registry.configs.return_value = [disabled_cfg, enabled_cfg]

        config = SimpleNamespace(plex_library_ids=None)

        processor = MagicMock()
        processor.list_canonical_paths.return_value = iter([])

        with patch(
            "media_preview_generator.processing.get_processor_for",
            return_value=processor,
        ):
            list(_enumerate_plex_full_scan_items(config, registry))

        # Pin the SUT's contract: the *enabled* Plex's config is what
        # gets handed to the processor — not media_servers[0].
        processor.list_canonical_paths.assert_called_once()
        call_args = processor.list_canonical_paths.call_args
        assert call_args.args[0] is enabled_cfg, (
            f"enumerator passed wrong ServerConfig: expected enabled Plex-B, got {call_args.args[0]!r}"
        )
