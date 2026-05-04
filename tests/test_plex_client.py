"""
Tests for plex_client.py module.

Tests Plex server connection, retry logic, library queries,
and duplicate filtering.
"""

import os
import xml.etree.ElementTree as ET
from unittest.mock import MagicMock, call, patch

import pytest
import requests

from media_preview_generator.config.paths import (
    expand_path_mapping_candidates,
    plex_path_to_local,
)
from media_preview_generator.plex_client import (
    VIDEO_EXTENSIONS,
    WebhookResolutionResult,
    _detect_path_prefix_mismatches,
    _expand_directory_to_media_files,
    _map_plex_path_to_local,
    _mismatch_covered_by_mappings,
    filter_duplicate_locations,
    get_library_sections,
    get_media_items_by_paths,
    plex_server,
    retry_plex_call,
    trigger_plex_partial_scan,
)


class TestPlexServerConnection:
    """Test Plex server connection."""

    @patch("plexapi.server.PlexServer")
    @patch("requests.Session")
    def test_plex_server_connection_success(self, mock_session, mock_plex_server, mock_config):
        """Test successful connection to Plex server.

        Pin the kwargs the SUT controls: URL/token (positional), timeout, and the
        configured session. A regression that flipped any of these to a default
        (e.g. dropping the timeout, or constructing a fresh session that bypasses
        retry/SSL config) would still satisfy a bare ``assert_called_once``.
        """
        mock_plex = MagicMock()
        mock_plex_server.return_value = mock_plex
        session_instance = mock_session.return_value

        result = plex_server(mock_config)

        assert result is mock_plex
        mock_plex_server.assert_called_once_with(
            mock_config.plex_url,
            mock_config.plex_token,
            timeout=mock_config.plex_timeout,
            session=session_instance,
        )

    @patch("plexapi.server.PlexServer")
    @patch("requests.Session")
    def test_plex_server_connection_failure(self, mock_session, mock_plex_server, mock_config):
        """Test connection error handling."""
        mock_plex_server.side_effect = requests.exceptions.ConnectionError("Connection refused")

        with pytest.raises(ConnectionError):
            plex_server(mock_config)

    @patch("plexapi.server.PlexServer")
    @patch("requests.Session")
    def test_plex_server_timeout(self, mock_session, mock_plex_server, mock_config):
        """Test timeout handling."""
        mock_plex_server.side_effect = requests.exceptions.ReadTimeout("Timeout")

        with pytest.raises(ConnectionError):
            plex_server(mock_config)

    @patch("plexapi.server.PlexServer")
    def test_plex_server_retry_strategy(self, mock_plex_server, mock_config):
        """Verify the Session is mounted with an HTTPAdapter whose Retry config
        matches what plex_client wants — total=3, backoff_factor=0.3, and
        the 5xx status_forcelist. Just asserting "Session was called" let
        a regression where retries are silently disabled slip through.
        """
        from requests.adapters import HTTPAdapter

        mock_plex_server.return_value = MagicMock()

        with patch("media_preview_generator.plex_client.requests.Session") as mock_session_cls:
            session_instance = MagicMock()
            mock_session_cls.return_value = session_instance

            plex_server(mock_config)

        # Session.mount should be called for both http and https with the
        # SAME HTTPAdapter instance.
        mount_calls = session_instance.mount.call_args_list
        schemes = sorted(call.args[0] for call in mount_calls)
        assert schemes == ["http://", "https://"], f"expected both http:// and https:// mounted; got {schemes}"

        adapters = [call.args[1] for call in mount_calls]
        assert all(isinstance(a, HTTPAdapter) for a in adapters), "non-HTTPAdapter mounted on session"
        # Same adapter instance used for both schemes (single Retry config).
        assert adapters[0] is adapters[1]

        retry = adapters[0].max_retries
        assert retry.total == 3, f"Retry.total expected 3, got {retry.total}"
        assert retry.backoff_factor == 0.3, f"Retry.backoff_factor expected 0.3, got {retry.backoff_factor}"
        assert sorted(retry.status_forcelist) == [500, 502, 503, 504], (
            f"Retry.status_forcelist expected [500,502,503,504], got {sorted(retry.status_forcelist)}"
        )

    @patch("plexapi.server.PlexServer")
    @patch("requests.Session")
    def test_plex_server_respects_ssl_verify_setting(self, mock_session, mock_plex_server, mock_config):
        """Test that session.verify follows config.plex_verify_ssl."""
        mock_config.plex_verify_ssl = False
        mock_plex_server.return_value = MagicMock()

        plex_server(mock_config)

        session_instance = mock_session.return_value
        assert session_instance.verify is False


class TestRetryPlexCall:
    """Test Plex API retry logic."""

    def test_retry_plex_call_success(self):
        """Test call succeeds on first try."""
        mock_func = MagicMock(return_value="success")

        result = retry_plex_call(mock_func, "arg1", "arg2", kwarg1="value1")

        assert result == "success"
        mock_func.assert_called_once_with("arg1", "arg2", kwarg1="value1")

    def test_retry_plex_call_xml_error_retry(self):
        """Test retry on XML parse error."""
        mock_func = MagicMock()
        # Fail first time, succeed second time
        mock_func.side_effect = [ET.ParseError("syntax error"), "success"]

        result = retry_plex_call(mock_func, max_retries=2, retry_delay=0.01)

        assert result == "success"
        assert mock_func.call_count == 2

    def test_retry_plex_call_max_retries(self):
        """Test give up after max retries."""
        mock_func = MagicMock()
        mock_func.side_effect = ET.ParseError("syntax error")

        with pytest.raises(ET.ParseError):
            retry_plex_call(mock_func, max_retries=2, retry_delay=0.01)

        # Should try 3 times (initial + 2 retries)
        assert mock_func.call_count == 3

    def test_retry_plex_call_non_xml_error(self):
        """Test non-XML errors are not retried."""
        mock_func = MagicMock()
        mock_func.side_effect = ValueError("Some other error")

        with pytest.raises(ValueError):
            retry_plex_call(mock_func, max_retries=2)

        # Should only try once (no retry for non-XML errors)
        assert mock_func.call_count == 1


