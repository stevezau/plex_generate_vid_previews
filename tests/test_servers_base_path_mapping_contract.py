"""Contract tests for ``MediaServer`` path-mapping handling.

The base class centralises path-mapping translation so every subclass
(Plex, Emby, Jellyfin) handles webhook paths the same way:

* ``resolve_remote_path_to_item_id(canonical_path)`` expands the
  canonical path through ``expand_path_mapping_candidates`` and calls
  the subclass hook ``_resolve_one_path(server_view_path)`` for each
  candidate. First non-None hit wins.
* ``trigger_refresh(item_id=..., remote_path=...)`` does the same
  expansion and calls ``_trigger_path_refresh`` once per candidate
  (multi-mount nudge fan-out — Plex, Emby, Jellyfin all do this
  identically). When ``item_id`` is supplied it ALSO calls
  ``_trigger_item_refresh`` once.

The tests below run against a deterministic in-memory stub subclass so
the contract surface is asserted without HTTP. Subclass-specific
behaviour (the actual API call inside each hook) is covered by the
existing per-vendor test modules.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from media_preview_generator.servers.base import (
    ConnectionResult,
    Library,
    MediaItem,
    MediaServer,
    ServerConfig,
    ServerType,
    WebhookEvent,
)


class _StubServer(MediaServer):
    """Records calls to the new abstract hooks so the base wrapper's
    behaviour is observable without any HTTP interaction.
    """

    def __init__(
        self,
        path_mappings: list[dict[str, Any]] | None = None,
        resolve_returns: dict[str, str] | None = None,
        path_refresh_raises: dict[str, Exception] | None = None,
    ) -> None:
        super().__init__(server_id="stub-1", name="Stub")
        self._mappings = path_mappings or []
        self._resolve_returns = resolve_returns or {}
        self._path_refresh_raises = path_refresh_raises or {}
        self.resolve_calls: list[str] = []
        self.path_refresh_calls: list[str] = []
        self.item_refresh_calls: list[str] = []

    # ------------------------------------------------------------------
    # Required abstract MediaServer surface (irrelevant to these tests
    # but must exist for instantiation).
    @property
    def type(self) -> ServerType:
        return ServerType.PLEX

    @property
    def path_mappings(self) -> list[dict[str, Any]]:
        return self._mappings

    def test_connection(self) -> ConnectionResult:
        return ConnectionResult(ok=True, message="stub")

    def list_libraries(self) -> list[Library]:
        return []

    def list_items(self, library_id: str) -> Iterator[MediaItem]:
        return iter(())

    def resolve_item_to_remote_path(self, item_id: str) -> str | None:
        return None

    def parse_webhook(self, payload: dict[str, Any] | bytes, headers: dict[str, str]) -> WebhookEvent | None:
        return None

    # ------------------------------------------------------------------
    # The new hooks the base class delegates to.
    def _resolve_one_path(self, server_view_path: str) -> str | None:
        self.resolve_calls.append(server_view_path)
        return self._resolve_returns.get(server_view_path)

    def _trigger_path_refresh(self, server_view_path: str) -> None:
        self.path_refresh_calls.append(server_view_path)
        if server_view_path in self._path_refresh_raises:
            raise self._path_refresh_raises[server_view_path]

    def _trigger_item_refresh(self, item_id: str) -> None:
        self.item_refresh_calls.append(item_id)


# ---------------------------------------------------------------------------
# resolve_remote_path_to_item_id contract
# ---------------------------------------------------------------------------


class TestResolveBaseClassContract:
    """The base class wraps ``_resolve_one_path`` with path-mapping
    translation so subclasses don't each have to re-implement it. This
    is what closes the historic gap where Plex's resolver translated
    paths via ``path_mappings`` but Emby/Jellyfin's did not.
    """

    def test_walks_every_mapped_candidate_when_first_misses(self):
        """canonical /data/x.mkv with mapping /data → /mnt → both
        candidates are tried. First call (`/data/x.mkv`) returns None;
        second call (`/mnt/x.mkv`) returns the id. Result: the id from
        the second candidate; both candidates were visited.
        """
        srv = _StubServer(
            path_mappings=[{"plex_prefix": "/mnt", "local_prefix": "/data"}],
            resolve_returns={"/mnt/x.mkv": "found-via-mnt"},
        )
        result = srv.resolve_remote_path_to_item_id("/data/x.mkv")
        assert result == "found-via-mnt"
        # Both candidates were visited in order — without this the
        # historical gap (Emby Path=<exact> miss when canonical ≠
        # server-stored) returns "False not found" silently.
        assert srv.resolve_calls == ["/data/x.mkv", "/mnt/x.mkv"], (
            f"Expected both candidates to be tried; got {srv.resolve_calls!r}"
        )

    def test_short_circuits_on_first_hit(self):
        """Subclass returns id on the FIRST candidate → second
        candidate is NOT called. Saves a network round-trip when the
        canonical path matches the server's stored path (the dominant
        case for Docker setups where host mount = container mount).
        """
        srv = _StubServer(
            path_mappings=[{"plex_prefix": "/mnt", "local_prefix": "/data"}],
            resolve_returns={"/data/x.mkv": "found-immediately"},
        )
        result = srv.resolve_remote_path_to_item_id("/data/x.mkv")
        assert result == "found-immediately"
        assert srv.resolve_calls == ["/data/x.mkv"], (
            f"Resolver should short-circuit on first hit; got {srv.resolve_calls!r}"
        )

    def test_with_no_mappings_calls_once_with_canonical(self):
        """No path_mappings configured → exactly one call with the raw
        canonical path. Preserves today's behaviour for installs that
        don't use mappings.
        """
        srv = _StubServer(path_mappings=[])
        srv.resolve_remote_path_to_item_id("/data/x.mkv")
        assert srv.resolve_calls == ["/data/x.mkv"]

    def test_returns_none_when_all_candidates_miss(self):
        srv = _StubServer(
            path_mappings=[{"plex_prefix": "/mnt", "local_prefix": "/data"}],
            resolve_returns={},  # nobody knows this path
        )
        assert srv.resolve_remote_path_to_item_id("/data/x.mkv") is None
        assert srv.resolve_calls == ["/data/x.mkv", "/mnt/x.mkv"]

    def test_empty_remote_path_returns_none_without_calling_hook(self):
        srv = _StubServer()
        assert srv.resolve_remote_path_to_item_id("") is None
        assert srv.resolve_calls == []


# ---------------------------------------------------------------------------
# trigger_refresh contract — multi-mount nudge fan-out
# ---------------------------------------------------------------------------


class TestTriggerRefreshBaseClassContract:
    """``trigger_refresh`` fires ``_trigger_path_refresh`` once per
    mapped candidate so multi-disk installs nudge every disk where the
    file might be. Plex's ``trigger_plex_partial_scan`` already did
    this; the base class lifts the pattern up so Emby + Jellyfin
    inherit it automatically.
    """

    def test_fires_one_nudge_per_mapped_candidate(self):
        """3 mapped candidates → 3 path-refresh calls. Mirrors the
        multi-disk Plex scan behaviour visible in production logs:
            [Plex] Triggered partial scan for section 12: /data_16tb/Sports/...
            [Plex] Triggered partial scan for section 12: /data_16tb2/Sports/...
            [Plex] Triggered partial scan for section 12: /data_16tb3/Sports/...
        After this refactor Emby + Jellyfin will fan out the same way.
        """
        srv = _StubServer(
            path_mappings=[
                {"plex_prefix": "/mnt/a", "local_prefix": "/data"},
                {"plex_prefix": "/mnt/b", "local_prefix": "/data"},
            ],
        )
        srv.trigger_refresh(item_id=None, remote_path="/data/x.mkv")
        # 3 candidates: original /data/x.mkv + 2 mapped expansions.
        assert sorted(srv.path_refresh_calls) == sorted(
            [
                "/data/x.mkv",
                "/mnt/a/x.mkv",
                "/mnt/b/x.mkv",
            ]
        ), f"Expected one nudge per candidate; got {srv.path_refresh_calls!r}"

    def test_swallows_per_candidate_exceptions(self):
        """One candidate raises (transient HTTP error, plugin 404) →
        the others still get nudged. Best-effort contract — the goal is
        for the file to be picked up by ANY mapped path's scan, so a
        single failure shouldn't block the rest.
        """
        srv = _StubServer(
            path_mappings=[{"plex_prefix": "/mnt", "local_prefix": "/data"}],
            path_refresh_raises={"/data/x.mkv": RuntimeError("connection refused")},
        )
        # Must not raise.
        srv.trigger_refresh(item_id=None, remote_path="/data/x.mkv")
        # Both candidates were attempted (the failing one was logged + skipped).
        assert sorted(srv.path_refresh_calls) == sorted(["/data/x.mkv", "/mnt/x.mkv"])

    def test_fires_item_refresh_only_when_item_id_present(self):
        srv = _StubServer()
        srv.trigger_refresh(item_id=None, remote_path=None)
        assert srv.item_refresh_calls == []
        assert srv.path_refresh_calls == []

        srv.trigger_refresh(item_id="abc-123", remote_path=None)
        assert srv.item_refresh_calls == ["abc-123"]
        assert srv.path_refresh_calls == []

    def test_fires_both_path_and_item_refresh_when_both_present(self):
        srv = _StubServer()
        srv.trigger_refresh(item_id="abc-123", remote_path="/data/x.mkv")
        assert srv.item_refresh_calls == ["abc-123"]
        assert srv.path_refresh_calls == ["/data/x.mkv"]

    def test_no_path_mappings_with_remote_path_fires_single_nudge(self):
        srv = _StubServer(path_mappings=[])
        srv.trigger_refresh(item_id=None, remote_path="/data/x.mkv")
        assert srv.path_refresh_calls == ["/data/x.mkv"]

    def test_empty_remote_path_skips_path_refresh(self):
        srv = _StubServer()
        srv.trigger_refresh(item_id=None, remote_path="")
        assert srv.path_refresh_calls == []
        assert srv.item_refresh_calls == []


# ---------------------------------------------------------------------------
# Subclass parametric coverage — every concrete server inherits the
# same contract. If any subclass overrides resolve_remote_path_to_item_id
# or trigger_refresh and bypasses the base loop, these fail.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "server_factory_name",
    ["plex", "emby", "jellyfin"],
)
class TestSubclassesInheritBaseContract:
    """Each subclass MUST inherit the base-class wrapper for both
    ``resolve_remote_path_to_item_id`` and ``trigger_refresh``. They're
    free to override ``_resolve_one_path`` / ``_trigger_path_refresh``
    / ``_trigger_item_refresh`` (that's where their vendor-specific
    API calls live), but the public method that does mapping
    translation must not be overridden.
    """

    @staticmethod
    def _make_server(name: str):
        from media_preview_generator.servers import (
            EmbyServer,
            JellyfinServer,
            PlexServer,
        )

        cfg_dict = {
            "id": f"{name}-1",
            "type": getattr(ServerType, name.upper()),
            "name": f"Test {name.capitalize()}",
            "enabled": True,
            "url": "http://localhost:1234",
            "auth": {"method": "api_key", "api_key": "k"} if name in ("emby", "jellyfin") else {"token": "t"},
            "libraries": [],
            "path_mappings": [{"plex_prefix": "/mnt", "local_prefix": "/data"}],
        }
        cfg = ServerConfig(
            id=cfg_dict["id"],
            type=cfg_dict["type"],
            name=cfg_dict["name"],
            enabled=cfg_dict["enabled"],
            url=cfg_dict["url"],
            auth=cfg_dict["auth"],
            libraries=cfg_dict["libraries"],
            path_mappings=cfg_dict["path_mappings"],
        )
        if name == "plex":
            return PlexServer(cfg)
        if name == "emby":
            return EmbyServer(cfg)
        if name == "jellyfin":
            return JellyfinServer(cfg)
        raise ValueError(f"unknown server name {name!r}")

    def test_resolve_walks_mapped_candidates(self, server_factory_name, monkeypatch):
        """Every concrete server must call _resolve_one_path for each
        mapped candidate. Mock the hook to record calls — the public
        method's behaviour is what's asserted, not the per-vendor API.
        """
        srv = self._make_server(server_factory_name)
        calls: list[str] = []

        def fake_resolve_one_path(self_, path):
            calls.append(path)
            return None  # force every candidate to be tried

        monkeypatch.setattr(type(srv), "_resolve_one_path", fake_resolve_one_path)
        result = srv.resolve_remote_path_to_item_id("/data/x.mkv")
        assert result is None
        assert "/data/x.mkv" in calls and "/mnt/x.mkv" in calls, (
            f"{server_factory_name} did not walk mapped candidates; calls={calls!r}"
        )

    def test_trigger_refresh_fires_per_candidate(self, server_factory_name, monkeypatch):
        srv = self._make_server(server_factory_name)
        path_calls: list[str] = []

        def fake_trigger_path_refresh(self_, path):
            path_calls.append(path)

        monkeypatch.setattr(type(srv), "_trigger_path_refresh", fake_trigger_path_refresh)
        srv.trigger_refresh(item_id=None, remote_path="/data/x.mkv")
        assert sorted(path_calls) == sorted(["/data/x.mkv", "/mnt/x.mkv"]), (
            f"{server_factory_name} did not fire per-candidate path refresh; calls={path_calls!r}"
        )
