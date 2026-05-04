# Audit: tests/test_plex_client.py — ~70 tests

Tests for plex_client.py: PlexServer connection, retry logic, partial scan, library section retrieval, webhook path resolution, directory expansion, path-prefix mismatch detection.

## TestPlexServerConnection

| Line | Test | Verdict |
|---|---|---|
| 35 | `test_plex_server_connection_success` | **Weak** — only asserts `result == mock_plex` and `mock_plex_server.assert_called_once()`. No kwargs check on PlexServer call. Bare success path. |
| 47 | `test_plex_server_connection_failure` | **Strong** — pins `ConnectionError` raised. |
| 56 | `test_plex_server_timeout` | **Strong** — pins `ConnectionError` raised on ReadTimeout. |
| 64 | `test_plex_server_retry_strategy` | **Strong** — comprehensive: pins both http/https mounted with same HTTPAdapter instance, Retry.total==3, backoff_factor==0.3, status_forcelist==[500,502,503,504]. |
| 100 | `test_plex_server_respects_ssl_verify_setting` | **Strong** — pins `session.verify is False`. |

## TestRetryPlexCall

| Line | Test | Verdict |
|---|---|---|
| 114 | `test_retry_plex_call_success` | **Strong** — pins return value AND `assert_called_once_with` on args+kwargs. |
| 123 | `test_retry_plex_call_xml_error_retry` | **Strong** — pins return value after retry + `call_count == 2`. |
| 134 | `test_retry_plex_call_max_retries` | **Strong** — pins exception raised + `call_count == 3` (initial + 2 retries). |
| 145 | `test_retry_plex_call_non_xml_error` | **Strong** — pins exception + `call_count == 1` (no retry). |

## TestTriggerPlexPartialScan

| Line | Test | Verdict |
|---|---|---|
| 161 | `test_empty_input_returns_empty_without_http_calls` | **Strong** — `result == []` + `mock_get.assert_not_called()`. |
| 173 | `test_longest_prefix_match_wins` | **Strong** — pins exact `call(URL, params={"path": ...}, headers, timeout, verify)` — full kwargs match. |
| 204 | `test_path_mapping_expansion_triggers_scan_for_mapped_plex_path` | **Strong** — pins exact call with verify=False AND mapped /data → /data_16tb prefix. |
| 257 | `test_scan_folder_targets_series_or_movie_root` | **Strong** — parametrized; pins `params == {"path": expected_scan_folder}` for both TV and movies cells. |
| 283 | `test_sections_request_error_returns_empty` | **Strong** — pins `[]` on RequestException. |
| 296 | `test_refresh_http_error_is_handled_gracefully` | **Strong** — pins `[]` on 500 response. |
| 315 | `test_multi_drive_scans_all_matching_candidates` | **Strong** — pins exact list of 3 calls with expected per-drive paths. |

## TestFilterDuplicateLocations

| Line | Test | Verdict |
|---|---|---|
| 388 | `test_filter_duplicate_locations` | **Strong** — pins `len == 2` AND specific tuples in/out. |
| 409 | `test_filter_duplicate_locations_multiple_files` | **Strong** — overlap case; pins kept tuple. |
| 432 | `test_filter_duplicate_locations_empty` | **Strong** — `[]`. |
| 437 | `test_filter_duplicate_locations_no_duplicates` | **Strong** — pins `len == 3`. |

## TestGetLibrarySections

| Line | Test | Verdict |
|---|---|---|
| 453 | `test_get_library_sections_movies` | **Strong** — D31 contract: pins `media[0][0] == "1"` (bare ratingKey, NOT URL form), no `/` in id. Mocks set BOTH `.ratingKey` and `.key` to mirror real plexapi. |
| 492 | `test_get_library_sections_random_skips_plex_sort_param` | **Strong** — pins `"sort" not in kwargs` (random handled client-side). |
| 514 | `test_get_library_sections_newest_passes_plex_sort_param` | **Strong** — pins `kwargs.get("sort") == "addedAt:desc"`. |

## TestPathMapping

| Line | Test | Verdict |
|---|---|---|
| 547 | `test_path_mapping_unraid_standard` | **Framework trivia** — only tests `str.replace()`, not any production function. Just demonstrates Python string ops. |
| 559 | `test_path_mapping_nested_paths` | **Framework trivia** — same. |
| 571 | `test_path_mapping_with_spaces` | **Framework trivia** — same. |
| 581 | `test_path_mapping_trailing_slash_consistency` | **Framework trivia** — same. |
| 600 | `test_path_mapping_no_mapping_needed` | **Framework trivia** — tests an `if` statement in the test itself. |
| 615 | `test_path_mapping_unraid_smb_share` | **Framework trivia** — same. |
| 626 | `test_path_mapping_case_sensitivity` | **Framework trivia** — same. |
| 638 | `test_path_mapping_partial_match_avoided` | **Framework trivia + Bug-locking** — actively asserts the WRONG behavior (`/database` becomes `/mediabase`) and even comments "this demonstrates a limitation". The test asserts `mapped_path == "/mediabase/Movies/movie.mkv"` — locking in a known-broken behavior. **Should be deleted or rewritten to test real production path-mapping function (e.g. `_apply_path_mapping`).** |
| 652 | `test_path_mapping_docker_volume_mounts` | **Framework trivia** — same. |