class TestTriggerPlexPartialScan:
    """Test targeted Plex partial scan behavior for unresolved webhook paths."""

    @patch("requests.get")
    def test_empty_input_returns_empty_without_http_calls(self, mock_get):
        """Empty unresolved path list should short-circuit."""
        result = trigger_plex_partial_scan(
            plex_url="http://plex:32400",
            plex_token="token",
            unresolved_paths=[],
        )

        assert result == []
        mock_get.assert_not_called()

    @patch("requests.get")
    def test_longest_prefix_match_wins(self, mock_get):
        """More specific section root should be chosen before a broader one."""
        sections_response = MagicMock()
        sections_response.raise_for_status = MagicMock()
        sections_response.json.return_value = {
            "MediaContainer": {
                "Directory": [
                    {"key": "1", "Location": [{"path": "/data"}]},
                    {"key": "2", "Location": [{"path": "/data/tv"}]},
                ]
            }
        }
        refresh_response = MagicMock(status_code=200)
        mock_get.side_effect = [sections_response, refresh_response]

        result = trigger_plex_partial_scan(
            plex_url="http://plex:32400",
            plex_token="token",
            unresolved_paths=["/data/tv/Show/Season 01/Episode 01.mkv"],
        )

        assert result == ["/data/tv/Show/Season 01/Episode 01.mkv"]
        assert mock_get.call_args_list[1] == call(
            "http://plex:32400/library/sections/2/refresh",
            params={"path": "/data/tv/Show"},
            headers={"X-Plex-Token": "token"},
            timeout=10,
            verify=True,
        )

    @patch("requests.get")
    def test_path_mapping_expansion_triggers_scan_for_mapped_plex_path(self, mock_get):
        """Webhook aliases should expand into Plex-native paths before scanning."""
        sections_response = MagicMock()
        sections_response.raise_for_status = MagicMock()
        sections_response.json.return_value = {
            "MediaContainer": {
                "Directory": [
                    {"key": "9", "Location": [{"path": "/data_16tb/tv"}]},
                ]
            }
        }
        refresh_response = MagicMock(status_code=200)
        mock_get.side_effect = [sections_response, refresh_response]

        result = trigger_plex_partial_scan(
            plex_url="https://plex.example:32400",
            plex_token="token",
            unresolved_paths=["/data/tv/Example Show/Season 01/S01E01.mkv"],
            path_mappings=[
                {
                    "plex_prefix": "/data_16tb",
                    "local_prefix": "/mnt/media",
                    "webhook_prefixes": ["/data"],
                }
            ],
            verify_ssl=False,
        )

        assert result == ["/data/tv/Example Show/Season 01/S01E01.mkv"]
        assert mock_get.call_args_list[0].kwargs["verify"] is False
        assert mock_get.call_args_list[1] == call(
            "https://plex.example:32400/library/sections/9/refresh",
            params={"path": "/data_16tb/tv/Example Show"},
            headers={"X-Plex-Token": "token"},
            timeout=10,
            verify=False,
        )

    @pytest.mark.parametrize(
        ("unresolved_path", "location_path", "expected_scan_folder"),
        [
            (
                "/data/tv/Test Show/Season 01/Test Show - S01E01.mkv",
                "/data/tv",
                "/data/tv/Test Show",
            ),
            (
                "/data/movies/Test Movie (2024)/Test Movie (2024).mkv",
                "/data/movies",
                "/data/movies/Test Movie (2024)",
            ),
        ],
    )
    @patch("requests.get")
    def test_scan_folder_targets_series_or_movie_root(
        self,
        mock_get,
        unresolved_path,
        location_path,
        expected_scan_folder,
    ):
        """Partial scan should target the top-level show/movie folder."""
        sections_response = MagicMock()
        sections_response.raise_for_status = MagicMock()
        sections_response.json.return_value = {
            "MediaContainer": {"Directory": [{"key": "3", "Location": [{"path": location_path}]}]}
        }
        refresh_response = MagicMock(status_code=200)
        mock_get.side_effect = [sections_response, refresh_response]

        result = trigger_plex_partial_scan(
            plex_url="http://plex:32400",
            plex_token="token",
            unresolved_paths=[unresolved_path],
        )

        assert result == [unresolved_path]
        assert mock_get.call_args_list[1].kwargs["params"] == {"path": expected_scan_folder}

    @patch("requests.get")
    def test_sections_request_error_returns_empty(self, mock_get):
        """Section lookup failures should be logged and treated as non-fatal."""
        mock_get.side_effect = requests.RequestException("boom")

        result = trigger_plex_partial_scan(
            plex_url="http://plex:32400",
            plex_token="token",
            unresolved_paths=["/data/tv/Show/Season 01/Episode 01.mkv"],
        )

        assert result == []

    @patch("requests.get")
    def test_refresh_http_error_is_handled_gracefully(self, mock_get):
        """Non-200 refresh responses should not be reported as scanned."""
        sections_response = MagicMock()
        sections_response.raise_for_status = MagicMock()
        sections_response.json.return_value = {
            "MediaContainer": {"Directory": [{"key": "7", "Location": [{"path": "/data/movies"}]}]}
        }
        refresh_response = MagicMock(status_code=500)
        mock_get.side_effect = [sections_response, refresh_response]

        result = trigger_plex_partial_scan(
            plex_url="http://plex:32400",
            plex_token="token",
            unresolved_paths=["/data/movies/Broken Movie/Broken Movie.mkv"],
        )

        assert result == []

    @patch("requests.get")
    def test_multi_drive_scans_all_matching_candidates(self, mock_get):
        """Expanded path mappings should scan every matching Plex drive root."""
        sections_response = MagicMock()
        sections_response.raise_for_status = MagicMock()
        sections_response.json.return_value = {
            "MediaContainer": {
                "Directory": [
                    {"key": "1", "Location": [{"path": "/drive1/tv"}]},
                    {"key": "2", "Location": [{"path": "/drive2/tv"}]},
                    {"key": "3", "Location": [{"path": "/drive3/tv"}]},
                ]
            }
        }
        mock_get.side_effect = [
            sections_response,
            MagicMock(status_code=200),
            MagicMock(status_code=200),
            MagicMock(status_code=200),
        ]

        result = trigger_plex_partial_scan(
            plex_url="http://plex:32400",
            plex_token="token",
            unresolved_paths=["/data/tv/Test Show/Season 01/Test Show - S01E01.mkv"],
            path_mappings=[
                {
                    "plex_prefix": "/drive1",
                    "local_prefix": "/drive1",
                    "webhook_prefixes": ["/data"],
                },
                {
                    "plex_prefix": "/drive2",
                    "local_prefix": "/drive2",
                    "webhook_prefixes": ["/data"],
                },
                {
                    "plex_prefix": "/drive3",
                    "local_prefix": "/drive3",
                    "webhook_prefixes": ["/data"],
                },
            ],
        )

        assert result == ["/data/tv/Test Show/Season 01/Test Show - S01E01.mkv"]
        assert mock_get.call_args_list[1:] == [
            call(
                "http://plex:32400/library/sections/1/refresh",
                params={"path": "/drive1/tv/Test Show"},
                headers={"X-Plex-Token": "token"},
                timeout=10,
                verify=True,
            ),
            call(
                "http://plex:32400/library/sections/2/refresh",
                params={"path": "/drive2/tv/Test Show"},
                headers={"X-Plex-Token": "token"},
                timeout=10,
                verify=True,
            ),
            call(
                "http://plex:32400/library/sections/3/refresh",
                params={"path": "/drive3/tv/Test Show"},
                headers={"X-Plex-Token": "token"},
                timeout=10,
                verify=True,
            ),
        ]


class TestFilterDuplicateLocations:
    """Test duplicate location filtering."""

    def test_filter_duplicate_locations(self):
        """Test basic duplicate filtering."""
        media_items = [
            ("key1", ["/path/to/video1.mkv"], "Show S01E01", "episode"),
            ("key2", ["/path/to/video2.mkv"], "Show S01E02", "episode"),
            (
                "key3",
                ["/path/to/video1.mkv"],
                "Show S01E01 Part 2",
                "episode",
            ),  # Duplicate!
        ]

        filtered = filter_duplicate_locations(media_items)

        # Should only have 2 items (duplicate removed)
        assert len(filtered) == 2
        assert ("key1", "Show S01E01", "episode") in filtered
        assert ("key2", "Show S01E02", "episode") in filtered
        assert ("key3", "Show S01E01 Part 2", "episode") not in filtered

    def test_filter_duplicate_locations_multiple_files(self):
        """Test filtering with multi-part episodes."""
        media_items = [
            (
                "key1",
                ["/path/to/video1.mkv", "/path/to/video2.mkv"],
                "Show S01E01-E02",
                "episode",
            ),
            (
                "key2",
                ["/path/to/video2.mkv", "/path/to/video3.mkv"],
                "Show S01E02-E03",
                "episode",
            ),  # Overlaps!
        ]

        filtered = filter_duplicate_locations(media_items)

        # Second item should be filtered out due to overlap
        assert len(filtered) == 1
        assert ("key1", "Show S01E01-E02", "episode") in filtered

    def test_filter_duplicate_locations_empty(self):
        """Test filtering empty list."""
        filtered = filter_duplicate_locations([])
        assert filtered == []

    def test_filter_duplicate_locations_no_duplicates(self):
        """Test filtering with no duplicates."""
        media_items = [
            ("key1", ["/path/to/video1.mkv"], "Movie 1", "movie"),
            ("key2", ["/path/to/video2.mkv"], "Movie 2", "movie"),
            ("key3", ["/path/to/video3.mkv"], "Movie 3", "movie"),
        ]

        filtered = filter_duplicate_locations(media_items)

        assert len(filtered) == 3


