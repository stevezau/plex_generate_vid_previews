"""Tests for the server registry: settings.json → live MediaServer clients."""

from __future__ import annotations

import pytest

from media_preview_generator.servers import (
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

    def test_unknown_server_type_skipped_with_warning(self, mock_config):
        """Unknown vendor type is dropped AND a warning is logged.

        Audit fix — original used the ``caplog`` fixture but never asserted
        on its contents (loguru doesn't emit through stdlib logging anyway).
        Either drop the fixture OR assert the warning. Asserting the
        warning catches a regression to silent-drop without log line —
        operator would have no idea their config row was skipped.
        """
        from loguru import logger as _loguru_logger

        captured: list[str] = []
        sink_id = _loguru_logger.add(lambda msg: captured.append(str(msg)), level="DEBUG")
        try:
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
                        "id": "kodi-1",
                        "type": "kodi",  # not a supported vendor
                        "name": "Kodi",
                        "url": "http://kodi",
                    },
                ],
                legacy_config=mock_config,
            )
        finally:
            _loguru_logger.remove(sink_id)
        assert [s.id for s in registry.servers()] == ["plex-default"]
        assert {c.id for c in registry.configs()} == {"plex-default"}
        # The unknown vendor MUST surface a log line so operators can debug
        # why their kodi entry was silently dropped from processing.
        assert any("kodi" in line.lower() for line in captured), (
            f"unknown server type 'kodi' was silently skipped — operator gets no diagnostic. "
            f"captured log lines: {captured!r}"
        )

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

    def test_loads_plex_server_without_legacy_config(self):
        """Regression for the 'PlexServer requires a legacy Config' bug.

        The Preview Inspector instantiates the registry with no legacy_config
        — we must NOT throw UnsupportedServerTypeError. PlexServer accepts
        ServerConfig directly now and synthesizes the legacy shape internally.
        """
        registry = ServerRegistry.from_settings(
            [
                {
                    "id": "plex-default",
                    "type": "plex",
                    "name": "Home Plex",
                    "enabled": True,
                    "url": "http://plex:32400",
                    "auth": {"method": "token", "token": "t-xyz"},
                    "verify_ssl": True,
                    "timeout": 15,
                    "output": {"plex_config_folder": "/plex", "frame_interval": 5},
                    "libraries": [
                        {"id": "1", "name": "Movies", "remote_paths": ["/m"], "enabled": True},
                        {"id": "2", "name": "TV", "remote_paths": ["/tv"], "enabled": False},
                    ],
                }
            ],
            # No legacy_config — used to raise UnsupportedServerTypeError.
        )
        servers = registry.servers()
        assert len(servers) == 1
        plex = servers[0]
        assert isinstance(plex, PlexServer)
        assert plex.id == "plex-default"
        assert plex.name == "Home Plex"
        # Synthesized legacy shape exposes the per-server fields.
        assert plex._config.plex_url == "http://plex:32400"
        assert plex._config.plex_token == "t-xyz"
        assert plex._config.plex_verify_ssl is True
        assert plex._config.plex_timeout == 15
        assert plex._config.plex_config_folder == "/plex"
        assert plex._config.plex_bif_frame_interval == 5
        # Only enabled libraries flow through to the legacy id list.
        assert plex._config.plex_library_ids == ["1"]


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
