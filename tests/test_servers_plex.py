"""Tests for the :class:`media_preview_generator.servers.plex.PlexServer` wrapper.

The wrapper is a thin façade over the existing ``plex_client`` helpers, so
these tests verify the *interface translation*: that the abstract methods
delegate correctly and convert results to the new dataclass types.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
import requests

from media_preview_generator.servers import (
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
        from media_preview_generator.servers import MediaServer

        assert isinstance(plex_wrapper, MediaServer)

    def test_type_is_plex(self, plex_wrapper):
        assert plex_wrapper.type is ServerType.PLEX

    def test_id_and_name_propagate(self, plex_wrapper):
        assert plex_wrapper.id == "plex-test"
        assert plex_wrapper.name == "Test Plex"

    def test_construction_does_not_connect(self, mock_config):
        # Constructing a wrapper should be cheap; no plexapi import call yet.
        with patch("media_preview_generator.plex_client.plex_server") as connect:
            PlexServer(mock_config)
            connect.assert_not_called()


class TestTestConnection:
    def test_success_carries_identity(self, plex_wrapper):
        with patch("media_preview_generator.servers.plex.requests.get") as get:
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
        with patch("media_preview_generator.servers.plex.requests.get") as get:
            get.side_effect = requests.exceptions.Timeout()

            result = plex_wrapper.test_connection()

        assert result.ok is False
        assert "timed out" in result.message.lower()

    def test_unauthorized_returns_specific_message(self, plex_wrapper):
        with patch("media_preview_generator.servers.plex.requests.get") as get:
            err_response = MagicMock(status_code=401)
            err = requests.exceptions.HTTPError(response=err_response)
            response = MagicMock()
            response.raise_for_status.side_effect = err
            get.return_value = response

            result = plex_wrapper.test_connection()

        assert result.ok is False
        assert "401" in result.message

    def test_ssl_error_returns_specific_message(self, plex_wrapper):
        with patch("media_preview_generator.servers.plex.requests.get") as get:
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

    def test_explicit_per_library_disabled_via_server_config(self, mock_config):
        """Modern multi-server path: per-library ``enabled=False`` from
        ``ServerConfig.libraries`` must be respected, even when ALL libraries
        are unticked.

        Regression: previously the synthesised ``plex_library_ids`` list was
        the only signal — empty meant "no filter, enable everything", so
        unticking the user's only library silently re-enabled it. The
        Plex-pinned scan would then walk every library the user thought
        they had disabled.
        """
        from media_preview_generator.servers.base import Library as LibCfg
        from media_preview_generator.servers.base import ServerConfig, ServerType

        sc = ServerConfig(
            id="plex-x",
            type=ServerType.PLEX,
            name="Plex X",
            enabled=True,
            url="http://plex:32400",
            auth={"token": "tok"},
            libraries=[
                LibCfg(id="1", name="Movies", enabled=False, remote_paths=()),
                LibCfg(id="2", name="TV Shows", enabled=True, remote_paths=()),
            ],
        )
        wrapper = PlexServer(sc)

        section_movies = MagicMock()
        section_movies.key = 1
        section_movies.title = "Movies"
        section_movies.locations = []
        section_movies.METADATA_TYPE = "movie"
        section_tv = MagicMock()
        section_tv.key = 2
        section_tv.title = "TV Shows"
        section_tv.locations = []
        section_tv.METADATA_TYPE = "episode"
        section_anime = MagicMock()
        section_anime.key = 99
        section_anime.title = "Anime"
        section_anime.locations = []
        section_anime.METADATA_TYPE = "episode"

        plex = MagicMock()
        plex.library.sections.return_value = [section_movies, section_tv, section_anime]
        wrapper._plex = plex

        libs = wrapper.list_libraries()
        by_name = {lib.name: lib.enabled for lib in libs}
        assert by_name["Movies"] is False, "Unticked library must stay disabled"
        assert by_name["TV Shows"] is True, "Ticked library must stay enabled"
        # Library not in the snapshot at all → treat as disabled (user hasn't
        # consciously opted in). Important for the case where the vendor adds
        # a new library MPG hasn't seen yet.
        assert by_name["Anime"] is False

    def test_all_libraries_unticked_means_all_disabled(self, mock_config):
        """If every per-library tick is False, no library is enabled — full stop.

        Sister-regression to the explicit-disabled test: this one ensures the
        all-unticked case doesn't fall through to the legacy "no filter →
        all enabled" branch.
        """
        from media_preview_generator.servers.base import Library as LibCfg
        from media_preview_generator.servers.base import ServerConfig, ServerType

        sc = ServerConfig(
            id="plex-x",
            type=ServerType.PLEX,
            name="Plex X",
            enabled=True,
            url="http://plex:32400",
            auth={"token": "tok"},
            libraries=[
                LibCfg(id="1", name="Movies", enabled=False, remote_paths=()),
                LibCfg(id="2", name="TV Shows", enabled=False, remote_paths=()),
            ],
        )
        wrapper = PlexServer(sc)

        section_movies = MagicMock()
        section_movies.key = 1
        section_movies.title = "Movies"
        section_movies.locations = []
        section_movies.METADATA_TYPE = "movie"
        section_tv = MagicMock()
        section_tv.key = 2
        section_tv.title = "TV Shows"
        section_tv.locations = []
        section_tv.METADATA_TYPE = "episode"

        plex = MagicMock()
        plex.library.sections.return_value = [section_movies, section_tv]
        wrapper._plex = plex

        libs = wrapper.list_libraries()
        assert all(not lib.enabled for lib in libs), (
            f"Every library must be disabled when all are unticked; got: {[(lib.name, lib.enabled) for lib in libs]}"
        )

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
        # Regression: id must be the bare ratingKey, not the full
        # "/library/metadata/<id>" URL — passing the URL doubles the
        # prefix in PlexBundleAdapter and reports skipped_not_indexed.
        assert items[0].id == "54321"

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
        assert items[0].id == "12345"

    def test_falls_back_to_key_when_ratingkey_missing(self, mock_config):
        """``_plex_item_id`` strips ``/library/metadata/`` from ``m.key`` when
        ``ratingKey`` is unavailable — defensive against custom plexapi shims."""
        wrapper = PlexServer(mock_config)
        movie = MagicMock(spec=["key", "title", "locations"])
        movie.key = "/library/metadata/777"
        movie.title = "Legacy"
        movie.locations = ["/data/x.mkv"]

        section = MagicMock()
        section.key = 1
        section.METADATA_TYPE = "movie"
        section.search.return_value = [movie]

        plex = MagicMock()
        plex.library.sections.return_value = [section]
        wrapper._plex = plex

        items = list(wrapper.list_items("1"))
        assert items[0].id == "777"

    def test_unknown_library_yields_nothing(self, mock_config):
        wrapper = PlexServer(mock_config)
        plex = MagicMock()
        plex.library.sections.return_value = []
        wrapper._plex = plex

        assert list(wrapper.list_items("missing")) == []

    def test_captures_bundle_metadata_from_plexapi_parts(self, mock_config):
        """Enumeration must capture ``item.media[*].parts[*].(hash, file)``.

        plexapi's ``section.search()`` returns Movie objects with their
        Media + MediaPart already loaded — including the bundle ``hash``
        attribute. Capturing it here lets PlexBundleAdapter skip the
        per-item ``/library/metadata/{id}/tree`` round-trip; without
        capture, a 9981-item full-library scan paid 9981 sequential
        round-trips for hashes that ``section.search()`` already returned.
        """
        wrapper = PlexServer(mock_config)
        # Build a movie with a real .media[*].parts[*] structure (the bare
        # MagicMock fixture skips this — getattr would return a MagicMock
        # the iteration helper wouldn't decode correctly).
        part = MagicMock()
        part.hash = "abcdef0123456789"
        part.file = "/data/movies/Foo (2024)/Foo.mkv"
        media = MagicMock()
        media.parts = [part]
        movie = MagicMock(spec=["key", "ratingKey", "title", "locations", "media"])
        movie.key = "/library/metadata/54321"
        movie.ratingKey = 54321
        movie.title = "Foo (2024)"
        movie.locations = ["/data/movies/Foo (2024)/Foo.mkv"]
        movie.media = [media]

        section = MagicMock()
        section.key = 1
        section.METADATA_TYPE = "movie"
        section.search.return_value = [movie]

        plex = MagicMock()
        plex.library.sections.return_value = [section]
        wrapper._plex = plex

        items = list(wrapper.list_items("1"))
        assert len(items) == 1
        assert items[0].bundle_metadata == (("abcdef0123456789", "/data/movies/Foo (2024)/Foo.mkv"),), (
            "list_items must capture (hash, file) from item.media[*].parts[*] "
            "so PlexBundleAdapter can skip /tree per item — see commit "
            "introducing _extract_plex_bundle_metadata."
        )


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


class TestGetBundleMetadata:
    """D31 — get_bundle_metadata is the canary's path to Plex's bundle hash.

    Critical regression test: the f-string used to build /tree's URL must
    NEVER double-prefix /library/metadata/. The bug we shipped silently
    in production (and only caught after 3 days of users reporting
    "skipped_not_indexed" on every Sonarr/Radarr → Plex webhook):

        item_id="/library/metadata/557676"        # caller passed full path
        f"/library/metadata/{item_id}/tree"        # naive f-string
        → "/library/metadata//library/metadata/557676/tree"
        → 404 silently swallowed, reported as "no MediaPart with bundle hash yet"

    These tests assert the URL passed to plex.query() is exactly correct
    for both the bare-ratingKey input ("557676") AND the URL-form input
    ("/library/metadata/557676") that webhook resolution accidentally fed
    in for months.
    """

    def _xml_with_part(self, hash_value: str, file_path: str):
        from xml.etree import ElementTree as ET

        root = ET.fromstring(
            f"<MediaContainer><MetadataItem><MediaItem>"
            f'<MediaPart hash="{hash_value}" file="{file_path}" />'
            f"</MediaItem></MetadataItem></MediaContainer>"
        )
        return root

    def test_bare_rating_key_builds_correct_url(self, plex_wrapper):
        plex = MagicMock()
        plex.query.return_value = self._xml_with_part("abc123", "/data/foo.mkv")
        plex_wrapper._plex = plex

        result = plex_wrapper.get_bundle_metadata("557676")

        # Exact URL — must NOT have any duplicated prefix.
        plex.query.assert_called_once_with("/library/metadata/557676/tree")
        assert result == [("abc123", "/data/foo.mkv")]

    def test_full_path_form_does_not_double_the_prefix(self, plex_wrapper):
        """The bug: webhook resolution used to pass /library/metadata/<id>
        as the item_id. Without normalisation the URL became
        /library/metadata//library/metadata/<id>/tree → 404. This test
        proves both input shapes produce the SAME, single-prefix URL."""
        plex = MagicMock()
        plex.query.return_value = self._xml_with_part("abc123", "/data/foo.mkv")
        plex_wrapper._plex = plex

        result = plex_wrapper.get_bundle_metadata("/library/metadata/557676")

        plex.query.assert_called_once_with("/library/metadata/557676/tree")
        # No "/library/metadata//library/metadata/..." anywhere.
        called_url = plex.query.call_args.args[0]
        assert "//library/metadata" not in called_url, (
            f"URL doubled the prefix: {called_url!r} — webhooks would 404 silently"
        )
        assert called_url.count("/library/metadata/") == 1
        assert result == [("abc123", "/data/foo.mkv")]

    def test_extracts_every_mediapart_with_hash(self, plex_wrapper):
        """Multi-part items (e.g. multi-disc movies) report several MediaParts."""
        from xml.etree import ElementTree as ET

        plex = MagicMock()
        plex.query.return_value = ET.fromstring(
            "<MediaContainer><MetadataItem><MediaItem>"
            '<MediaPart hash="hash1" file="/data/disc1.mkv" />'
            '<MediaPart hash="hash2" file="/data/disc2.mkv" />'
            "</MediaItem></MetadataItem></MediaContainer>"
        )
        plex_wrapper._plex = plex

        result = plex_wrapper.get_bundle_metadata("12345")

        assert ("hash1", "/data/disc1.mkv") in result
        assert ("hash2", "/data/disc2.mkv") in result

    def test_skips_mediaparts_with_empty_hash(self, plex_wrapper):
        """A MediaPart without a hash attribute means deep analysis hasn't
        completed for that part. These should be filtered, not crash."""
        from xml.etree import ElementTree as ET

        plex = MagicMock()
        plex.query.return_value = ET.fromstring(
            "<MediaContainer><MetadataItem><MediaItem>"
            '<MediaPart file="/data/no-hash.mkv" />'
            '<MediaPart hash="hash2" file="/data/has-hash.mkv" />'
            "</MediaItem></MetadataItem></MediaContainer>"
        )
        plex_wrapper._plex = plex

        result = plex_wrapper.get_bundle_metadata("12345")

        assert result == [("hash2", "/data/has-hash.mkv")]

    def test_query_failure_returns_empty_list(self, plex_wrapper):
        """When the /tree query raises (404, network, anything), return []
        so the publisher routes the file to the slow-backoff retry queue
        instead of crashing the dispatcher."""
        plex = MagicMock()
        plex.query.side_effect = Exception("(404) not_found")
        plex_wrapper._plex = plex

        assert plex_wrapper.get_bundle_metadata("12345") == []

    def test_empty_item_id_returns_empty_without_query(self, plex_wrapper):
        """Empty/None item_id must NOT issue a malformed query."""
        plex = MagicMock()
        plex_wrapper._plex = plex

        assert plex_wrapper.get_bundle_metadata("") == []
        assert plex_wrapper.get_bundle_metadata(None) == []  # type: ignore[arg-type]
        plex.query.assert_not_called()


class TestTriggerRefresh:
    def test_dispatches_to_partial_scan(self, plex_wrapper):
        with patch("media_preview_generator.plex_client.trigger_plex_partial_scan") as scan:
            plex_wrapper.trigger_refresh(item_id=None, remote_path="/m/foo.mkv")

            scan.assert_called_once()
            kwargs = scan.call_args.kwargs
            assert kwargs["unresolved_paths"] == ["/m/foo.mkv"]
            assert kwargs["plex_url"] == plex_wrapper.config.plex_url
            assert kwargs["plex_token"] == plex_wrapper.config.plex_token

    def test_no_path_no_op(self, plex_wrapper):
        with patch("media_preview_generator.plex_client.trigger_plex_partial_scan") as scan:
            plex_wrapper.trigger_refresh(item_id="42", remote_path=None)
            scan.assert_not_called()

    def test_swallows_exceptions(self, plex_wrapper):
        with patch("media_preview_generator.plex_client.trigger_plex_partial_scan") as scan:
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
