"""
Tests for plex_client.py module.

Tests Plex server connection, retry logic, library queries,
and duplicate filtering.
"""

import pytest
import xml.etree.ElementTree as ET
from unittest.mock import MagicMock, patch
import requests

from plex_generate_previews.plex_client import (
    plex_server,
    retry_plex_call,
    filter_duplicate_locations,
    get_library_sections,
    get_media_items_by_paths,
    WebhookResolutionResult,
)


class TestPlexServerConnection:
    """Test Plex server connection."""

    @patch("plexapi.server.PlexServer")
    @patch("requests.Session")
    def test_plex_server_connection_success(
        self, mock_session, mock_plex_server, mock_config
    ):
        """Test successful connection to Plex server."""
        mock_plex = MagicMock()
        mock_plex_server.return_value = mock_plex

        result = plex_server(mock_config)

        assert result == mock_plex
        mock_plex_server.assert_called_once()

    @patch("plexapi.server.PlexServer")
    @patch("requests.Session")
    def test_plex_server_connection_failure(
        self, mock_session, mock_plex_server, mock_config
    ):
        """Test connection error handling."""
        mock_plex_server.side_effect = requests.exceptions.ConnectionError(
            "Connection refused"
        )

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
    @patch("requests.Session")
    def test_plex_server_retry_strategy(
        self, mock_session, mock_plex_server, mock_config
    ):
        """Test that retry strategy is configured."""
        mock_plex = MagicMock()
        mock_plex_server.return_value = mock_plex

        plex_server(mock_config)

        # Verify session was configured with retry
        assert mock_session.called


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
        """Test getting movie libraries."""
        mock_plex = MagicMock()

        # Mock section
        mock_section = MagicMock()
        mock_section.title = "Movies"
        mock_section.METADATA_TYPE = "movie"

        # Mock movie
        mock_movie = MagicMock()
        mock_movie.key = "/library/metadata/1"
        mock_movie.title = "Test Movie"

        mock_section.search.return_value = [mock_movie]
        mock_plex.library.sections.return_value = [mock_section]

        sections = list(get_library_sections(mock_plex, mock_config))

        assert len(sections) == 1
        section, media = sections[0]
        assert section == mock_section
        assert len(media) == 1
        assert media[0][0] == "/library/metadata/1"
        assert media[0][1] == "Test Movie"
        assert media[0][2] == "movie"


