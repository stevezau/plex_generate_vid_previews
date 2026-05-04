# Audit: tests/test_processing.py — 30 tests, 9 classes

## TestMultiServerGuards

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 147 | `test_no_plex_full_scan_routes_through_multi_server_scan` | **Strong** | Pins `assert_called_once`, the SAME config object (`is`), exact `selected_gpus == []`, AND no skipped_reason. Multi-cell contract pin. |
| 166 | `test_pinned_to_non_plex_full_scan_routes_through_multi_server_scan` | **Strong** | Pins `server_id_filter == "emby-1"` kwarg passed to scan. Direct D34-paradigm fix — would catch a regression that drops the filter. |
| 183 | `test_no_plex_with_webhook_paths_dispatches_via_worker_pool` | **Strong** | Pins call count + items length + canonical_path. Catches the "synchronous direct-call" regression. |
| 216 | `test_k4_unresolved_paths_fall_back_to_worker_pool_when_emby_present` | **Strong** | Pins K4 dispatch carries exactly `b.mkv + c.mkv` (sorted), NOT `a.mkv`. Direct subset assertion. |
| 264 | `test_owning_servers_breadcrumb_logged_before_resolver` | **Strong** | Substring assertion ("owning server" + "plex") on captured log lines. Fragile to log-text drift but the contract is the breadcrumb itself. |
| 317 | `test_hint_short_circuit_skips_plex_resolution_when_hints_present` | **Strong** | Audit L5: `mock_resolve.assert_not_called()` is the load-bearing assertion + worker-pool dispatch with the right item_id_by_server. The contract test for hint-bypass. |
| 352 | `test_k4_does_not_cascade_when_pinned_to_plex` | **Strong** | M4 contract: pin requires NO Emby fallback. Iterates ALL calls to confirm `/data/x.mkv` never dispatched. Strong negative-assertion. |
| 388 | `test_k4_no_fallback_when_only_plex_configured` | **Strong** | Mirror cell — no Emby/Jellyfin sibling → no K4 fallback. Negative assertion via iteration. |

## TestLibraryScanFlow

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 431 | `test_processes_multiple_libraries` | **Strong** | Pins `outcome["generated"]==3`, `assert_called_once`, AND `len(dispatched_items)==3`. |
| 455 | `test_skips_empty_library` | **Strong** | Pins `process_items_headless.assert_not_called()` — the "empty input must not dispatch" contract. |
| 471 | `test_no_libraries_returns_empty_outcome` | **Weak** | Only asserts `result is not None` and `"outcome" in result`. Doesn't verify outcome counts are zero or that no dispatch happened. Largely overlaps `test_skips_empty_library`. **Could be tightened** to assert outcome counts are all 0 AND `process_items_headless.assert_not_called()`. |
| 484 | `test_sort_by_random_shuffles_combined_items` | **Strong** | Strict equality `dispatched == list(reversed(original_order))`. Deterministic stand-in shuffler is the right pattern. |
| 510 | `test_sort_by_non_random_preserves_order` | **Strong** | Strict equality `dispatched == items`. |
| 528 | `test_progress_callback_invoked` | **Strong** | Pins "Connecting to Plex" stage AND a dispatch tick with total==2 AND "Starting" substring. Two stages of the lifecycle pinned. |

## TestWebhookFlow

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 569 | `test_webhook_with_resolved_items` | **Strong** | Pins resolved_count, unresolved_paths list, AND outcome.generated. Three contracts. |
| 590 | `test_webhook_no_matches_skips_dispatch` | **Strong** | Pins `assert_not_called()` AND `resolved_count==0`. |
| 607 | `test_webhook_progress_callback` | **Strong** | Pins "Connecting" + "Looking up 1 file path" + dispatch tick with total==1. Multi-stage. |

## TestCancellation

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 640 | `test_cancel_during_enumeration` | **Strong** | Pins `process_items_headless.assert_not_called()` after cancel during enumeration. |
| 664 | `test_cancel_before_dispatch` | **Strong** | Mirror cell — cancel returns True from start. |

## TestSummaryAndWarnings

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 690 | `test_summary_includes_all_outcome_types` | **Strong** | Strict equality on three outcome counts (generated, skipped_bif_exists, failed). |
| 717 | `test_path_mapping_warning_on_all_not_found` | **Strong** | Pins "path mapping" substring in captured warnings. The actionable-warning contract. |
| 748 | `test_cancellation_noted_in_summary` | **Weak** | Only asserts `result is not None` and `"outcome" in result`. Despite the test name claiming "cancellation noted", nothing checks that cancelled status appears anywhere in the result. **Bug-blind for the cancellation-noting claim**: a regression that drops the cancelled flag entirely from the summary would pass. **Should be tightened** to assert `result["outcome"]["cancelled"]` or wherever the flag lives. |