class TestGetLibrarySections:
    """Test library section retrieval."""

    def test_get_library_sections_movies(self, mock_config):
        """Test getting movie libraries.

        D31 — assert the BARE ratingKey is stored, not the URL form. Production
        code now mirrors the webhook resolver's behaviour (uses ratingKey, falls
        back to parsing the trailing segment of m.key). Mocks set BOTH attributes
        so this test fails loudly if production ever picks .key again.
        """
        mock_plex = MagicMock()

        # Mock section
        mock_section = MagicMock()
        mock_section.title = "Movies"
        mock_section.METADATA_TYPE = "movie"

        # Mock movie — real plexapi populates both attrs.
        mock_movie = MagicMock()
        mock_movie.ratingKey = 1
        mock_movie.key = "/library/metadata/1"
        mock_movie.title = "Test Movie"

        mock_section.search.return_value = [mock_movie]
        mock_plex.library.sections.return_value = [mock_section]

        sections = list(get_library_sections(mock_plex, mock_config))

        assert len(sections) == 1
        section, media = sections[0]
        assert section == mock_section
        assert len(media) == 1
        # BARE ratingKey, NOT the URL form — anything else doubles the prefix
        # downstream when PlexBundleAdapter builds f"/library/metadata/{id}/tree".
        assert media[0][0] == "1"
        assert "/" not in media[0][0], (
            f"item id contains '/' ({media[0][0]!r}) — would double the URL prefix in /tree query"
        )
        assert media[0][1] == "Test Movie"
        assert media[0][2] == "movie"

    def test_get_library_sections_random_skips_plex_sort_param(self, mock_config):
        """With sort_by='random', no Plex-side sort kwarg is passed — the orchestrator shuffles instead."""
        mock_config.sort_by = "random"
        mock_plex = MagicMock()

        mock_section = MagicMock()
        mock_section.title = "Movies"
        mock_section.METADATA_TYPE = "movie"

        mock_movie = MagicMock()
        mock_movie.ratingKey = 1
        mock_movie.key = "/library/metadata/1"
        mock_movie.title = "Test Movie"
        mock_section.search.return_value = [mock_movie]
        mock_plex.library.sections.return_value = [mock_section]

        list(get_library_sections(mock_plex, mock_config))

        mock_section.search.assert_called_once()
        _, kwargs = mock_section.search.call_args
        assert "sort" not in kwargs

    def test_get_library_sections_newest_passes_plex_sort_param(self, mock_config):
        """sort_by='newest' still asks Plex to sort by addedAt desc (unchanged behaviour)."""
        mock_config.sort_by = "newest"
        mock_plex = MagicMock()

        mock_section = MagicMock()
        mock_section.title = "Movies"
        mock_section.METADATA_TYPE = "movie"

        mock_movie = MagicMock()
        mock_movie.ratingKey = 1
        mock_movie.key = "/library/metadata/1"
        mock_movie.title = "Test Movie"
        mock_section.search.return_value = [mock_movie]
        mock_plex.library.sections.return_value = [mock_section]

        list(get_library_sections(mock_plex, mock_config))

        _, kwargs = mock_section.search.call_args
        assert kwargs.get("sort") == "addedAt:desc"


class TestPathMappingProduction:
    """Tests against the real path-mapping helpers in ``config.paths``.

    The previous ``TestPathMapping`` class only exercised ``str.replace`` —
    it never invoked any project code, and one case actively bug-locked the
    wrong behaviour (``/database`` -> ``/mediabase``). These tests pin the
    contract of the production helpers (``plex_path_to_local``,
    ``expand_path_mapping_candidates``) and the ``_map_plex_path_to_local``
    wrapper used by ``plex_client``.
    """

    def _row(self, plex_prefix: str, local_prefix: str, webhook_prefixes=None) -> dict:
        """Build a single normalised path-mapping row."""
        return {
            "plex_prefix": plex_prefix,
            "local_prefix": local_prefix,
            "webhook_prefixes": list(webhook_prefixes or []),
        }

    def test_plex_path_to_local_basic_mapping(self):
        """Standard Plex prefix -> local prefix substitution."""
        mappings = [self._row("/data", "/media")]
        assert plex_path_to_local("/data/Movies/x.mkv", mappings) == "/media/Movies/x.mkv"

    def test_plex_path_to_local_partial_prefix_avoidance(self):
        """``/database`` must NOT be mapped by a ``/data`` rule.

        Pins the production fix to the bug-locking case the deleted
        ``test_path_mapping_partial_match_avoided`` froze in place. If
        ``_path_matches_prefix`` regresses to a substring check this fails.
        """
        mappings = [self._row("/data", "/media")]
        assert plex_path_to_local("/database/x.mkv", mappings) == "/database/x.mkv"

    def test_plex_path_to_local_trailing_slash_equivalence(self):
        """``/data`` and ``/data/`` in the prefix produce the same output."""
        without_slash = [self._row("/data", "/media")]
        with_slash = [self._row("/data/", "/media/")]
        assert plex_path_to_local("/data/Movies/x.mkv", without_slash) == "/media/Movies/x.mkv"
        assert plex_path_to_local("/data/Movies/x.mkv", with_slash) == "/media/Movies/x.mkv"
        assert plex_path_to_local("/data/Movies/x.mkv", without_slash) == plex_path_to_local(
            "/data/Movies/x.mkv", with_slash
        )

    def test_plex_path_to_local_no_mappings_returns_input(self):
        """Empty mapping list returns the path unchanged."""
        assert plex_path_to_local("/anywhere/x.mkv", []) == "/anywhere/x.mkv"

    def test_plex_path_to_local_nested_paths_preserved(self):
        """Deeply nested suffix is preserved verbatim after the prefix swap."""
        mappings = [self._row("/mnt/user/media", "/media")]
        actual = plex_path_to_local("/mnt/user/media/tv/show/Episode.mkv", mappings)
        assert actual == "/media/tv/show/Episode.mkv"

    def test_plex_path_to_local_case_sensitivity_preserved(self):
        """Linux semantics: case mismatch means no mapping is applied."""
        mappings = [self._row("/data", "/media")]
        # ``/Data`` differs from ``/data`` by case — no mapping should fire.
        assert plex_path_to_local("/Data/x.mkv", mappings) == "/Data/x.mkv"

    def test_map_plex_path_to_local_wrapper_forwards_to_helper(self, mock_config):
        """``_map_plex_path_to_local`` reads ``config.path_mappings`` and delegates.

        Smoke-tests the plex_client wrapper at lines 415-418 — confirms the
        wrapper actually forwards the configured mappings instead of swallowing
        them. With no mappings, returns input unchanged.
        """
        mock_config.path_mappings = [self._row("/data", "/media")]
        assert _map_plex_path_to_local("/data/Movies/x.mkv", mock_config) == "/media/Movies/x.mkv"

        mock_config.path_mappings = []
        assert _map_plex_path_to_local("/data/Movies/x.mkv", mock_config) == "/data/Movies/x.mkv"

    def test_expand_path_mapping_candidates_bidirectional_fanout(self):
        """A mapping row fans out a path into both Plex and local equivalents.

        Whichever side of the mapping the input path matches, the helper must
        produce the equivalent on the other side. The original input is always
        first, and duplicates are de-duplicated.
        """
        mappings = [self._row("/data", "/media")]

        from_plex = expand_path_mapping_candidates("/data/Movies/x.mkv", mappings)
        assert from_plex[0] == "/data/Movies/x.mkv"
        assert "/media/Movies/x.mkv" in from_plex

        from_local = expand_path_mapping_candidates("/media/Movies/x.mkv", mappings)
        assert from_local[0] == "/media/Movies/x.mkv"
        assert "/data/Movies/x.mkv" in from_local

        # No duplicates introduced.
        assert len(from_plex) == len(set(from_plex))
        assert len(from_local) == len(set(from_local))

    def test_expand_path_mapping_candidates_webhook_alias(self):
        """Webhook prefix should expand into the local-prefix equivalent.

        Row: webhook ``/data`` aliases the on-disk ``/data_16tb`` mount. A
        webhook payload of ``/data/x.mkv`` must produce a ``/data_16tb/x.mkv``
        candidate so we can match it against the actual file.
        """
        mappings = [
            self._row("/plex_data", "/data_16tb", webhook_prefixes=["/data"]),
        ]
        candidates = expand_path_mapping_candidates("/data/x.mkv", mappings)
        assert candidates[0] == "/data/x.mkv"
        assert "/data_16tb/x.mkv" in candidates
        # Webhook -> Plex form also fans out (used for cross-matching against
        # Plex-reported locations).
        assert "/plex_data/x.mkv" in candidates


