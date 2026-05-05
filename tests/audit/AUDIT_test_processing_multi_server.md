# Audit: tests/test_processing_multi_server.py — 31 tests (re-audit, batch 6)

Tests for `process_canonical_path` (Phase D / multi-server fan-out): owner resolution, exclude_paths, sibling-mount probe, source-missing routing, single + multi-publisher fan-out, cross-server BIF reuse (D34), partial-failure isolation, not-yet-indexed routing, not-in-library routing, skip-if-exists, publisher failure modes (EACCES/ENOSPC), no-frames, adapter factory, friendly summarise messages (D16), and item-id resolver memoisation (P0.5 / 1f09c3a).

## TestNoOwners

| Line | Test | Verdict | Note |
|---|---|---|---|
| 124 | `test_returns_no_owners_when_no_server_covers_path` | Strong | Pins `status is NO_OWNERS` + `publishers == []`. |

## TestPerServerExcludePaths

| Line | Test | Verdict | Note |
|---|---|---|---|
| 144 | `test_excluded_server_is_filtered_out` | Strong | Pins emby-1 NOT in publishers AND jelly-1 IN publishers. |
| 183 | `test_no_servers_remain_after_exclusion_returns_no_owners` | Strong | Pins `status is NO_OWNERS`. |

## TestSiblingMountProbe

| Line | Test | Verdict | Note |
|---|---|---|---|
| 217 | `test_finds_file_at_sibling_mount_when_canonical_stale` | Strong | Audit-fixed: pins `result.canonical_path == str(live_file)` not just NOT-skipped (closes "could pick wrong sibling" gap). |
| 263 | `test_single_mount_falls_through_to_skipped` | Strong | Pins `status is SKIPPED_FILE_NOT_FOUND`. |

## TestSourceMissing

| Line | Test | Verdict | Note |
|---|---|---|---|
| 283 | `test_returns_skipped_file_not_found_when_source_file_missing` | Strong | Pins exact status (D33 contract — must be SKIPPED, not FAILED, for retry path) + lower-case substring `"not found"`. |

## TestSinglePublisher

| Line | Test | Verdict | Note |
|---|---|---|---|
| 321 | `test_emby_publisher_runs_one_ffmpeg_pass` | Strong | Pins status, frame_count==5, len(publishers)==1, publisher status, gen.call_count==1, sidecar file exists at expected path. Multi-invariant. |

## TestMultiPublisherFanOut

| Line | Test | Verdict | Note |
|---|---|---|---|
| 371 | `test_one_pass_feeds_emby_and_jellyfin` | Strong | Pins gen.call_count==1, status, len(publishers)==2, per-server statuses, both output files on disk in vendor-specific layouts. Cornerstone fan-out test. |

## TestCrossServerBifReuse

| Line | Test | Verdict | Note |
|---|---|---|---|
| 454 | `test_emby_existing_bif_feeds_jellyfin_without_running_ffmpeg` | Strong | Pins `gen.call_count == 0` (the headline) + status PUBLISHED + per-server statuses (jelly-1 PUBLISHED, emby-1 SKIPPED_OUTPUT_EXISTS). |
| 529 | `test_single_publisher_does_not_attempt_bif_reuse` | Strong | Pins `gen.call_count == 1` + publisher status PUBLISHED. |

## TestPartialFailureIsolation

| Line | Test | Verdict | Note |
|---|---|---|---|
| 568 | `test_jellyfin_missing_item_id_does_not_block_emby` | Strong | Pins aggregate PUBLISHED (≥1 succeeded) + per-server statuses (emby PUBLISHED, jelly SKIPPED_NOT_IN_LIBRARY). |

## TestNotYetIndexedRoutesToSkip

| Line | Test | Verdict | Note |
|---|---|---|---|
| 622 | `test_plex_returns_skipped_not_indexed_when_hash_missing` | Strong | D31-aware: stubs `plex.query` (NOT get_bundle_metadata) so URL construction runs. Pins per-publisher status, aggregate SKIPPED_NOT_INDEXED, message contains `"Waiting for 1 server"` AND no `"publisher"` jargon, AND every plex.query URL equals `/library/metadata/42/tree` (no doubling). Excellent multi-invariant + boundary-correct mocking. |

## TestNotInLibraryRoutesToSkip

