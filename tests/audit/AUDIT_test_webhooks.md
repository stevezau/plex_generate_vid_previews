# Audit: tests/test_webhooks.py — ~52 tests (re-audit, batch 6)

Module-level helper tests + Radarr/Sonarr/Custom webhook integration tests + dedup + create_vendor_webhook_job pinning.

## Module-level helpers (no class)

| Line | Test | Verdict | Note |
|---|---|---|---|
| 112 | `test_clean_title_from_basename` | Strong | Multi-row matrix pinning every branch (episode/scene/movie/plain/no-ext/empty). Exemplary. |
| 130 | `test_format_sonarr_episode_title` | Strong | Multi-row matrix; pins exact strings for empty/None/single/multi-episode cases. |

## Radarr / Sonarr Webhook Tests

| Line | Test | Verdict | Note |
|---|---|---|---|
| 162 | `test_radarr_webhook_download_event` | Strong | Pins 202 + body content + `assert_called_once_with("radarr", "Inception", "/movies/...mkv")`. |
| 178 | `test_sonarr_webhook_download_event` | Strong | Same pattern: pins 202 + body + exact `assert_called_once_with`. |
| 194 | `test_sonarr_webhook_download_with_episode_info_includes_season_episode_in_title` | Strong | Pins 202 + body contains S01E05 + exact 3-arg `assert_called_once_with`. |
| 217 | `test_radarr_webhook_test_event` | Strong | Pins 200 + `"configured successfully"` substring. |
| 225 | `test_radarr_webhook_grab_ignored` | Strong | Pins 200 + `"Ignored"` substring. |

## Custom Webhook Tests

| Line | Test | Verdict | Note |
|---|---|---|---|
| 238 | `test_custom_webhook_test_event` | Strong | Pins 200 + body success + message substring. |
| 248 | `test_custom_webhook_single_file_path` | Strong | Pins 202 + body + exact `assert_called_once_with`. |
| 262 | `test_custom_webhook_multiple_file_paths` | Weak | Asserts 202 + body `"2 files"` + `call_count == 2`. **Why downgraded:** doesn't assert WHICH paths were sent — a regression that scheduled the same path twice (instead of two distinct paths) would still pass. Should pin exact `set(call.args)` matches the input set. |
| 279 | `test_custom_webhook_with_title` | Strong | Pins exact `assert_called_once_with("custom", "My Show S01E01", normpath(...))`. |
| 291 | `test_custom_webhook_deduplicates_paths` | Weak | Asserts only `call_count == 2`. **Why downgraded:** doesn't pin which 2 paths were dedup-survivors (could be A+A or B+B if dedup is broken in opposite direction). Should pin the exact 2 distinct path-args. |
| 303 | `test_custom_webhook_missing_paths_returns_400` | Strong | Pins 400 + body success=False + `"file_path"` in error. |
| 313 | `test_custom_webhook_empty_body_returns_400` | Strong | Pins 400. |
| 324 | `test_custom_webhook_empty_file_paths_array_returns_400` | Strong | Pins 400. |
| 331 | `test_custom_webhook_disabled` | Strong | Pins 200 + `"disabled"` substring + `assert_not_called()`. |
| 347 | `test_custom_webhook_no_auth` | Strong | Pins 401. |
| 357 | `test_custom_webhook_appears_in_history` | Strong | Pins exact count == 1 (avoids the "double-record regression" gap noted in audit fix). |

## Authentication Tests

| Line | Test | Verdict | Note |
|---|---|---|---|
| 381 | `test_webhook_no_auth` | Strong | Pins 401. |
| 391 | `test_webhook_invalid_token` | Strong | Pins 401. |
| 401 | `test_webhook_secret_auth` | Strong | Pins 200 + body success. |
| 419 | `test_webhook_bearer_token_auth` | Strong | Pins 200 + body success. |
| 439 | `test_webhook_basic_auth_password_as_token` | Strong | Pins 200 + body success. |

## Disabled / Malformed Tests

| Line | Test | Verdict | Note |
|---|---|---|---|
| 461 | `test_webhook_disabled` | Strong | Pins 200 + `"disabled"` substring + `assert_not_called()`. |
| 481 | `test_webhook_malformed_payload` | Strong | Pins 400. |
| 492 | `test_webhook_malformed_payload_logs_warning` | Strong | Pins 400 + `assert_called_once()` + log message contains `"sonarr"` or `"webhook"` substring. Audit fix pinned. |

## History Tests

| Line | Test | Verdict | Note |
|---|---|---|---|
| 518 | `test_webhook_history_endpoint` | Weak | Asserts `len(events) >= 1` and `events[0]["source"] == "radarr"`. **Why downgraded:** the loose `>= 1` would let a regression that double-recorded the test event pass silently — same gap that test_custom_webhook_appears_in_history (line 357) audit-fixed. |
| 535 | `test_webhook_clear_history` | Strong | Pins 200 + body success + post-delete `len == 0`. |

## Debounce Test

| Line | Test | Verdict | Note |
|---|---|---|---|
| 556 | `test_webhook_debounce` | Strong | Pins `mock_timer.cancel.called` AND `mock_timer_cls.call_count == 2`. |

## Misc payload Tests

| Line | Test | Verdict | Note |
|---|---|---|---|
| 582 | `test_radarr_download_missing_file_path_is_ignored` | Strong | Pins 200 + body + lowercase substring. |
| 592 | `test_radarr_download_missing_file_path_logs_warning` | Strong | Pins canonical phrase `"didn't carry a file path"` AND `"radarr"` source identifier. Audit-fixed (was an OR-fork). |
| 615 | `test_sonarr_download_missing_file_path_is_ignored` | Strong | Pins 200 + body + lowercase substring. |
| 625 | `test_radarr_download_malformed_movie_file_payload_is_ignored` | Strong | Pins 200 + body + lowercase substring. |
| 639 | `test_sonarr_download_malformed_episode_file_payload_is_ignored` | Strong | Pins 200 + body + lowercase substring. |

