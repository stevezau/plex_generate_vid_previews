"""
Tests for config.py module.

Tests configuration loading, validation, path checking,
FFmpeg detection, and environment-specific behavior.
"""

from unittest.mock import MagicMock, patch

import pytest

from plex_generate_previews.config import (
    expand_path_mapping_candidates,
    get_config_value,
    get_path_mapping_pairs,
    is_path_excluded,
    load_config,
    local_path_to_webhook_aliases,
    normalize_exclude_paths,
    normalize_path_mappings,
    path_to_canonical_local,
    plex_path_to_local,
    show_docker_help,
    split_library_selectors,
)


class TestGetConfigValue:
    """Test config value precedence."""

    def test_get_config_value_cli_precedence(self):
        """Test that CLI args take precedence over env vars."""
        cli_args = MagicMock()
        cli_args.test_field = "cli_value"

        with patch.dict("os.environ", {"TEST_FIELD": "env_value"}):
            result = get_config_value(cli_args, "test_field", "TEST_FIELD", "default")
            assert result == "cli_value"

    def test_get_config_value_env_fallback(self):
        """Test that env vars are used when CLI args are None."""
        cli_args = MagicMock()
        cli_args.test_field = None

        with patch.dict("os.environ", {"TEST_FIELD": "env_value"}):
            result = get_config_value(cli_args, "test_field", "TEST_FIELD", "default")
            assert result == "env_value"

    def test_get_config_value_default_fallback(self):
        """Test that defaults are used when neither CLI nor env are set."""
        cli_args = MagicMock()
        cli_args.test_field = None

        with patch.dict("os.environ", {}, clear=True):
            result = get_config_value(cli_args, "test_field", "TEST_FIELD", "default")
            assert result == "default"

    def test_get_config_value_boolean_conversion(self):
        """Test boolean value conversion."""
        cli_args = MagicMock()
        cli_args.bool_field = None

        # Test true values
        for value in ["true", "True", "1", "yes", "YES"]:
            with patch.dict("os.environ", {"BOOL_FIELD": value}):
                result = get_config_value(
                    cli_args, "bool_field", "BOOL_FIELD", False, bool
                )
                assert result is True

        # Test false values
        for value in ["false", "False", "0", "no", "NO"]:
            with patch.dict("os.environ", {"BOOL_FIELD": value}):
                result = get_config_value(
                    cli_args, "bool_field", "BOOL_FIELD", True, bool
                )
                assert result is False

    def test_get_config_value_int_conversion(self):
        """Test integer value conversion."""
        cli_args = MagicMock()
        cli_args.int_field = None

        with patch.dict("os.environ", {"INT_FIELD": "42"}):
            result = get_config_value(cli_args, "int_field", "INT_FIELD", 0, int)
            assert result == 42

    def test_get_config_value_handles_non_vars_cli_object(self):
        """Fallback to env/default when vars(cli_args) is unsupported."""

        class NoVarsObject:
            __slots__ = ()

        with patch.dict("os.environ", {"TEST_FIELD": "env_value"}, clear=True):
            result = get_config_value(
                NoVarsObject(), "test_field", "TEST_FIELD", "default"
            )
            assert result == "env_value"


class TestGetPathMappingPairs:
    """Test path mapping pair parsing (single, mergefs, explicit pairs)."""

    def test_get_path_mapping_pairs_single(self):
        """Single Plex and single local returns one pair."""
        assert get_path_mapping_pairs("/data/media", "/media") == [
            ("/data/media", "/media")
        ]

    def test_get_path_mapping_pairs_mergefs(self):
        """Multiple Plex roots and one local returns all Plex mapped to that local."""
        assert get_path_mapping_pairs(
            "/data_disk1;/data_disk2;/data_disk3", "/data"
        ) == [
            ("/data_disk1", "/data"),
            ("/data_disk2", "/data"),
            ("/data_disk3", "/data"),
        ]

    def test_get_path_mapping_pairs_same_count(self):
        """Same number of Plex and local pairs by index."""
        assert get_path_mapping_pairs("/a;/b", "/x;/y") == [
            ("/a", "/x"),
            ("/b", "/y"),
        ]

    def test_get_path_mapping_pairs_empty(self):
        """Empty or missing mapping returns empty list."""
        assert get_path_mapping_pairs("", "/media") == []
        assert get_path_mapping_pairs("/data", "") == []
        assert get_path_mapping_pairs("", "") == []
        assert get_path_mapping_pairs(None, "/media") == []
        assert get_path_mapping_pairs("/data", None) == []

    def test_get_path_mapping_pairs_strips_whitespace(self):
        """Semicolon-separated values are stripped."""
        assert get_path_mapping_pairs("  /a  ;  /b  ", "  /x  ") == [
            ("/a", "/x"),
            ("/b", "/x"),
        ]

    def test_get_path_mapping_pairs_mismatched_lengths_fallback(self):
        """Legacy: 3 plex vs 2 local uses first of each (backward compat)."""
        assert get_path_mapping_pairs("/a;/b;/c", "/x;/y") == [("/a", "/x")]


class TestSplitLibrarySelectors:
    """Test splitting selected library values into IDs and names."""

    def test_split_library_selectors_ids_only(self):
        """Numeric selectors are treated as Plex section IDs."""
        ids, names = split_library_selectors(["1", 2, "003"])
        assert ids == ["1", "2", "003"]
        assert names == []

    def test_split_library_selectors_names_only(self):
        """Non-numeric selectors are normalized as lowercase titles."""
        ids, names = split_library_selectors(["Movies", " TV Shows "])
        assert ids == []
        assert names == ["movies", "tv shows"]

    def test_split_library_selectors_mixed_and_deduplicated(self):
        """Mixed selectors are split and deduplicated while preserving order."""
        ids, names = split_library_selectors(
            ["1", "Movies", "1", "movies", "2", "TV Shows", "", None]
        )
        assert ids == ["1", "2"]
        assert names == ["movies", "tv shows"]


