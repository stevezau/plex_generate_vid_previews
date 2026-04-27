"""Tests for the multi-server library ownership resolver.

Covers the three cases the dispatcher must distinguish:

1. Path is under no enabled library on this server → skip permanently.
2. Path is under an enabled library → publish (slow-backoff retry if not
   yet indexed by the server itself).
3. Disabled servers and disabled libraries are excluded by both functions.
"""

from __future__ import annotations

import pytest

from plex_generate_previews.servers import (
    Library,
    OwnershipMatch,
    ServerConfig,
    ServerType,
    find_owning_servers,
    server_owns_path,
)


def _server(
    *,
    server_id: str = "s1",
    name: str = "Server",
    enabled: bool = True,
    libraries: list[Library] | None = None,
    path_mappings: list[dict] | None = None,
    server_type: ServerType = ServerType.PLEX,
) -> ServerConfig:
    return ServerConfig(
        id=server_id,
        type=server_type,
        name=name,
        enabled=enabled,
        url="http://x",
        auth={},
        libraries=libraries or [],
        path_mappings=path_mappings or [],
    )


class TestServerOwnsPath:
    def test_match_under_enabled_library(self):
        server = _server(libraries=[Library(id="1", name="Movies", remote_paths=("/data/movies",), enabled=True)])
        match = server_owns_path("/data/movies/Foo (2024)/Foo (2024).mkv", server)
        assert isinstance(match, OwnershipMatch)
        assert match.server_id == "s1"
        assert match.library_id == "1"
        assert match.library_name == "Movies"

    def test_no_match_when_path_not_under_any_library(self):
        server = _server(libraries=[Library(id="1", name="Movies", remote_paths=("/data/movies",), enabled=True)])
        assert server_owns_path("/data/tv/Show/S01E01.mkv", server) is None

    def test_disabled_library_does_not_match(self):
        server = _server(
            libraries=[
                Library(id="1", name="Movies", remote_paths=("/data/movies",), enabled=False),
                Library(id="2", name="TV", remote_paths=("/data/tv",), enabled=True),
            ]
        )
        # Movies is disabled — don't match.
        assert server_owns_path("/data/movies/Foo.mkv", server) is None
        # TV is enabled — match.
        match = server_owns_path("/data/tv/Show/S01E01.mkv", server)
        assert match is not None
        assert match.library_id == "2"

    def test_disabled_server_never_owns(self):
        server = _server(
            enabled=False,
            libraries=[Library(id="1", name="Movies", remote_paths=("/data/movies",), enabled=True)],
        )
        assert server_owns_path("/data/movies/Foo.mkv", server) is None

    def test_folder_boundary_prevents_partial_prefix_match(self):
        """`/data/movies` must not match `/data/movies-archive/...`."""
        server = _server(libraries=[Library(id="1", name="Movies", remote_paths=("/data/movies",), enabled=True)])
        assert server_owns_path("/data/movies-archive/Foo.mkv", server) is None
        assert server_owns_path("/data/movies/Foo.mkv", server) is not None

    def test_first_matching_library_wins(self):
        """When two libraries cover the same path, the first one matches."""
        server = _server(
            libraries=[
                Library(id="1", name="Movies", remote_paths=("/data/movies",), enabled=True),
                Library(id="2", name="4K Movies", remote_paths=("/data/movies",), enabled=True),
            ]
        )
        match = server_owns_path("/data/movies/Foo.mkv", server)
        assert match is not None
        assert match.library_id == "1"

    def test_multiple_remote_paths_in_library(self):
        server = _server(
            libraries=[
                Library(
                    id="1",
                    name="Movies",
                    remote_paths=("/data/4k", "/data/movies"),
                    enabled=True,
                )
            ]
        )
        match = server_owns_path("/data/movies/Foo.mkv", server)
        assert match is not None
        assert match.local_prefix == "/data/movies"

    def test_path_mapping_translates_remote_to_local(self):
        """Server reports `/media/movies`; on disk it is `/data/movies`."""
        server = _server(
            libraries=[Library(id="1", name="Movies", remote_paths=("/media/movies",), enabled=True)],
            path_mappings=[{"remote_prefix": "/media", "local_prefix": "/data"}],
        )
        match = server_owns_path("/data/movies/Foo.mkv", server)
        assert match is not None
        # The local_prefix returned reflects the *local* path the dispatcher
        # uses, not the server's view.
        assert match.local_prefix.startswith("/data")

    def test_legacy_plex_prefix_mapping_key_supported(self):
        """Legacy mapping rows used `plex_prefix` instead of `remote_prefix`."""
        server = _server(
            libraries=[Library(id="1", name="Movies", remote_paths=("/media/movies",), enabled=True)],
            path_mappings=[{"plex_prefix": "/media", "local_prefix": "/data"}],
        )
        assert server_owns_path("/data/movies/Foo.mkv", server) is not None

    def test_no_libraries_means_no_match(self):
        """A server without libraries owns no path — explicit empty enabled set."""
        server = _server(libraries=[])
        assert server_owns_path("/data/movies/Foo.mkv", server) is None


