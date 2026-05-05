# Audit: tests/test_plex_client.py — ~74 tests (re-audit, batch 6)

Tests for plex_client.py: PlexServer connection, retry logic, partial scan, library section retrieval, webhook path resolution, directory expansion, path-prefix mismatch detection.

**Note:** the previous audit's `TestPathMapping` class (framework-trivia / bug-locking) has been **replaced** in production by `TestPathMappingProduction`, which exercises the real helpers (`plex_path_to_local`, `expand_path_mapping_candidates`, `_map_plex_path_to_local`). Original concerns no longer apply.

## TestPlexServerConnection

| Line | Test | Verdict | Note |
|---|---|---|---|
| 40 | `test_plex_server_connection_success` | Strong | Now pins `assert_called_once_with(url, token, timeout=, session=)` (was weak in prior audit; fix landed). |
| 64 | `test_plex_server_connection_failure` | Strong | Pins `ConnectionError` raised. |
| 73 | `test_plex_server_timeout` | Strong | Pins `ConnectionError` raised on ReadTimeout. |
| 81 | `test_plex_server_retry_strategy` | Strong | Comprehensive: pins both http/https mounted with same HTTPAdapter instance, Retry.total==3, backoff_factor==0.3, status_forcelist==[500,502,503,504]. |
| 117 | `test_plex_server_respects_ssl_verify_setting` | Strong | Pins `session.verify is False`. |

## TestRetryPlexCall

| Line | Test | Verdict | Note |
|---|---|---|---|
| 131 | `test_retry_plex_call_success` | Strong | Pins return value AND `assert_called_once_with` on args+kwargs. |
| 140 | `test_retry_plex_call_xml_error_retry` | Strong | Pins return value after retry + `call_count == 2`. |
| 151 | `test_retry_plex_call_max_retries` | Strong | Pins exception raised + `call_count == 3` (initial + 2 retries). |
| 162 | `test_retry_plex_call_non_xml_error` | Strong | Pins exception + `call_count == 1` (no retry). |

## TestTriggerPlexPartialScan

| Line | Test | Verdict | Note |
|---|---|---|---|
| 178 | `test_empty_input_returns_empty_without_http_calls` | Strong | `result == []` + `mock_get.assert_not_called()`. |
| 190 | `test_longest_prefix_match_wins` | Strong | Pins exact `call(URL, params, headers, timeout, verify)`. |
| 220 | `test_path_mapping_expansion_triggers_scan_for_mapped_plex_path` | Strong | Pins exact call with verify=False AND mapped /data → /data_16tb prefix. |
| 274 | `test_scan_folder_targets_series_or_movie_root` | Strong | Parametrized; pins `params == {"path": expected_scan_folder}` for both TV and movies cells. |
| 300 | `test_sections_request_error_returns_empty` | Strong | Pins `[]` on RequestException. |
| 313 | `test_refresh_http_error_is_handled_gracefully` | Strong | Pins `[]` on 500 response. |
| 332 | `test_multi_drive_scans_all_matching_candidates` | Strong | Pins exact list of 3 calls with expected per-drive paths. |

## TestFilterDuplicateLocations

| Line | Test | Verdict | Note |
|---|---|---|---|
| 405 | `test_filter_duplicate_locations` | Strong | Pins `len == 2` AND specific tuples in/out. |
| 426 | `test_filter_duplicate_locations_multiple_files` | Strong | Overlap case; pins kept tuple. |
| 449 | `test_filter_duplicate_locations_empty` | Strong | `[]`. |
| 454 | `test_filter_duplicate_locations_no_duplicates` | Strong | Pins `len == 3`. |

## TestGetLibrarySections

| Line | Test | Verdict | Note |
|---|---|---|---|
| 470 | `test_get_library_sections_movies` | Strong | D31 contract: pins `media[0][0] == "1"` (bare ratingKey, NOT URL form), no `/` in id. |
| 509 | `test_get_library_sections_random_skips_plex_sort_param` | Strong | Pins `"sort" not in kwargs` (random handled client-side). |
| 531 | `test_get_library_sections_newest_passes_plex_sort_param` | Strong | Pins `kwargs.get("sort") == "addedAt:desc"`. |

## TestPathMappingProduction

| Line | Test | Verdict | Note |
|---|---|---|---|
| 572 | `test_plex_path_to_local_basic_mapping` | Strong | Tests real `plex_path_to_local` helper. |
| 577 | `test_plex_path_to_local_partial_prefix_avoidance` | Strong | Closes the bug-lock from previous audit; pins `/database` is NOT mapped by `/data` rule. |
| 587 | `test_plex_path_to_local_trailing_slash_equivalence` | Strong | Pins `/data` and `/data/` produce identical output. |
| 597 | `test_plex_path_to_local_no_mappings_returns_input` | Strong | Empty mapping list returns input. |
| 601 | `test_plex_path_to_local_nested_paths_preserved` | Strong | Deep suffix preserved verbatim. |
| 607 | `test_plex_path_to_local_case_sensitivity_preserved` | Strong | Case mismatch → no mapping. |
| 613 | `test_map_plex_path_to_local_wrapper_forwards_to_helper` | Strong | Pins wrapper forwards `config.path_mappings`. |
| 626 | `test_expand_path_mapping_candidates_bidirectional_fanout` | Strong | Pins fan-out + dedup invariants. |
| 647 | `test_expand_path_mapping_candidates_webhook_alias` | Strong | Pins all 3 expected candidates (input, webhook→local, webhook→plex). |