## Execute / batch / hint Tests

| Line | Test | Verdict | Note |
|---|---|---|---|
| 656 | `test_execute_webhook_job_batches_paths` | Strong | Pins `assert_called_once` + sorted `webhook_paths` list. |
| 686 | `test_execute_webhook_job_single_file_uses_title_for_library_display` | Strong | Pins `library_name` exact string. |
| 723 | `test_execute_webhook_job_uses_selected_libraries` | Strong | Pins `["1", "2"]`. |
| 755 | `test_execute_webhook_job_includes_retry_settings` | Strong | Pins both retry kwargs (count + delay). |
| 786 | `test_webhook_payload_path_in_job_config_for_mapping` | Strong | Pins payload path is in job config webhook_paths. |
| 826 | `test_triggered_history_entry_includes_batch_metadata` | Strong | Pins job_id, title, path_count, files_preview list — multi-field. |

## Dedup Tests

| Line | Test | Verdict | Note |
|---|---|---|---|
| 867 | `test_schedule_webhook_job_dedupes_within_ttl` | Strong | Pins result False + `assert_not_called` + no batch + history entry exists. |
| 899 | `test_schedule_webhook_job_allows_dispatch_after_ttl` | Strong | Pins result True + `assert_called_once` + fresh_ts > stale_ts. |
| 931 | `test_schedule_webhook_job_dedup_is_per_source` | Strong | Pins result True + `assert_called_once`. |
| 955 | `test_execute_webhook_job_records_dispatch_before_start` | Strong | Pins both normalized paths in `_recent_dispatches` AND `assert_called_once`. |
| 984 | `test_schedule_webhook_job_per_server_dedup_is_independent` | Strong | Pins result True + `assert_called_once`. |
| 1008 | `test_schedule_webhook_job_per_server_keeps_separate_batches` | Strong | Pins both keys present + key inequality + per-batch server_id. |
| 1031 | `test_duplicate_after_dispatch_is_dropped_end_to_end` | Strong | Pins `call_count == 1` after second attempt + result False + `assert_not_called`. |

## Page Route Test

| Line | Test | Verdict | Note |
|---|---|---|---|
| 1064 | `test_webhooks_page_requires_login` | Strong | Pins 302 + `/login` in Location. |
| 1071 | `test_webhooks_page_redirects_to_automation` | Strong | Pins 302 + endswith `/automation#webhooks`. |

## create_vendor_webhook_job Tests

| Line | Test | Verdict | Note |
|---|---|---|---|
| 1086 | `test_create_vendor_webhook_job_regenerate_propagates_force_generate` | Strong | Pins `force_generate is True` + path AND hint shape. Multi-field. |
| 1117 | `test_create_vendor_webhook_job_carries_hints_keyed_by_path` | Strong | Pins exact `{path: {server_id: item_id}}` shape. |
| 1136 | `test_create_vendor_webhook_job_dedupes_within_ttl` | Strong | Pins first non-None, second None, single mock_start call. |
| 1162 | `test_create_vendor_webhook_job_does_NOT_dedup_across_sources` | Strong | Pins distinct job ids + 2 calls + per-job source from job DB. Audit-fix pinned source identity in DB row. |
| 1222 | `test_create_vendor_webhook_job_uses_clean_title_when_title_omitted` | Strong | Pins exact `library_name == "Margarita S02E01"` + clear regression failure message. |
| 1257 | `test_create_vendor_webhook_job_uses_clean_title_for_movie_basename` | Strong | Pins exact `"Inception (2010)"`. |
| 1278 | `test_create_vendor_webhook_job_uses_explicit_title_when_provided` | Strong | Pins explicit title wins. |
| 1301 | `test_create_vendor_webhook_job_clean_title_used_when_title_is_empty_string` | Strong | Pins fallback fires for empty string (not None special-case). |
| 1327 | `test_create_vendor_webhook_job_handles_unicode_path` | Strong | Pins exact path round-trip + hint shape. |
| 1347 | `test_create_vendor_webhook_job_empty_hint_dict_treated_as_no_hint` | Strong | Pins absence of `webhook_item_id_hints` in overrides. |
| 1367 | `test_create_vendor_webhook_job_filters_falsy_hint_keys` | Strong | Pins exact filtered `{path: {valid-sid: valid-id}}` shape. |
| 1388 | `test_create_vendor_webhook_job_server_id_filter_pins_publishers` | Strong | Closes the previously needs-human gap: now uses DIFFERENT values for `server_id` vs `server_id_filter` and asserts which one drives `overrides["server_id"]`. Excellent. |

**File verdict: STRONG (2 weak count-only tests, 1 weak history-loose-count).** Down from "MIXED (1 weak, 1 needs-human)" — the prior needs-human test (L1389) has been definitively fixed with the `server_id != server_id_filter` discriminator. Remaining gaps are minor count-only assertions on path lists.

## Fix queue

- **L262 `test_custom_webhook_multiple_file_paths`** — pin the SET of paths sent to `_schedule_webhook_job` matches the input set, not just `call_count == 2`.
- **L291 `test_custom_webhook_deduplicates_paths`** — pin the 2 surviving args are `/movies/A.mkv` and `/movies/B.mkv`, not just count.
- **L518 `test_webhook_history_endpoint`** — tighten `>= 1` to `== 1` (matches the `test_custom_webhook_appears_in_history` audit fix at L357), so a double-record regression fails.
