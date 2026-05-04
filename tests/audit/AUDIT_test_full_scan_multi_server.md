# Audit: tests/test_full_scan_multi_server.py — ~30 tests

Tests for the multi-server full-library scan dispatcher (`_run_full_scan_multi_server`) and `_dispatch_processable_items`.

Includes the D34 server_id_filter regression matrix (Plex/Jellyfin/Emby × pinned/unpinned), file/worker callback emission, hot-reload, pause gating, dedup, and a full real end-to-end test.

## TestMultiServerFullScan

| Line | Test | Verdict |
|---|---|---|
| 49 | `test_no_servers_configured_returns_zero_counts` | **Strong** — pins all-zero counts AND `mock_pcp.assert_not_called()`. Audit-already-fixed (was bug-blind). |
| 67 | `test_pinned_to_server_only_walks_that_server` | **Strong** — pins `proc_a.list_canonical_paths.assert_called_once()`, `proc_b.assert_not_called()`, AND `kwargs["canonical_path"]`. |
| 114 | `test_no_pin_walks_every_enabled_server` | **Strong** — pins both processors called once, disabled server skipped, mock_process.call_count == 2. |
| 159 | `test_aggregates_per_publisher_outcomes_into_processing_result_counts` | **Strong** — pins `published == 1` AND `failed == 1` (different publish outcomes). |
| 195 | `test_zero_items_logs_warning_not_info` | **Strong** — pins WARNING-level message AND substring `"bogus-library-id"`. Real loguru sink. |

## TestPinnedFilterForwardedToProcessCanonicalPath

| Line | Test | Verdict |
|---|---|---|
| 276 | `test_plex_pinned_forwards_filter_to_process_canonical_path` | **Strong** — D34 reproducer: pins `kwargs["server_id_filter"] == "plex-default"` for EVERY call. The exact bug-class assertion. |
| 321 | `test_no_pin_plex_originator_fans_out` | **Strong** — pins `server_id_filter is None`. |
| 352 | `test_no_pin_non_plex_originator_scopes_to_originator` | **Strong** — pins `server_id_filter == "jelly-only"`. |
| 383 | `test_no_pin_emby_originator_scopes_to_originator` | **Strong** — Emby matrix cell. |
| 419 | `test_jellyfin_pinned_forwards_filter_to_process_canonical_path` | **Strong** — Jellyfin pinned matrix cell. |
| 467 | `test_emby_pinned_forwards_filter_to_process_canonical_path` | **Strong** — Emby pinned matrix cell. Full matrix. |
| 515 | `test_regenerate_thumbnails_propagates_to_process_canonical_path` | **Strong** — pins `regenerate is True`. P0.10 boundary contract. |
| 579 | `test_regenerate_default_false_when_attribute_missing` | **Strong** — `is False` strict (rejects truthy 1/"yes"). Mirror cell. |

## TestEnumerationStatusBanner

| Line | Test | Verdict |
|---|---|---|
| 629 | `test_querying_banner_emitted_per_server_before_enumeration` | **Strong** — substring matches on per-server name in progress messages. |
| 670 | `test_dispatch_banner_emitted_with_total_after_enumeration` | **Strong** — pins `(0, 4, "Dispatching...")` first emission. |

## TestFileResultEmissionFromMultiServerDispatch

| Line | Test | Verdict |
|---|---|---|
| 727 | `test_emits_file_result_per_completed_item` | **Strong** — pins exactly 2 records, exact paths, exact outcomes (`"generated"`, `"skipped_bif_exists"`), and worker label non-empty. D34 contract pin. |
| 776 | `test_emits_failed_file_result_when_process_canonical_path_raises` | **Strong** — pins `outcome == "failed"` for exception-swallowed branch. |

## TestWorkerCallbackEmissionFromMultiServerDispatch

| Line | Test | Verdict |
|---|---|---|
| 821 | `test_worker_callback_fires_with_active_workers_during_run` | **Strong** — pins ≥1 "processing" snapshot AND final state all idle AND current_title cleared. Multi-invariant. |
| 880 | `test_worker_callback_carries_current_title` | **Strong** — pins title appears in snapshot. |

