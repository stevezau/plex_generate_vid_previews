# Audit: tests/test_servers_plex.py — 35 tests, 11 classes

## TestConstruction

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 32 | `test_implements_media_server` | **Strong** — isinstance check pins protocol |
| 37 | `test_type_is_plex` | **Strong** — strict `is ServerType.PLEX` |
| 40 | `test_id_and_name_propagate` | **Strong** — strict equality on id + name |
| 44 | `test_construction_does_not_connect` | **Strong** — pins lazy-connect contract via `assert_not_called` on `plex_server` |

## TestTestConnection

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 52 | `test_success_carries_identity` | **Strong** — strict equality on id/name/version |
| 73 | `test_missing_credentials_short_circuits` | **Weak** — substring `"required" in result.message.lower()`; ok-flag is strict |
| 83 | `test_timeout_returns_failure` | **Weak** — substring `"timed out"` |
| 92 | `test_unauthorized_returns_specific_message` | **Weak** — substring `"401" in result.message` |
| 105 | `test_ssl_error_returns_specific_message` | **Weak** — substring `"ssl"` |

## TestListLibraries

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 116 | `test_returns_library_objects_with_enabled_filter_by_id` | **Strong** — len + per-name enabled True/False + remote_paths tuple + kind |
| 146 | `test_returns_library_objects_with_enabled_filter_by_title` | **Strong** — pins title-based filter cell |
| 172 | `test_no_filter_means_all_enabled` | **Strong** — pins legacy "no filter" branch |
| 190 | `test_explicit_per_library_disabled_via_server_config` | **Strong** — pins multi-server explicit-disabled regression catcher (3 cells: ticked False, ticked True, not in snapshot → False) |
| 247 | `test_all_libraries_unticked_means_all_disabled` | **Strong** — sister regression test; pins all-False outcome |
| 291 | `test_returns_empty_list_on_failure` | **Strong** — strict `== []` |

## TestListItems

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 301 | `test_yields_movies` | **Strong** — pins title + library_id + remote_path + bare ratingKey id (D31 regression catcher) |
| 324 | `test_yields_episodes_with_formatted_title` | **Strong** — pins formatted title + id |
| 342 | `test_falls_back_to_key_when_ratingkey_missing` | **Strong** — pins strip of `/library/metadata/` prefix |
| 363 | `test_unknown_library_yields_nothing` | **Strong** — strict `== []` |
| 371 | `test_captures_bundle_metadata_from_plexapi_parts` | **Strong** — strict equality on bundle_metadata tuple-of-tuples (D31 regression catcher) |

## TestResolveItemToRemotePath

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 416 | `test_returns_first_part_path` | **Strong** — strict equality + `assert_called_once_with(42)` int-cast pinned |
| 431 | `test_non_numeric_id_returns_none` | **Strong** — strict `is None` |
| 435 | `test_lookup_failure_returns_none` | **Strong** — strict `is None` |
| 442 | `test_no_media_parts_returns_none` | **Strong** — strict `is None` |

## TestGetBundleMetadata

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 481 | `test_bare_rating_key_builds_correct_url` | **Strong** — `assert_called_once_with` pins exact URL + result tuple |
| 492 | `test_full_path_form_does_not_double_the_prefix` | **Strong** — D31 regression catcher; pins URL exactly + double-slash defence + count==1 |
| 512 | `test_extracts_every_mediapart_with_hash` | **Strong** — pins multi-part list contents (in-tests, both required) |
| 530 | `test_skips_mediaparts_with_empty_hash` | **Strong** — strict equality on filtered list |
| 548 | `test_query_failure_returns_empty_list` | **Strong** — strict `== []` |
| 558 | `test_empty_item_id_returns_empty_without_query` | **Strong** — pins both `""` and None inputs + `assert_not_called` on plex.query |

## TestTriggerRefresh

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 569 | `test_dispatches_to_partial_scan` | **Strong** — pins `unresolved_paths`, `plex_url`, `plex_token` kwargs (matches the D34-style "assert what SUT controls" rule from CLAUDE.md) |
| 579 | `test_no_path_no_op` | **Strong** — `assert_not_called` |
| 584 | `test_swallows_exceptions` | **Strong** — audit-fixed; asserts call_count >= 1 (catches early-return regression) |

## TestParseWebhook

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 602 | `test_library_new_with_rating_key` | **Strong** — strict equality on event_type + item_id + remote_path |
| 614 | `test_irrelevant_event_returns_none` | **Strong** — matrix of irrelevant events |
| 619 | `test_accepts_raw_bytes` | **Strong** — pins bytes contract |
| 625 | `test_invalid_json_bytes_returns_none` | **Strong** — strict `is None` |
| 628 | `test_non_dict_payload_returns_none` | **Strong** — strict `is None` |
| 631 | `test_missing_rating_key_yields_none_item_id` | **Strong** — pins None contract for missing field |

## TestPlexSettingsHealth

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 648 | `test_no_issues_when_all_recommended` | **Strong** — strict `== []` |
| 657 | `test_reports_each_misset_pref_as_server_wide` | **Strong** — len + library_id None + critical severity + flag name pinned |
| 673 | `test_empty_on_request_failure` | **Strong** — strict `== []` |

## TestPlexApplyRecommended

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 689 | `test_writes_only_misset_prefs` | **Strong** — strict set equality on results.keys + put.call_count==2 + per-call params strict equality |
| 720 | `test_flag_filter_restricts_target` | **Strong** — pins flag filter respected; only one PUT |

## Summary

- **39 tests total** (11 classes)
- **35 Strong**
- **4 Weak** — message-substring tests in `TestTestConnection` (L73, L83, L92, L105)

**Weak tests to fix (low priority):**
- L73 `test_missing_credentials_short_circuits` — substring "required"
- L83 `test_timeout_returns_failure` — substring "timed out"
- L92 `test_unauthorized_returns_specific_message` — substring "401"
- L105 `test_ssl_error_returns_specific_message` — substring "ssl"

These pin user-visible error category but not exact message. Same pattern as Emby/Jellyfin connection tests.

**File verdict: STRONG.** All load-bearing contracts pinned: D31 URL construction (no double-prefix), D31 bundle_metadata capture, lazy connect, multi-server explicit-disabled cells, ratingKey vs URL handling, settings health, recommended-pref apply (PUT params strictly verified), webhook parsing matrix.
