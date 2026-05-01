"""Tests for the per-vendor processor registry.

Phase A of the multi-server processing completion. Locks down the
registry contract so Phase B's vendor modules can self-register
predictably and the orchestrator (Phase C onwards) can call
``get_processor_for(server.type)`` without branching.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import FrozenInstanceError

import pytest

from media_preview_generator.processing.base import VendorProcessor
from media_preview_generator.processing.registry import (
    get_processor_for,
    register_processor,
    registered_types,
)
from media_preview_generator.processing.types import ProcessableItem, ScanOutcome
from media_preview_generator.servers.base import Library, ServerConfig, ServerType


class _StubProcessor:
    """Bare-minimum VendorProcessor for registry-shape tests.

    Returns an empty Library list / no items. Phase B replaces this
    with real per-vendor implementations.
    """

    def list_libraries(self, server_config: ServerConfig) -> list[Library]:
        return []

    def list_canonical_paths(
        self,
        server_config: ServerConfig,
        *,
        library_ids=None,
        cancel_check=None,
        progress_callback=None,
    ) -> Iterator[ProcessableItem]:
        return iter(())

    def scan_recently_added(
        self,
        server_config: ServerConfig,
        *,
        lookback_hours: int,
        library_ids=None,
    ) -> Iterator[ProcessableItem]:
        return iter(())

    def resolve_canonical_path(self, server_config: ServerConfig, *, item_id: str) -> str | None:
        return None


@pytest.fixture(autouse=True)
def _isolate_registry(monkeypatch):
    """Each test gets a fresh registry — prevents test pollution from
    leaking processors registered by an earlier test (or by Phase B's
    self-registration once those modules exist)."""
    from media_preview_generator.processing import registry

    monkeypatch.setattr(registry, "_PROCESSORS", {})


class TestRegistryRoundTrip:
    def test_register_then_get_round_trips(self):
        proc = _StubProcessor()
        register_processor(ServerType.PLEX, proc)
        assert get_processor_for(ServerType.PLEX) is proc

    def test_get_accepts_string_form(self):
        proc = _StubProcessor()
        register_processor(ServerType.JELLYFIN, proc)
        # Call sites that read straight from settings JSON pass strings.
        assert get_processor_for("jellyfin") is proc

    def test_unknown_string_raises_keyerror(self):
        with pytest.raises(KeyError, match="unknown server type"):
            get_processor_for("frobnicator")

    def test_unregistered_known_type_raises_keyerror(self):
        with pytest.raises(KeyError, match="no VendorProcessor registered"):
            get_processor_for(ServerType.EMBY)

    def test_re_registration_overrides(self):
        first = _StubProcessor()
        second = _StubProcessor()
        register_processor(ServerType.PLEX, first)
        register_processor(ServerType.PLEX, second)
        assert get_processor_for(ServerType.PLEX) is second

    def test_registered_types_lists_what_was_added(self):
        register_processor(ServerType.PLEX, _StubProcessor())
        register_processor(ServerType.EMBY, _StubProcessor())
        assert sorted(t.value for t in registered_types()) == ["emby", "plex"]


class TestProtocolShape:
    def test_stub_satisfies_protocol(self):
        # `Protocol` here is the vendor contract; structural conformance
        # is the only assertion we need (no `runtime_checkable` overhead).
        proc: VendorProcessor = _StubProcessor()
        # Just exercise each method to make sure the signatures align.
        assert proc.list_libraries(server_config=None) == []  # type: ignore[arg-type]
        assert list(proc.list_canonical_paths(server_config=None)) == []  # type: ignore[arg-type]
        assert list(proc.scan_recently_added(server_config=None, lookback_hours=24)) == []  # type: ignore[arg-type]
        assert proc.resolve_canonical_path(server_config=None, item_id="x") is None  # type: ignore[arg-type]


class TestProcessableItemShape:
    def test_minimal_construction(self):
        item = ProcessableItem(canonical_path="/data/movies/Foo.mkv", server_id="srv-1")
        assert item.canonical_path == "/data/movies/Foo.mkv"
        assert item.server_id == "srv-1"
        assert item.item_id_by_server == {}
        assert item.title == ""
        assert item.library_id is None

    def test_full_construction(self):
        item = ProcessableItem(
            canonical_path="/data/tv/Show/S01E01.mkv",
            server_id="srv-2",
            item_id_by_server={"srv-2": "12345"},
            title="Show - S01E01",
            library_id="2",
        )
        assert item.item_id_by_server == {"srv-2": "12345"}
        assert item.title == "Show - S01E01"
        assert item.library_id == "2"

    def test_is_frozen(self):
        item = ProcessableItem(canonical_path="/x", server_id="s")
        with pytest.raises(FrozenInstanceError):
            item.canonical_path = "/y"  # type: ignore[misc]


class TestScanOutcomeShape:
    def test_default_zeroes(self):
        outcome = ScanOutcome()
        assert outcome.items_yielded == 0
        assert outcome.libraries_walked == 0
        assert outcome.skipped_reason is None

    def test_mutability(self):
        outcome = ScanOutcome()
        outcome.items_yielded = 5
        outcome.libraries_walked = 1
        outcome.skipped_reason = "library list was empty"
        assert outcome.items_yielded == 5
        assert outcome.skipped_reason == "library list was empty"
