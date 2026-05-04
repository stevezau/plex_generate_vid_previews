# Audit: tests/test_webhooks.py — ~50 tests

Module-level helper tests + Radarr/Sonarr/Custom webhook integration tests + dedup + create_vendor_webhook_job pinning.

## Module-level helpers (no class)

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 112 | `test_clean_title_from_basename` | **Strong** — 8 strict-equality assertions across episode/movie/plain/empty cases. Covers the matrix. |
| 130 | `test_format_sonarr_episode_title` | **Strong** — multi-case strict-equality matrix (None, [], single ep, multi-ep, named series). |

## Radarr/Sonarr Webhook Tests

| Line | Test | Verdict |
|---|---|---|
| 162 | `test_radarr_webhook_download_event` | **Strong** — `assert_called_once_with("radarr", "Inception", "/movies/...")` pins all 3 args. |
| 178 | `test_sonarr_webhook_download_event` | **Strong** — same as above for sonarr. |
| 194 | `test_sonarr_webhook_download_with_episode_info_includes_season_episode_in_title` | **Strong** — pins title formatting includes SxxExx via positional kwargs. |
| 217 | `test_radarr_webhook_test_event` | **Strong** — substring `"configured successfully"`; status code is the contract floor. |
| 225 | `test_radarr_webhook_grab_ignored` | **Strong** — substring `"Ignored"` plus 200 status. |

## Custom Webhook Tests

| Line | Test | Verdict |
|---|---|---|
| 238 | `test_custom_webhook_test_event` | **Strong** — 200 + `success=True` + substring on canonical message. |
| 249 | `test_custom_webhook_single_file_path` | **Strong** — `assert_called_once_with` pins all 3 positional args + path normalization. |
| 263 | `test_custom_webhook_multiple_file_paths` | **Strong** — `call_count == 2` plus substring `"2 files"`. |
| 280 | `test_custom_webhook_with_title` | **Strong** — pins title-as-display via `assert_called_once_with`. |
| 292 | `test_custom_webhook_deduplicates_paths` | **Strong** — `call_count == 2` from 3 inputs (one dup); 202 status. |
| 303 | `test_custom_webhook_missing_paths_returns_400` | **Strong** — 400 + `success: False` + error string contains `"file_path"`. |
| 313 | `test_custom_webhook_empty_body_returns_400` | **Strong** — 400 status. |
| 324 | `test_custom_webhook_empty_file_paths_array_returns_400` | **Strong** — 400. |
| 332 | `test_custom_webhook_disabled` | **Strong** — 200 + `"disabled"` substring + `mock_schedule.assert_not_called()`. |
| 347 | `test_custom_webhook_no_auth` | **Strong** — 401. |
| 357 | `test_custom_webhook_appears_in_history` | **Weak** — uses `>= 1` length check and only filter on source field. Doesn't pin event type or content. Marginal but acceptable. |

## Authentication Tests

| Line | Test | Verdict |
|---|---|---|
| 376 | `test_webhook_no_auth` | **Strong** — 401. |
| 386 | `test_webhook_invalid_token` | **Strong** — 401. |
| 396 | `test_webhook_secret_auth` | **Strong** — 200 + `success=True` after configuring webhook_secret. |
| 414 | `test_webhook_bearer_token_auth` | **Strong** — Bearer header path. |
| 434 | `test_webhook_basic_auth_password_as_token` | **Strong** — Basic auth path with token in password. |

## Disabled / Malformed Tests

| Line | Test | Verdict |
|---|---|---|
| 457 | `test_webhook_disabled` | **Strong** — 200 + `"disabled"` substring + `mock_schedule.assert_not_called()`. |
| 476 | `test_webhook_malformed_payload` | **Strong** — 400. |
| 488 | `test_webhook_malformed_payload_logs_warning` | **Strong** — explicit comment block; asserts the message identifies endpoint, NOT bare `assert_called_once`. Bug-class fix already applied. |

## History Tests

| Line | Test | Verdict |
|---|---|---|
| 513 | `test_webhook_history_endpoint` | **Strong** — pins `events[0]["source"] == "radarr"` after a Test POST. |
| 530 | `test_webhook_clear_history` | **Strong** — DELETE returns 200, then `events == 0`. |

## Debounce + Job Building Tests

| Line | Test | Verdict |
|---|---|---|
| 552 | `test_webhook_debounce` | **Strong** — `mock_timer.cancel.called` + `call_count == 2` pins debounce semantics. |
| 577 | `test_radarr_download_missing_file_path_is_ignored` | **Strong** — 200 + substring on canonical phrase. |
| 588 | `test_radarr_download_missing_file_path_logs_warning` | **Strong** — pins canonical phrase `"didn't carry a file path"` + source identifier. Audit-already-fixed. |
| 610 | `test_sonarr_download_missing_file_path_is_ignored` | **Strong** — same as above for sonarr. |
| 620 | `test_radarr_download_malformed_movie_file_payload_is_ignored` | **Strong** — pins graceful degradation. |
| 634 | `test_sonarr_download_malformed_episode_file_payload_is_ignored` | **Strong** — same for sonarr. |
| 651 | `test_execute_webhook_job_batches_paths` | **Strong** — sorted compare on `webhook_paths`. |
| 681 | `test_execute_webhook_job_single_file_uses_title_for_library_display` | **Strong** — strict equality on `library_name`. |
| 718 | `test_execute_webhook_job_uses_selected_libraries` | **Strong** — pins selected_libraries override. |
| 750 | `test_execute_webhook_job_includes_retry_settings` | **Strong** — strict equality on `webhook_retry_count` AND `webhook_retry_delay`. |
| 781 | `test_webhook_payload_path_in_job_config_for_mapping` | **Strong** — confirms expected normalized path in webhook_paths. |
| 821 | `test_triggered_history_entry_includes_batch_metadata` | **Strong** — pins `job_id`, `title`, `path_count`, `files_preview` (4 strict-equality assertions). |