| Line | Test | Verdict | Note |
|---|---|---|---|
| 733 | `test_jellyfin_returns_skipped_not_in_library_when_item_id_unresolvable` | Strong | Pins per-publisher status, message has `"library"` and lacks `"bookkeeping"`, aggregate SKIPPED_NOT_INDEXED, scan-nudges captured with `(None, str(media_file))`. |
| 815 | `test_plex_returns_skipped_not_in_library_when_item_id_unresolvable` | Strong | Matrix completion (Plex bundle adapter); same multi-invariant pin as Jellyfin variant. |

## TestSkipIfExists

| Line | Test | Verdict | Note |
|---|---|---|---|
| 895 | `test_skips_publisher_when_output_already_present` | Strong | Pins SKIPPED_OUTPUT_EXISTS + existing file untouched + frame_source == "output_existed". |
| 933 | `test_regenerate_overrides_skip` | Strong | Pins PUBLISHED with `regenerate=True`. |

## TestPublisherFailureModes

| Line | Test | Verdict | Note |
|---|---|---|---|
| 970 | `test_permission_denied_during_publish_returns_failed` | Strong | Pins aggregate FAILED + per-publisher FAILED + message contains "Permission" or "denied". |
| 1025 | `test_disk_full_during_publish_returns_failed` | Strong | Pins aggregate FAILED + per-publisher FAILED + message contains "No space" or "28". |
| 1064 | `test_compute_output_paths_oserror_returns_failed` | Strong | Pins aggregate FAILED + per-publisher FAILED. |

## TestNoFrames

| Line | Test | Verdict | Note |
|---|---|---|---|
| 1111 | `test_returns_no_frames_when_ffmpeg_produces_zero` | Strong | Pins `status is NO_FRAMES`. |

## TestAdapterFactory

| Line | Test | Verdict | Note |
|---|---|---|---|
| 1142 | `test_picks_default_per_server_type` | Strong | Pins adapter not None + name == "plex_bundle". |
| 1156 | `test_unknown_adapter_returns_none` | Strong | Pins None. |
| 1168 | `test_plex_without_config_folder_returns_none` | Strong | Pins None. |

## TestSummariseResults

| Line | Test | Verdict | Note |
|---|---|---|---|
| 1189 | `test_published_one_server_says_published_to_1` | Strong | Pins exact string. |
| 1195 | `test_published_two_servers_uses_plural` | Strong | Pins exact string. |
| 1201 | `test_partial_published_shows_n_of_m` | Strong | Pins exact string. |
| 1207 | `test_skipped_outputs_existed_uses_friendly_phrase` | Strong | Pins exact string `"Already up to date on 1 server"`. |
| 1216 | `test_skipped_not_indexed_phrasing` | Strong | Pins exact string AND no "publisher" jargon. |
| 1227 | `test_no_publisher_jargon_in_any_branch` | Strong | Loops 4 status pairs and pins `"publisher" not in msg`. Matrix coverage. |

## TestItemIdResolverMemoisation

| Line | Test | Verdict | Note |
|---|---|---|---|
| 1261 | `test_same_server_queried_repeatedly_hits_backend_once` | Strong | Pins all 5 results == "item-42" + `backend.call_count == 1` (1f09c3a contract). |
| 1289 | `test_different_servers_each_query_their_own_backend` | Strong | Pins distinct returns per server (catches cache-mix-up wrong-id, not just call-count) + `call_count == 3`. |
| 1322 | `test_cache_remembers_none_for_not_in_library` | Strong | Pins all 4 results == None + `call_count == 1` (negative-result caching). |
| 1351 | `test_cache_is_per_dispatch_not_global` | Strong | Pins `call_count == 2` for two separate dispatches (per-dispatch isolation). |

**File verdict: STRONG.** Re-audit found ZERO weak/bug-blind/tautological tests across all 31. The matrix coverage is exemplary (Plex/Jellyfin × not-indexed × not-in-library × OSError variants), boundary mocking is at the right layer (e.g. `plex.query` for D31-aware URL coverage), and every test pins multiple invariants rather than a single status code. The cross-server BIF reuse + sibling-mount probe tests close real production bugs (D34, D35) with strong assertions.

## Fix queue

(empty — file is gold-standard quality)
