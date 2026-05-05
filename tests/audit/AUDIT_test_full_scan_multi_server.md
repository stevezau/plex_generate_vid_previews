# Audit: tests/test_full_scan_multi_server.py — ~32 tests (re-audit, batch 6)

Tests for `_run_full_scan_multi_server` and `_dispatch_processable_items` (Phase D34): multi-server scan dispatcher, server pinning, dispatch fan-out, file-result + worker-callback emission, friendly device labels, hot-reload, pause gate, multi-Plex dedup, and end-to-end real-publish boundary test.

## TestMultiServerFullScan

| Line | Test | Verdict | Note |
|---|---|---|---|
| 49 | `test_no_servers_configured_returns_zero_counts` | Strong | Pins all-zero counts AND `mock_pcp.assert_not_called()`. |
| 67 | `test_pinned_to_server_only_walks_that_server` | Strong | Pins per-processor `assert_called_once`/`assert_not_called` AND `kwargs["canonical_path"]`. |
| 114 | `test_no_pin_walks_every_enabled_server` | Strong | Pins enable-state filter; both processors invoked once, dispatch fired twice. |
| 159 | `test_aggregates_per_publisher_outcomes_into_processing_result_counts` | Strong | Pins `published==1` and `failed==1` from outcome aggregation. |
| 195 | `test_zero_items_logs_warning_not_info` | Strong | Loguru sink-attach captures level + message; pins `"bogus-library-id"` substring at WARN level. |

## TestPinnedFilterForwardedToProcessCanonicalPath

| Line | Test | Verdict | Note |
|---|---|---|---|
| 276 | `test_plex_pinned_forwards_filter_to_process_canonical_path` | Strong | The exact d9918149 reproducer; pins `server_id_filter="plex-default"` for every dispatch call. |
| 321 | `test_no_pin_plex_originator_fans_out` | Strong | Pins `server_id_filter is None`. |
| 352 | `test_no_pin_non_plex_originator_scopes_to_originator` | Strong | Pins `server_id_filter == "jelly-only"`. |
| 383 | `test_no_pin_emby_originator_scopes_to_originator` | Strong | Matrix completion (Emby leg). |
| 419 | `test_jellyfin_pinned_forwards_filter_to_process_canonical_path` | Strong | Pin=Jellyfin matrix variant. |
| 467 | `test_emby_pinned_forwards_filter_to_process_canonical_path` | Strong | Pin=Emby matrix variant. |
| 515 | `test_regenerate_thumbnails_propagates_to_process_canonical_path` | Strong | Pins `kwargs.get("regenerate") is True` (P0.10 silent-skip class). |
| 579 | `test_regenerate_default_false_when_attribute_missing` | Strong | Strict `is False` (rejects truthy values 1, "yes"). |

## TestEnumerationStatusBanner

| Line | Test | Verdict | Note |
|---|---|---|---|
| 629 | `test_querying_banner_emitted_per_server_before_enumeration` | Strong | Pins per-server `"Test jellyfin"` / `"Test emby"` substrings on Querying messages. |
| 670 | `test_dispatch_banner_emitted_with_total_after_enumeration` | Strong | Pins first emit `(0, 4, "Dispatching")`. |

## TestFileResultEmissionFromMultiServerDispatch

| Line | Test | Verdict | Note |
|---|---|---|---|
| 727 | `test_emits_file_result_per_completed_item` | Strong | Pins exact `(path, outcome, worker)` for each row + non-empty worker label. |
| 776 | `test_emits_failed_file_result_when_process_canonical_path_raises` | Strong | Pins `path == "/data/m0.mkv"` AND `outcome == "failed"`. |

## TestWorkerCallbackEmissionFromMultiServerDispatch

| Line | Test | Verdict | Note |
|---|---|---|---|
| 821 | `test_worker_callback_fires_with_active_workers_during_run` | Strong | Pins ≥1 snapshot has 'processing' AND final snapshot all idle AND current_title cleared. |
| 880 | `test_worker_callback_carries_current_title` | Strong | Pins `"The Named Movie"` appeared in worker snapshots. |

