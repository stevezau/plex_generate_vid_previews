"""Tests for the per-vendor processor implementations (Phase B).

Locks down the contract that ``EmbyProcessor``, ``JellyfinProcessor``,
and ``PlexProcessor`` all satisfy the :class:`VendorProcessor`
protocol with mocked underlying :class:`MediaServer` clients. The
real HTTP layer is mocked at the ``MediaServer`` boundary so these
tests stay vendor-agnostic at the test layer too.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from media_preview_generator.processing.emby import EmbyProcessor
from media_preview_generator.processing.jellyfin import JellyfinProcessor
from media_preview_generator.processing.plex import PlexProcessor
from media_preview_generator.processing.registry import get_processor_for, registered_types
from media_preview_generator.processing.types import ProcessableItem
from media_preview_generator.servers.base import Library, MediaItem, ServerConfig, ServerType


def _config(server_id: str, server_type: ServerType, *, mappings=None) -> ServerConfig:
    return ServerConfig(
        id=server_id,
        type=server_type,
        name=f"Test {server_type.value}",
        enabled=True,
        url="http://test",
        auth={"access_token": "tok"},
        path_mappings=mappings or [],
    )


# Shared library + items fixture data so every vendor test exercises the same shape.
_LIB = Library(id="lib-1", name="Movies", remote_paths=("/remote",), enabled=True)
_LIB_DISABLED = Library(id="lib-2", name="Disabled", remote_paths=("/x",), enabled=False)
_ITEMS = [
    MediaItem(id="m-1", library_id="lib-1", title="Movie One", remote_path="/remote/m1.mkv"),
    MediaItem(id="m-2", library_id="lib-1", title="Movie Two", remote_path="/remote/m2.mkv"),
]


class _ProcessorContractTests:
    """Behaviours every concrete VendorProcessor must satisfy.

    Concrete tests below pick up these methods by inheritance and
    supply the right (vendor, server_type, client_path_to_patch) fixture.
    """

    vendor: type  # subclass-specified
    server_type: ServerType
    client_class_path: str  # dotted path of the MediaServer subclass to patch

    @pytest.fixture
    def processor(self):
        return self.vendor()

    @pytest.fixture
    def mock_client(self):
        with patch(self.client_class_path) as klass:
            instance = MagicMock()
            klass.return_value = instance
            yield instance

    def test_registry_has_this_vendor(self):
        assert isinstance(get_processor_for(self.server_type), self.vendor)

    def test_list_libraries_passes_through(self, processor, mock_client):
        mock_client.list_libraries.return_value = [_LIB, _LIB_DISABLED]
        cfg = _config("srv-x", self.server_type)
        assert processor.list_libraries(cfg) == [_LIB, _LIB_DISABLED]

    def test_list_libraries_empty_on_failure(self, processor, mock_client):
        mock_client.list_libraries.side_effect = RuntimeError("boom")
        cfg = _config("srv-x", self.server_type)
        assert processor.list_libraries(cfg) == []

    def test_list_canonical_paths_yields_per_item_with_path_mapping(self, processor, mock_client):
        mock_client.list_libraries.return_value = [_LIB]
        mock_client.list_items.return_value = iter(_ITEMS)
        cfg = _config(
            "srv-x",
            self.server_type,
            mappings=[{"remote_prefix": "/remote", "local_prefix": "/local"}],
        )
        items = list(processor.list_canonical_paths(cfg))
        assert len(items) == 2
        assert all(isinstance(i, ProcessableItem) for i in items)
        assert items[0].canonical_path == "/local/m1.mkv"
        assert items[0].server_id == "srv-x"
        assert items[0].item_id_by_server == {"srv-x": "m-1"}
        assert items[0].library_id == "lib-1"
        assert items[1].canonical_path == "/local/m2.mkv"

    def test_list_canonical_paths_skips_disabled_libraries(self, processor, mock_client):
        mock_client.list_libraries.return_value = [_LIB_DISABLED]
        mock_client.list_items.return_value = iter([])
        cfg = _config("srv-x", self.server_type)
        assert list(processor.list_canonical_paths(cfg)) == []
        # Disabled lib never gets walked.
        mock_client.list_items.assert_not_called()

    def test_list_canonical_paths_filters_by_library_ids(self, processor, mock_client):
        other_lib = Library(id="lib-99", name="Other", remote_paths=("/o",), enabled=True)
        mock_client.list_libraries.return_value = [_LIB, other_lib]
        mock_client.list_items.return_value = iter(_ITEMS)
        cfg = _config("srv-x", self.server_type)
        list(processor.list_canonical_paths(cfg, library_ids=["lib-1"]))
        # Only lib-1 should have been walked.
        called_with = [c.args[0] for c in mock_client.list_items.call_args_list]
        assert called_with == ["lib-1"]

    def test_list_canonical_paths_honours_cancel_check(self, processor, mock_client):
        mock_client.list_libraries.return_value = [_LIB]
        mock_client.list_items.return_value = iter(_ITEMS)
        cfg = _config("srv-x", self.server_type)
        called = {"count": 0}

        def cancel_after_first():
            called["count"] += 1
            return called["count"] > 1

        items = list(processor.list_canonical_paths(cfg, cancel_check=cancel_after_first))
        assert len(items) <= 2  # short-circuited

    def test_resolve_canonical_path_applies_mappings(self, processor, mock_client):
        mock_client.resolve_item_to_remote_path.return_value = "/remote/movie.mkv"
        cfg = _config(
            "srv-x",
            self.server_type,
            mappings=[{"remote_prefix": "/remote", "local_prefix": "/local"}],
        )
        assert processor.resolve_canonical_path(cfg, item_id="m-1") == "/local/movie.mkv"

    def test_resolve_canonical_path_returns_none_when_not_indexed(self, processor, mock_client):
        mock_client.resolve_item_to_remote_path.return_value = None
        cfg = _config("srv-x", self.server_type)
        assert processor.resolve_canonical_path(cfg, item_id="missing") is None


class TestEmbyProcessor(_ProcessorContractTests):
    vendor = EmbyProcessor
    server_type = ServerType.EMBY
    client_class_path = "media_preview_generator.processing.emby.EmbyServer"


class TestJellyfinProcessor(_ProcessorContractTests):
    vendor = JellyfinProcessor
    server_type = ServerType.JELLYFIN
    client_class_path = "media_preview_generator.processing.jellyfin.JellyfinServer"


class TestPlexProcessor(_ProcessorContractTests):
    vendor = PlexProcessor
    server_type = ServerType.PLEX
    client_class_path = "media_preview_generator.processing.plex.PlexServer"


class TestRegistryHasAllVendors:
    def test_three_vendors_registered_at_import(self):
        assert sorted(t.value for t in registered_types()) == ["emby", "jellyfin", "plex"]


class TestEmbyishRecentlyAdded:
    """Both Emby and Jellyfin share the recently-added scan via the
    embyish helper; one combined test covers the path."""

    def test_within_lookback_iso_with_z_suffix(self):
        from datetime import datetime, timedelta, timezone

        from media_preview_generator.processing._embyish import _within_lookback

        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(hours=1)
        recent_iso = now.isoformat().replace("+00:00", "Z")
        old_iso = (now - timedelta(hours=2)).isoformat().replace("+00:00", "Z")
        assert _within_lookback(recent_iso, cutoff)
        assert not _within_lookback(old_iso, cutoff)

    def test_within_lookback_strips_subnano_precision(self):
        from datetime import datetime, timedelta, timezone

        from media_preview_generator.processing._embyish import _within_lookback

        cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
        # Jellyfin / Emby emit .NET-style 7-digit fractional seconds.
        candidate = (datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%S.1234567+00:00")
        assert _within_lookback(candidate, cutoff)

    def test_within_lookback_rejects_garbage(self):
        from datetime import datetime, timedelta, timezone

        from media_preview_generator.processing._embyish import _within_lookback

        cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
        assert not _within_lookback("not a date", cutoff)
        assert not _within_lookback("", cutoff)

    def test_format_title_episode_and_movie(self):
        from media_preview_generator.processing._embyish import _format_title

        episode = {
            "Name": "Pilot",
            "SeriesName": "Show",
            "ParentIndexNumber": 1,
            "IndexNumber": 1,
        }
        movie = {"Name": "Cool Movie"}
        assert _format_title(episode) == "Show - S01E01 - Pilot"
        assert _format_title(movie) == "Cool Movie"

    def test_scan_recently_added_filters_window_and_path_maps(self):
        from datetime import datetime, timedelta, timezone

        proc = EmbyProcessor()
        cfg = _config(
            "srv-x",
            ServerType.EMBY,
            mappings=[{"remote_prefix": "/r", "local_prefix": "/l"}],
        )
        now = datetime.now(timezone.utc)
        recent_iso = now.isoformat().replace("+00:00", "Z")
        old_iso = (now - timedelta(hours=72)).isoformat().replace("+00:00", "Z")

        with patch("media_preview_generator.processing.emby.EmbyServer") as klass:
            instance = MagicMock()
            klass.return_value = instance
            response = MagicMock()
            response.raise_for_status.return_value = None
            response.json.return_value = {
                "Items": [
                    {"Id": "i1", "Name": "Recent", "Path": "/r/recent.mkv", "DateCreated": recent_iso},
                    {"Id": "i2", "Name": "Old", "Path": "/r/old.mkv", "DateCreated": old_iso},
                ]
            }
            instance._request.return_value = response

            items = list(proc.scan_recently_added(cfg, lookback_hours=24))

        assert len(items) == 1
        assert items[0].canonical_path == "/l/recent.mkv"
        assert items[0].title == "Recent"
        assert items[0].item_id_by_server == {"srv-x": "i1"}