## TestGetLibrarySectionsExtended

| Line | Test | Verdict |
|---|---|---|
| 686 | `test_get_library_sections_episodes` | **Strong** — D31 bare ratingKey check + title/season formatting. |
| 723 | `test_get_library_sections_filter` | **Strong** — pins `len == 1` + section title. |
| 746 | `test_get_library_sections_unsupported` | **Strong** — pins `len == 0` (photos skipped). |
| 762 | `test_get_library_sections_api_error` | **Strong** — pins `[]` on RequestException. |
| 772 | `test_get_library_sections_search_error` | **Strong** — pins `len == 0` on search exception. |
| 791 | `test_get_library_sections_duplicate_filtering` | **Strong** — pins `len == 1` after dedup. |
| 825 | `test_get_library_sections_cancel_before_section` | **Strong** — pins `[]` + `mock_section.search.assert_not_called()`. |
| 841 | `test_get_library_sections_cancel_after_retrieval` | **Strong** — pins call counts on both sections (first searched, second not). |
| 879 | `test_get_library_sections_no_cancel_check` | **Strong** — pins `len == 1` (default behavior). |
| 898 | `test_get_library_sections_progress_callback` | **Strong** — pins all 3 expected progress messages by substring. |

## TestGetMediaItemsByPaths

| Line | Test | Verdict |
|---|---|---|
| 930 | `test_get_media_items_by_paths_empty` | **Strong** — pins all 3 result fields == [] (full WebhookResolutionResult shape). |
| 940 | `test_get_media_items_by_paths_logs_received_and_file_path_query` | **Strong** — substring matches on canonical log phrases. |
| 957 | `test_get_media_items_by_paths_non_string_path_skipped` | **Strong** — pins `items == []` + `mock_warning.call_count == 1`. |
| 965 | `test_get_media_items_by_paths_movie_match` | **Strong** — D31 bare ratingKey + title + type + ekey contains `type=1` AND filename. |
| 994 | `test_get_media_items_by_paths_logs_per_path_resolved_status` | **Strong** — pins `[1/1]` indicator (or loguru placeholder form). |
| 1021 | `test_get_media_items_by_paths_no_match` | **Strong** — pins both `items == []` AND `unresolved_paths == [path]`. |
| 1037 | `test_get_media_items_by_paths_logs_per_path_unresolved_reason` | **Strong** — pins specific reason substring. |
| 1058 | `test_get_media_items_by_paths_episode_match` | **Strong** — D31 bare ratingKey + ekey contains `type=4` AND filename. |
| 1092 | `test_get_media_items_by_paths_upgrade_file_found_via_file_path_search` | **Strong** — pins ratingKey + audit-fixed `file=` filter check (not bare `assert_called`). |
| 1140 | `test_get_media_items_by_paths_file_path_search_resolves_match` | **Strong** — same audit-fix `file=` filter pin. |
| 1169 | `test_get_media_items_by_paths_prefers_explicit_ratingKey_over_url_key` | **Strong** — D31 contract: mocks set BOTH attrs, asserts `item_id == "999"` (not URL form). |
| 1206 | `test_get_media_items_by_paths_logs_file_path_query` | **Strong** — substring match. |
| 1225 | `test_get_media_items_by_paths_item_without_key_is_skipped` | **Strong** — pins `items == []` AND warning fired with "metadata key" substring. |
| 1251 | `test_get_media_items_by_paths_webhook_path_matches_plex_via_mapping` | **Strong** — pins ratingKey + type. |
| 1284 | `test_get_media_items_by_paths_plex_form_path_matches_with_mapping` | **Strong** — pins ratingKey. |
| 1316 | `test_get_media_items_by_paths_no_mapping_path_unchanged` | **Strong** — pins ratingKey via direct match. |
| 1338 | `test_get_media_items_by_paths_multi_row_same_webhook_alias` | **Strong** — pins ratingKey from second-disk match. |
| 1376 | `test_get_media_items_by_paths_fans_out_local_path_across_plex_roots` | **Strong** — pins ratingKey from fan-out match. |
| 1414 | `test_get_media_items_by_paths_logs_skipped_unselected_library` | **Strong** — pins both warning AND info substrings. |
| 1465 | `test_get_media_items_by_paths_logs_unselected_library_file_path_search` | **Strong** — pins warning substring. |