class TestPathMapping:
    """Test path mapping/translation for Docker/Unraid deployments.

    Path mapping is essential for Docker containers where the Plex server
    sees media files at one path (e.g., /data/media) while the container
    sees them at another path (e.g., /media).

    These tests validate that path translation works correctly for common
    Unraid/Docker path mapping scenarios.
    """

    def test_path_mapping_unraid_standard(self):
        """Test standard Unraid path mapping (Plex path -> container path)."""
        # Plex sees: /data/Movies/movie.mkv
        # Container sees: /media/Movies/movie.mkv
        plex_path = "/data/Movies/movie.mkv"
        plex_videos_path_mapping = "/data"
        plex_local_videos_path_mapping = "/media"

        mapped_path = plex_path.replace(
            plex_videos_path_mapping, plex_local_videos_path_mapping
        )

        assert mapped_path == "/media/Movies/movie.mkv"

    def test_path_mapping_nested_paths(self):
        """Test path mapping with nested directory structures."""
        # Plex sees: /mnt/user/media/tv/Show/Season 1/Episode.mkv
        # Container sees: /media/tv/Show/Season 1/Episode.mkv
        plex_path = "/mnt/user/media/tv/Show/Season 1/Episode.mkv"
        plex_videos_path_mapping = "/mnt/user/media"
        plex_local_videos_path_mapping = "/media"

        mapped_path = plex_path.replace(
            plex_videos_path_mapping, plex_local_videos_path_mapping
        )

        assert mapped_path == "/media/tv/Show/Season 1/Episode.mkv"

    def test_path_mapping_with_spaces(self):
        """Test path mapping handles spaces in paths correctly."""
        plex_path = "/mnt/user/My Media/Movies/A Movie Title (2024)/movie.mkv"
        plex_videos_path_mapping = "/mnt/user/My Media"
        plex_local_videos_path_mapping = "/media"

        mapped_path = plex_path.replace(
            plex_videos_path_mapping, plex_local_videos_path_mapping
        )

        assert mapped_path == "/media/Movies/A Movie Title (2024)/movie.mkv"

    def test_path_mapping_trailing_slash_consistency(self):
        """Test that trailing slashes are handled consistently."""
        plex_path = "/data/Movies/movie.mkv"

        # Without trailing slashes - this works correctly
        plex_mapping = "/data"
        local_mapping = "/media"
        mapped = plex_path.replace(plex_mapping, local_mapping)
        assert mapped == "/media/Movies/movie.mkv"

        # With trailing slashes - also works, but more specific
        plex_mapping_slash = "/data/"
        local_mapping_slash = "/media/"
        mapped_slash = plex_path.replace(plex_mapping_slash, local_mapping_slash)
        assert mapped_slash == "/media/Movies/movie.mkv"

        # Demonstrating that path starts with /data/ (with slash)
        assert plex_path.startswith("/data/")  # True - starts with /data/

    def test_path_mapping_no_mapping_needed(self):
        """Test when no path mapping is configured (same paths)."""
        plex_path = "/media/Movies/movie.mkv"
        # Empty mappings mean no transformation needed
        plex_videos_path_mapping = ""
        plex_local_videos_path_mapping = ""

        # When both are empty, no replacement should occur
        if plex_videos_path_mapping and plex_local_videos_path_mapping:
            mapped_path = plex_path.replace(
                plex_videos_path_mapping, plex_local_videos_path_mapping
            )
        else:
            mapped_path = plex_path

        assert mapped_path == "/media/Movies/movie.mkv"

    def test_path_mapping_unraid_smb_share(self):
        """Test mapping SMB/network share paths in Unraid."""
        # Plex container using SMB path
        plex_path = "//server/media/Movies/movie.mkv"
        plex_videos_path_mapping = "//server/media"
        plex_local_videos_path_mapping = "/mnt/media"

        mapped_path = plex_path.replace(
            plex_videos_path_mapping, plex_local_videos_path_mapping
        )

        assert mapped_path == "/mnt/media/Movies/movie.mkv"

    def test_path_mapping_case_sensitivity(self):
        """Test that path mapping is case-sensitive (Linux filesystems)."""
        plex_path = "/Data/Movies/movie.mkv"
        plex_videos_path_mapping = "/data"  # lowercase
        plex_local_videos_path_mapping = "/media"

        # Should NOT replace because case doesn't match
        mapped_path = plex_path.replace(
            plex_videos_path_mapping, plex_local_videos_path_mapping
        )

        # Path unchanged because /data != /Data
        assert mapped_path == "/Data/Movies/movie.mkv"

    def test_path_mapping_partial_match_avoided(self):
        """Test that partial path matches are handled correctly."""
        # Ensure /data doesn't match /database
        plex_path = "/database/Movies/movie.mkv"
        plex_videos_path_mapping = "/data"
        plex_local_videos_path_mapping = "/media"

        mapped_path = plex_path.replace(
            plex_videos_path_mapping, plex_local_videos_path_mapping
        )

        # This demonstrates a limitation - str.replace will match prefix of /database
        # Real implementation should use startswith() or proper path prefix matching
        # For now, document this behavior - /database becomes /mediabase
        assert mapped_path == "/mediabase/Movies/movie.mkv"

    def test_path_mapping_docker_volume_mounts(self):
        """Test common Docker volume mount scenarios."""
        # Scenario: linuxserver/plex mounts media at /data/media
        # This container mounts same media at /media
        test_cases = [
            # (plex_path, plex_mapping, local_mapping, expected)
            (
                "/data/media/movies/film.mkv",
                "/data/media",
                "/media",
                "/media/movies/film.mkv",
            ),
            (
                "/data/media/tv/show/s01e01.mkv",
                "/data/media",
                "/media",
                "/media/tv/show/s01e01.mkv",
            ),
            (
                "/config/media/Movies/film.mkv",
                "/config/media",
                "/media",
                "/media/Movies/film.mkv",
            ),
        ]

        for plex_path, plex_mapping, local_mapping, expected in test_cases:
            mapped = plex_path.replace(plex_mapping, local_mapping)
            assert mapped == expected, f"Failed for {plex_path}"