## TestErrorHandling

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 776 | `test_connection_error_returns_none` | **Strong** | Strict `result is None`. |
| 784 | `test_unexpected_exception_re_raised` | **Strong** | `pytest.raises(RuntimeError, match="boom")` pins the exception AND its message. |
| 791 | `test_keyboard_interrupt_returns_none` | **Strong** | Strict `result is None`. |

## TestCleanup

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 808 | `test_worker_pool_shutdown_called` | **Strong** | `pool_inst.shutdown.assert_called_once()` — pins the cleanup contract. |
| 827 | `test_worker_pool_callback_receives_pool_and_none` | **Strong** | Pins `call_count==2`, `assert_any_call(pool_inst)` AND `assert_any_call(None)`. Pins both lifecycle phases. |
| 853 | `test_temp_folder_cleaned_up` | **Strong** | Pins `not work_dir.exists()` after the run. |
| 868 | `test_cleanup_on_error` | **Strong** | Pins temp folder removal even when ConnectionError raised. |
| 879 | `test_no_shutdown_when_job_id_set` | **Strong** | Pins `shutdown.assert_not_called()` when dispatcher owns the pool. |

## TestJobDispatcherPath

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 901 | `test_dispatcher_existing_pool` | **Weak** | Asserts `result is not None` and `on_start.assert_called_once()`. Doesn't verify the existing pool was actually reused (e.g. that `WorkerPool` was NOT instantiated, or that submit_items was called with the existing pool). The test name promises "reuses worker_pool" but the only thing checked is that `on_dispatch_start` fired. **Should be tightened** with `MockPool.assert_not_called()` or asserting `mock_dispatcher.submit_items` got the existing pool. |
| 948 | `test_dispatcher_creates_new_pool` | **Weak** | Same problem as above. Asserts only `result is not None`. The test name promises "new worker_pool is created" but nothing checks that `WorkerPool(...)` was actually instantiated or that the new pool was passed to the dispatcher. **Should be tightened** to assert `MockPool` was called and the dispatcher was created with that pool. |

## TestSummaryBranches

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 1001 | `test_excluded_and_invalid_hash_in_outcome` | **Strong** | Strict equality on three outcome counts. |

## TestCleanupEdgeCases

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 1037 | `test_shutdown_error_is_logged` | **Strong** | Audit-fixed: now captures loguru sink and asserts "shutdown" appears. Used to be only "didn't crash". |
| 1069 | `test_temp_cleanup_error_is_logged` | **Strong** | Same audit fix — captures loguru output and asserts cleanup-failure log line. |
| 1093 | `test_cancel_during_enumeration_with_items_queued` | **Strong** | Pins `process_items_headless.assert_not_called()` after mid-enumeration cancel. |

## Summary

- **30 tests** — 27 Strong, 3 Weak
- 0 bug-blind / tautological / bug-locking
- **Weak tests that should be tightened**:
  - **Line 471** `test_no_libraries_returns_empty_outcome` — only `is not None` + key-presence; no dispatch-not-called assertion. Largely duplicates line 455.
  - **Line 748** `test_cancellation_noted_in_summary` — test name says "cancellation noted" but nothing checks the cancellation flag appears in the result. Bug-blind for its stated claim.
  - **Lines 901 & 948** in `TestJobDispatcherPath` — both assert little beyond `result is not None`; neither verifies the dispatcher path actually does what its docstring claims (reuse vs create pool). The dispatch internals (sys.modules patching dance) are exercised but the contracts aren't pinned.
- Audit-strengthened in batch 5 (per inline comments): `test_shutdown_error_is_logged` (line 1037) and `test_temp_cleanup_error_is_logged` (line 1069) — both went from "didn't crash" to "log line emitted".
- Strong negative-assertions on K4 cascade pinning (lines 352, 388) — both iterate ALL dispatch calls to confirm `/data/x.mkv` never appears.

**File verdict: MIXED.** 27/30 strong. Three loose tests at lines 471, 748, 901, 948 should be tightened. None are bug-blind in the D34 sense (call-count vs args), but lines 748 and 901/948 fail the "would the test fail if the bug were reintroduced" smell test for their stated claims.