## TestExpandDirectoryToMediaFiles

| Line | Test | Verdict |
|---|---|---|
| 1509 | `test_video_files_discovered_recursively` | **Strong** — pins `len == 3` + 3 specific basenames in + 2 not in. |
| 1533 | `test_empty_directory_passes_through` | **Strong** — pins exact pass-through. |
| 1543 | `test_nonexistent_path_passes_through` | **Strong** — pins pass-through. |
| 1551 | `test_file_path_passes_through` | **Strong** — pins file path returned unchanged. |
| 1560 | `test_mixed_files_and_directories` | **Strong** — pins `len == 3` + first element is standalone + remaining are .mkv. |
| 1576 | `test_results_are_sorted_within_directory` | **Strong** — pins exact sorted basename list. |
| 1589 | `test_all_video_extensions_recognized` | **Strong** — pins `len == len(VIDEO_EXTENSIONS)` (catches new extensions added without test). |
| 1601 | `test_mapped_directory_expanded_via_path_mappings` | **Strong** — pins `len == 2` + basenames + mapped prefix in result. |
| 1626 | `test_unmapped_nonexistent_directory_passes_through` | **Strong** — pins pass-through. |

## TestDetectPathPrefixMismatches

| Line | Test | Verdict |
|---|---|---|
| 1645 | `test_trash_guides_docker_mismatch` | **Strong** — pins `len == 1` + exact tuple `("/data/media", "/media")`. |
| 1659 | `test_no_mismatch_when_prefix_matches` | **Strong** — pins `[]`. |
| 1668 | `test_empty_inputs` | **Strong** — three empty cases. |
| 1674 | `test_single_level_plex_location` | **Strong** — pins exact tuple. |
| 1684 | `test_deep_extra_prefix` | **Strong** — pins exact tuple. |
| 1694 | `test_partial_segment_not_matched` | **Strong** — pins `[]` (partial-segment safety). |
| 1703 | `test_deduplicates_across_paths` | **Strong** — pins `len == 1` after dedup. |
| 1716 | `test_case_insensitive_matching` | **Strong** — pins `len == 1`. |
| 1725 | `test_longest_location_wins` | **Strong** — pins specific tuple (longest wins). |

## TestMismatchCoveredByMappings

| Line | Test | Verdict |
|---|---|---|
| 1739 | `test_exact_webhook_prefix_match` | **Strong** — pins True. |
| 1750 | `test_plex_and_local_cover_mismatch` | **Strong** — pins True. |
| 1761 | `test_no_mapping_configured` | **Strong** — pins False. |
| 1765 | `test_unrelated_mapping_not_matched` | **Strong** — pins False (different prefixes). |
| 1776 | `test_case_insensitive` | **Strong** — pins True. |
| 1787 | `test_trailing_slashes_ignored` | **Strong** — pins True. |
| 1798 | `test_none_mappings` | **Strong** — pins False on None. |
| 1802 | `test_multiple_rows_second_matches` | **Strong** — pins True via second mapping row. |

## Summary

- **~70 tests** total
- **Strong**: ~60
- **Weak**: 1 (`test_plex_server_connection_success` line 35)
- **Framework trivia**: 8 (entire `TestPathMapping` class lines 547-680, except the bug-locking variant)
- **Bug-locking**: 1 (`test_path_mapping_partial_match_avoided` line 638)
- **Bug-blind / Tautological / Needs human**: 0

**File verdict: MIXED.** Core test coverage (retry, partial scan, library sections, webhook resolution, directory expansion, mismatch detection) is STRONG. The `TestPathMapping` class is mostly framework trivia testing `str.replace` rather than any production function — these don't test the project's actual path-mapping code (`_apply_path_mapping`, `path_mappings` config). The bug-locking test at line 638 is the worst offender: it asserts the wrong-result behavior (`/database` → `/mediabase`) and locks it in.

Recommended fixes:
- **Line 638 `test_path_mapping_partial_match_avoided`** — bug-locking. Either delete or rewrite to test the real `_apply_path_mapping` (or equivalent) production helper that DOES handle prefix-boundary correctly. Currently the test admits the bug in its docstring and asserts the broken result.
- **Lines 547-680 (TestPathMapping class except the bug-locking one)** — framework trivia. These don't test any project code. Consider deleting OR rewriting to test the actual production path-mapping helper. As-is they add no regression-catching value.
- **Line 35 `test_plex_server_connection_success`** — weak. Add at least one kwarg check on the PlexServer constructor call (e.g. `mock_plex_server.assert_called_once_with(baseurl=mock_config.plex_url, token=mock_config.plex_token, ...)`) so a regression that drops auth or changes the URL form fails the test.