class TestGetLibrarySectionsExtended:
    """Extended tests for library section retrieval."""

    def test_get_library_sections_episodes(self, mock_config):
        """Test getting TV show libraries.

        D31 — assert the BARE ratingKey is stored, not the URL form (see
        ``test_get_library_sections_movies`` for the rationale).
        """
        mock_plex = MagicMock()

        # Mock section
        mock_section = MagicMock()
        mock_section.title = "TV Shows"
        mock_section.METADATA_TYPE = "episode"

        # Mock episode — real plexapi populates both attrs.
        mock_episode = MagicMock()
        mock_episode.ratingKey = 123
        mock_episode.key = "/library/metadata/123"
        mock_episode.grandparentTitle = "Test Show"
        mock_episode.seasonEpisode = "s01e01"
        mock_episode.locations = ["/path/to/show.mkv"]

        mock_section.search.return_value = [mock_episode]
        mock_plex.library.sections.return_value = [mock_section]

        sections = list(get_library_sections(mock_plex, mock_config))

        assert len(sections) == 1
        section, media = sections[0]
        assert section == mock_section
        assert len(media) == 1
        # BARE ratingKey, NOT the URL form — see test_get_library_sections_movies.
        assert media[0][0] == "123"
        assert "/" not in media[0][0]
        assert "Test Show" in media[0][1]
        assert "S01E01" in media[0][1]
        assert media[0][2] == "episode"

    def test_get_library_sections_filter(self, mock_config):
        """Test filtering by configured libraries."""
        mock_config.plex_libraries = ["movies"]

        mock_plex = MagicMock()

        # Mock two sections
        mock_section_movies = MagicMock()
        mock_section_movies.title = "Movies"
        mock_section_movies.METADATA_TYPE = "movie"

        mock_section_tv = MagicMock()
        mock_section_tv.title = "TV Shows"
        mock_section_tv.METADATA_TYPE = "episode"

        mock_plex.library.sections.return_value = [mock_section_movies, mock_section_tv]

        sections = list(get_library_sections(mock_plex, mock_config))

        # Should only include Movies
        assert len(sections) == 1
        assert sections[0][0].title == "Movies"

    def test_get_library_sections_unsupported(self, mock_config):
        """Test skipping unsupported library types."""
        mock_plex = MagicMock()

        # Mock photo section (unsupported)
        mock_section = MagicMock()
        mock_section.title = "Photos"
        mock_section.METADATA_TYPE = "photo"

        mock_plex.library.sections.return_value = [mock_section]

        sections = list(get_library_sections(mock_plex, mock_config))

        # Should skip photos
        assert len(sections) == 0

    def test_get_library_sections_api_error(self, mock_config):
        """Test handling of API errors."""
        mock_plex = MagicMock()
        mock_plex.library.sections.side_effect = requests.exceptions.RequestException("API error")

        sections = list(get_library_sections(mock_plex, mock_config))

        # Should handle error gracefully
        assert sections == []

    def test_get_library_sections_search_error(self, mock_config):
        """Test handling of search errors."""
        import requests

        mock_plex = MagicMock()

        mock_section = MagicMock()
        mock_section.title = "Movies"
        mock_section.METADATA_TYPE = "movie"
        # Use a specific exception type that's caught by the code
        mock_section.search.side_effect = requests.exceptions.RequestException("Search failed")

        mock_plex.library.sections.return_value = [mock_section]

        sections = list(get_library_sections(mock_plex, mock_config))

        # Should handle error and skip this library
        assert len(sections) == 0

    def test_get_library_sections_duplicate_filtering(self, mock_config):
        """Test that duplicate episodes are filtered."""
        mock_plex = MagicMock()

        mock_section = MagicMock()
        mock_section.title = "TV Shows"
        mock_section.METADATA_TYPE = "episode"

        # Two episodes pointing to same file (multi-part episode).
        # Mocks set both .ratingKey and .key (real plexapi shape).
        mock_episode1 = MagicMock()
        mock_episode1.ratingKey = 1
        mock_episode1.key = "/library/metadata/1"
        mock_episode1.grandparentTitle = "Show"
        mock_episode1.seasonEpisode = "s01e01"
        mock_episode1.locations = ["/path/to/episode.mkv"]

        mock_episode2 = MagicMock()
        mock_episode2.ratingKey = 2
        mock_episode2.key = "/library/metadata/2"
        mock_episode2.grandparentTitle = "Show"
        mock_episode2.seasonEpisode = "s01e02"
        mock_episode2.locations = ["/path/to/episode.mkv"]  # Same file!

        mock_section.search.return_value = [mock_episode1, mock_episode2]
        mock_plex.library.sections.return_value = [mock_section]

        sections = list(get_library_sections(mock_plex, mock_config))

        # Should only have 1 episode (duplicate filtered)
        assert len(sections) == 1
        section, media = sections[0]
        assert len(media) == 1

    def test_get_library_sections_cancel_before_section(self, mock_config):
        """Test that cancel_check aborts before processing a section."""
        mock_plex = MagicMock()

        mock_section = MagicMock()
        mock_section.title = "Movies"
        mock_section.METADATA_TYPE = "movie"
        mock_section.search.return_value = []

        mock_plex.library.sections.return_value = [mock_section]

        sections = list(get_library_sections(mock_plex, mock_config, cancel_check=lambda: True))

        assert sections == []
        mock_section.search.assert_not_called()

    def test_get_library_sections_cancel_after_retrieval(self, mock_config):
        """Test that cancel_check aborts after retrieving a section's items."""
        mock_plex = MagicMock()

        mock_section1 = MagicMock()
        mock_section1.title = "Movies"
        mock_section1.key = "1"
        mock_section1.METADATA_TYPE = "movie"
        mock_movie = MagicMock()
        mock_movie.ratingKey = 1
        mock_movie.key = "/library/metadata/1"
        mock_movie.title = "Test Movie"
        mock_section1.search.return_value = [mock_movie]

        mock_section2 = MagicMock()
        mock_section2.title = "TV Shows"
        mock_section2.key = "2"
        mock_section2.METADATA_TYPE = "episode"
        mock_section2.search.return_value = []

        mock_plex.library.sections.return_value = [mock_section1, mock_section2]

        call_count = 0

        def cancel_after_first():
            nonlocal call_count
            call_count += 1
            # First call (before section1): not cancelled
            # Second call (after section1 retrieval): cancel
            return call_count > 1

        sections = list(get_library_sections(mock_plex, mock_config, cancel_check=cancel_after_first))

        # Only the first section should have been returned before cancellation
        assert len(sections) == 0  # cancelled after retrieval, before yield
        mock_section1.search.assert_called_once()
        mock_section2.search.assert_not_called()

    def test_get_library_sections_no_cancel_check(self, mock_config):
        """Test that get_library_sections works normally without cancel_check."""
        mock_plex = MagicMock()

        mock_section = MagicMock()
        mock_section.title = "Movies"
        mock_section.METADATA_TYPE = "movie"
        mock_movie = MagicMock()
        mock_movie.ratingKey = 1
        mock_movie.key = "/library/metadata/1"
        mock_movie.title = "Test Movie"
        mock_section.search.return_value = [mock_movie]

        mock_plex.library.sections.return_value = [mock_section]

        sections = list(get_library_sections(mock_plex, mock_config))

        assert len(sections) == 1

    def test_get_library_sections_progress_callback(self, mock_config):
        """progress_callback receives a 'Listing' tick and one per in-scope section."""
        mock_plex = MagicMock()
        mock_config.plex_libraries = []
        mock_config.plex_library_ids = []

        section1 = MagicMock()
        section1.title = "Movies"
        section1.key = 1
        section1.METADATA_TYPE = "movie"
        section1.search.return_value = []

        section2 = MagicMock()
        section2.title = "TV"
        section2.key = 2
        section2.METADATA_TYPE = "episode"
        section2.search.return_value = []

        mock_plex.library.sections.return_value = [section1, section2]

        progress = MagicMock()
        list(get_library_sections(mock_plex, mock_config, progress_callback=progress))

        messages = [call.args[2] for call in progress.call_args_list if call.args]
        assert any("Listing Plex libraries" in m for m in messages)
        assert any("Querying library 'Movies' (1/2)" in m for m in messages)
        assert any("Querying library 'TV' (2/2)" in m for m in messages)


