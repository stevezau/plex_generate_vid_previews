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
        assert item.remote_path == "/m/foo.mkv"


class TestWebhookEvent:
    def test_path_only_event(self):
        ev = WebhookEvent(event_type="library.new", remote_path="/m/foo.mkv")
        assert ev.item_id is None
        assert ev.remote_path == "/m/foo.mkv"

    def test_item_id_only_event(self):
        ev = WebhookEvent(event_type="ItemAdded", item_id="42")
        assert ev.remote_path is None


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
        assert r.ok
        assert r.server_id == "abc123"


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
        assert b.bif_path is None

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