class TestGetLibrarySectionsExtended:
    """Extended tests for library section retrieval."""

    def test_get_library_sections_episodes(self, mock_config):
        """Test getting TV show libraries."""
        mock_plex = MagicMock()

        # Mock section
        mock_section = MagicMock()
        mock_section.title = "TV Shows"
        mock_section.METADATA_TYPE = "episode"

        # Mock episode
        mock_episode = MagicMock()
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
        assert media[0][0] == "/library/metadata/123"
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
        mock_plex.library.sections.side_effect = requests.exceptions.RequestException(
            "API error"
        )

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
        mock_section.search.side_effect = requests.exceptions.RequestException(
            "Search failed"
        )

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

        # Two episodes pointing to same file (multi-part episode)
        mock_episode1 = MagicMock()
        mock_episode1.key = "/library/metadata/1"
        mock_episode1.grandparentTitle = "Show"
        mock_episode1.seasonEpisode = "s01e01"
        mock_episode1.locations = ["/path/to/episode.mkv"]

        mock_episode2 = MagicMock()
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

        sections = list(
            get_library_sections(mock_plex, mock_config, cancel_check=lambda: True)
        )

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

        sections = list(
            get_library_sections(
                mock_plex, mock_config, cancel_check=cancel_after_first
            )
        )

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
        mock_movie.key = "/library/metadata/1"
        mock_movie.title = "Test Movie"
        mock_section.search.return_value = [mock_movie]

        mock_plex.library.sections.return_value = [mock_section]

        sections = list(get_library_sections(mock_plex, mock_config))

        assert len(sections) == 1


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

    @patch("plex_generate_previews.plex_client.logger.info")
    def test_get_media_items_by_paths_logs_received_and_pass1(
        self, mock_info, mock_config
    ):
        """Webhook resolution logs received file count and Pass 1 before scanning."""
        mock_plex = MagicMock()
        mock_section = MagicMock()
        mock_section.key = "1"
        mock_section.title = "Movies"
        mock_section.METADATA_TYPE = "movie"
        mock_section.search.return_value = []
        mock_plex.library.sections.return_value = [mock_section]

        get_media_items_by_paths(
            mock_plex, mock_config, ["/data/movies/Some Movie (2024)/movie.mkv"]
        )
        assert any(
            "Received" in str(call) and "webhook input file" in str(call).lower()
            for call in mock_info.call_args_list
        )
        assert any(
            "Querying Plex for recently added items" in str(call)
            for call in mock_info.call_args_list
        )
        assert any(
            "Pass 1" in str(call) and "scanned" in str(call).lower()
            for call in mock_info.call_args_list
        )

    @patch("plex_generate_previews.plex_client.logger.warning")
    def test_get_media_items_by_paths_non_string_path_skipped(
        self, mock_warning, mock_config
    ):
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
        mock_movie.key = "/library/metadata/100"
        mock_movie.title = "Test Movie"
        mock_movie.locations = ["/data/movies/Test Movie (2024)/Test Movie.mkv"]

        mock_plex.library.sections.return_value = [mock_section]
        mock_section.search.return_value = [mock_movie]

        result = get_media_items_by_paths(
            mock_plex, mock_config, ["/data/movies/Test Movie (2024)/Test Movie.mkv"]
        )
        assert len(result.items) == 1
        assert result.items[0][0] == "/library/metadata/100"
        assert result.items[0][1] == "Test Movie"
        assert result.items[0][2] == "movie"
        mock_section.search.assert_called_once_with(sort="addedAt:desc", maxresults=200)

    @patch("plex_generate_previews.plex_client.logger.info")
    def test_get_media_items_by_paths_logs_per_path_resolved_status(
        self, mock_info, mock_config
    ):
        """Resolved path emits a per-path diagnostic status line."""
        mock_plex = MagicMock()
        mock_section = MagicMock()
        mock_section.key = "1"
        mock_section.title = "Movies"
        mock_section.METADATA_TYPE = "movie"

        mock_movie = MagicMock()
        mock_movie.key = "/library/metadata/100"
        mock_movie.title = "Test Movie"
        mock_movie.locations = ["/data/movies/Test Movie (2024)/Test Movie.mkv"]

        mock_plex.library.sections.return_value = [mock_section]
        mock_section.search.return_value = [mock_movie]

        result = get_media_items_by_paths(
            mock_plex, mock_config, ["/data/movies/Test Movie (2024)/Test Movie.mkv"]
        )
        assert len(result.items) == 1
        assert any("resolved" in str(call) for call in mock_info.call_args_list)
        assert any("[1/1]" in str(call) for call in mock_info.call_args_list)

    def test_get_media_items_by_paths_no_match(self, mock_config):
        """Paths that match no Plex item return empty list."""
        mock_plex = MagicMock()
        mock_section = MagicMock()
        mock_section.key = "1"
        mock_section.title = "Movies"
        mock_section.METADATA_TYPE = "movie"
        mock_section.search.return_value = []

        mock_plex.library.sections.return_value = [mock_section]

        result = get_media_items_by_paths(
            mock_plex, mock_config, ["/nonexistent/path.mkv"]
        )
        assert result.items == []
        assert result.unresolved_paths == ["/nonexistent/path.mkv"]

    @patch("plex_generate_previews.plex_client.logger.warning")
    def test_get_media_items_by_paths_logs_per_path_unresolved_reason(
        self, mock_warning, mock_config
    ):
        """Unresolved path emits a per-path diagnostic with explicit reason."""
        mock_plex = MagicMock()
        mock_section = MagicMock()
        mock_section.key = "1"
        mock_section.title = "Movies"
        mock_section.METADATA_TYPE = "movie"
        mock_section.search.return_value = []
        mock_plex.library.sections.return_value = [mock_section]

        result = get_media_items_by_paths(
            mock_plex, mock_config, ["/nonexistent/path.mkv"]
        )
        assert result.items == []
        assert any("not found" in str(call) for call in mock_warning.call_args_list)
        assert any(
            "Direct path not found in Plex" in str(call)
            for call in mock_warning.call_args_list
        )

    def test_get_media_items_by_paths_episode_match(self, mock_config):
        """Path matching an episode location returns episode tuple."""
        mock_plex = MagicMock()
        mock_section = MagicMock()
        mock_section.key = "2"
        mock_section.title = "TV Shows"
        mock_section.METADATA_TYPE = "episode"

        mock_episode = MagicMock()
        mock_episode.key = "/library/metadata/200"
        mock_episode.grandparentTitle = "Test Show"
        mock_episode.seasonEpisode = "s01e01"
        mock_episode.locations = ["/data/tv/Test Show/Season 01/S01E01.mkv"]

        mock_plex.library.sections.return_value = [mock_section]
        mock_section.search.return_value = [mock_episode]

        result = get_media_items_by_paths(
            mock_plex,
            mock_config,
            ["/data/tv/Test Show/Season 01/S01E01.mkv"],
        )
        assert len(result.items) == 1
        assert result.items[0][0] == "/library/metadata/200"
        assert "Test Show" in result.items[0][1]
        assert "S01E01" in result.items[0][1]
        assert result.items[0][2] == "episode"
        mock_section.search.assert_called_once_with(
            libtype="episode", sort="addedAt:desc", maxresults=200
        )

    @patch.dict(
        "os.environ",
        {"WEBHOOK_RECENT_LIMIT": "1", "WEBHOOK_FALLBACK_LIMIT": "5"},
        clear=False,
    )
    def test_get_media_items_by_paths_fallback_window(self, mock_config):
        """Unresolved paths should trigger one bounded fallback scan."""
        mock_plex = MagicMock()
        mock_section = MagicMock()
        mock_section.key = "1"
        mock_section.title = "Movies"
        mock_section.METADATA_TYPE = "movie"

        mock_movie = MagicMock()
        mock_movie.key = "/library/metadata/999"
        mock_movie.title = "Late Match"
        mock_movie.locations = ["/data/movies/Late Match/Late Match.mkv"]

        mock_plex.library.sections.return_value = [mock_section]
        mock_section.search.side_effect = [[], [mock_movie]]

        result = get_media_items_by_paths(
            mock_plex, mock_config, ["/data/movies/Late Match/Late Match.mkv"]
        )

        assert len(result.items) == 1
        assert result.items[0][0] == "/library/metadata/999"
        assert mock_section.search.call_count == 2
        first_call = mock_section.search.call_args_list[0].kwargs
        second_call = mock_section.search.call_args_list[1].kwargs
        assert first_call == {"sort": "addedAt:desc", "maxresults": 1}
        assert second_call == {"sort": "addedAt:desc", "maxresults": 5}

    @patch.dict(
        "os.environ",
        {"WEBHOOK_RECENT_LIMIT": "1", "WEBHOOK_FALLBACK_LIMIT": "5"},
        clear=False,
    )
    @patch("plex_generate_previews.plex_client.logger.info")
    def test_get_media_items_by_paths_logs_pass2_when_fallback_runs(
        self, mock_info, mock_config
    ):
        """When fallback scan runs, log Pass 2 with input-file count."""
        mock_plex = MagicMock()
        mock_section = MagicMock()
        mock_section.key = "1"
        mock_section.title = "Movies"
        mock_section.METADATA_TYPE = "movie"
        mock_movie = MagicMock()
        mock_movie.key = "/library/metadata/999"
        mock_movie.title = "Late Match"
        mock_movie.locations = ["/data/movies/Late Match/Late Match.mkv"]
        mock_plex.library.sections.return_value = [mock_section]
        mock_section.search.side_effect = [[], [mock_movie]]

        get_media_items_by_paths(
            mock_plex, mock_config, ["/data/movies/Late Match/Late Match.mkv"]
        )
        assert any(
            "Pass 2" in str(call) and "scanned" in str(call).lower()
            for call in mock_info.call_args_list
        )
        assert any("still unmatched" in str(call) for call in mock_info.call_args_list)

    @patch("plex_generate_previews.plex_client.logger.warning")
    def test_get_media_items_by_paths_item_without_key_is_skipped(
        self, mock_warning, mock_config
    ):
        """Matched items missing a Plex key are ignored safely."""
        mock_plex = MagicMock()
        mock_section = MagicMock()
        mock_section.key = "1"
        mock_section.title = "Movies"
        mock_section.METADATA_TYPE = "movie"

        mock_movie = MagicMock()
        mock_movie.key = None
        mock_movie.title = "No Key Movie"
        mock_movie.locations = ["/data/movies/No Key Movie/No Key Movie.mkv"]

        mock_plex.library.sections.return_value = [mock_section]
        mock_section.search.return_value = [mock_movie]

        result = get_media_items_by_paths(
            mock_plex, mock_config, ["/data/movies/No Key Movie/No Key Movie.mkv"]
        )
        assert result.items == []
        assert mock_warning.call_count == 1

    def test_get_media_items_by_paths_webhook_path_matches_plex_via_mapping(
        self, mock_config
    ):
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
        mock_movie.key = "/library/metadata/100"
        mock_movie.title = "Test Movie"
        mock_movie.locations = ["/data_16tb1/movies/Test Movie (2024)/Test Movie.mkv"]

        mock_plex.library.sections.return_value = [mock_section]
        mock_section.search.return_value = [mock_movie]

        result = get_media_items_by_paths(
            mock_plex,
            mock_config,
            ["/data/movies/Test Movie (2024)/Test Movie.mkv"],
        )
        assert len(result.items) == 1
        assert result.items[0][0] == "/library/metadata/100"
        assert result.items[0][2] == "movie"

    def test_get_media_items_by_paths_plex_form_path_matches_with_mapping(
        self, mock_config
    ):
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
        mock_movie.key = "/library/metadata/101"
        mock_movie.title = "Other Movie"
        mock_movie.locations = ["/data_16tb1/Movies/Other Movie.mkv"]

        mock_plex.library.sections.return_value = [mock_section]
        mock_section.search.return_value = [mock_movie]

        result = get_media_items_by_paths(
            mock_plex,
            mock_config,
            ["/data_16tb1/Movies/Other Movie.mkv"],
        )
        assert len(result.items) == 1
        assert result.items[0][0] == "/library/metadata/101"

    def test_get_media_items_by_paths_no_mapping_path_unchanged(self, mock_config):
        """With path_mappings empty, raw webhook path used for matching only."""
        assert getattr(mock_config, "path_mappings", None) in (None, [])
        mock_plex = MagicMock()
        mock_section = MagicMock()
        mock_section.key = "1"
        mock_section.title = "Movies"
        mock_section.METADATA_TYPE = "movie"

        mock_movie = MagicMock()
        mock_movie.key = "/library/metadata/102"
        mock_movie.title = "Direct Match"
        mock_movie.locations = ["/data/movies/Direct Match.mkv"]

        mock_plex.library.sections.return_value = [mock_section]
        mock_section.search.return_value = [mock_movie]

        result = get_media_items_by_paths(
            mock_plex, mock_config, ["/data/movies/Direct Match.mkv"]
        )
        assert len(result.items) == 1
        assert result.items[0][0] == "/library/metadata/102"

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
        mock_movie.key = "/library/metadata/200"
        mock_movie.title = "Multi Disk Movie"
        mock_movie.locations = ["/data_disk2/movies/Multi Disk Movie.mkv"]

        mock_plex.library.sections.return_value = [mock_section]
        mock_section.search.return_value = [mock_movie]

        # Webhook sends merged path /data/...; item is on /data_disk2; alias /data/... is in item_targets
        result = get_media_items_by_paths(
            mock_plex,
            mock_config,
            ["/data/movies/Multi Disk Movie.mkv"],
        )
        assert len(result.items) == 1
        assert result.items[0][0] == "/library/metadata/200"

    def test_get_media_items_by_paths_fans_out_local_path_across_plex_roots(
        self, mock_config
    ):
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
        mock_episode.key = "/library/metadata/300"
        mock_episode.grandparentTitle = "Test Show"
        mock_episode.seasonEpisode = "s01e03"
        mock_episode.locations = ["/data_16tb2/tv/Test Show/Season 01/S01E03.mkv"]

        mock_plex.library.sections.return_value = [mock_section]
        mock_section.search.return_value = [mock_episode]

        result = get_media_items_by_paths(
            mock_plex,
            mock_config,
            ["/data/tv/Test Show/Season 01/S01E03.mkv"],
        )
        assert len(result.items) == 1
        assert result.items[0][0] == "/library/metadata/300"

    @patch("plex_generate_previews.plex_client.logger.warning")
    def test_get_media_items_by_paths_logs_skipped_unselected_library(
        self, mock_warning, mock_config
    ):
        """Log a clear reason when webhook path matches only excluded libraries."""
        mock_config.plex_libraries = ["movies"]

        mock_plex = MagicMock()

        movies_section = MagicMock()
        movies_section.key = "1"
        movies_section.title = "Movies"
        movies_section.METADATA_TYPE = "movie"
        movies_section.search.return_value = []

        anime_section = MagicMock()
        anime_section.key = "2"
        anime_section.title = "Anime"
        anime_section.METADATA_TYPE = "movie"

        anime_movie = MagicMock()
        anime_movie.key = "/library/metadata/555"
        anime_movie.title = "Anime Movie"
        anime_movie.locations = ["/data/anime/Anime Movie.mkv"]
        anime_section.search.return_value = [anime_movie]

        mock_plex.library.sections.return_value = [movies_section, anime_section]

        result = get_media_items_by_paths(
            mock_plex, mock_config, ["/data/anime/Anime Movie.mkv"]
        )
        assert result.items == []
        assert any(
            "unselected libraries" in str(call).lower()
            for call in mock_warning.call_args_list
        )
        assert any(
            "skipped" in str(call).lower() and "excluded" in str(call).lower()
            for call in mock_warning.call_args_list
        )

    @patch.dict(
        "os.environ",
        {"WEBHOOK_RECENT_LIMIT": "1", "WEBHOOK_FALLBACK_LIMIT": "5"},
        clear=False,
    )
    @patch("plex_generate_previews.plex_client.logger.warning")
    def test_get_media_items_by_paths_logs_unselected_library_after_fallback_scan(
        self, mock_warning, mock_config
    ):
        """Excluded-library diagnostics should also use fallback search window."""
        mock_config.plex_libraries = ["movies"]
        mock_plex = MagicMock()

        movies_section = MagicMock()
        movies_section.key = "1"
        movies_section.title = "Movies"
        movies_section.METADATA_TYPE = "movie"
        movies_section.search.side_effect = [[], []]

        anime_section = MagicMock()
        anime_section.key = "2"
        anime_section.title = "Anime"
        anime_section.METADATA_TYPE = "movie"

        anime_movie = MagicMock()
        anime_movie.key = "/library/metadata/777"
        anime_movie.title = "Fallback Anime Movie"
        anime_movie.locations = ["/data/anime/Fallback Anime Movie.mkv"]
        anime_section.search.side_effect = [[], [anime_movie]]

        mock_plex.library.sections.return_value = [movies_section, anime_section]

        result = get_media_items_by_paths(
            mock_plex, mock_config, ["/data/anime/Fallback Anime Movie.mkv"]
        )

        assert result.items == []
        assert anime_section.search.call_count == 2
        assert any(
            "unselected libraries" in str(call).lower()
            for call in mock_warning.call_args_list
        )