class TestGetMediaItemsByPaths:
    """Test webhook path-to-Plex-item resolution."""

    def test_get_media_items_by_paths_empty(self, mock_config):
        """Empty path list returns result with empty items."""
        mock_plex = MagicMock()
        result = get_media_items_by_paths(mock_plex, mock_config, [])
        assert isinstance(result, WebhookResolutionResult)
        assert result.items == []
        assert result.unresolved_paths == []
        assert result.skipped_paths == []

    @patch("media_preview_generator.plex_client.logger.info")
    def test_get_media_items_by_paths_logs_received_and_file_path_query(self, mock_info, mock_config):
        """Webhook resolution logs received file count and file-path query."""
        mock_plex = MagicMock()
        mock_section = MagicMock()
        mock_section.key = "1"
        mock_section.title = "Movies"
        mock_section.METADATA_TYPE = "movie"
        mock_plex.library.sections.return_value = [mock_section]
        mock_plex.fetchItems.return_value = []

        get_media_items_by_paths(mock_plex, mock_config, ["/data/movies/Some Movie (2024)/movie.mkv"])
        assert any(
            "Received" in str(call) and "webhook input file" in str(call).lower() for call in mock_info.call_args_list
        )
        assert any("Querying Plex by file path" in str(call) for call in mock_info.call_args_list)

    @patch("media_preview_generator.plex_client.logger.warning")
    def test_get_media_items_by_paths_non_string_path_skipped(self, mock_warning, mock_config):
        """Non-string webhook paths are skipped without raising."""
        mock_plex = MagicMock()
        result = get_media_items_by_paths(mock_plex, mock_config, [123, None, "   "])
        assert isinstance(result, WebhookResolutionResult)
        assert result.items == []
        assert mock_warning.call_count == 1

    def test_get_media_items_by_paths_movie_match(self, mock_config):
        """Path matching a movie location returns (key, title, media_type)."""
        mock_plex = MagicMock()
        mock_section = MagicMock()
        mock_section.key = "1"
        mock_section.title = "Movies"
        mock_section.METADATA_TYPE = "movie"

        mock_movie = MagicMock()
        # D31 — set both attrs so we exercise the canonical (ratingKey) branch.
        mock_movie.ratingKey = 100
        mock_movie.key = "/library/metadata/100"
        mock_movie.title = "Test Movie"
        mock_movie.locations = ["/data/movies/Test Movie (2024)/Test Movie.mkv"]

        mock_plex.library.sections.return_value = [mock_section]
        mock_plex.fetchItems.return_value = [mock_movie]

        result = get_media_items_by_paths(mock_plex, mock_config, ["/data/movies/Test Movie (2024)/Test Movie.mkv"])
        assert len(result.items) == 1
        assert result.items[0][0] == "100"  # D31 — bare ratingKey, not URL
        assert "/" not in result.items[0][0]
        assert result.items[0][1] == "Test Movie"
        assert result.items[0][2] == "movie"
        assert mock_plex.fetchItems.called
        call_ekey = mock_plex.fetchItems.call_args[0][0]
        assert "type=1" in call_ekey
        assert "Test Movie.mkv" in call_ekey or "Test%20Movie.mkv" in call_ekey

    @patch("media_preview_generator.plex_client.logger.info")
    def test_get_media_items_by_paths_logs_per_path_resolved_status(self, mock_info, mock_config):
        """Resolved path emits a per-path diagnostic status line."""
        mock_plex = MagicMock()
        mock_section = MagicMock()
        mock_section.key = "1"
        mock_section.title = "Movies"
        mock_section.METADATA_TYPE = "movie"

        mock_movie = MagicMock()
        mock_movie.ratingKey = 100
        mock_movie.key = "/library/metadata/100"
        mock_movie.title = "Test Movie"
        mock_movie.locations = ["/data/movies/Test Movie (2024)/Test Movie.mkv"]

        mock_plex.library.sections.return_value = [mock_section]
        mock_plex.fetchItems.return_value = [mock_movie]

        result = get_media_items_by_paths(mock_plex, mock_config, ["/data/movies/Test Movie (2024)/Test Movie.mkv"])
        assert len(result.items) == 1
        assert any("resolved" in str(call) for call in mock_info.call_args_list)
        # Match either pre-formatted "[1/1]" or Loguru placeholder form "[{}/{}]" with args 1, 1.
        assert any(
            "[1/1]" in str(call) or ("[{}/{}]" in str(call) and "1, 1" in str(call))
            for call in mock_info.call_args_list
        )

    def test_get_media_items_by_paths_no_match(self, mock_config):
        """Paths that match no Plex item return empty list."""
        mock_plex = MagicMock()
        mock_section = MagicMock()
        mock_section.key = "1"
        mock_section.title = "Movies"
        mock_section.METADATA_TYPE = "movie"

        mock_plex.library.sections.return_value = [mock_section]
        mock_plex.fetchItems.return_value = []

        result = get_media_items_by_paths(mock_plex, mock_config, ["/nonexistent/path.mkv"])
        assert result.items == []
        assert result.unresolved_paths == ["/nonexistent/path.mkv"]

    @patch("media_preview_generator.plex_client.logger.info")
    def test_get_media_items_by_paths_logs_per_path_unresolved_reason(self, mock_info, mock_config):
        """Unresolved path emits a per-path diagnostic with explicit reason.

        These per-file lines are info-level diagnostics (the aggregate
        ``Unresolved`` warning at the end of the run is the actionable
        summary).
        """
        mock_plex = MagicMock()
        mock_section = MagicMock()
        mock_section.key = "1"
        mock_section.title = "Movies"
        mock_section.METADATA_TYPE = "movie"

        mock_plex.library.sections.return_value = [mock_section]
        mock_plex.fetchItems.return_value = []

        result = get_media_items_by_paths(mock_plex, mock_config, ["/nonexistent/path.mkv"])
        assert result.items == []
        assert any("not found" in str(call) for call in mock_info.call_args_list)
        assert any("Direct path not found in Plex" in str(call) for call in mock_info.call_args_list)

    def test_get_media_items_by_paths_episode_match(self, mock_config):
        """Path matching an episode location returns episode tuple."""
        mock_plex = MagicMock()
        mock_section = MagicMock()
        mock_section.key = "2"
        mock_section.title = "TV Shows"
        mock_section.METADATA_TYPE = "episode"

        mock_episode = MagicMock()
        mock_episode.ratingKey = 200
        mock_episode.key = "/library/metadata/200"
        mock_episode.grandparentTitle = "Test Show"
        mock_episode.seasonEpisode = "s01e01"
        mock_episode.locations = ["/data/tv/Test Show/Season 01/S01E01.mkv"]

        mock_plex.library.sections.return_value = [mock_section]
        mock_plex.fetchItems.return_value = [mock_episode]

        result = get_media_items_by_paths(
            mock_plex,
            mock_config,
            ["/data/tv/Test Show/Season 01/S01E01.mkv"],
        )
        assert len(result.items) == 1
        assert result.items[0][0] == "200"  # D31 — bare ratingKey, not URL
        assert "/" not in result.items[0][0]
        assert "Test Show" in result.items[0][1]
        assert "S01E01" in result.items[0][1]
        assert result.items[0][2] == "episode"
        assert mock_plex.fetchItems.called
        call_ekey = mock_plex.fetchItems.call_args[0][0]
        assert "type=4" in call_ekey
        assert "S01E01.mkv" in call_ekey

    def test_get_media_items_by_paths_upgrade_file_found_via_file_path_search(self, mock_config):
        """File-path search finds upgraded files (Plex keeps old addedAt; file= filter does not depend on it)."""
        mock_config.path_mappings = [
            {
                "plex_prefix": "/data_16tb",
                "local_prefix": "/data_16tb",
                "webhook_prefixes": ["/data"],
            }
        ]
        mock_plex = MagicMock()
        mock_section = MagicMock()
        mock_section.key = "2"
        mock_section.title = "TV Shows"
        mock_section.METADATA_TYPE = "episode"

        mock_episode = MagicMock()
        mock_episode.ratingKey = 481077
        mock_episode.key = "/library/metadata/481077"
        mock_episode.grandparentTitle = "The Mind, Explained"
        mock_episode.seasonEpisode = "s02e02"
        mock_episode.locations = [
            "/data_16tb/TV Shows/The Mind, Explained/Season 02/"
            "The Mind, Explained (2019) - S02E02 - Teenage Brain [NF WEBDL-1080p].mkv"
        ]

        mock_plex.library.sections.return_value = [mock_section]
        mock_plex.fetchItems.return_value = [mock_episode]

        webhook_path = (
            "/data/TV Shows/The Mind, Explained/Season 02/"
            "The Mind, Explained (2019) - S02E02 - Teenage Brain [NF WEBDL-1080p].mkv"
        )
        result = get_media_items_by_paths(mock_plex, mock_config, [webhook_path])

        assert len(result.items) == 1
        assert result.items[0][0] == "481077"  # D31 — bare ratingKey, not URL
        assert "/" not in result.items[0][0]
        assert "The Mind, Explained" in result.items[0][1]
        assert result.items[0][2] == "episode"
        # Audit fix — was bare ``mock_plex.fetchItems.called``. Tighten to
        # assert the file= filter is in the ekey so a regression that called
        # fetchItems with the WRONG filter (e.g. addedAt= or no filter) fails.
        assert mock_plex.fetchItems.called
        all_ekeys = " | ".join(str(c.args[0]) if c.args else "" for c in mock_plex.fetchItems.call_args_list)
        assert "file=" in all_ekeys, (
            f"fetchItems was called but with no file= filter — would silently full-scan. ekeys: {all_ekeys!r}"
        )

    def test_get_media_items_by_paths_file_path_search_resolves_match(self, mock_config):
        """Targeted file-path search resolves path to Plex item."""
        mock_plex = MagicMock()
        mock_section = MagicMock()
        mock_section.key = "1"
        mock_section.title = "Movies"
        mock_section.METADATA_TYPE = "movie"

        mock_movie = MagicMock()
        mock_movie.ratingKey = 999
        mock_movie.key = "/library/metadata/999"
        mock_movie.title = "Late Match"
        mock_movie.locations = ["/data/movies/Late Match/Late Match.mkv"]

        mock_plex.library.sections.return_value = [mock_section]
        mock_plex.fetchItems.return_value = [mock_movie]

        result = get_media_items_by_paths(mock_plex, mock_config, ["/data/movies/Late Match/Late Match.mkv"])

        assert len(result.items) == 1
        assert result.items[0][0] == "999"  # D31 — bare ratingKey, not URL
        assert "/" not in result.items[0][0]
        # Audit fix — assert the file= filter is in the ekey, not bare called().
        assert mock_plex.fetchItems.called
        all_ekeys = " | ".join(str(c.args[0]) if c.args else "" for c in mock_plex.fetchItems.call_args_list)
        assert "file=" in all_ekeys, (
            f"fetchItems was called but with no file= filter — would silently full-scan. ekeys: {all_ekeys!r}"
        )

    def test_get_media_items_by_paths_prefers_explicit_ratingKey_over_url_key(self, mock_config):
        """D31 — when plexapi populates BOTH .ratingKey (the canonical bare
        id, e.g. ``999``) AND .key (the URL form ``/library/metadata/999``),
        we MUST pick ratingKey. Picking .key was the silent root cause of
        every Sonarr/Radarr → Plex webhook returning ``skipped_not_indexed``
        for months — downstream code built ``f"/library/metadata/{item_id}/tree"``
        which doubled the prefix, 404'd, and lied about the cause.

        Mocks here mirror real plexapi shape (both attrs present) so the
        test can't be silently falsified by an unconfigured MagicMock."""
        mock_plex = MagicMock()
        mock_section = MagicMock()
        mock_section.key = "1"
        mock_section.title = "Movies"
        mock_section.METADATA_TYPE = "movie"

        mock_movie = MagicMock()
        # plexapi populates both — ratingKey is the bare canonical id (int
        # in real responses), .key is the API path. Setting both makes the
        # test fail loudly if production ever picks .key again.
        mock_movie.ratingKey = 999
        mock_movie.key = "/library/metadata/999"
        mock_movie.title = "Late Match"
        mock_movie.locations = ["/data/movies/Late Match/Late Match.mkv"]

        mock_plex.library.sections.return_value = [mock_section]
        mock_plex.fetchItems.return_value = [mock_movie]

        result = get_media_items_by_paths(mock_plex, mock_config, ["/data/movies/Late Match/Late Match.mkv"])

        assert len(result.items) == 1
        item_id = result.items[0][0]
        # Bare ratingKey, NOT the URL form — anything else doubles the prefix
        # downstream when PlexBundleAdapter builds f"/library/metadata/{id}/tree".
        assert item_id == "999"
        assert "/" not in item_id, f"item_id contains '/' ({item_id!r}) — would double the URL prefix in /tree query"

    @patch("media_preview_generator.plex_client.logger.info")
    def test_get_media_items_by_paths_logs_file_path_query(self, mock_info, mock_config):
        """File-path resolution logs the query step."""
        mock_plex = MagicMock()
        mock_section = MagicMock()
        mock_section.key = "1"
        mock_section.title = "Movies"
        mock_section.METADATA_TYPE = "movie"
        mock_movie = MagicMock()
        mock_movie.ratingKey = 999
        mock_movie.key = "/library/metadata/999"
        mock_movie.title = "Late Match"
        mock_movie.locations = ["/data/movies/Late Match/Late Match.mkv"]
        mock_plex.library.sections.return_value = [mock_section]
        mock_plex.fetchItems.return_value = [mock_movie]

        get_media_items_by_paths(mock_plex, mock_config, ["/data/movies/Late Match/Late Match.mkv"])
        assert any("Querying Plex by file path" in str(call) for call in mock_info.call_args_list)

    @patch("media_preview_generator.plex_client.logger.warning")
    def test_get_media_items_by_paths_item_without_key_is_skipped(self, mock_warning, mock_config):
        """Matched items missing a Plex key are ignored safely."""
        mock_plex = MagicMock()
        mock_section = MagicMock()
        mock_section.key = "1"
        mock_section.title = "Movies"
        mock_section.METADATA_TYPE = "movie"

        mock_movie = MagicMock()
        # Both ratingKey AND key absent — production must skip with a warning.
        mock_movie.ratingKey = None
        mock_movie.key = None
        mock_movie.title = "No Key Movie"
        mock_movie.locations = ["/data/movies/No Key Movie/No Key Movie.mkv"]

        mock_plex.library.sections.return_value = [mock_section]
        mock_plex.fetchItems.return_value = [mock_movie]

        result = get_media_items_by_paths(mock_plex, mock_config, ["/data/movies/No Key Movie/No Key Movie.mkv"])
        assert result.items == []
        assert any(
            "metadata key" in str(call).lower() or "without metadata key" in str(call)
            for call in mock_warning.call_args_list
        )

    def test_get_media_items_by_paths_webhook_path_matches_plex_via_mapping(self, mock_config):
        """Webhook sends /data/...; Plex item at /data_16tb1/...; mapping links them."""
        mock_config.path_mappings = [
            {
                "plex_prefix": "/data_16tb1",
                "local_prefix": "/data_16tb1",
                "webhook_prefixes": ["/data"],
            }
        ]
        mock_plex = MagicMock()
        mock_section = MagicMock()
        mock_section.key = "1"
        mock_section.title = "Movies"
        mock_section.METADATA_TYPE = "movie"

        mock_movie = MagicMock()
        mock_movie.ratingKey = 100
        mock_movie.key = "/library/metadata/100"
        mock_movie.title = "Test Movie"
        mock_movie.locations = ["/data_16tb1/movies/Test Movie (2024)/Test Movie.mkv"]

        mock_plex.library.sections.return_value = [mock_section]
        mock_plex.fetchItems.return_value = [mock_movie]

        result = get_media_items_by_paths(
            mock_plex,
            mock_config,
            ["/data/movies/Test Movie (2024)/Test Movie.mkv"],
        )
        assert len(result.items) == 1
        assert result.items[0][0] == "100"  # D31 — bare ratingKey, not URL
        assert result.items[0][2] == "movie"

    def test_get_media_items_by_paths_plex_form_path_matches_with_mapping(self, mock_config):
        """Webhook path in Plex form (/data_16tb1/...) canonicalized and matched."""
        mock_config.path_mappings = [
            {
                "plex_prefix": "/data_16tb1",
                "local_prefix": "/data",
                "webhook_prefixes": ["/data"],
            }
        ]
        mock_plex = MagicMock()
        mock_section = MagicMock()
        mock_section.key = "1"
        mock_section.title = "Movies"
        mock_section.METADATA_TYPE = "movie"

        mock_movie = MagicMock()
        mock_movie.ratingKey = 101
        mock_movie.key = "/library/metadata/101"
        mock_movie.title = "Other Movie"
        mock_movie.locations = ["/data_16tb1/Movies/Other Movie.mkv"]

        mock_plex.library.sections.return_value = [mock_section]
        mock_plex.fetchItems.return_value = [mock_movie]

        result = get_media_items_by_paths(
            mock_plex,
            mock_config,
            ["/data_16tb1/Movies/Other Movie.mkv"],
        )
        assert len(result.items) == 1
        assert result.items[0][0] == "101"  # D31 — bare ratingKey, not URL

    def test_get_media_items_by_paths_no_mapping_path_unchanged(self, mock_config):
        """With path_mappings empty, raw webhook path used for matching only."""
        assert getattr(mock_config, "path_mappings", None) in (None, [])
        mock_plex = MagicMock()
        mock_section = MagicMock()
        mock_section.key = "1"
        mock_section.title = "Movies"
        mock_section.METADATA_TYPE = "movie"

        mock_movie = MagicMock()
        mock_movie.ratingKey = 102
        mock_movie.key = "/library/metadata/102"
        mock_movie.title = "Direct Match"
        mock_movie.locations = ["/data/movies/Direct Match.mkv"]

        mock_plex.library.sections.return_value = [mock_section]
        mock_plex.fetchItems.return_value = [mock_movie]

        result = get_media_items_by_paths(mock_plex, mock_config, ["/data/movies/Direct Match.mkv"])
        assert len(result.items) == 1
        assert result.items[0][0] == "102"  # D31 — bare ratingKey, not URL

    def test_get_media_items_by_paths_multi_row_same_webhook_alias(self, mock_config):
        """Two rows with same webhook_prefix; webhook path /data/... matches item on either plex root."""
        mock_config.path_mappings = [
            {
                "plex_prefix": "/data_disk1",
                "local_prefix": "/data",
                "webhook_prefixes": ["/data"],
            },
            {
                "plex_prefix": "/data_disk2",
                "local_prefix": "/data",
                "webhook_prefixes": ["/data"],
            },
        ]
        mock_plex = MagicMock()
        mock_section = MagicMock()
        mock_section.key = "1"
        mock_section.title = "Movies"
        mock_section.METADATA_TYPE = "movie"

        mock_movie = MagicMock()
        mock_movie.ratingKey = 200
        mock_movie.key = "/library/metadata/200"
        mock_movie.title = "Multi Disk Movie"
        mock_movie.locations = ["/data_disk2/movies/Multi Disk Movie.mkv"]

        mock_plex.library.sections.return_value = [mock_section]
        mock_plex.fetchItems.return_value = [mock_movie]

        # Webhook sends merged path /data/...; item is on /data_disk2; alias /data/... is in item_targets
        result = get_media_items_by_paths(
            mock_plex,
            mock_config,
            ["/data/movies/Multi Disk Movie.mkv"],
        )
        assert len(result.items) == 1
        assert result.items[0][0] == "200"  # D31 — bare ratingKey, not URL

    def test_get_media_items_by_paths_fans_out_local_path_across_plex_roots(self, mock_config):
        """Webhook /data path should fan out to multiple Plex roots and match first hit."""
        mock_config.path_mappings = [
            {
                "plex_prefix": "/data_16tb1",
                "local_prefix": "/data",
                "webhook_prefixes": [],
            },
            {
                "plex_prefix": "/data_16tb2",
                "local_prefix": "/data",
                "webhook_prefixes": [],
            },
        ]
        mock_plex = MagicMock()
        mock_section = MagicMock()
        mock_section.key = "2"
        mock_section.title = "TV Shows"
        mock_section.METADATA_TYPE = "episode"

        mock_episode = MagicMock()
        mock_episode.ratingKey = 300
        mock_episode.key = "/library/metadata/300"
        mock_episode.grandparentTitle = "Test Show"
        mock_episode.seasonEpisode = "s01e03"
        mock_episode.locations = ["/data_16tb2/tv/Test Show/Season 01/S01E03.mkv"]

        mock_plex.library.sections.return_value = [mock_section]
        mock_plex.fetchItems.return_value = [mock_episode]

        result = get_media_items_by_paths(
            mock_plex,
            mock_config,
            ["/data/tv/Test Show/Season 01/S01E03.mkv"],
        )
        assert len(result.items) == 1
        assert result.items[0][0] == "300"  # D31 — bare ratingKey, not URL

    @patch("media_preview_generator.plex_client.logger.info")
    @patch("media_preview_generator.plex_client.logger.warning")
    def test_get_media_items_by_paths_logs_skipped_unselected_library(self, mock_warning, mock_info, mock_config):
        """Log a clear reason when webhook path matches only excluded libraries.

        The aggregate "Skipped N input file(s): matched Plex items in
        unselected libraries" stays at warning level (it's the
        actionable summary). The per-file "Found in excluded library /
        Result: skipped (excluded library: ...)" detail lines are
        info-level diagnostics.
        """
        mock_config.plex_libraries = ["movies"]

        mock_plex = MagicMock()

        movies_section = MagicMock()
        movies_section.key = "1"
        movies_section.title = "Movies"
        movies_section.METADATA_TYPE = "movie"

        anime_section = MagicMock()
        anime_section.key = "2"
        anime_section.title = "Anime"
        anime_section.METADATA_TYPE = "movie"

        anime_movie = MagicMock()
        anime_movie.key = "/library/metadata/555"
        anime_movie.title = "Anime Movie"
        anime_movie.locations = ["/data/anime/Anime Movie.mkv"]

        mock_plex.library.sections.return_value = [movies_section, anime_section]

        def fetchItems_side_effect(ekey):
            if "/sections/1/" in ekey:
                return []
            if "/sections/2/" in ekey:
                return [anime_movie]
            return []

        mock_plex.fetchItems.side_effect = fetchItems_side_effect

        result = get_media_items_by_paths(mock_plex, mock_config, ["/data/anime/Anime Movie.mkv"])
        assert result.items == []
        assert any(
            "haven't selected" in str(call).lower() or "unselected libraries" in str(call).lower()
            for call in mock_warning.call_args_list
        )
        assert any(
            "skipped" in str(call).lower() and "excluded" in str(call).lower() for call in mock_info.call_args_list
        )

    @patch("media_preview_generator.plex_client.logger.warning")
    def test_get_media_items_by_paths_logs_unselected_library_file_path_search(self, mock_warning, mock_config):
        """Excluded-library diagnostics when file-path search finds item there."""
        mock_config.plex_libraries = ["movies"]
        mock_plex = MagicMock()

        movies_section = MagicMock()
        movies_section.key = "1"
        movies_section.title = "Movies"
        movies_section.METADATA_TYPE = "movie"

        anime_section = MagicMock()
        anime_section.key = "2"
        anime_section.title = "Anime"
        anime_section.METADATA_TYPE = "movie"

        anime_movie = MagicMock()
        anime_movie.key = "/library/metadata/777"
        anime_movie.title = "Fallback Anime Movie"
        anime_movie.locations = ["/data/anime/Fallback Anime Movie.mkv"]

        mock_plex.library.sections.return_value = [movies_section, anime_section]

        def fetchItems_side_effect(ekey):
            if "/sections/1/" in ekey:
                return []
            if "/sections/2/" in ekey:
                return [anime_movie]
            return []

        mock_plex.fetchItems.side_effect = fetchItems_side_effect

        result = get_media_items_by_paths(mock_plex, mock_config, ["/data/anime/Fallback Anime Movie.mkv"])

        assert result.items == []
        assert any(
            "haven't selected" in str(call).lower() or "unselected libraries" in str(call).lower()
            for call in mock_warning.call_args_list
        )


