"""Tests for the server registry: settings.json → live MediaServer clients."""

from __future__ import annotations

import pytest

from plex_generate_previews.servers import (
    Library,
    PlexServer,
    ServerConfig,
    ServerRegistry,
    ServerType,
    UnsupportedServerTypeError,
    server_config_from_dict,
    server_config_to_dict,
)


class TestServerConfigFromDict:
    def test_minimal_entry(self):
        cfg = server_config_from_dict({"id": "s1", "type": "plex", "name": "Plex", "enabled": True, "url": "http://x"})
        assert cfg.id == "s1"
        assert cfg.type is ServerType.PLEX
        assert cfg.libraries == []

    def test_libraries_normalised(self):
        cfg = server_config_from_dict(
            {
                "id": "s1",
                "type": "jellyfin",
                "name": "Jellyfin",
                "enabled": True,
                "url": "http://x",
                "libraries": [
                    {
                        "id": "abc",
                        "name": "Movies",
                        "remote_paths": ["/m"],
                        "enabled": True,
                    },
                    {
                        "id": "def",
                        "name": "TV",
                        "remote_paths": ["/tv"],
                        "enabled": False,
                    },
                ],
            }
        )
        assert len(cfg.libraries) == 2
        assert isinstance(cfg.libraries[0], Library)
        assert cfg.libraries[1].enabled is False

    def test_unknown_type_raises(self):
        with pytest.raises(UnsupportedServerTypeError):
            server_config_from_dict({"id": "s1", "type": "kodi", "name": "K", "url": ""})

    def test_malformed_library_skipped_not_raised(self):
        cfg = server_config_from_dict(
            {
                "id": "s1",
                "type": "plex",
                "name": "Plex",
                "url": "http://x",
                "libraries": [
                    "not-a-dict",
                    {"id": "1", "name": "Good", "remote_paths": ["/m"]},
                ],
            }
        )
        assert len(cfg.libraries) == 1
        assert cfg.libraries[0].name == "Good"


class TestServerConfigRoundTrip:
    def test_to_dict_inverse_of_from_dict(self):
        original_data = {
            "id": "s1",
            "type": "plex",
            "name": "Home",
            "enabled": True,
            "url": "http://x",
            "auth": {"token": "t"},
            "verify_ssl": False,
            "timeout": 90,
            "libraries": [
                {
                    "id": "1",
                    "name": "Movies",
                    "remote_paths": ["/m"],
                    "enabled": True,
                    "kind": "movie",
                }
            ],
            "path_mappings": [{"plex_prefix": "/p", "local_prefix": "/l"}],
            "output": {"adapter": "plex_bundle"},
        }
        cfg = server_config_from_dict(original_data)
        round_tripped = server_config_to_dict(cfg)
        # Library kind survives the round-trip.
        assert round_tripped["libraries"][0]["kind"] == "movie"
        assert round_tripped["url"] == "http://x"
        assert round_tripped["timeout"] == 90


class TestServerRegistryFromSettings:
    def test_loads_plex_server_with_legacy_config(self, mock_config):
        registry = ServerRegistry.from_settings(
            [
                {
                    "id": "plex-default",
                    "type": "plex",
                    "name": "Home Plex",
                    "enabled": True,
                    "url": "http://plex:32400",
                    "auth": {"token": "t"},
                }
            ],
            legacy_config=mock_config,
        )
        servers = registry.servers()
        assert len(servers) == 1
        assert isinstance(servers[0], PlexServer)
        assert servers[0].id == "plex-default"
        assert servers[0].name == "Home Plex"

    def test_skips_unsupported_types_with_warning(self, mock_config, caplog):
        registry = ServerRegistry.from_settings(
            [
                {
                    "id": "plex-default",
                    "type": "plex",
                    "name": "Plex",
                    "enabled": True,
                    "url": "http://plex:32400",
                    "auth": {},
                },
                {
                    "id": "jelly-1",
                    "type": "jellyfin",  # Phase 3 — not yet supported
                    "name": "Jellyfin",
                    "enabled": True,
                    "url": "http://jellyfin:8096",
                    "auth": {},
                },
            ],
            legacy_config=mock_config,
        )
        # Plex and Emby ship in Phase 1/2; Jellyfin is skipped until Phase 3.
        assert [s.id for s in registry.servers()] == ["plex-default"]
        # ServerConfig for skipped server is still in the configs list so
        # ownership decisions can include it later, but the live client map
        # omits it.
        assert {c.id for c in registry.configs()} == {"plex-default", "jelly-1"}

    def test_unknown_type_string_skipped(self, mock_config):
        registry = ServerRegistry.from_settings(
            [{"id": "x", "type": "kodi", "name": "K", "url": ""}],
            legacy_config=mock_config,
        )
        assert registry.servers() == []
        assert registry.configs() == []

    def test_empty_input_yields_empty_registry(self, mock_config):
        registry = ServerRegistry.from_settings([], legacy_config=mock_config)
        assert registry.servers() == []
        assert registry.find_owning_servers("/anything") == []


class TestServerRegistryFromLegacyConfig:
    def test_synthesises_single_plex_server(self, mock_config):
        registry = ServerRegistry.from_legacy_config(mock_config)
        servers = registry.servers()
        assert len(servers) == 1
        assert isinstance(servers[0], PlexServer)

    def test_returns_empty_when_no_legacy_plex(self, mock_config):
        mock_config.plex_url = ""
        mock_config.plex_token = ""
        registry = ServerRegistry.from_legacy_config(mock_config)
        assert registry.servers() == []


class TestServerRegistryAccessors:
    def test_get_returns_live_client(self, mock_config):
        registry = ServerRegistry.from_legacy_config(mock_config)
        plex = registry.get("plex-default")
        assert isinstance(plex, PlexServer)

    def test_get_unknown_returns_none(self, mock_config):
        registry = ServerRegistry.from_legacy_config(mock_config)
        assert registry.get("nope") is None

    def test_get_config_includes_disabled_servers(self, mock_config):
        registry = ServerRegistry.from_settings(
            [
                {
                    "id": "plex-default",
                    "type": "plex",
                    "name": "Plex",
                    "enabled": False,
                    "url": "http://plex:32400",
                    "auth": {},
                }
            ],
            legacy_config=mock_config,
        )
        cfg = registry.get_config("plex-default")
        assert isinstance(cfg, ServerConfig)
        assert cfg.enabled is False


class TestFindOwningServers:
    def test_dispatches_to_underlying_resolver(self, mock_config):
        registry = ServerRegistry.from_settings(
            [
                {
                    "id": "plex-default",
                    "type": "plex",
                    "name": "Plex",
                    "enabled": True,
                    "url": "http://plex:32400",
                    "auth": {},
                    "libraries": [
                        {
                            "id": "1",
                            "name": "Movies",
                            "remote_paths": ["/data/movies"],
                            "enabled": True,
                        }
                    ],
                }
            ],
            legacy_config=mock_config,
        )
        matches = registry.find_owning_servers("/data/movies/Foo.mkv")
        assert [m.server_id for m in matches] == ["plex-default"]

        no_match = registry.find_owning_servers("/elsewhere/Foo.mkv")
        assert no_match == []