class TestFindOwningServers:
    def test_fan_out_to_multiple_servers_for_shared_volume(self):
        """Plex A, Jellyfin, and Emby share `/data`; Plex B has its own `/storage`."""
        plex_a = _server(
            server_id="plex-a",
            libraries=[
                Library(id="1", name="Movies", remote_paths=("/data/movies",), enabled=True),
                Library(id="2", name="TV", remote_paths=("/data/tv",), enabled=True),
            ],
        )
        plex_b = _server(
            server_id="plex-b",
            libraries=[Library(id="3", name="Movies", remote_paths=("/storage/movies",), enabled=True)],
        )
        jellyfin = _server(
            server_id="jf",
            server_type=ServerType.JELLYFIN,
            libraries=[Library(id="9", name="Movies", remote_paths=("/data/movies",), enabled=True)],
        )
        emby = _server(
            server_id="em",
            server_type=ServerType.EMBY,
            libraries=[Library(id="7", name="TV", remote_paths=("/data/tv",), enabled=True)],
        )

        # A new file under /data/movies should fan out to plex-a and jellyfin.
        matches = find_owning_servers(
            "/data/movies/Foo (2024)/Foo (2024).mkv",
            [plex_a, plex_b, jellyfin, emby],
        )
        ids = [m.server_id for m in matches]
        assert ids == ["plex-a", "jf"]  # order preserved from input

    def test_path_only_in_plex_b(self):
        plex_a = _server(
            server_id="plex-a",
            libraries=[Library(id="1", name="Movies", remote_paths=("/data/movies",), enabled=True)],
        )
        plex_b = _server(
            server_id="plex-b",
            libraries=[Library(id="3", name="Movies", remote_paths=("/storage/movies",), enabled=True)],
        )
        matches = find_owning_servers("/storage/movies/Foo.mkv", [plex_a, plex_b])
        assert [m.server_id for m in matches] == ["plex-b"]

    def test_no_servers_own_path_returns_empty(self):
        plex_a = _server(
            server_id="plex-a",
            libraries=[Library(id="1", name="Movies", remote_paths=("/data/movies",), enabled=True)],
        )
        matches = find_owning_servers("/elsewhere/Foo.mkv", [plex_a])
        assert matches == []

    def test_disabled_servers_excluded_from_fan_out(self):
        plex_a = _server(
            server_id="plex-a",
            libraries=[Library(id="1", name="Movies", remote_paths=("/data/movies",), enabled=True)],
        )
        plex_b = _server(
            server_id="plex-b",
            enabled=False,
            libraries=[Library(id="3", name="Movies", remote_paths=("/data/movies",), enabled=True)],
        )
        matches = find_owning_servers("/data/movies/Foo.mkv", [plex_a, plex_b])
        assert [m.server_id for m in matches] == ["plex-a"]


class TestEdgeCases:
    @pytest.mark.parametrize(
        "remote_paths",
        [
            (),
            ("",),
            ("   ",),
        ],
    )
    def test_empty_remote_paths_never_match(self, remote_paths):
        server = _server(libraries=[Library(id="1", name="X", remote_paths=remote_paths)])
        assert server_owns_path("/anything", server) is None

    def test_canonical_path_with_trailing_slash_does_not_match_directory_as_file(self):
        """``server_owns_path`` is a *file* matcher; the dispatcher feeds it
        canonical file paths, not directory paths. We don't require special
        handling for trailing slashes — paths just compare as-is."""
        server = _server(libraries=[Library(id="1", name="Movies", remote_paths=("/data/movies",), enabled=True)])
        # A pseudo-file path under the library still matches.
        assert server_owns_path("/data/movies/Foo (2024)/Foo (2024).mkv", server) is not None