class TestExpandDirectoryToMediaFiles:
    """Test directory-to-media-file expansion for manual trigger / webhook paths."""

    def test_video_files_discovered_recursively(self, tmp_path):
        """Directory with nested season folders expands into all media files."""
        show_dir = tmp_path / "Show (2024) [tvdb-12345]"
        s01 = show_dir / "Season 01"
        s02 = show_dir / "Season 02"
        s01.mkdir(parents=True)
        s02.mkdir(parents=True)

        (s01 / "S01E01.mkv").write_text("")
        (s01 / "S01E02.mp4").write_text("")
        (s02 / "S02E01.avi").write_text("")
        (s01 / "S01E01.srt").write_text("")
        (show_dir / "poster.jpg").write_text("")

        result = _expand_directory_to_media_files([str(show_dir)])

        assert len(result) == 3
        basenames = [os.path.basename(p) for p in result]
        assert "S01E01.mkv" in basenames
        assert "S01E02.mp4" in basenames
        assert "S02E01.avi" in basenames
        assert "S01E01.srt" not in basenames
        assert "poster.jpg" not in basenames

    def test_empty_directory_passes_through(self, tmp_path):
        """Directory with no media files passes through as-is for downstream error reporting."""
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()
        (empty_dir / "readme.txt").write_text("")

        result = _expand_directory_to_media_files([str(empty_dir)])

        assert result == [str(empty_dir)]

    def test_nonexistent_path_passes_through(self):
        """Path that doesn't exist on disk passes through unchanged (file or unmapped path)."""
        fake_path = "/nonexistent/media/Movie (2024)/movie.mkv"

        result = _expand_directory_to_media_files([fake_path])

        assert result == [fake_path]

    def test_file_path_passes_through(self, tmp_path):
        """An existing file path is returned unchanged (not treated as a directory)."""
        video = tmp_path / "movie.mkv"
        video.write_text("")

        result = _expand_directory_to_media_files([str(video)])

        assert result == [str(video)]

    def test_mixed_files_and_directories(self, tmp_path):
        """Mix of file paths and directory paths expands correctly."""
        show_dir = tmp_path / "Show"
        show_dir.mkdir()
        (show_dir / "S01E01.mkv").write_text("")
        (show_dir / "S01E02.mkv").write_text("")

        standalone = tmp_path / "movie.mp4"
        standalone.write_text("")

        result = _expand_directory_to_media_files([str(standalone), str(show_dir)])

        assert len(result) == 3
        assert result[0] == str(standalone)
        assert all(os.path.basename(p).endswith(".mkv") for p in result[1:])

    def test_results_are_sorted_within_directory(self, tmp_path):
        """Media files from a single directory are returned in sorted order."""
        show_dir = tmp_path / "Show"
        show_dir.mkdir()
        (show_dir / "S01E03.mkv").write_text("")
        (show_dir / "S01E01.mkv").write_text("")
        (show_dir / "S01E02.mkv").write_text("")

        result = _expand_directory_to_media_files([str(show_dir)])

        basenames = [os.path.basename(p) for p in result]
        assert basenames == ["S01E01.mkv", "S01E02.mkv", "S01E03.mkv"]

    def test_all_video_extensions_recognized(self, tmp_path):
        """Every extension in VIDEO_EXTENSIONS is picked up from a directory."""
        media_dir = tmp_path / "media"
        media_dir.mkdir()
        for ext in VIDEO_EXTENSIONS:
            (media_dir / f"test{ext}").write_text("")
        (media_dir / "test.txt").write_text("")

        result = _expand_directory_to_media_files([str(media_dir)])

        assert len(result) == len(VIDEO_EXTENSIONS)

    def test_mapped_directory_expanded_via_path_mappings(self, tmp_path):
        """Directory that only exists under a mapped prefix is expanded."""
        local_root = tmp_path / "data_16tb" / "TV Shows" / "Show (2024)"
        s01 = local_root / "Season 01"
        s01.mkdir(parents=True)
        (s01 / "S01E01.mkv").write_text("")
        (s01 / "S01E02.mkv").write_text("")

        mappings = [
            {
                "plex_prefix": "/data",
                "local_prefix": str(tmp_path / "data_16tb"),
                "webhook_prefixes": [],
            }
        ]
        webhook_path = "/data/TV Shows/Show (2024)"

        result = _expand_directory_to_media_files([webhook_path], mappings)

        assert len(result) == 2
        basenames = [os.path.basename(p) for p in result]
        assert "S01E01.mkv" in basenames
        assert "S01E02.mkv" in basenames
        assert all(str(tmp_path / "data_16tb") in p for p in result)

    def test_unmapped_nonexistent_directory_passes_through(self):
        """Path that doesn't exist even after mapping passes through unchanged."""
        mappings = [
            {
                "plex_prefix": "/plex",
                "local_prefix": "/local",
                "webhook_prefixes": [],
            }
        ]
        path = "/data/TV Shows/Nonexistent Show"

        result = _expand_directory_to_media_files([path], mappings)

        assert result == [path]


