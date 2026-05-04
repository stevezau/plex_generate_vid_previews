# Audit: tests/test_processing_multi_server.py — 28 tests, 14 classes

## TestNoOwners

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 124 | `test_returns_no_owners_when_no_server_covers_path` | **Strong** | status `is` MultiServerStatus.NO_OWNERS + publishers == [] |

## TestPerServerExcludePaths

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 144 | `test_excluded_server_is_filtered_out` | **Strong** | Asserts emby-1 NOT in published_server_ids AND jellyfin-1 IS — proves filter works without false-positive |
| 183 | `test_no_servers_remain_after_exclusion_returns_no_owners` | **Strong** | NO_OWNERS pin when all excluded |

## TestSiblingMountProbe

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 217 | `test_finds_file_at_sibling_mount_when_canonical_stale` | **Strong** | Audit-fixed — now asserts canonical_path == live_file (not just status != SKIPPED). Pins D35 fix to RIGHT sibling, not just any sibling. |
| 263 | `test_single_mount_falls_through_to_skipped` | **Strong** | Status `is` SKIPPED_FILE_NOT_FOUND |

## TestSourceMissing

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 283 | `test_returns_skipped_file_not_found_when_source_file_missing` | **Strong** | D33 regression lock — explicit message: FAILED here would silently disable retry |

## TestSinglePublisher

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 321 | `test_emby_publisher_runs_one_ffmpeg_pass` | **Strong** | Pins status, frame_count, publisher status, gen.call_count==1, AND sidecar file exists on disk |

## TestMultiPublisherFanOut

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 371 | `test_one_pass_feeds_emby_and_jellyfin` | **Strong** | gen.call_count==1 (cornerstone pin) + per-server status dict + both formats land on disk |

## TestCrossServerBifReuse

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 454 | `test_emby_existing_bif_feeds_jellyfin_without_running_ffmpeg` | **Strong** | gen.call_count==0 — headline assertion. Plus per-server status pin |
| 529 | `test_single_publisher_does_not_attempt_bif_reuse` | **Strong** | gen.call_count==1 + PUBLISHED status |

## TestPartialFailureIsolation

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 568 | `test_jellyfin_missing_item_id_does_not_block_emby` | **Strong** | Per-server status dict — emby PUBLISHED, jellyfin SKIPPED_NOT_IN_LIBRARY |

## TestNotYetIndexedRoutesToSkip

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 622 | `test_plex_returns_skipped_not_indexed_when_hash_missing` | **Strong** | D31-aware: stubs at plex.query layer (not get_bundle_metadata) so URL builder runs. Pins URL == "/library/metadata/42/tree" exactly — would catch double-prefix regression |

## TestNotInLibraryRoutesToSkip

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 733 | `test_jellyfin_returns_skipped_not_in_library_when_item_id_unresolvable` | **Strong** | Pins status, message contains "library", does NOT contain "bookkeeping", aggregate status, scan-nudge args |
| 815 | `test_plex_returns_skipped_not_in_library_when_item_id_unresolvable` | **Strong** | Matrix completion of above for Plex bundle adapter — explicit per CLAUDE.md "cover the matrix" rule |

## TestSkipIfExists

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 895 | `test_skips_publisher_when_output_already_present` | **Strong** | Status pin + content unchanged + frame_source=="output_existed" pin |
| 933 | `test_regenerate_overrides_skip` | **Strong** | regenerate=True → PUBLISHED status |

## TestPublisherFailureModes

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 970 | `test_permission_denied_during_publish_returns_failed` | **Strong** | FAILED status + message substring (Permission/denied) — graceful error contract |
| 1025 | `test_disk_full_during_publish_returns_failed` | **Strong** | ENOSPC → FAILED + message check |
| 1064 | `test_compute_output_paths_oserror_returns_failed` | **Strong** | OSError in different layer also FAILED |

## TestNoFrames

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 1111 | `test_returns_no_frames_when_ffmpeg_produces_zero` | **Strong** | NO_FRAMES status pin |

## TestAdapterFactory

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 1142 | `test_picks_default_per_server_type` | **Strong** | adapter.name == "plex_bundle" — exact name pin |
| 1156 | `test_unknown_adapter_returns_none` | **Strong** | None contract |
| 1168 | `test_plex_without_config_folder_returns_none` | **Strong** | None contract for missing required setting |

## TestSummariseResults

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 1189 | `test_published_one_server_says_published_to_1` | **Strong** | Strict equality on user-facing string |
| 1195 | `test_published_two_servers_uses_plural` | **Strong** | Plural form pinned |
| 1201 | `test_partial_published_shows_n_of_m` | **Strong** | "1 of 2" form pin |
| 1207 | `test_skipped_outputs_existed_uses_friendly_phrase` | **Strong** | Strict equality on user-facing message |
| 1216 | `test_skipped_not_indexed_phrasing` | **Strong** | Strict equality + "publisher" jargon NOT in msg |
| 1227 | `test_no_publisher_jargon_in_any_branch` | **Strong** | Loops 4 status combinations — matrix coverage |

## TestItemIdResolverMemoisation

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 1261 | `test_same_server_queried_repeatedly_hits_backend_once` | **Strong** | backend.call_count == 1 across 5 lookups — pins 1f09c3a fix |
| 1289 | `test_different_servers_each_query_their_own_backend` | **Strong** | Distinct return per server + call_count == 3 — catches both flat-key cache leak AND wrong id |
| 1322 | `test_cache_remembers_none_for_not_in_library` | **Strong** | Negative-cache pin (THE bug 1f09c3a fixed) |
| 1351 | `test_cache_is_per_dispatch_not_global` | **Strong** | Two resolvers → 2 backend calls (per-dispatch isolation) |

## Summary

- **31 tests** total
- **31 Strong / 0 Weak / 0 Bug-blind / 0 Tautological / 0 Bug-locking / 0 Needs-human**
- D31 URL-shape regression lock at line 715-719 is exemplary (asserts every plex.query URL exactly)
- D33 (skipped vs failed routing for retry) and D34 (kwarg pinning) regression locks present
- D35 sibling mount probe pins to specific live path, not just status negation
- 1f09c3a memoisation regression has 4-cell matrix coverage

**File verdict: STRONG.** No changes needed.