## TestGetLibrarySectionsExtended

| Line | Test | Verdict | Note |
|---|---|---|---|
| 668 | `test_get_library_sections_episodes` | Strong | D31 bare ratingKey check + title/season formatting. |
| 705 | `test_get_library_sections_filter` | Strong | Pins `len == 1` + section title. |
| 728 | `test_get_library_sections_unsupported` | Strong | Pins `len == 0` (photos skipped). |
| 744 | `test_get_library_sections_api_error` | Strong | Pins `[]` on RequestException. |
| 754 | `test_get_library_sections_search_error` | Strong | Pins `len == 0` on search exception. |
| 773 | `test_get_library_sections_duplicate_filtering` | Strong | Pins `len == 1` after dedup. |
| 807 | `test_get_library_sections_cancel_before_section` | Strong | Pins `[]` + `mock_section.search.assert_not_called()`. |
| 823 | `test_get_library_sections_cancel_after_retrieval` | Strong | Pins call counts on both sections. |
| 861 | `test_get_library_sections_no_cancel_check` | Strong | Pins `len == 1` (default behavior). |
| 880 | `test_get_library_sections_progress_callback` | Strong | Pins all 3 expected progress messages by substring. |

## TestGetMediaItemsByPaths

| Line | Test | Verdict | Note |
|---|---|---|---|
| 912 | `test_get_media_items_by_paths_empty` | Strong | Pins all 3 result fields == [] (full WebhookResolutionResult shape). |
| 922 | `test_get_media_items_by_paths_logs_received_and_file_path_query` | Weak | Substring matches on `"Received"` and `"webhook input file"` via `str(call).lower()` — would pass even if log fired with mangled args. **Why downgraded:** loose substring scan against `str(call_args_list)` with no count check; a regression that emitted only one of the two phrases (or doubled the log) would pass. |
| 939 | `test_get_media_items_by_paths_non_string_path_skipped` | Strong | Pins `items == []` + `mock_warning.call_count == 1`. |
| 947 | `test_get_media_items_by_paths_movie_match` | Strong | D31 bare ratingKey + title + type + ekey contains `type=1` AND filename. |
| 977 | `test_get_media_items_by_paths_logs_per_path_resolved_status` | Weak | OR-chained `[1/1] in str(call) OR ([{}/{}] AND "1, 1")` — unanchored substring `"1, 1"` would match many other call shapes. **Why downgraded:** weak substring on log message; could match e.g. `Querying section 1, 1 path`. |
| 1003 | `test_get_media_items_by_paths_no_match` | Strong | Pins both `items == []` AND `unresolved_paths == [path]`. |
| 1019 | `test_get_media_items_by_paths_logs_per_path_unresolved_reason` | Strong | Pins specific `"Direct path not found in Plex"` substring. |
| 1040 | `test_get_media_items_by_paths_episode_match` | Strong | D31 bare ratingKey + ekey contains `type=4` AND filename. |
| 1074 | `test_get_media_items_by_paths_upgrade_file_found_via_file_path_search` | Strong | Pins ratingKey + audit-fixed `file=` filter check. |
| 1122 | `test_get_media_items_by_paths_file_path_search_resolves_match` | Strong | Same audit-fix `file=` filter pin. |
| 1151 | `test_get_media_items_by_paths_prefers_explicit_ratingKey_over_url_key` | Strong | D31 contract: mocks set BOTH attrs, asserts `item_id == "999"` (not URL form). |
| 1188 | `test_get_media_items_by_paths_logs_file_path_query` | Weak | Single `any("Querying Plex by file path" in str(call) ...)` — bare presence check, no count, no per-path content. **Why downgraded:** loose; survives a regression that logged only the FIRST file's query (silent drop for batches). |
| 1207 | `test_get_media_items_by_paths_item_without_key_is_skipped` | Strong | Pins `items == []` AND warning fired with "metadata key" substring. |
| 1233 | `test_get_media_items_by_paths_webhook_path_matches_plex_via_mapping` | Strong | Pins ratingKey + type. |
| 1266 | `test_get_media_items_by_paths_plex_form_path_matches_with_mapping` | Strong | Pins ratingKey. |
| 1298 | `test_get_media_items_by_paths_no_mapping_path_unchanged` | Strong | Pins ratingKey via direct match. |
| 1320 | `test_get_media_items_by_paths_multi_row_same_webhook_alias` | Strong | Pins ratingKey from second-disk match. |
| 1358 | `test_get_media_items_by_paths_fans_out_local_path_across_plex_roots` | Strong | Pins ratingKey from fan-out match. |
| 1396 | `test_get_media_items_by_paths_logs_skipped_unselected_library` | Weak | Both warning and info checks are OR-chained substring matches over `str(call).lower()`. **Why downgraded:** doesn't pin which path was skipped or which library was named in the warning — a regression that conflated paths could pass. |
| 1447 | `test_get_media_items_by_paths_logs_unselected_library_file_path_search` | Weak | Same OR-chained substring weakness as above. **Why downgraded:** same as 1396. |

