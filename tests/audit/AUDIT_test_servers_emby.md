# Audit: tests/test_servers_emby.py — 38 tests, 12 classes

## TestConstruction

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 58 | `test_implements_media_server` | **Strong** — isinstance check pins protocol conformance |
| 63 | `test_type_is_emby` | **Strong** — strict `is ServerType.EMBY` |
| 66 | `test_id_and_name_propagate` | **Strong** — strict equality on both fields |

## TestTokenExtraction

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 72 | `test_api_key` | **Strong** — strict equality on extracted token |
| 76 | `test_access_token_from_password_flow` | **Strong** — pins token + user_id in one |
| 81 | `test_legacy_token_field` | **Strong** — legacy fallback path covered |
| 85 | `test_no_auth_returns_empty_string` | **Strong** — strict `== ""` for empty auth |

## TestRequestUrlConstruction

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 123 | `test_url_is_base_plus_path_no_doubled_prefix` | **Strong** — strict equality on URL + double-slash defence (D31 regression) |
| 134 | `test_url_strips_trailing_slash_on_base` | **Strong** — pins rstrip behaviour at the transport layer |
| 146 | `test_x_emby_token_header_is_set_from_config_token` | **Strong** — header presence + Accept asserted |
| 156 | `test_session_is_reused_across_calls` | **Strong** — `is` identity pins keep-alive contract |

## TestTestConnection

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 168 | `test_success` | **Strong** — strict equality on Id/ServerName/Version |
| 183 | `test_missing_url` | **Weak** — substring check on message ("url" in message.lower()); ok-flag is strict but message could drift |
| 189 | `test_missing_token` | **Weak** — substring check on message; same as above |
| 195 | `test_unauthorized` | **Weak** — `"401" in result.message` substring; could pass with garbage message containing "401" |
| 208 | `test_timeout` | **Weak** — substring `"timed out"` in message |

## TestListLibraries

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 219 | `test_maps_virtual_folders_to_library_objects` | **Strong** — strict tuple equality on remote_paths + kind + isinstance |
| 247 | `test_preserves_existing_enabled_toggles` | **Strong** — explicit False vs True per id, real merge contract |
| 273 | `test_empty_on_failure` | **Strong** — strict `== []` |
| 277 | `test_empty_on_unexpected_shape` | **Strong** — strict `== []` |

## TestListItems

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 288 | `test_yields_movies_and_episodes` | **Strong** — len + isinstance + S01E01 substring (acceptable for episode-format) |
| 316 | `test_skips_items_without_paths` | **Strong** — strict equality on filtered title list |

## TestResolveItemToRemotePath

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 338 | `test_prefers_media_sources_path` | **Strong** — strict path equality + URL/method/params kwargs verified |
| 356 | `test_falls_back_to_top_level_path` | **Strong** — strict equality |
| 365 | `test_returns_none_on_failure` | **Strong** — strict `is None` |
| 369 | `test_returns_none_when_no_path_anywhere` | **Strong** — strict `is None` |
| 378 | `test_per_user_endpoint_when_user_id_present` | **Strong** — pins URL `/Users/u-1/Items/42` exactly (matrix cell complement) |

## TestResolveRemotePathToItemIdViaExactPath

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 407 | `test_uses_exact_path_filter_when_item_found` | **Strong** — call_count==1, args/params/Limit pinned |
| 423 | `test_falls_back_to_search_when_exact_path_returns_empty` | **Strong** — pins fallback path + first-call params |

## TestTriggerRefresh

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 444 | `test_uses_library_media_updated_when_path_known` | **Strong** — `assert_called_once_with` pins method, URL, full json_body |
| 458 | `test_falls_back_to_item_refresh_when_only_id` | **Strong** — `assert_called_once_with` strict |
| 468 | `test_swallows_exceptions_for_path_refresh` | **Strong** — audit-fixed; asserts call_count >= 1 (catches early-return regression) |

## TestParseWebhook

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 485 | `test_library_new_event` | **Strong** — strict equality on event_type/item_id/remote_path |
| 497 | `test_library_new_event_captures_path_when_present` | **Strong** — pins captured remote_path; D34-class regression catcher |
| 514 | `test_itemadded_event` | **Strong** — strict event_type and item_id equality |
| 521 | `test_irrelevant_events_return_none` | **Strong** — matrix of irrelevant events returns None |
| 526 | `test_accepts_raw_bytes` | **Strong** — pins bytes-input contract |
| 532 | `test_invalid_json_returns_none` | **Strong** — strict `is None` |
| 535 | `test_missing_item_id_yields_none_item_id` | **Strong** — pins None contract for missing field (NOT raise) |

## TestEmbySettingsHealth

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 545 | `test_no_issues_when_recommended` | **Strong** — strict `== []` |
| 560 | `test_reports_misset_flags_per_library` | **Strong** — strict set equality on flags + library_id+fixable per-issue |
| 582 | `test_skips_trickplay_flag_on_older_emby` | **Strong** — pins backward-compat skip (matrix cell for older Emby) |

## TestEmbyApplyRecommended

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 601 | `test_writes_only_misset_flags` | **Strong** — strict set equality on results.keys + verifies POSTed json_body retains/flips correct fields |

## TestRegistryWiring

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 632 | `test_registry_can_construct_emby_server` | **Strong** — audit-fixed; verifies type, id, AND configured URL survives round-trip |

## Summary

- **38 tests total**
- **34 Strong**
- **4 Weak** — substring checks on error messages in `TestTestConnection` (lines 183, 189, 195, 208)
- **0 Bug-blind / Tautological / Dead / Bug-locking / Needs-human**

**Weak tests to fix (low priority):**
- `test_missing_url` (L183) — substring "url" in message; could match unrelated messages
- `test_missing_token` (L189) — disjunction of substrings; loose
- `test_unauthorized` (L195) — `"401" in result.message`; would pass on any message containing "401"
- `test_timeout` (L208) — substring "timed out"

These pin the user-visible error category but not the exact message. Acceptable for connection error UX but could be tightened (e.g. `result.error_kind == "unauthorized"`).

**File verdict: STRONG.** 4 message-substring tests are minor nits, not bug-blind. Real contracts (URL construction, header injection, session reuse, auth path branching, webhook path capture, library setting flags) are all pinned tightly with the recently audit-fixed assertions doing the load-bearing work.
