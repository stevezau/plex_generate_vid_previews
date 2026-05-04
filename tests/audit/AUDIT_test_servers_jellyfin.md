# Audit: tests/test_servers_jellyfin.py — 32 tests, 11 classes

## TestConstruction

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 58 | `test_implements_media_server` | **Strong** — isinstance pins protocol conformance |
| 63 | `test_type_is_jellyfin` | **Strong** — strict `is ServerType.JELLYFIN` |

## TestTokenExtraction

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 68 | `test_quick_connect_token` | **Strong** — strict equality |
| 72 | `test_password_flow_token` | **Strong** — strict equality |
| 76 | `test_api_key` | **Strong** — strict equality |
| 80 | `test_no_auth_returns_empty_string` | **Strong** — strict `== ""` |

## TestTestConnection

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 86 | `test_success` | **Strong** — strict equality on Id/ServerName/Version |
| 105 | `test_missing_url` | **Weak** — only asserts `not result.ok`; doesn't pin which error message/category |
| 110 | `test_missing_token` | **Weak** — only asserts `not result.ok` |
| 115 | `test_unauthorized` | **Weak** — substring `"401" in result.message` |

## TestListLibraries

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 130 | `test_maps_virtual_folders` | **Strong** — strict list equality on names + kind check |
| 155 | `test_preserves_existing_enabled_toggles` | **Strong** — explicit per-id True/False |
| 179 | `test_empty_on_failure` | **Strong** — strict `== []` |

## TestListItems

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 185 | `test_yields_movies_and_episodes` | **Strong** — len + isinstance + S01E01 substring |

## TestResolveItemToRemotePath

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 213 | `test_prefers_media_sources_path` | **Strong** — pins URL `/Users/u/Items/42` exactly + result equality |
| 242 | `test_falls_back_to_plural_items_endpoint_when_no_user_id` | **Strong** — pins URL `/Items` + Ids param (matrix cell) |
| 263 | `test_returns_none_on_failure` | **Strong** — strict `is None` |

## TestResolveRemotePathToItemIdViaPlugin

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 276 | `test_uses_plugin_resolve_path_when_installed` | **Strong** — call_count==1, args + params pinned (no fallback fired) |
| 289 | `test_falls_back_to_public_api_when_plugin_returns_404` | **Strong** — pins first call was plugin probe + base class kicked in |
| 307 | `test_falls_back_when_plugin_request_raises` | **Weak** — only asserts `is None` (which both branches return); doesn't verify fallback was attempted. A regression where the plugin exception swallowed and didn't fall through to base would still pass |

## TestTriggerRefresh

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 322 | `test_calls_plugin_bridge_then_per_item_refresh_when_id_known` | **Strong** — call_count==2 + per-call args strict |
| 337 | `test_continues_to_per_item_refresh_when_plugin_not_installed` | **Strong** — pins fallthrough chain after 404 |
| 350 | `test_path_based_nudge_when_no_item_id` | **Strong** — `assert_called_once_with` pins method, URL, json_body |
| 367 | `test_falls_back_to_full_refresh_when_no_path_and_no_id` | **Strong** — `assert_called_once_with` strict |
| 379 | `test_full_refresh_is_rate_limited_per_server` | **Strong** — pins `assert_called_once_with` after 3 calls (cooldown contract) |
| 400 | `test_path_nudge_failure_falls_back_to_full_refresh` | **Strong** — pins call_count==2 + ordered URLs |
| 425 | `test_falls_back_to_library_refresh_when_per_item_fails` | **Strong** — pins call_count==3 + ordered URLs |

## TestParseWebhook

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 449 | `test_itemadded_event` | **Strong** — strict event_type and item_id |
| 461 | `test_itemadded_event_captures_path_when_template_provides_it` | **Strong** — pins remote_path equality (regression catcher) |
| 482 | `test_library_new_emby_template` | **Strong** — strict id |
| 488 | `test_irrelevant_events_return_none` | **Strong** — matrix of irrelevant events |
| 493 | `test_accepts_raw_bytes` | **Strong** — pins raw bytes contract |
| 499 | `test_invalid_json_returns_none` | **Strong** — strict `is None` |

## TestSettingsHealth

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 506 | `test_no_issues_when_all_recommended` | **Strong** — strict `== []` |
| 522 | `test_reports_each_misset_flag_per_library` | **Strong** — len-per-library + per-issue severity + critical-flag set |
| 563 | `test_empty_on_request_failure` | **Strong** — strict `== []` |

## TestApplyRecommendedSettings

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 571 | `test_writes_only_misset_flags_back` | **Strong** — strict dict equality on results + posted body field-by-field assertions on critical flags (regression catcher) |
| 621 | `test_skips_libraries_already_correct` | **Strong** — strict `results == {}` + zero POST calls |
| 647 | `test_flag_filter_restricts_target` | **Strong** — pins flag-filter respects scope; other flags untouched |

## TestRegistryWiring

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 680 | `test_registry_can_construct_jellyfin_server` | **Strong** — pins type + url survives round-trip |

## Summary

- **34 tests total** (11 classes)
- **30 Strong**
- **4 Weak** — 3 connection-error-message tests (L105, L110, L115) and `test_falls_back_when_plugin_request_raises` (L307)

**Weak tests to fix (low priority):**
- L105 `test_missing_url`, L110 `test_missing_token` — only check `not result.ok`. Could pin error category/message.
- L115 `test_unauthorized` — substring `"401" in result.message`
- L307 `test_falls_back_when_plugin_request_raises` — asserts `is None` only. Both "swallow & no-op" AND "fall through" return None for an empty library, so the test doesn't pin which behaviour. Could assert call_count >= 2 to confirm fallback was attempted.

**File verdict: STRONG.** Heavy contracts (plugin path, URL construction matrix, refresh cascade, rate-limit cooldown, settings health/apply, posted-body field preservation) are all pinned tightly. Weak tests are limited to message-substring checks and one fallback path that doesn't distinguish "fell through" from "swallowed".