## TestExpandDirectoryToMediaFiles

| Line | Test | Verdict | Note |
|---|---|---|---|
| 1491 | `test_video_files_discovered_recursively` | Strong | Pins `len == 3` + 3 specific basenames in + 2 not in. |
| 1515 | `test_empty_directory_passes_through` | Strong | Pins exact pass-through. |
| 1525 | `test_nonexistent_path_passes_through` | Strong | Pins pass-through. |
| 1533 | `test_file_path_passes_through` | Strong | Pins file path returned unchanged. |
| 1542 | `test_mixed_files_and_directories` | Strong | Pins `len == 3` + first element is standalone + remaining are .mkv. |
| 1558 | `test_results_are_sorted_within_directory` | Strong | Pins exact sorted basename list. |
| 1571 | `test_all_video_extensions_recognized` | Strong | Pins `len == len(VIDEO_EXTENSIONS)` (catches new extensions added without test). |
| 1583 | `test_mapped_directory_expanded_via_path_mappings` | Strong | Pins `len == 2` + basenames + mapped prefix in result. |
| 1608 | `test_unmapped_nonexistent_directory_passes_through` | Strong | Pins pass-through. |

## TestDetectPathPrefixMismatches

| Line | Test | Verdict | Note |
|---|---|---|---|
| 1627 | `test_trash_guides_docker_mismatch` | Strong | Pins `len == 1` + exact tuple `("/data/media", "/media")`. |
| 1641 | `test_no_mismatch_when_prefix_matches` | Strong | Pins `[]`. |
| 1650 | `test_empty_inputs` | Strong | Three empty cases. |
| 1656 | `test_single_level_plex_location` | Strong | Pins exact tuple. |
| 1666 | `test_deep_extra_prefix` | Strong | Pins exact tuple. |
| 1676 | `test_partial_segment_not_matched` | Strong | Pins `[]` (partial-segment safety). |
| 1685 | `test_deduplicates_across_paths` | Strong | Pins `len == 1` after dedup. |
| 1698 | `test_case_insensitive_matching` | Weak | Pins `len == 1` only — does NOT pin the tuple value. **Why downgraded:** a regression that picked the wrong webhook prefix (e.g. `/Data/Media` raw instead of normalised) would still produce `len == 1` and pass. |
| 1707 | `test_longest_location_wins` | Strong | Pins specific tuple (longest wins). |

## TestMismatchCoveredByMappings

| Line | Test | Verdict | Note |
|---|---|---|---|
| 1721 | `test_exact_webhook_prefix_match` | Strong | Pins True. |
| 1732 | `test_plex_and_local_cover_mismatch` | Strong | Pins True. |
| 1743 | `test_no_mapping_configured` | Strong | Pins False. |
| 1747 | `test_unrelated_mapping_not_matched` | Strong | Pins False (different prefixes). |
| 1758 | `test_case_insensitive` | Strong | Pins True. |
| 1769 | `test_trailing_slashes_ignored` | Strong | Pins True. |
| 1780 | `test_none_mappings` | Strong | Pins False on None. |
| 1784 | `test_multiple_rows_second_matches` | Strong | Pins True via second mapping row. |

**File verdict: MIXED (5 weak log-substring tests, 1 weak tuple-presence test).** Down from "STRONG (gold-standard)" — the production helper rewrite (TestPathMappingProduction) closed the worst gaps from the previous audit, but the log-substring tests in TestGetMediaItemsByPaths remain weakly anchored.

## Fix queue

- **L922 `test_get_media_items_by_paths_logs_received_and_file_path_query`** — pin exact log message (e.g. `"Received N webhook input file(s)"`) and assert call_count for each phrase to detect drift.
- **L977 `test_get_media_items_by_paths_logs_per_path_resolved_status`** — drop the OR fork; assert the resolved Loguru `record.message` directly via a sink-attach, or pin a unique anchor like `"resolved=1/1"`.
- **L1188 `test_get_media_items_by_paths_logs_file_path_query`** — assert `call_count` for the phrase matches the path count, not just `any(...)`.
- **L1396 `test_get_media_items_by_paths_logs_skipped_unselected_library`** — pin the library name (`"Anime"`) in the warning to detect path/library conflation.
- **L1447 `test_get_media_items_by_paths_logs_unselected_library_file_path_search`** — same as 1396.
- **L1698 `test_case_insensitive_matching`** — assert the exact tuple value, not just the length.