## TestFriendlyDeviceLabel

| Line | Test | Verdict | Note |
|---|---|---|---|
| 933 | `test_intel_long_name_collapses_to_bracketed_marketing_name` | Strong | Pins exact label `"GPU Worker 1 (Intel UHD Graphics 770)"` AND NVIDIA pass-through. |
| 984 | `test_no_brackets_falls_back_to_full_name` | Weak | Uses `"NVIDIA GeForce RTX 4090" in n` — does not pin the full label format `"GPU Worker N (...)"`. **Why downgraded:** if the formatter dropped the `"GPU Worker N"` prefix, this test still passes (the device name is still there). Asymmetric with the strict equality of L933. |

## TestParallelismRespectsPerDeviceWorkerCount

| Line | Test | Verdict | Note |
|---|---|---|---|
| 1032 | `test_per_device_workers_count_expands_into_real_concurrency` | Strong | Pins peak concurrency == 4, slot count always 4, stable worker_id 1..4, name format includes both device strings. |
| 1126 | `test_workers_zero_treated_as_one` | Strong | Pins exactly 1 GPU slot for cuda:0 in last snapshot — closes the prior weak-`any(snapshots)` finding from previous audit. |
| 1173 | `test_slot_rows_persist_across_items_no_flashing` | Strong | Pins `sizes == {2}` AND identity-by-id stable across run. |

## TestHotReloadWorkerCount

| Line | Test | Verdict | Note |
|---|---|---|---|
| 1264 | `test_growing_gpu_workers_mid_job_adds_a_slot` | Strong | Pins max snapshot length ≥3 AND last big snapshot's labels match `"NVIDIA TITAN RTX"` format. Real-thread test with mutable settings stub. |

## TestPerJobLogCapture

| Line | Test | Verdict | Note |
|---|---|---|---|
| 1365 | `test_dispatch_processable_items_registers_executor_threads` | Strong | Pins every executor thread mapped to `"job-D27-test"`. |

## TestPauseGate

| Line | Test | Verdict | Note |
|---|---|---|---|
| 1444 | `test_pause_check_blocks_dispatch_until_unpaused` | Strong | Pins `pause_calls["count"] >= 2` AND `mock_process.assert_called_once()`. |
| 1480 | `test_pause_then_cancel_aborts_without_dispatch` | Strong | Pins `mock_process.assert_not_called()`. |

## TestMultiPlexDeduping

| Line | Test | Verdict | Note |
|---|---|---|---|
| 1524 | `test_two_plex_servers_sharing_media_dispatches_once_per_path` | Strong | Pins `call_count == 1` AND merged `item_id_by_server` carries BOTH `"rk-A"` and `"rk-B"`. |
| 1590 | `test_plex_plus_jellyfin_sharing_media_dispatches_once` | Strong | Cross-vendor variant; pins both vendor ids in merged hint. |

## TestRealEndToEndMultiServerFullScan

| Line | Test | Verdict | Note |
|---|---|---|---|
| 1662 | `test_full_scan_drives_real_publish_with_well_formed_inputs` | Strong | Boundary-only end-to-end: pins canonical_path AND `item_id == "12345"` AND not URL-form (D31 contract) on both compute_output_paths and publish; also pins `frame_count == 8`. |

**File verdict: STRONG (1 weak label-substring test).** Big improvement from prior "MIXED (1 weak)" — the prior weak `test_workers_zero_treated_as_one` has been strengthened. The matrix coverage for pin/originator combinations is exemplary, and the end-to-end boundary test is exactly the canary the previous audit asked for.

## Fix queue

- **L984 `test_no_brackets_falls_back_to_full_name`** — assert the full label format `"GPU Worker N (NVIDIA GeForce RTX 4090)"` rather than just substring containment, mirroring the rigour of `test_intel_long_name_collapses_to_bracketed_marketing_name` at L933.
