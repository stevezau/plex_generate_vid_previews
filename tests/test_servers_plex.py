"""Tests for the :class:`plex_generate_previews.servers.plex.PlexServer` wrapper.

The wrapper is a thin façade over the existing ``plex_client`` helpers, so
these tests verify the *interface translation*: that the abstract methods
delegate correctly and convert results to the new dataclass types.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
import requests

from plex_generate_previews.servers import (
    ConnectionResult,
    MediaItem,
    PlexServer,
    ServerType,
    WebhookEvent,
)


@pytest.fixture
def plex_wrapper(mock_config):
    """Construct a :class:`PlexServer` from the standard ``mock_config``."""
    return PlexServer(mock_config, server_id="plex-test", name="Test Plex")


class TestConstruction:
    def test_implements_media_server(self, plex_wrapper):
        from plex_generate_previews.servers import MediaServer

        assert isinstance(plex_wrapper, MediaServer)

    def test_type_is_plex(self, plex_wrapper):
        assert plex_wrapper.type is ServerType.PLEX

    def test_id_and_name_propagate(self, plex_wrapper):
        assert plex_wrapper.id == "plex-test"
        assert plex_wrapper.name == "Test Plex"

    def test_construction_does_not_connect(self, mock_config):
        # Constructing a wrapper should be cheap; no plexapi import call yet.
        with patch("plex_generate_previews.plex_client.plex_server") as connect:
            PlexServer(mock_config)
            connect.assert_not_called()


class TestTestConnection:
    def test_success_carries_identity(self, plex_wrapper):
        with patch("plex_generate_previews.servers.plex.requests.get") as get:
            response = MagicMock()
            response.json.return_value = {
                "MediaContainer": {
                    "machineIdentifier": "abc123",
                    "friendlyName": "Home Plex",
                    "version": "1.40.0",
                }
            }
            response.raise_for_status.return_value = None
            get.return_value = response

            result = plex_wrapper.test_connection()

        assert isinstance(result, ConnectionResult)
        assert result.ok is True
        assert result.server_id == "abc123"
        assert result.server_name == "Home Plex"
        assert result.version == "1.40.0"

    def test_missing_credentials_short_circuits(self, mock_config):
        mock_config.plex_url = ""
        mock_config.plex_token = ""
        wrapper = PlexServer(mock_config)

        result = wrapper.test_connection()

        assert result.ok is False
        assert "required" in result.message.lower()

    def test_timeout_returns_failure(self, plex_wrapper):
        with patch("plex_generate_previews.servers.plex.requests.get") as get:
            get.side_effect = requests.exceptions.Timeout()

            result = plex_wrapper.test_connection()

        assert result.ok is False
        assert "timed out" in result.message.lower()

    def test_unauthorized_returns_specific_message(self, plex_wrapper):
        with patch("plex_generate_previews.servers.plex.requests.get") as get:
            err_response = MagicMock(status_code=401)
            err = requests.exceptions.HTTPError(response=err_response)
            response = MagicMock()
            response.raise_for_status.side_effect = err
            get.return_value = response

            result = plex_wrapper.test_connection()

        assert result.ok is False
        assert "401" in result.message

    def test_ssl_error_returns_specific_message(self, plex_wrapper):
        with patch("plex_generate_previews.servers.plex.requests.get") as get:
            get.side_effect = requests.exceptions.SSLError("bad cert")

            result = plex_wrapper.test_connection()

        assert result.ok is False
        assert "ssl" in result.message.lower()


class TestListLibraries:
    def test_returns_library_objects_with_enabled_filter_by_id(self, mock_config):
        mock_config.plex_libraries = []
        mock_config.plex_library_ids = ["1"]
        wrapper = PlexServer(mock_config)

        section_movies = MagicMock()
        section_movies.key = 1
        section_movies.title = "Movies"
        section_movies.locations = ["/media/movies"]
        section_movies.METADATA_TYPE = "movie"

        section_tv = MagicMock()
        section_tv.key = 2
        section_tv.title = "TV Shows"
        section_tv.locations = ["/media/tv"]
        section_tv.METADATA_TYPE = "episode"

        plex = MagicMock()
        plex.library.sections.return_value = [section_movies, section_tv]
        wrapper._plex = plex

        libs = wrapper.list_libraries()

        assert len(libs) == 2
        by_name = {lib.name: lib for lib in libs}
        assert by_name["Movies"].enabled is True
        assert by_name["TV Shows"].enabled is False
        assert by_name["Movies"].remote_paths == ("/media/movies",)
        assert by_name["Movies"].kind == "movie"

    def test_returns_library_objects_with_enabled_filter_by_title(self, mock_config):
        mock_config.plex_libraries = ["movies"]
        mock_config.plex_library_ids = None
        wrapper = PlexServer(mock_config)

        section_movies = MagicMock()
        section_movies.key = 1
        section_movies.title = "Movies"
        section_movies.locations = ["/m"]
        section_movies.METADATA_TYPE = "movie"

        section_tv = MagicMock()
        section_tv.key = 2
        section_tv.title = "TV Shows"
        section_tv.locations = ["/tv"]
        section_tv.METADATA_TYPE = "episode"

        plex = MagicMock()
        plex.library.sections.return_value = [section_movies, section_tv]
        wrapper._plex = plex

        libs = wrapper.list_libraries()
        by_name = {lib.name: lib for lib in libs}
        assert by_name["Movies"].enabled is True
        assert by_name["TV Shows"].enabled is False

    def test_no_filter_means_all_enabled(self, mock_config):
        mock_config.plex_libraries = []
        mock_config.plex_library_ids = None
        wrapper = PlexServer(mock_config)

        section = MagicMock()
        section.key = 99
        section.title = "Anime"
        section.locations = ["/a"]
        section.METADATA_TYPE = "episode"

        plex = MagicMock()
        plex.library.sections.return_value = [section]
        wrapper._plex = plex

        libs = wrapper.list_libraries()
        assert libs[0].enabled is True

    def test_returns_empty_list_on_failure(self, plex_wrapper):
        plex = MagicMock()
        plex.library.sections.side_effect = RuntimeError("boom")
        plex_wrapper._plex = plex

        libs = plex_wrapper.list_libraries()
        assert libs == []


class TestListItems:
    def test_yields_movies(self, mock_config, mock_plex_movie):
        wrapper = PlexServer(mock_config)
        section = MagicMock()
        section.key = 1
        section.title = "Movies"
        section.METADATA_TYPE = "movie"
        section.search.return_value = [mock_plex_movie]

        plex = MagicMock()
        plex.library.sections.return_value = [section]
        wrapper._plex = plex

        items = list(wrapper.list_items("1"))
        assert len(items) == 1
        assert isinstance(items[0], MediaItem)
        assert items[0].title == "Test Movie"
        assert items[0].library_id == "1"
        assert items[0].remote_path.endswith(".mkv")

    def test_yields_episodes_with_formatted_title(self, mock_config, mock_plex_episode):
        wrapper = PlexServer(mock_config)
        section = MagicMock()
        section.key = 2
        section.title = "TV Shows"
        section.METADATA_TYPE = "episode"
        section.search.return_value = [mock_plex_episode]

        plex = MagicMock()
        plex.library.sections.return_value = [section]
        wrapper._plex = plex

        items = list(wrapper.list_items("2"))
        assert len(items) == 1
        assert "Test Show" in items[0].title
        assert "S01E01" in items[0].title.upper()

    def test_unknown_library_yields_nothing(self, mock_config):
        wrapper = PlexServer(mock_config)
        plex = MagicMock()
        plex.library.sections.return_value = []
        wrapper._plex = plex

        assert list(wrapper.list_items("missing")) == []


class TestResolveItemToRemotePath:
    def test_returns_first_part_path(self, plex_wrapper):
        part = MagicMock()
        part.file = "/media/foo.mkv"
        media = MagicMock()
        media.parts = [part]
        item = MagicMock()
        item.media = [media]

        plex = MagicMock()
        plex.fetchItem.return_value = item
        plex_wrapper._plex = plex

        assert plex_wrapper.resolve_item_to_remote_path("42") == "/media/foo.mkv"
        plex.fetchItem.assert_called_once_with(42)

    def test_non_numeric_id_returns_none(self, plex_wrapper):
        plex_wrapper._plex = MagicMock()
        assert plex_wrapper.resolve_item_to_remote_path("abc") is None

    def test_lookup_failure_returns_none(self, plex_wrapper):
        plex = MagicMock()
        plex.fetchItem.side_effect = RuntimeError("not found")
        plex_wrapper._plex = plex

        assert plex_wrapper.resolve_item_to_remote_path("42") is None

    def test_no_media_parts_returns_none(self, plex_wrapper):
        item = MagicMock()
        item.media = []
        plex = MagicMock()
        plex.fetchItem.return_value = item
        plex_wrapper._plex = plex

        assert plex_wrapper.resolve_item_to_remote_path("42") is None


class TestTriggerRefresh:
    def test_dispatches_to_partial_scan(self, plex_wrapper):
        with patch("plex_generate_previews.plex_client.trigger_plex_partial_scan") as scan:
            plex_wrapper.trigger_refresh(item_id=None, remote_path="/m/foo.mkv")

            scan.assert_called_once()
            kwargs = scan.call_args.kwargs
            assert kwargs["unresolved_paths"] == ["/m/foo.mkv"]
            assert kwargs["plex_url"] == plex_wrapper.config.plex_url
            assert kwargs["plex_token"] == plex_wrapper.config.plex_token

    def test_no_path_no_op(self, plex_wrapper):
        with patch("plex_generate_previews.plex_client.trigger_plex_partial_scan") as scan:
            plex_wrapper.trigger_refresh(item_id="42", remote_path=None)
            scan.assert_not_called()

    def test_swallows_exceptions(self, plex_wrapper):
        with patch("plex_generate_previews.plex_client.trigger_plex_partial_scan") as scan:
            scan.side_effect = RuntimeError("network is down")
            # Must not raise — the dispatcher relies on this being best-effort.
            plex_wrapper.trigger_refresh(item_id=None, remote_path="/m/foo.mkv")


class TestParseWebhook:
    def test_library_new_with_rating_key(self, plex_wrapper):
        payload = {
            "event": "library.new",
            "Metadata": {"ratingKey": "12345", "type": "episode"},
            "Server": {"uuid": "abc123", "title": "Home Plex"},
        }
        ev = plex_wrapper.parse_webhook(payload, headers={})
        assert isinstance(ev, WebhookEvent)
        assert ev.event_type == "library.new"
        assert ev.item_id == "12345"
        assert ev.remote_path is None

    def test_irrelevant_event_returns_none(self, plex_wrapper):
        for event_type in ["media.play", "media.stop", "media.pause", "media.resume"]:
            payload = {"event": event_type, "Metadata": {"ratingKey": "1"}}
            assert plex_wrapper.parse_webhook(payload, headers={}) is None

    def test_accepts_raw_bytes(self, plex_wrapper):
        body = json.dumps({"event": "library.new", "Metadata": {"ratingKey": "7"}}).encode("utf-8")
        ev = plex_wrapper.parse_webhook(body, headers={})
        assert ev is not None
        assert ev.item_id == "7"

    def test_invalid_json_bytes_returns_none(self, plex_wrapper):
        assert plex_wrapper.parse_webhook(b"not-json{", headers={}) is None

    def test_non_dict_payload_returns_none(self, plex_wrapper):
        assert plex_wrapper.parse_webhook("string-payload", headers={}) is None  # type: ignore[arg-type]

    def test_missing_rating_key_yields_none_item_id(self, plex_wrapper):
        ev = plex_wrapper.parse_webhook({"event": "library.new", "Metadata": {}}, headers={})
        assert ev is not None
        assert ev.item_id is None
