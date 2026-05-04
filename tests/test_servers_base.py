"""Tests for the MediaServer / OutputAdapter abstractions (Phase 1 scaffold).

These tests pin the dataclass shapes and ABC contract so that future
refactors which break the interface fail loudly. Concrete vendor
implementations are added in later phases and tested separately.
"""

import dataclasses
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from media_preview_generator.output import BifBundle, OutputAdapter
from media_preview_generator.servers import (
    ConnectionResult,
    Library,
    LibraryNotYetIndexedError,
    MediaItem,
    MediaServer,
    ServerConfig,
    ServerType,
    WebhookEvent,
)


class TestServerType:
    def test_values_are_lowercase_strings(self):
        assert ServerType.PLEX.value == "plex"
        assert ServerType.EMBY.value == "emby"
        assert ServerType.JELLYFIN.value == "jellyfin"

    def test_can_round_trip_through_string(self):
        assert ServerType("plex") is ServerType.PLEX
        assert ServerType("jellyfin") is ServerType.JELLYFIN


class TestLibrary:
    def test_minimal_library_defaults_to_enabled(self):
        lib = Library(id="1", name="Movies", remote_paths=("/media/movies",))
        assert lib.enabled is True
        assert lib.kind is None

    def test_remote_paths_is_tuple(self):
        lib = Library(id="1", name="Movies", remote_paths=("/a", "/b"))
        assert isinstance(lib.remote_paths, tuple)

    def test_is_frozen(self):
        lib = Library(id="1", name="Movies", remote_paths=("/m",))
        with pytest.raises(dataclasses.FrozenInstanceError):
            lib.enabled = False  # type: ignore[misc]


class TestMediaItem:
    def test_required_fields(self):
        item = MediaItem(id="42", library_id="1", title="Foo", remote_path="/m/foo.mkv")
        assert item.id == "42"
        assert item.library_id == "1"
        assert item.title == "Foo"
        assert item.remote_path == "/m/foo.mkv"
        # Default for the optional pre-fetched bundle metadata is an empty tuple.
        assert item.bundle_metadata == ()


class TestWebhookEvent:
    def test_path_only_event(self):
        ev = WebhookEvent(event_type="library.new", remote_path="/m/foo.mkv")
        assert ev.item_id is None
        assert ev.remote_path == "/m/foo.mkv"

    def test_item_id_only_event(self):
        ev = WebhookEvent(event_type="ItemAdded", item_id="42")
        assert ev.event_type == "ItemAdded"
        assert ev.item_id == "42"
        assert ev.remote_path is None
        assert ev.raw is None


class TestConnectionResult:
    def test_failure_minimum(self):
        r = ConnectionResult(ok=False, message="connection refused")
        assert not r.ok
        assert r.server_id is None

    def test_success_carries_identity(self):
        r = ConnectionResult(
            ok=True,
            server_id="abc123",
            server_name="Home Plex",
            version="1.40.0",
        )
        assert r.ok is True
        assert r.server_id == "abc123"
        assert r.server_name == "Home Plex"
        assert r.version == "1.40.0"
        # ``message`` defaults to empty string when only identity fields are set.
        assert r.message == ""


class TestServerConfig:
    def test_defaults(self):
        cfg = ServerConfig(
            id="uuid",
            type=ServerType.PLEX,
            name="Home",
            enabled=True,
            url="http://x",
            auth={"token": "t"},
        )
        assert cfg.libraries == []
        assert cfg.path_mappings == []
        assert cfg.output == {}
        assert cfg.verify_ssl is True


class _FakeServer(MediaServer):
    """Minimal MediaServer implementation used to verify the ABC contract."""

    def __init__(self) -> None:
        super().__init__(server_id="fake-id", name="Fake")

    @property
    def type(self) -> ServerType:
        return ServerType.PLEX

    def test_connection(self) -> ConnectionResult:
        return ConnectionResult(ok=True, server_id="fake-id")

    def list_libraries(self) -> list[Library]:
        return [Library(id="1", name="Movies", remote_paths=("/m",))]

    def list_items(self, library_id: str) -> Iterator[MediaItem]:
        yield MediaItem(id="42", library_id=library_id, title="Foo", remote_path="/m/foo.mkv")

    def resolve_item_to_remote_path(self, item_id: str) -> str | None:
        return "/m/foo.mkv" if item_id == "42" else None

    def trigger_refresh(self, *, item_id: str | None, remote_path: str | None) -> None:
        return None

    def parse_webhook(self, payload: dict[str, Any] | bytes, headers: dict[str, str]) -> WebhookEvent | None:
        return WebhookEvent(event_type="test")


