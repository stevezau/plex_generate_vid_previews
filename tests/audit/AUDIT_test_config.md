# Audit: tests/test_config.py — 107 tests, 15 classes

## TestGetConfigValue

| Line | Test | Verdict |
|---|---|---|
| 84 | `test_get_config_value_cli_precedence` | **Strong** — strict equality + precedence pin |
| 93 | `test_get_config_value_env_fallback` | **Strong** — strict equality |
| 102 | `test_get_config_value_default_fallback` | **Strong** — strict equality |
| 111 | `test_get_config_value_boolean_conversion` | **Strong** — sweeps multiple truthy/falsy strings |
| 128 | `test_get_config_value_int_conversion` | **Strong** — strict `== 42` |
| 137 | `test_get_config_value_handles_non_vars_cli_object` | **Strong** — non-vars CLI object falls through correctly |

## TestGetPathMappingPairs

| Line | Test | Verdict |
|---|---|---|
| 151 | `test_get_path_mapping_pairs_single` | **Strong** — full list equality |
| 155 | `test_get_path_mapping_pairs_mergefs` | **Strong** — full list equality, 3 entries |
| 163 | `test_get_path_mapping_pairs_same_count` | **Strong** — full list equality |
| 170 | `test_get_path_mapping_pairs_empty` | **Strong** — exhaustive empty matrix (5 cells: empty, missing, both empty, None, ...) |
| 178 | `test_get_path_mapping_pairs_strips_whitespace` | **Strong** — full list equality after stripping |
| 185 | `test_get_path_mapping_pairs_mismatched_lengths_fallback` | **Strong** — backward-compat pin |

## TestSplitLibrarySelectors

| Line | Test | Verdict |
|---|---|---|
| 193 | `test_split_library_selectors_ids_only` | **Strong** — strict list equality both buckets |
| 199 | `test_split_library_selectors_names_only` | **Strong** — case-folding pinned |
| 205 | `test_split_library_selectors_mixed_and_deduplicated` | **Strong** — dedup + ordering pinned |
| 211 | `test_split_library_selectors_jellyfin_uuid_is_id` | **Strong** — closes silent-misclassification regression with explicit comment |
| 237 | `test_split_library_selectors_non_uuid_non_digit_stays_title` | **Strong** — boundary cell |

## TestExpandPathMappingCandidates

| Line | Test | Verdict |
|---|---|---|
| 247 | `test_expand_candidates_webhook_to_multiple_plex_roots` | **Strong** — multi-root fan-out asserted via `in candidates` (substring on a known finite set is fine) |
| 266 | `test_expand_candidates_local_to_multiple_plex_roots_without_webhook_aliases` | **Strong** — same shape, legacy variant |
| 287 | `test_expand_candidates_windows_backslash_path` | **Strong** — Windows path normalisation |

## TestNormalizePathMappings

| Line | Test | Verdict |
|---|---|---|
| 304 | `test_normalize_path_mappings_new_format` | **Strong** — len + per-key equality |
| 327 | `test_normalize_path_mappings_legacy` | **Strong** — full dict equality on each row |
| 346 | `test_normalize_path_mappings_empty` | **Strong** — exhaustive empty matrix |
| 352 | `test_normalize_path_mappings_new_format_precedence_over_legacy` | **Strong** — full row equality |
| 370 | `test_normalize_path_mappings_accepts_modern_remote_prefix_key` | **Strong** — closes silent-drop regression |
| 392 | `test_normalize_path_mappings_skips_malformed_rows` | **Strong** — sweeps 6 input shapes, asserts 2 surviving with full-key equality |
| 416 | `test_normalize_path_mappings_empty_vs_missing_webhook_prefixes` | **Strong** — both branches pinned |

## TestNormalizeExcludePaths

| Line | Test | Verdict |
|---|---|---|
| 430 | `test_normalize_exclude_paths_list_of_dicts` | **Strong** — full row equality |
| 441 | `test_normalize_exclude_paths_list_of_strings` | **Strong** — full row equality |
| 449 | `test_normalize_exclude_paths_empty` | **Strong** — exhaustive (None, [], {}) |
| 455 | `test_normalize_exclude_paths_skips_empty_value` | **Strong** — `== []` strict |
| 460 | `test_normalize_exclude_paths_invalid_type_defaults_to_path` | **Strong** — strict type field check |

## TestIsPathExcluded

| Line | Test | Verdict |
|---|---|---|
| 470 | `test_is_path_excluded_empty` | **Strong** — 3 boundary cells |
| 476 | `test_is_path_excluded_path_prefix_match` | **Strong** — 5 sub-cells incl. negative cases |
| 485 | `test_is_path_excluded_path_prefix_normalized` | **Strong** — trailing slash handling |
| 491 | `test_is_path_excluded_regex_match` | **Strong** — 4 cells with `is True/False` |
| 499 | `test_is_path_excluded_regex_invalid_skipped` | **Strong** — invalid regex doesn't raise |
| 504 | `test_is_path_excluded_first_match_wins` | **Strong** — precedence pin |

## TestPathToCanonicalLocal

All 11 tests in this class (lines 517–652) — **Strong**: exact path-string equality with diagnostic positioning. Covers single, mergerfs, multi-root, first-match-wins, case-sensitivity, Windows backslash variants. Each test uses `==` on the full output path.

## TestLocalPathToWebhookAliases