## TestFriendlyDeviceLabel

| Line | Test | Verdict |
|---|---|---|
| 933 | `test_intel_long_name_collapses_to_bracketed_marketing_name` | **Strong** — strict equality on label format `"GPU Worker 1 (Intel UHD Graphics 770)"`. |
| 984 | `test_no_brackets_falls_back_to_full_name` | **Strong** — substring on full name. |

## TestParallelismRespectsPerDeviceWorkerCount

| Line | Test | Verdict |
|---|---|---|
| 1032 | `test_per_device_workers_count_expands_into_real_concurrency` | **Strong** — pins `peak_processing == 4`, slot counts always 4, ids stable {1,2,3,4}, label format. Multi-invariant for D34 dict-vs-attribute bug. |
| 1126 | `test_workers_zero_treated_as_one` | **Weak** — only asserts `any(snap for snap in snapshots)`, which is true if any worker snapshot exists at all. Doesn't pin the workers=0→1 clamp behavior. Should assert at least 1 slot present. |
| 1159 | `test_slot_rows_persist_across_items_no_flashing` | **Strong** — pins `sizes == {2}` exactly + per-slot identity stability AND `set(identity_by_id) == {1, 2}`. |

## TestHotReloadWorkerCount

| Line | Test | Verdict |
|---|---|---|
| 1250 | `test_growing_gpu_workers_mid_job_adds_a_slot` | **Strong** — pins `max_seen >= 3` after bump + label format on new slot. Real-time threading test. |

## TestPerJobLogCapture

| Line | Test | Verdict |
|---|---|---|
| 1351 | `test_dispatch_processable_items_registers_executor_threads` | **Strong** — pins `mapped_job == "job-D27-test"` for every executor thread. D27 boundary pin. |

## TestPauseGate

| Line | Test | Verdict |
|---|---|---|
| 1430 | `test_pause_check_blocks_dispatch_until_unpaused` | **Strong** — pins `pause_calls["count"] >= 2` (must spin-wait, not consult once) + `mock_process.assert_called_once()`. |
| 1466 | `test_pause_then_cancel_aborts_without_dispatch` | **Strong** — pins `mock_process.assert_not_called()`. |

## TestMultiPlexDeduping

| Line | Test | Verdict |
|---|---|---|
| 1510 | `test_two_plex_servers_sharing_media_dispatches_once_per_path` | **Strong** — pins `call_count == 1` (dedup) AND merged hint contains both server keys. |
| 1576 | `test_plex_plus_jellyfin_sharing_media_dispatches_once` | **Strong** — cross-vendor variant; pins both vendor identifiers in merged hint. |

## TestRealEndToEndMultiServerFullScan

| Line | Test | Verdict |
|---|---|---|
| 1648 | `test_full_scan_drives_real_publish_with_well_formed_inputs` | **Strong** — boundary-only e2e, captures all compute_output_paths + publish calls. Pins canonical_path equality, item_id equality, item_id NOT URL-form (D31). Frame_count flowed through. The "canary" test. |

## Summary

- **~30 tests** total
- **Strong**: ~29
- **Weak**: 1 (`test_workers_zero_treated_as_one` line 1126 — too loose)
- **Bug-blind / Tautological / Bug-locking**: 0
- **Needs human**: 0

**File verdict: STRONG.**

Recommended fixes:
- Line 1126 `test_workers_zero_treated_as_one` — strengthen the assertion. Currently `assert any(snap for snap in snapshots)` only confirms snapshots exist (would pass even if the clamp logic were broken to skip the device entirely with 0 workers, as long as some other path emitted snapshots). Suggest: `assert any(len(snap) >= 1 for snap in snapshots)` and ideally pin one slot present (workers=0 clamp to 1).

Notable strengths:
- Full ServerType matrix (Plex/Jellyfin/Emby × pinned/unpinned) — catches D34's per-vendor branch regressions.
- D31 URL-form leak prevention pinned in real-end-to-end test.
- Threading tests with timing (parallelism, hot-reload, pause gate) use real timing not mocks.
- File/worker callback emission tests catch the synthetic-worker bypass class (D34).