class TestDetectPathPrefixMismatches:
    """Tests for _detect_path_prefix_mismatches helper."""

    def test_trash_guides_docker_mismatch(self):
        """Detects the classic TRaSH Guides mismatch: /data/media vs /media."""
        unresolved = [
            "/data/media/tv/For All Mankind (2019)/Season 05/For All Mankind (2019) - S05E01 - First Light.mkv"
        ]
        plex_locations = ["/media/tv", "/media/movies"]

        result = _detect_path_prefix_mismatches(unresolved, plex_locations)

        assert len(result) == 1
        webhook_pfx, plex_pfx = result[0]
        assert webhook_pfx == "/data/media"
        assert plex_pfx == "/media"

    def test_no_mismatch_when_prefix_matches(self):
        """No suggestions when the webhook path already starts with a Plex root."""
        unresolved = ["/media/tv/Show/S01E01.mkv"]
        plex_locations = ["/media/tv"]

        result = _detect_path_prefix_mismatches(unresolved, plex_locations)

        assert result == []

    def test_empty_inputs(self):
        """Returns empty list when either input is empty."""
        assert _detect_path_prefix_mismatches([], ["/media/tv"]) == []
        assert _detect_path_prefix_mismatches(["/data/tv/f.mkv"], []) == []
        assert _detect_path_prefix_mismatches([], []) == []

    def test_single_level_plex_location(self):
        """Falls back to direct mapping when Plex location has no meaningful parent."""
        unresolved = ["/nas/tv/Show/S01E01.mkv"]
        plex_locations = ["/tv"]

        result = _detect_path_prefix_mismatches(unresolved, plex_locations)

        assert len(result) == 1
        assert result[0] == ("/nas/tv", "/tv")

    def test_deep_extra_prefix(self):
        """Handles multiple extra leading components in the webhook path."""
        unresolved = ["/a/b/media/tv/Show/file.mkv"]
        plex_locations = ["/media/tv"]

        result = _detect_path_prefix_mismatches(unresolved, plex_locations)

        assert len(result) == 1
        assert result[0] == ("/a/b/media", "/media")

    def test_partial_segment_not_matched(self):
        """Plex location /media/tv must not match a path containing /media/tv2."""
        unresolved = ["/data/media/tv2/Show/file.mkv"]
        plex_locations = ["/media/tv"]

        result = _detect_path_prefix_mismatches(unresolved, plex_locations)

        assert result == []

    def test_deduplicates_across_paths(self):
        """Same mismatch detected from multiple files is reported once."""
        unresolved = [
            "/data/media/tv/Show1/S01E01.mkv",
            "/data/media/tv/Show2/S02E01.mkv",
        ]
        plex_locations = ["/media/tv", "/media/movies"]

        result = _detect_path_prefix_mismatches(unresolved, plex_locations)

        assert len(result) == 1
        assert result[0] == ("/data/media", "/media")

    def test_case_insensitive_matching(self):
        """Detection is case-insensitive for cross-platform compatibility."""
        unresolved = ["/Data/Media/TV/Show/S01E01.mkv"]
        plex_locations = ["/media/tv"]

        result = _detect_path_prefix_mismatches(unresolved, plex_locations)

        assert len(result) == 1

    def test_longest_location_wins(self):
        """More-specific Plex locations are preferred over shorter ones."""
        unresolved = ["/extra/data/media/tv/Show/file.mkv"]
        plex_locations = ["/data/media/tv", "/media/tv"]

        result = _detect_path_prefix_mismatches(unresolved, plex_locations)

        assert len(result) == 1
        assert result[0] == ("/extra/data/media", "/data/media")