class TestExpandPathMappingCandidates:
    """Test multi-row path candidate fan-out across mapping rows."""

    def test_expand_candidates_webhook_to_multiple_plex_roots(self):
        """Webhook path /data should fan out to all matching Plex/local roots."""
        path_mappings = [
            {
                "plex_prefix": "/data_16tb",
                "local_prefix": "/data_16tb",
                "webhook_prefixes": ["/data"],
            },
            {
                "plex_prefix": "/data_16tb2",
                "local_prefix": "/data_16tb2",
                "webhook_prefixes": ["/data"],
            },
        ]
        candidates = expand_path_mapping_candidates(
            "/data/tv/Show/S01E01.mkv", path_mappings
        )
        assert "/data/tv/Show/S01E01.mkv" in candidates
        assert "/data_16tb/tv/Show/S01E01.mkv" in candidates
        assert "/data_16tb2/tv/Show/S01E01.mkv" in candidates

    def test_expand_candidates_local_to_multiple_plex_roots_without_webhook_aliases(
        self,
    ):
        """Legacy-style rows still fan out from local prefix to each Plex root."""
        path_mappings = [
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
        candidates = expand_path_mapping_candidates(
            "/data/tv/Show/S01E03.mkv", path_mappings
        )
        assert "/data/tv/Show/S01E03.mkv" in candidates
        assert "/data_16tb1/tv/Show/S01E03.mkv" in candidates
        assert "/data_16tb2/tv/Show/S01E03.mkv" in candidates


class TestNormalizePathMappings:
    """Test path_mappings normalization from settings (new format and legacy)."""

    def test_normalize_path_mappings_new_format(self):
        """New path_mappings list is validated and returned."""
        settings = {
            "path_mappings": [
                {
                    "plex_prefix": "/data",
                    "local_prefix": "/mnt/data",
                    "webhook_prefixes": [],
                },
                {
                    "plex_prefix": "/data_disk1",
                    "local_prefix": "/data",
                    "webhook_prefixes": ["/data"],
                },
            ]
        }
        result = normalize_path_mappings(settings)
        assert len(result) == 2
        assert result[0]["plex_prefix"] == "/data"
        assert result[0]["local_prefix"] == "/mnt/data"
        assert result[0]["webhook_prefixes"] == []
        assert result[1]["webhook_prefixes"] == ["/data"]

    def test_normalize_path_mappings_legacy(self):
        """Legacy semicolon pair is converted to path_mappings."""
        settings = {
            "plex_videos_path_mapping": "/data_disk1;/data_disk2",
            "plex_local_videos_path_mapping": "/data",
        }
        result = normalize_path_mappings(settings)
        assert len(result) == 2
        assert result[0] == {
            "plex_prefix": "/data_disk1",
            "local_prefix": "/data",
            "webhook_prefixes": [],
        }
        assert result[1] == {
            "plex_prefix": "/data_disk2",
            "local_prefix": "/data",
            "webhook_prefixes": [],
        }

    def test_normalize_path_mappings_empty(self):
        """Empty or missing settings returns empty list."""
        assert normalize_path_mappings({}) == []
        assert normalize_path_mappings({"path_mappings": []}) == []
        assert normalize_path_mappings({"plex_videos_path_mapping": ""}) == []

    def test_normalize_path_mappings_new_format_precedence_over_legacy(self):
        """When both path_mappings and legacy pair exist, new format wins."""
        settings = {
            "path_mappings": [
                {
                    "plex_prefix": "/plex",
                    "local_prefix": "/local",
                    "webhook_prefixes": [],
                },
            ],
            "plex_videos_path_mapping": "/legacy_plex",
            "plex_local_videos_path_mapping": "/legacy_local",
        }
        result = normalize_path_mappings(settings)
        assert len(result) == 1
        assert result[0]["plex_prefix"] == "/plex"
        assert result[0]["local_prefix"] == "/local"

    def test_normalize_path_mappings_skips_malformed_rows(self):
        """Non-dict entries and rows missing plex/local are skipped."""
        settings = {
            "path_mappings": [
                {"plex_prefix": "/a", "local_prefix": "/x", "webhook_prefixes": []},
                "not a dict",
                {"plex_prefix": "/b", "local_prefix": ""},  # missing local
                {"plex_prefix": "", "local_prefix": "/y"},  # missing plex
                {"local_prefix": "/z"},  # missing plex_prefix
                {
                    "plex_prefix": "/c",
                    "local_prefix": "/w",
                    "webhook_prefixes": ["/data"],
                },
            ]
        }
        result = normalize_path_mappings(settings)
        assert len(result) == 2
        assert result[0]["plex_prefix"] == "/a"
        assert result[0]["local_prefix"] == "/x"
        assert result[1]["plex_prefix"] == "/c"
        assert result[1]["local_prefix"] == "/w"
        assert result[1]["webhook_prefixes"] == ["/data"]

    def test_normalize_path_mappings_empty_vs_missing_webhook_prefixes(self):
        """Empty list and missing webhook_prefixes key both yield webhook_prefixes=[]."""
        for settings in [
            {"path_mappings": [{"plex_prefix": "/p", "local_prefix": "/l"}]},
            {
                "path_mappings": [
                    {"plex_prefix": "/p", "local_prefix": "/l", "webhook_prefixes": []}
                ]
            },
        ]:
            result = normalize_path_mappings(settings)
            assert len(result) == 1
            assert result[0]["webhook_prefixes"] == []


class TestNormalizeExcludePaths:
    """Test exclude_paths normalization from settings."""

    def test_normalize_exclude_paths_list_of_dicts(self):
        """List of {value, type} is normalized."""
        raw = [
            {"value": "/mnt/media/archive", "type": "path"},
            {"value": r".*\.iso$", "type": "regex"},
        ]
        result = normalize_exclude_paths(raw)
        assert len(result) == 2
        assert result[0] == {"value": "/mnt/media/archive", "type": "path"}
        assert result[1] == {"value": r".*\.iso$", "type": "regex"}

    def test_normalize_exclude_paths_list_of_strings(self):
        """List of strings is treated as path prefix entries."""
        raw = ["/mnt/foo", "/mnt/bar"]
        result = normalize_exclude_paths(raw)
        assert len(result) == 2
        assert result[0] == {"value": "/mnt/foo", "type": "path"}
        assert result[1] == {"value": "/mnt/bar", "type": "path"}

    def test_normalize_exclude_paths_empty(self):
        """Empty or missing returns empty list."""
        assert normalize_exclude_paths(None) == []
        assert normalize_exclude_paths([]) == []
        assert normalize_exclude_paths({}) == []

    def test_normalize_exclude_paths_skips_empty_value(self):
        """Entries with empty value are skipped."""
        raw = [{"value": "", "type": "path"}, {"value": "  ", "type": "regex"}]
        assert normalize_exclude_paths(raw) == []

    def test_normalize_exclude_paths_invalid_type_defaults_to_path(self):
        """Invalid type is normalized to path."""
        raw = [{"value": "/x", "type": "other"}]
        result = normalize_exclude_paths(raw)
        assert result[0]["type"] == "path"


class TestIsPathExcluded:
    """Test is_path_excluded (path prefix and regex)."""

    def test_is_path_excluded_empty(self):
        """No exclude list or empty path returns False."""
        assert is_path_excluded("", []) is False
        assert is_path_excluded("/mnt/foo", None) is False
        assert is_path_excluded("/mnt/foo", []) is False

    def test_is_path_excluded_path_prefix_match(self):
        """Path prefix excludes subpaths."""
        exclude = [{"value": "/mnt/media/archive", "type": "path"}]
        assert is_path_excluded("/mnt/media/archive", exclude) is True
        assert is_path_excluded("/mnt/media/archive/video.mkv", exclude) is True
        assert is_path_excluded("/mnt/media/archive/sub/movie.mkv", exclude) is True
        assert is_path_excluded("/mnt/media/other/video.mkv", exclude) is False
        assert is_path_excluded("/mnt/media/archived/video.mkv", exclude) is False

    def test_is_path_excluded_path_prefix_normalized(self):
        """Trailing slashes are handled for prefix match."""
        exclude = [{"value": "/mnt/media/foo/", "type": "path"}]
        assert is_path_excluded("/mnt/media/foo/video.mkv", exclude) is True
        assert is_path_excluded("/mnt/media/foo", exclude) is True

    def test_is_path_excluded_regex_match(self):
        """Regex type matches full path."""
        exclude = [{"value": r".*\.iso$", "type": "regex"}]
        assert is_path_excluded("/mnt/media/disc.iso", exclude) is True
        assert is_path_excluded("/any/path/file.iso", exclude) is True
        assert is_path_excluded("/mnt/media/disc.iso.bak", exclude) is False
        assert is_path_excluded("/mnt/media/video.mkv", exclude) is False

    def test_is_path_excluded_regex_invalid_skipped(self):
        """Invalid regex does not match and does not raise."""
        exclude = [{"value": "[invalid(regex", "type": "regex"}]
        assert is_path_excluded("/mnt/foo", exclude) is False

    def test_is_path_excluded_first_match_wins(self):
        """First matching rule excludes."""
        exclude = [
            {"value": "/mnt/media/archive", "type": "path"},
            {"value": r".*\.mkv$", "type": "regex"},
        ]
        assert is_path_excluded("/mnt/media/archive/video.mkv", exclude) is True
        assert is_path_excluded("/mnt/other/video.mkv", exclude) is True


class TestPathToCanonicalLocal:
    """Test path_to_canonical_local and plex_path_to_local."""

    def test_plex_to_local_single(self):
        """Plex path is mapped to local."""
        mappings = [
            {
                "plex_prefix": "/data",
                "local_prefix": "/mnt/data",
                "webhook_prefixes": [],
            }
        ]
        assert (
            path_to_canonical_local("/data/Movies/foo.mkv", mappings)
            == "/mnt/data/Movies/foo.mkv"
        )
        assert (
            plex_path_to_local("/data/Movies/foo.mkv", mappings)
            == "/mnt/data/Movies/foo.mkv"
        )

    def test_webhook_to_local(self):
        """Webhook path (webhook_prefixes) maps to same local as plex."""
        mappings = [
            {
                "plex_prefix": "/data_disk1",
                "local_prefix": "/data",
                "webhook_prefixes": ["/data"],
            }
        ]
        assert (
            path_to_canonical_local("/data/Movies/foo.mkv", mappings)
            == "/data/Movies/foo.mkv"
        )
        assert (
            path_to_canonical_local("/data_disk1/Movies/foo.mkv", mappings)
            == "/data/Movies/foo.mkv"
        )

    def test_no_partial_match(self):
        """Prefix /data does not match /database."""
        mappings = [
            {
                "plex_prefix": "/data",
                "local_prefix": "/mnt/data",
                "webhook_prefixes": [],
            }
        ]
        assert path_to_canonical_local("/database/x.mkv", mappings) == "/database/x.mkv"

    def test_empty_mappings_returns_unchanged(self):
        """Empty mappings returns path unchanged."""
        assert path_to_canonical_local("/data/foo.mkv", []) == "/data/foo.mkv"

    def test_mergerfs_multiple_plex_roots(self):
        """Multiple Plex roots map to one local (mergerfs)."""
        mappings = [
            {
                "plex_prefix": "/data_disk1",
                "local_prefix": "/data",
                "webhook_prefixes": [],
            },
            {
                "plex_prefix": "/data_disk2",
                "local_prefix": "/data",
                "webhook_prefixes": [],
            },
        ]
        assert (
            path_to_canonical_local("/data_disk1/Movies/a.mkv", mappings)
            == "/data/Movies/a.mkv"
        )
        assert (
            path_to_canonical_local("/data_disk2/TV/b.mkv", mappings)
            == "/data/TV/b.mkv"
        )

    def test_first_match_wins_overlapping_prefixes(self):
        """When multiple mappings could match, first matching row is used."""
        # /data is before /data_disk1; path /data_disk1/... matches second row only
        mappings = [
            {
                "plex_prefix": "/data",
                "local_prefix": "/mnt/data",
                "webhook_prefixes": [],
            },
            {
                "plex_prefix": "/data_disk1",
                "local_prefix": "/data",
                "webhook_prefixes": [],
            },
        ]
        assert (
            path_to_canonical_local("/data/Movies/foo.mkv", mappings)
            == "/mnt/data/Movies/foo.mkv"
        )
        assert (
            path_to_canonical_local("/data_disk1/Movies/foo.mkv", mappings)
            == "/data/Movies/foo.mkv"
        )

    def test_case_sensitive_prefix_match(self):
        """Prefix matching is case-sensitive; /Data does not match plex_prefix /data."""
        mappings = [
            {
                "plex_prefix": "/data",
                "local_prefix": "/mnt/data",
                "webhook_prefixes": [],
            }
        ]
        assert (
            path_to_canonical_local("/data/Movies/foo.mkv", mappings)
            == "/mnt/data/Movies/foo.mkv"
        )
        assert (
            path_to_canonical_local("/Data/Movies/foo.mkv", mappings)
            == "/Data/Movies/foo.mkv"
        )

    def test_first_match_plex_before_webhook_prefix(self):
        """When path matches both plex_prefix of row1 and webhook_prefix of row2, first row wins (plex)."""
        # Row1: plex /data -> /mnt/data; Row2: plex /other, local /other, webhook /data
        # Path /data/foo.mkv matches row1 by plex_prefix -> /mnt/data/foo.mkv
        mappings = [
            {
                "plex_prefix": "/data",
                "local_prefix": "/mnt/data",
                "webhook_prefixes": [],
            },
            {
                "plex_prefix": "/other",
                "local_prefix": "/other",
                "webhook_prefixes": ["/data"],
            },
        ]
        assert path_to_canonical_local("/data/foo.mkv", mappings) == "/mnt/data/foo.mkv"


class TestLocalPathToWebhookAliases:
    """Test local_path_to_webhook_aliases (for webhook matching when Plex and app see same disks)."""

    def test_returns_webhook_form_for_matching_row(self):
        """Local path under a row with webhook_prefixes returns that alias."""
        mappings = [
            {
                "plex_prefix": "/data_16tb1",
                "local_prefix": "/data_16tb1",
                "webhook_prefixes": ["/data"],
            }
        ]
        assert local_path_to_webhook_aliases(
            "/data_16tb1/Movies/foo.mkv", mappings
        ) == ["/data/Movies/foo.mkv"]

    def test_returns_empty_when_no_webhook_prefix(self):
        """Row without webhook_prefixes adds no alias."""
        mappings = [
            {
                "plex_prefix": "/data",
                "local_prefix": "/mnt/data",
                "webhook_prefixes": [],
            }
        ]
        assert local_path_to_webhook_aliases("/mnt/data/Movies/foo.mkv", mappings) == []

    def test_returns_empty_for_empty_mappings(self):
        assert local_path_to_webhook_aliases("/data/foo.mkv", []) == []

    def test_multiple_webhook_prefixes_returns_multiple_aliases(self):
        """Row with multiple webhook_prefixes returns one alias per prefix."""
        mappings = [
            {
                "plex_prefix": "/data_16tb1",
                "local_prefix": "/data_16tb1",
                "webhook_prefixes": ["/data", "/merged"],
            }
        ]
        result = local_path_to_webhook_aliases("/data_16tb1/Movies/foo.mkv", mappings)
        assert set(result) == {"/data/Movies/foo.mkv", "/merged/Movies/foo.mkv"}

    def test_skips_webhook_prefix_same_as_local_prefix(self):
        """When webhook_prefix equals local_prefix, that alias is skipped (no self-alias)."""
        mappings = [
            {
                "plex_prefix": "/data",
                "local_prefix": "/data",
                "webhook_prefixes": ["/data"],
            }
        ]
        # Implementation skips wp == local_prefix
        result = local_path_to_webhook_aliases("/data/Movies/foo.mkv", mappings)
        assert result == []


class TestLoadConfig:
    """Test configuration loading and validation."""

    @patch("shutil.which")
    @patch("subprocess.run")
    @patch("os.path.exists")
    @patch("os.path.isdir")
    @patch("os.listdir")
    @patch("os.access")
    @patch("os.statvfs", create=True)
    @patch("plex_generate_previews.logging_config.setup_logging")
    def test_load_config_all_required_present(
        self,
        mock_logging,
        mock_statvfs,
        mock_access,
        mock_listdir,
        mock_isdir,
        mock_exists,
        mock_run,
        mock_which,
    ):
        """Test that valid config loads successfully."""
        # Mock FFmpeg
        mock_which.return_value = "/usr/bin/ffmpeg"
        mock_run.return_value = MagicMock(returncode=0, stdout="ffmpeg version 7.0.0")

        # Mock file system - need to handle nested directory checks
        def mock_exists_fn(path):
            return True

        def mock_listdir_fn(path):
            # Check the specific path to determine what to return
            if "tmp" in path or path.startswith("/tmp"):
                # Tmp folder should be empty or not exist
                return []
            elif (
                path.endswith("/localhost")
                or "/localhost" in path
                and not path.endswith("Media")
            ):
                # Inside localhost directory - return hex folders
                return [
                    "0",
                    "1",
                    "2",
                    "3",
                    "4",
                    "5",
                    "6",
                    "7",
                    "8",
                    "9",
                    "a",
                    "b",
                    "c",
                    "d",
                    "e",
                    "f",
                ]
            elif path.endswith("/Media"):
                # Inside Media directory - return localhost
                return ["localhost"]
            else:
                # Top-level Plex directory - return standard folders
                return ["Cache", "Media", "Metadata", "Plug-ins", "Logs"]

        mock_exists.side_effect = mock_exists_fn
        mock_isdir.return_value = True
        mock_listdir.side_effect = mock_listdir_fn
        mock_access.return_value = True

        # Mock disk space
        statvfs_result = MagicMock()
        statvfs_result.f_frsize = 4096
        statvfs_result.f_bavail = 1024 * 1024 * 250  # 1GB+ free
        mock_statvfs.return_value = statvfs_result

        # Create args - use SimpleNamespace to avoid MagicMock attribute issues
        from types import SimpleNamespace

        args = SimpleNamespace(
            plex_url="http://localhost:32400",
            plex_token="test_token",
            plex_config_folder="/config/plex/Library/Application Support/Plex Media Server",
            plex_timeout=60,
            plex_libraries=None,
            plex_local_videos_path_mapping=None,
            plex_videos_path_mapping=None,
            plex_bif_frame_interval=None,
            thumbnail_quality=None,
            regenerate_thumbnails=False,
            gpu_threads=None,
            cpu_threads=None,
            gpu_selection=None,
            tmp_folder="/tmp/plex_generate_previews",
            log_level=None,
        )

        config = load_config(args)

        assert config is not None
        assert config.plex_url == "http://localhost:32400"
        assert config.plex_token == "test_token"
        assert config.plex_verify_ssl is True

    @patch("shutil.which")
    @patch("plex_generate_previews.logging_config.setup_logging")
    def test_load_config_missing_plex_url(self, mock_logging, mock_which):
        """Test error when PLEX_URL is missing."""
        mock_which.return_value = "/usr/bin/ffmpeg"

        from types import SimpleNamespace

        args = SimpleNamespace(
            plex_url=None,
            plex_token="token",
            plex_config_folder="/config/plex",
            plex_timeout=None,
            plex_libraries=None,
            plex_local_videos_path_mapping=None,
            plex_videos_path_mapping=None,
            plex_bif_frame_interval=None,
            thumbnail_quality=None,
            regenerate_thumbnails=False,
            gpu_threads=None,
            cpu_threads=None,
            gpu_selection=None,
            tmp_folder=None,
            log_level=None,
        )

        with patch.dict("os.environ", {}, clear=True):
            config = load_config(args)
            assert config is None

    @patch("shutil.which")
    @patch("plex_generate_previews.logging_config.setup_logging")
    def test_load_config_missing_plex_token(self, mock_logging, mock_which):
        """Test error when PLEX_TOKEN is missing."""
        mock_which.return_value = "/usr/bin/ffmpeg"

        from types import SimpleNamespace

        args = SimpleNamespace(
            plex_url="http://localhost:32400",
            plex_token=None,
            plex_config_folder="/config/plex",
            plex_timeout=None,
            plex_libraries=None,
            plex_local_videos_path_mapping=None,
            plex_videos_path_mapping=None,
            plex_bif_frame_interval=None,
            thumbnail_quality=None,
            regenerate_thumbnails=False,
            gpu_threads=None,
            cpu_threads=None,
            gpu_selection=None,
            tmp_folder=None,
            log_level=None,
        )

        with patch.dict("os.environ", {}, clear=True):
            config = load_config(args)
            assert config is None

    @patch("shutil.which")
    @patch("plex_generate_previews.config.load_dotenv")
    @patch("plex_generate_previews.logging_config.setup_logging")
    def test_load_config_missing_config_folder(
        self, mock_logging, mock_load_dotenv, mock_which
    ):
        """Test error when config folder is missing."""
        mock_which.return_value = "/usr/bin/ffmpeg"
        # Prevent load_dotenv from repopulating os.environ so missing_params is triggered
        mock_load_dotenv.return_value = None

        from types import SimpleNamespace

        args = SimpleNamespace(
            plex_url="http://localhost:32400",
            plex_token="token",
            plex_config_folder=None,
            plex_timeout=None,
            plex_libraries=None,
            plex_local_videos_path_mapping=None,
            plex_videos_path_mapping=None,
            plex_bif_frame_interval=None,
            thumbnail_quality=None,
            regenerate_thumbnails=False,
            gpu_threads=None,
            cpu_threads=None,
            gpu_selection=None,
            tmp_folder=None,
            log_level=None,
        )

        with patch.dict("os.environ", {}, clear=True):
            config = load_config(args)
            assert config is None

    @patch("shutil.which")
    @patch("subprocess.run")
    @patch("os.path.exists")
    @patch("plex_generate_previews.config.load_dotenv")
    @patch("plex_generate_previews.logging_config.setup_logging")
    def test_load_config_invalid_path(
        self, mock_logging, mock_load_dotenv, mock_exists, mock_run, mock_which
    ):
        """Test error when config folder doesn't exist."""
        mock_which.return_value = "/usr/bin/ffmpeg"
        mock_run.return_value = MagicMock(returncode=0, stdout="ffmpeg version 7.0.0")
        mock_exists.return_value = False

        from types import SimpleNamespace

        args = SimpleNamespace(
            plex_url="http://localhost:32400",
            plex_token="token",
            plex_config_folder="/nonexistent/path",
            plex_timeout=None,
            plex_libraries=None,
            plex_local_videos_path_mapping=None,
            plex_videos_path_mapping=None,
            plex_bif_frame_interval=None,
            thumbnail_quality=None,
            regenerate_thumbnails=False,
            gpu_threads=None,
            cpu_threads=None,
            gpu_selection=None,
            tmp_folder=None,
            log_level=None,
        )

        config = load_config(args)
        assert config is None

    @patch("shutil.which")
    @patch("subprocess.run")
    @patch("os.listdir")
    @patch("os.path.isdir")
    @patch("os.path.exists")
    @patch("plex_generate_previews.logging_config.setup_logging")
    def test_load_config_invalid_plex_structure(
        self, mock_logging, mock_exists, mock_isdir, mock_listdir, mock_run, mock_which
    ):
        """Test error when folder doesn't have Plex structure."""
        mock_which.return_value = "/usr/bin/ffmpeg"
        mock_run.return_value = MagicMock(returncode=0, stdout="ffmpeg version 7.0.0")
        mock_exists.return_value = True
        mock_isdir.return_value = True
        mock_listdir.return_value = ["random", "folders"]  # Missing Cache and Media

        from types import SimpleNamespace

        args = SimpleNamespace(
            plex_url="http://localhost:32400",
            plex_token="token",
            plex_config_folder="/wrong/folder",
            plex_timeout=None,
            plex_libraries=None,
            plex_local_videos_path_mapping=None,
            plex_videos_path_mapping=None,
            plex_bif_frame_interval=None,
            thumbnail_quality=None,
            regenerate_thumbnails=False,
            gpu_threads=None,
            cpu_threads=None,
            gpu_selection=None,
            tmp_folder=None,
            log_level=None,
        )

        config = load_config(args)
        assert config is None

    @patch("shutil.which")
    @patch("subprocess.run")
    @patch("os.statvfs", create=True)
    @patch("os.access")
    @patch("os.listdir")
    @patch("os.path.isdir")
    @patch("os.path.exists")
    @patch("plex_generate_previews.logging_config.setup_logging")
    def test_load_config_validates_numeric_ranges(
        self,
        mock_logging,
        mock_exists,
        mock_isdir,
        mock_listdir,
        mock_access,
        mock_statvfs,
        mock_run,
        mock_which,
    ):
        """Test validation of numeric ranges."""
        mock_which.return_value = "/usr/bin/ffmpeg"
        mock_run.return_value = MagicMock(returncode=0)
        mock_exists.return_value = True
        mock_isdir.return_value = True
        mock_listdir.side_effect = lambda path: (
            ["Cache", "Media"]
            if "Plex Media Server" in path
            else ["0", "1", "2", "a", "b", "c"]
        )
        mock_access.return_value = True
        statvfs_result = MagicMock()
        statvfs_result.f_frsize = 4096
        statvfs_result.f_bavail = 1024 * 1024 * 250
        mock_statvfs.return_value = statvfs_result

        args = MagicMock()
        args.plex_url = "http://localhost:32400"
        args.plex_token = "token"
        args.plex_config_folder = (
            "/config/plex/Library/Application Support/Plex Media Server"
        )
        args.plex_timeout = None
        args.plex_libraries = None
        args.plex_local_videos_path_mapping = None
        args.plex_videos_path_mapping = None
        args.plex_bif_frame_interval = 100  # Invalid: > 60
        args.thumbnail_quality = None
        args.regenerate_thumbnails = False
        args.gpu_threads = None
        args.cpu_threads = None
        args.gpu_selection = None
        args.tmp_folder = "/tmp/plex_generate_previews"
        args.log_level = None

        config = load_config(args)

        # Should fail validation due to invalid frame interval
        assert config is None

    @patch("shutil.which")
    @patch("subprocess.run")
    @patch("os.statvfs", create=True)
    @patch("os.access")
    @patch("os.listdir")
    @patch("os.path.isdir")
    @patch("os.path.exists")
    @patch("plex_generate_previews.logging_config.setup_logging")
    def test_load_config_validates_thread_counts(
        self,
        mock_logging,
        mock_exists,
        mock_isdir,
        mock_listdir,
        mock_access,
        mock_statvfs,
        mock_run,
        mock_which,
    ):
        """Test validation of thread counts."""
        mock_which.return_value = "/usr/bin/ffmpeg"
        mock_run.return_value = MagicMock(returncode=0)
        mock_exists.return_value = True
        mock_isdir.return_value = True
        mock_listdir.side_effect = lambda path: (
            ["Cache", "Media"] if "Plex Media Server" in path else ["0", "1", "a", "b"]
        )
        mock_access.return_value = True
        statvfs_result = MagicMock()
        statvfs_result.f_frsize = 4096
        statvfs_result.f_bavail = 1024 * 1024 * 250
        mock_statvfs.return_value = statvfs_result

        args = MagicMock()
        args.plex_url = "http://localhost:32400"
        args.plex_token = "token"
        args.plex_config_folder = (
            "/config/plex/Library/Application Support/Plex Media Server"
        )
        args.plex_timeout = None
        args.plex_libraries = None
        args.plex_local_videos_path_mapping = None
        args.plex_videos_path_mapping = None
        args.plex_bif_frame_interval = None
        args.thumbnail_quality = None
        args.regenerate_thumbnails = False
        args.gpu_threads = 50  # Invalid: > 32
        args.cpu_threads = None
        args.gpu_selection = None
        args.tmp_folder = "/tmp/plex_generate_previews"
        args.log_level = None

        config = load_config(args)

        # Should fail validation due to invalid thread count
        assert config is None

    @patch("shutil.which")
    @patch("subprocess.run")
    @patch("os.statvfs", create=True)
    @patch("os.access")
    @patch("os.listdir")
    @patch("os.path.isdir")
    @patch("os.path.exists")
    @patch("plex_generate_previews.logging_config.setup_logging")
    def test_load_config_validates_ffmpeg_threads(
        self,
        mock_logging,
        mock_exists,
        mock_isdir,
        mock_listdir,
        mock_access,
        mock_statvfs,
        mock_run,
        mock_which,
    ):
        """Test validation rejects ffmpeg_threads outside 0-32."""
        mock_which.return_value = "/usr/bin/ffmpeg"
        mock_run.return_value = MagicMock(returncode=0)
        mock_exists.return_value = True
        mock_isdir.return_value = True
        mock_listdir.side_effect = lambda path: (
            ["Cache", "Media"] if "Plex Media Server" in path else ["0", "1", "a", "b"]
        )
        mock_access.return_value = True
        statvfs_result = MagicMock()
        statvfs_result.f_frsize = 4096
        statvfs_result.f_bavail = 1024 * 1024 * 250
        mock_statvfs.return_value = statvfs_result

        args = MagicMock()
        args.plex_url = "http://localhost:32400"
        args.plex_token = "token"
        args.plex_config_folder = (
            "/config/plex/Library/Application Support/Plex Media Server"
        )
        args.plex_timeout = None
        args.plex_libraries = None
        args.plex_local_videos_path_mapping = None
        args.plex_videos_path_mapping = None
        args.plex_bif_frame_interval = None
        args.thumbnail_quality = None
        args.regenerate_thumbnails = False
        args.gpu_threads = None
        args.cpu_threads = None
        args.ffmpeg_threads = 50  # Invalid: > 32
        args.gpu_selection = None
        args.tmp_folder = "/tmp/plex_generate_previews"
        args.log_level = None

        config = load_config(args)

        # Should fail validation due to invalid ffmpeg_threads
        assert config is None

    @patch("shutil.which")
    @patch("subprocess.run")
    @patch("os.statvfs", create=True)
    @patch("os.access")
    @patch("os.listdir")
    @patch("os.path.isdir")
    @patch("os.path.exists")
    @patch("plex_generate_previews.logging_config.setup_logging")
    def test_load_config_validates_gpu_selection(
        self,
        mock_logging,
        mock_exists,
        mock_isdir,
        mock_listdir,
        mock_access,
        mock_statvfs,
        mock_run,
        mock_which,
    ):
        """Test validation of GPU selection format."""
        mock_which.return_value = "/usr/bin/ffmpeg"
        mock_run.return_value = MagicMock(returncode=0)
        mock_exists.return_value = True
        mock_isdir.return_value = True
        mock_listdir.side_effect = lambda path: (
            ["Cache", "Media"] if "Plex Media Server" in path else ["0", "1", "a", "b"]
        )
        mock_access.return_value = True
        statvfs_result = MagicMock()
        statvfs_result.f_frsize = 4096
        statvfs_result.f_bavail = 1024 * 1024 * 250
        mock_statvfs.return_value = statvfs_result

        args = MagicMock()
        args.plex_url = "http://localhost:32400"
        args.plex_token = "token"
        args.plex_config_folder = (
            "/config/plex/Library/Application Support/Plex Media Server"
        )
        args.plex_timeout = None
        args.plex_libraries = None
        args.plex_local_videos_path_mapping = None
        args.plex_videos_path_mapping = None
        args.plex_bif_frame_interval = None
        args.thumbnail_quality = None
        args.regenerate_thumbnails = False
        args.gpu_threads = None
        args.cpu_threads = None
        args.gpu_selection = "invalid,format,abc"  # Contains non-numeric
        args.tmp_folder = "/tmp/plex_generate_previews"
        args.log_level = None

        config = load_config(args)

        # Should fail validation due to invalid GPU selection
        assert config is None

    @patch("shutil.which")
    @patch("subprocess.run")
    @patch("os.statvfs", create=True)
    @patch("os.access")
    @patch("os.listdir")
    @patch("os.path.isdir")
    @patch("os.path.exists")
    @patch("os.makedirs")
    @patch("plex_generate_previews.logging_config.setup_logging")
    def test_load_config_tmp_folder_auto_creation(
        self,
        mock_logging,
        mock_makedirs,
        mock_exists,
        mock_isdir,
        mock_listdir,
        mock_access,
        mock_statvfs,
        mock_run,
        mock_which,
    ):
        """Test that temp folder is auto-created if it doesn't exist."""
        mock_which.return_value = "/usr/bin/ffmpeg"
        mock_run.return_value = MagicMock(returncode=0, stdout="ffmpeg version 7.0.0")

        # Mock that tmp_folder doesn't exist initially, but plex_config_folder does
        def mock_exists_side_effect(path):
            if path == "/tmp/plex_generate_previews":
                return False  # tmp folder doesn't exist
            return True  # other paths exist

        def mock_listdir_fn(path):
            if "tmp" in path or path.startswith("/tmp"):
                return []
            elif (
                path.endswith("/localhost")
                or "/localhost" in path
                and not path.endswith("Media")
            ):
                return [
                    "0",
                    "1",
                    "2",
                    "3",
                    "4",
                    "5",
                    "6",
                    "7",
                    "8",
                    "9",
                    "a",
                    "b",
                    "c",
                    "d",
                    "e",
                    "f",
                ]
            elif path.endswith("/Media"):
                return ["localhost"]
            else:
                return ["Cache", "Media", "Metadata", "Plug-ins", "Logs"]

        mock_exists.side_effect = mock_exists_side_effect
        mock_isdir.return_value = True
        mock_listdir.side_effect = mock_listdir_fn
        mock_access.return_value = True

        # Mock statvfs for disk space check
        mock_stat = MagicMock()
        mock_stat.f_frsize = 4096
        mock_stat.f_bavail = 1024 * 1024  # Plenty of space
        mock_statvfs.return_value = mock_stat

        from types import SimpleNamespace

        args = SimpleNamespace(
            plex_url="http://localhost:32400",
            plex_token="token",
            plex_config_folder="/config/plex",
            tmp_folder="/tmp/plex_generate_previews",
            plex_timeout=None,
            plex_libraries=None,
            plex_local_videos_path_mapping=None,
            plex_videos_path_mapping=None,
            plex_bif_frame_interval=None,
            thumbnail_quality=None,
            regenerate_thumbnails=False,
            gpu_threads=None,
            cpu_threads=None,
            gpu_selection=None,
            log_level=None,
        )

        config = load_config(args)

        # Should succeed and create the folder
        assert config is not None
        assert config.tmp_folder_created_by_us is True
        mock_makedirs.assert_called_once_with(
            "/tmp/plex_generate_previews", exist_ok=True
        )

    @patch("shutil.which")
    @patch("subprocess.run")
    @patch("os.statvfs", create=True)
    @patch("os.access")
    @patch("os.listdir")
    @patch("os.path.isdir")
    @patch("os.path.exists")
    @patch("plex_generate_previews.logging_config.setup_logging")
    def test_load_config_tmp_folder_not_empty(
        self,
        mock_logging,
        mock_exists,
        mock_isdir,
        mock_listdir,
        mock_access,
        mock_statvfs,
        mock_run,
        mock_which,
    ):
        """Test that config loads successfully even if tmp folder is not empty."""
        mock_which.return_value = "/usr/bin/ffmpeg"
        mock_run.return_value = MagicMock(returncode=0, stdout="ffmpeg version 7.0.0")

        def mock_exists_side_effect(path):
            return True  # All paths exist

        def mock_listdir_side_effect(path):
            if path == "/tmp/plex_generate_previews":
                return [
                    "file1.txt",
                    "file2.txt",
                ]  # tmp folder has contents - should be OK
            elif (
                path.endswith("/localhost")
                or "/localhost" in path
                and not path.endswith("Media")
            ):
                return [
                    "0",
                    "1",
                    "2",
                    "3",
                    "4",
                    "5",
                    "6",
                    "7",
                    "8",
                    "9",
                    "a",
                    "b",
                    "c",
                    "d",
                    "e",
                    "f",
                ]
            elif path.endswith("/Media"):
                return ["localhost"]
            else:
                return ["Cache", "Media", "Metadata", "Plug-ins", "Logs"]

        mock_exists.side_effect = mock_exists_side_effect
        mock_isdir.return_value = True
        mock_listdir.side_effect = mock_listdir_side_effect
        mock_access.return_value = True

        # Mock statvfs for disk space check
        mock_stat = MagicMock()
        mock_stat.f_frsize = 4096
        mock_stat.f_bavail = 1024 * 1024  # Plenty of space
        mock_statvfs.return_value = mock_stat

        from types import SimpleNamespace

        args = SimpleNamespace(
            plex_url="http://localhost:32400",
            plex_token="token",
            plex_config_folder="/config/plex",
            tmp_folder="/tmp/plex_generate_previews",
            plex_timeout=None,
            plex_libraries=None,
            plex_local_videos_path_mapping=None,
            plex_videos_path_mapping=None,
            plex_bif_frame_interval=None,
            thumbnail_quality=None,
            regenerate_thumbnails=False,
            gpu_threads=None,
            cpu_threads=None,
            gpu_selection=None,
            log_level=None,
        )

        config = load_config(args)

        # Should succeed even though tmp folder is not empty
        assert config is not None
        assert config.tmp_folder == "/tmp/plex_generate_previews"

    @patch("shutil.which")
    @patch("plex_generate_previews.logging_config.setup_logging")
    def test_load_config_ffmpeg_not_found(self, mock_logging, mock_which):
        """Test error when FFmpeg is not found."""
        mock_which.return_value = None

        args = MagicMock()
        args.plex_url = "http://localhost:32400"
        args.plex_token = "token"
        args.plex_config_folder = "/config/plex"

        with pytest.raises(SystemExit):
            load_config(args)

    @patch("shutil.which")
    @patch("subprocess.run")
    @patch("os.statvfs", create=True)
    @patch("os.access")
    @patch("os.listdir")
    @patch("os.path.isdir")
    @patch("os.path.exists")
    @patch("plex_generate_previews.utils.is_docker_environment")
    @patch("plex_generate_previews.logging_config.setup_logging")
    def test_load_config_docker_environment(
        self,
        mock_logging,
        mock_docker,
        mock_exists,
        mock_isdir,
        mock_listdir,
        mock_access,
        mock_statvfs,
        mock_run,
        mock_which,
    ):
        """Test Docker-specific error messages."""
        mock_docker.return_value = True
        mock_which.return_value = "/usr/bin/ffmpeg"
        mock_run.return_value = MagicMock(returncode=0, stdout="ffmpeg version 7.0.0")

        # Setup filesystem mocks (even though we expect early failure)
        mock_exists.return_value = True
        mock_isdir.return_value = True
        mock_listdir.return_value = ["Cache", "Media"]
        mock_access.return_value = True
        statvfs_result = MagicMock()
        statvfs_result.f_frsize = 4096
        statvfs_result.f_bavail = 1024 * 1024 * 250  # 1GB+ free
        mock_statvfs.return_value = statvfs_result

        from types import SimpleNamespace

        args = SimpleNamespace(
            plex_url=None,  # Missing required field
            plex_token="token",
            plex_config_folder="/config/plex",
            plex_timeout=None,
            plex_libraries=None,
            plex_local_videos_path_mapping=None,
            plex_videos_path_mapping=None,
            plex_bif_frame_interval=None,
            thumbnail_quality=None,
            regenerate_thumbnails=False,
            gpu_threads=None,
            cpu_threads=None,
            gpu_selection=None,
            tmp_folder=None,
            log_level=None,
        )

        with patch.dict("os.environ", {}, clear=True):
            config = load_config(args)
            assert config is None

    @patch("shutil.which")
    @patch("subprocess.run")
    @patch("os.statvfs", create=True)
    @patch("os.access")
    @patch("os.listdir")
    @patch("os.path.isdir")
    @patch("os.path.exists")
    @patch("plex_generate_previews.logging_config.setup_logging")
    def test_load_config_comma_separated_libraries(
        self,
        mock_logging,
        mock_exists,
        mock_isdir,
        mock_listdir,
        mock_access,
        mock_statvfs,
        mock_run,
        mock_which,
    ):
        """Test parsing comma-separated library list."""
        mock_which.return_value = "/usr/bin/ffmpeg"
        mock_run.return_value = MagicMock(returncode=0, stdout="ffmpeg version 7.0.0")
        mock_exists.return_value = True
        mock_isdir.return_value = True

        def mock_listdir_fn(path):
            # Check the specific path to determine what to return
            if "tmp" in path or path.startswith("/tmp"):
                # Tmp folder should be empty or not exist
                return []
            elif (
                path.endswith("/localhost")
                or "/localhost" in path
                and not path.endswith("Media")
            ):
                # Inside localhost directory - return hex folders
                return [
                    "0",
                    "1",
                    "2",
                    "3",
                    "4",
                    "5",
                    "6",
                    "7",
                    "8",
                    "9",
                    "a",
                    "b",
                    "c",
                    "d",
                    "e",
                    "f",
                ]
            elif path.endswith("/Media"):
                # Inside Media directory - return localhost
                return ["localhost"]
            else:
                # Top-level Plex directory - return standard folders
                return ["Cache", "Media", "Metadata", "Plug-ins", "Logs"]

        mock_listdir.side_effect = mock_listdir_fn
        mock_access.return_value = True
        statvfs_result = MagicMock()
        statvfs_result.f_frsize = 4096
        statvfs_result.f_bavail = 1024 * 1024 * 250
        mock_statvfs.return_value = statvfs_result

        from types import SimpleNamespace

        args = SimpleNamespace(
            plex_url="http://localhost:32400",
            plex_token="token",
            plex_config_folder="/config/plex/Library/Application Support/Plex Media Server",
            plex_timeout=None,
            plex_libraries="Movies, TV Shows, Anime",
            plex_local_videos_path_mapping=None,
            plex_videos_path_mapping=None,
            plex_bif_frame_interval=None,
            thumbnail_quality=None,
            regenerate_thumbnails=False,
            gpu_threads=None,
            cpu_threads=None,
            gpu_selection=None,
            tmp_folder="/tmp/plex_generate_previews",
            log_level=None,
        )

        config = load_config(args)

        assert config is not None
        assert "movies" in config.plex_libraries
        assert "tv shows" in config.plex_libraries
        assert "anime" in config.plex_libraries


class TestDockerHelp:
    """Test docker help rendering."""

    @patch("plex_generate_previews.config.logger.info")
    def test_show_docker_help_logs_key_sections(self, mock_info):
        """Ensure Docker help prints required guidance lines."""
        show_docker_help()
        logged_lines = [call.args[0] for call in mock_info.call_args_list]
        assert any("Docker Environment Detected" in line for line in logged_lines)
        assert any("Required Environment Variables" in line for line in logged_lines)
        assert any("Example Docker Run Command" in line for line in logged_lines)