class TestMediaServerABC:
    def test_cannot_instantiate_without_implementing_abstract_methods(self):
        with pytest.raises(TypeError):
            MediaServer(server_id="x", name="x")  # type: ignore[abstract]

    def test_concrete_subclass_works(self):
        s = _FakeServer()
        assert s.id == "fake-id"
        assert s.name == "Fake"
        assert s.type is ServerType.PLEX
        assert s.test_connection().ok
        libs = s.list_libraries()
        assert len(libs) == 1 and libs[0].name == "Movies"
        assert list(s.list_items("1"))[0].id == "42"
        assert s.resolve_item_to_remote_path("42") == "/m/foo.mkv"
        assert s.resolve_item_to_remote_path("nope") is None
        s.trigger_refresh(item_id=None, remote_path="/m/foo.mkv")  # no-op


class TestSearchItemsDefault:
    """D4 — base class search_items default falls back to a brute-force walk.

    The override on each vendor (Plex via library.search(), Emby/Jellyfin
    via /Items?searchTerm=) is what's actually used in production; the
    default is only the safety net for any vendor adapter that hasn't
    been overridden yet.
    """

    def test_empty_query_returns_empty(self):
        s = _FakeServer()
        assert s.search_items("") == []
        assert s.search_items("   ") == []

    def test_walks_libraries_and_items_filtering_substring(self):
        class _MultiItem(_FakeServer):
            def list_libraries(self) -> list[Library]:
                return [Library(id="1", name="Movies", remote_paths=("/m",))]

            def list_items(self, library_id: str) -> Iterator[MediaItem]:
                titles = ["Inception", "The Matrix", "Interstellar"]
                for i, t in enumerate(titles):
                    yield MediaItem(id=str(i), library_id=library_id, title=t, remote_path=f"/m/{t}.mkv")

        s = _MultiItem()
        results = s.search_items("inter")
        assert len(results) == 1 and results[0].title == "Interstellar"

    def test_respects_limit(self):
        class _ManyItems(_FakeServer):
            def list_libraries(self) -> list[Library]:
                return [Library(id="1", name="Movies", remote_paths=("/m",))]

            def list_items(self, library_id: str) -> Iterator[MediaItem]:
                for i in range(20):
                    yield MediaItem(id=str(i), library_id=library_id, title=f"Movie {i}", remote_path=f"/m/{i}.mkv")

        s = _ManyItems()
        assert len(s.search_items("Movie", limit=5)) == 5

    def test_case_insensitive(self):
        class _Mixed(_FakeServer):
            def list_libraries(self) -> list[Library]:
                return [Library(id="1", name="Movies", remote_paths=("/m",))]

            def list_items(self, library_id: str) -> Iterator[MediaItem]:
                yield MediaItem(id="1", library_id=library_id, title="The MATRIX", remote_path="/m/m.mkv")

        s = _Mixed()
        assert len(s.search_items("matrix")) == 1


class _FakeAdapter(OutputAdapter):
    @property
    def name(self) -> str:
        return "fake"

    def needs_server_metadata(self) -> bool:
        return False

    def compute_output_paths(self, bundle: BifBundle, server: MediaServer, item_id: str | None) -> list[Path]:
        return [Path(bundle.canonical_path).with_suffix(".bif")]

    def publish(self, bundle: BifBundle, output_paths: list[Path]) -> None:
        return None


class TestOutputAdapterABC:
    def test_cannot_instantiate_without_implementing_abstract_methods(self):
        with pytest.raises(TypeError):
            OutputAdapter()  # type: ignore[abstract]

    def test_bundle_dataclass_shape(self, tmp_path):
        b = BifBundle(
            canonical_path="/m/foo.mkv",
            frame_dir=tmp_path,
            bif_path=None,
            frame_interval=10,
            width=320,
            height=180,
            frame_count=540,
        )
        assert b.canonical_path == "/m/foo.mkv"
        assert b.frame_dir == tmp_path
        assert b.bif_path is None
        assert b.frame_interval == 10
        assert b.width == 320
        assert b.height == 180
        assert b.frame_count == 540
        # Defaults for vendor-pre-fetched metadata and the display-name hint.
        assert b.prefetched_bundle_metadata == ()
        assert b.server_display_name is None

    def test_concrete_adapter_works(self, tmp_path):
        adapter = _FakeAdapter()
        bundle = BifBundle(
            canonical_path="/m/foo.mkv",
            frame_dir=tmp_path,
            bif_path=None,
            frame_interval=10,
            width=320,
            height=180,
            frame_count=10,
        )
        paths = adapter.compute_output_paths(bundle, _FakeServer(), item_id=None)
        assert paths == [Path("/m/foo.bif")]
        assert not adapter.needs_server_metadata()


class TestLibraryNotYetIndexedError:
    def test_inherits_from_exception(self):
        assert issubclass(LibraryNotYetIndexedError, Exception)

    def test_can_carry_message(self):
        e = LibraryNotYetIndexedError("plex hasn't scanned yet")
        assert str(e) == "plex hasn't scanned yet"