class TestMismatchCoveredByMappings:
    """Tests for _mismatch_covered_by_mappings helper."""

    def test_exact_webhook_prefix_match(self):
        """Returns True when a mapping row has the webhook prefix in webhook_prefixes."""
        mappings = [
            {
                "plex_prefix": "/series",
                "local_prefix": "/series",
                "webhook_prefixes": ["/Volumes/NAS2/series"],
            }
        ]
        assert _mismatch_covered_by_mappings("/Volumes/NAS2/series", "/series", mappings)

    def test_plex_and_local_cover_mismatch(self):
        """Returns True when plex_prefix and local_prefix span the mismatch."""
        mappings = [
            {
                "plex_prefix": "/media",
                "local_prefix": "/data/media",
                "webhook_prefixes": [],
            }
        ]
        assert _mismatch_covered_by_mappings("/data/media", "/media", mappings)

    def test_no_mapping_configured(self):
        """Returns False when no mappings are configured."""
        assert not _mismatch_covered_by_mappings("/data/media", "/media", [])

    def test_unrelated_mapping_not_matched(self):
        """Returns False when the configured mapping covers different prefixes."""
        mappings = [
            {
                "plex_prefix": "/movies",
                "local_prefix": "/mnt/movies",
                "webhook_prefixes": ["/nas/movies"],
            }
        ]
        assert not _mismatch_covered_by_mappings("/data/tv", "/tv", mappings)

    def test_case_insensitive(self):
        """Matching is case-insensitive for cross-platform paths."""
        mappings = [
            {
                "plex_prefix": "/Series",
                "local_prefix": "/Series",
                "webhook_prefixes": ["/Volumes/NAS2/Series"],
            }
        ]
        assert _mismatch_covered_by_mappings("/volumes/nas2/series", "/series", mappings)

    def test_trailing_slashes_ignored(self):
        """Trailing slashes on prefixes don't affect matching."""
        mappings = [
            {
                "plex_prefix": "/media/",
                "local_prefix": "/data/media/",
                "webhook_prefixes": [],
            }
        ]
        assert _mismatch_covered_by_mappings("/data/media/", "/media/", mappings)

    def test_none_mappings(self):
        """Returns False when mappings is None."""
        assert not _mismatch_covered_by_mappings("/data", "/media", None)

    def test_multiple_rows_second_matches(self):
        """Returns True when the second mapping row covers the mismatch."""
        mappings = [
            {
                "plex_prefix": "/movies",
                "local_prefix": "/mnt/movies",
                "webhook_prefixes": [],
            },
            {
                "plex_prefix": "/tv",
                "local_prefix": "/tv",
                "webhook_prefixes": ["/nas/tv"],
            },
        ]
        assert _mismatch_covered_by_mappings("/nas/tv", "/tv", mappings)