## Dedup tests

| Line | Test | Verdict |
|---|---|---|
| 862 | `test_schedule_webhook_job_dedupes_within_ttl` | **Strong** — `result is False`, `mock_timer_cls.assert_not_called()`, batch absent, history "deduped" entry. |
| 894 | `test_schedule_webhook_job_allows_dispatch_after_ttl` | **Strong** — `result is True`, fresh timestamp > stale. |
| 926 | `test_schedule_webhook_job_dedup_is_per_source` | **Strong** — pins per-source isolation. |
| 950 | `test_execute_webhook_job_records_dispatch_before_start` | **Strong** — both keys present in `_recent_dispatches` post-execute. |
| 979 | `test_schedule_webhook_job_per_server_dedup_is_independent` | **Strong** — server-scoped isolation pinned. |
| 1003 | `test_schedule_webhook_job_per_server_keeps_separate_batches` | **Strong** — pins separate batch keys + per-server payload. |
| 1026 | `test_duplicate_after_dispatch_is_dropped_end_to_end` | **Strong** — end-to-end e2e dedup contract. |

## Page route tests

| Line | Test | Verdict |
|---|---|---|
| 1059 | `test_webhooks_page_requires_login` | **Strong** — 302 + Location contains `/login`. |
| 1066 | `test_webhooks_page_redirects_to_automation` | **Strong** — Location ends with canonical fragment `/automation#webhooks`. |

## create_vendor_webhook_job tests

| Line | Test | Verdict |
|---|---|---|
| 1081 | `test_create_vendor_webhook_job_regenerate_propagates_force_generate` | **Strong** — pins `force_generate`, `webhook_paths`, AND `webhook_item_id_hints`. Multi-arg contract pin. |
| 1112 | `test_create_vendor_webhook_job_carries_hints_keyed_by_path` | **Strong** — strict equality on hint shape. |
| 1131 | `test_create_vendor_webhook_job_dedupes_within_ttl` | **Strong** — `first is not None` + `second is None` + `mock_start.call_count == 1`. |
| 1157 | `test_create_vendor_webhook_job_does_NOT_dedup_across_sources` | **Strong** — asserts both job IDs differ AND inspects job rows for source identity. |
| 1217 | `test_create_vendor_webhook_job_uses_clean_title_when_title_omitted` | **Strong** — pins exact `library_name` from helper output. |
| 1252 | `test_create_vendor_webhook_job_uses_clean_title_for_movie_basename` | **Strong** — strict equality on movie name format. |
| 1274 | `test_create_vendor_webhook_job_uses_explicit_title_when_provided` | **Strong** — strict equality on caller-supplied title. |
| 1297 | `test_create_vendor_webhook_job_clean_title_used_when_title_is_empty_string` | **Strong** — pins empty-string fallback. |
| 1323 | `test_create_vendor_webhook_job_handles_unicode_path` | **Strong** — pins unicode round-trip in webhook_paths + hints. |
| 1343 | `test_create_vendor_webhook_job_empty_hint_dict_treated_as_no_hint` | **Strong** — `"webhook_item_id_hints" not in overrides`. |
| 1363 | `test_create_vendor_webhook_job_filters_falsy_hint_keys` | **Strong** — strict equality on filtered hints. |
| 1384 | `test_create_vendor_webhook_job_server_id_filter_pins_publishers` | **Weak (mild)** — only asserts `overrides.get("server_id") == "jelly-1"` — docstring talks about server_id_filter but test only pins server_id. The two are different overrides. Probably correct (server_id_filter becomes server_id at this layer) but the docstring drift is suspicious. **Needs human** confirm. |

## Summary

- **~52 tests** total (incl. 2 module-level)
- **Strong**: ~50
- **Weak (marginal)**: 1 (`test_custom_webhook_appears_in_history` — uses `>= 1` length check)
- **Needs human**: 1 (`test_create_vendor_webhook_job_server_id_filter_pins_publishers` line 1384 — docstring/test mismatch on `server_id` vs `server_id_filter`)
- **Bug-blind / Tautological / Bug-locking**: 0

**File verdict: STRONG.**

Recommended fixes:
- Line 1384 — clarify whether `server_id_filter` arg is supposed to surface as `overrides["server_id_filter"]` or `overrides["server_id"]`. Currently the test only pins `server_id`. If the SUT contract is "server_id_filter→server_id rename at this boundary", add a comment; otherwise add an assertion for `server_id_filter` too.
- Line 357 — could tighten `test_custom_webhook_appears_in_history` to `len(custom_events) == 1` (current `>= 1` would silently mask a regression that double-records the event).