| Line | Test | Verdict |
|---|---|---|
| 658 | `test_returns_webhook_form_for_matching_row` | **Strong** — full list equality |
| 669 | `test_returns_empty_when_no_webhook_prefix` | **Strong** — `== []` |
| 680 | `test_returns_empty_for_empty_mappings` | **Strong** |
| 683 | `test_multiple_webhook_prefixes_returns_multiple_aliases` | **Strong** — set comparison |
| 695 | `test_skips_webhook_prefix_same_as_local_prefix` | **Strong** — self-alias prevention |

## TestDeriveLegacyPlexView

| Line | Test | Verdict |
|---|---|---|
| 717 | `test_empty_list_returns_empty` | **Strong** |
| 720 | `test_non_list_returns_empty` | **Strong** — None + string |
| 724 | `test_no_plex_entry_returns_empty` | **Strong** — Emby-only host |
| 734 | `test_disabled_plex_entry_is_skipped` | **Strong** |
| 740 | `test_first_enabled_plex_wins_over_later_entries` | **Strong** — strict equality on URL+token |
| 763 | `test_full_projection_round_trip` | **Strong** — full dict equality (the load-bearing assertion); excellent regression guard |
| 799 | `test_empty_optional_fields_omitted` | **Strong** — pins absent-keys contract |
| 824 | `test_server_id_pin_picks_specific_plex_in_multi_plex_install` | **Strong** — multi-plex pin pin |
| 853 | `test_server_id_pin_unknown_falls_back_to_first_plex` | **Strong** — unknown ID falls back |
| 862 | `test_invalid_timeout_is_skipped` | **Strong** — garbage timeout handling |
| 877 | `test_server_display_name_uses_name_then_falls_back_to_id` | **Strong** — both branches |
| 898 | `test_pinned_server_picks_its_own_path_mappings_in_multi_plex` | **Strong** — closes K3 silent path-mapping bug |

## TestLoadConfig

| Line | Test | Verdict |
|---|---|---|
| 949 | `test_load_config_all_required_present` | **Strong** — config.plex_url/token/verify_ssl checked |
| 1047 | `test_load_config_succeeds_when_both_cpu_and_gpu_zero` | **Strong** — strict equality on threads |
| 1119 | `test_load_config_succeeds_when_only_emby_configured` | **Strong** — closes Emby-only validation regression |
| 1188 | `test_load_config_accepts_sort_by_random` | **Strong** — `cfg.sort_by == "random"` |
| 1244 | `test_load_config_rejects_invalid_sort_by` | **Strong** — `pytest.raises(ConfigValidationError)` |
| 1294 | `test_load_config_missing_plex_url` | **Strong** — raises check |
| 1323 | `test_load_config_missing_plex_token` | **Strong** — raises check |
| 1353 | `test_load_config_missing_config_folder` | **Strong** — raises check |
| 1387 | `test_load_config_invalid_path` | **Strong** — raises check |
| 1422 | `test_load_config_invalid_plex_structure` | **Strong** — raises check |
| 1463 | `test_load_config_validates_numeric_ranges` | **Strong** — bif_frame_interval=100 (>60) → raises |
| 1519 | `test_load_config_validates_thread_counts` | **Strong** — gpu_threads=50 (>32) → raises |
| 1575 | `test_load_config_validates_ffmpeg_threads` | **Strong** — ffmpeg_threads=50 → raises |
| 1633 | `test_load_config_tmp_folder_auto_creation` | **Strong** — `tmp_folder_created_by_us is True` + `mock_makedirs.assert_called_once_with(...)` exact-args |
| 1728 | `test_load_config_tmp_folder_not_empty` | **Strong** — `config.tmp_folder == ...` |
| 1815 | `test_load_config_ffmpeg_not_found` | **Strong** — `pytest.raises(SystemExit)` |
| 1851 | `test_load_config_docker_environment` | **Strong** — raises check |
| 1909 | `test_load_config_comma_separated_libraries` | **Strong** — `"movies" in config.plex_libraries` etc — given that's a list, substring check is OK as set-membership |

## TestValidateProcessingThreadTotals

| Line | Test | Verdict |
|---|---|---|
| 1996 | `test_warns_zero_cpu_and_zero_gpu_workers` | **Strong** — `ok is False` + substring "pending" in message |
| 2012 | `test_accepts_zero_cpu_when_gpu_has_workers` | **Strong** — `ok is True` + `msg == ""` |
| 2028 | `test_thread_totals_match_gpu_config_sum` | **Strong** — strict equality on both totals; correctly excludes disabled GPU |

## TestResolveFfmpegPath

| Line | Test | Verdict |
|---|---|---|
| 2058 | `test_resolve_ffmpeg_path_precedence` | **Strong** — parametrized matrix sweeps 4 cells (Jellyfin present/exec, fallback, none) with strict equality |

## TestDockerHelp

| Line | Test | Verdict |
|---|---|---|
| 2088 | `test_show_docker_help_logs_key_sections` | **Strong** — substring on 3 known section headings |

## TestThumbnailIntervalAlias

| Line | Test | Verdict |
|---|---|---|
| 2130 | `test_alias_returns_underlying_field` | **Strong** — strict equality |
| 2134 | `test_alias_setter_propagates_to_underlying_field` | **Strong** — both fields checked after setter |
| 2140 | `test_alias_setter_coerces_to_int` | **Strong** — value AND type pinned |

## Summary

- **107 tests**, all **Strong**
- Strong matrix coverage on path normalisation, exclude rules, derive_legacy_plex_view, load_config validation paths
- TestLoadConfig is consistently mocking the right boundaries (subprocess, os.exists, os.statvfs) and asserting the actual config field, not just "didn't raise"
- TestPathToCanonicalLocal is exhaustive (11 tests on a 1-arg fn) — covers Windows backslash variants explicitly

**File verdict: STRONG.** No changes needed.
