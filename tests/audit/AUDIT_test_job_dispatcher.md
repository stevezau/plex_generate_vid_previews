# Audit: tests/test_job_dispatcher.py — 26 tests, 9 classes

## TestJobTracker

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 53 | `test_record_completion_success` | **Strong** | Pins `total_items==2`, `completed==0` initial, then incremental successful counts AND `done_event.is_set()` trigger threshold. |
| 72 | `test_record_completion_failure` | **Strong** | Pins `failed==1` AND `done_event.is_set()` after final failure. |
| 83 | `test_cancel_drains_queue` | **Strong** | Multi-cell pin: queue==0, cancelled==True, done_event set, failed==3 (drained items count as failed). |
| 98 | `test_get_result` | **Strong** | Strict equality on each result key (`completed`, `failed`, `total`, `cancelled is False`). |
| 112 | `test_callbacks_fire` | **Strong** | Pins both progress callback args (1,1) AND on_item_complete tuple values. |

## TestJobDispatcher

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 145 | `test_single_job_completes` | **Strong** | Pins `completed==3` and `failed==0`. |
| 170 | `test_two_jobs_share_workers` | **Strong** | Pins per-tracker counts (2 and 3). Catches cross-job leakage. |
| 205 | `test_idle_workers_pick_up_next_job` | **Strong** | Asserts complete set of canonical_paths processed AND specific A path present — pins that worker spillover routes to specific items. |
| 252 | `test_cancel_one_job_others_continue` | **Strong** | Uses `processing_started` Event (not flaky sleep). Pins A.cancelled, B.completed==1, B.cancelled is False. |
| 304 | `test_pause_one_job_others_continue` | **Strong** | Pins B finishes while A blocks at completed==0; resume → A finishes. |
| 344 | `test_gpu_fallback_routes_to_correct_job` | **Strong** | GPU codec error → CPU fallback completes the item. Pins `completed==1`, `failed==0`. |
| 378 | `test_mixed_success_and_failure` | **Strong** | Pins `completed + failed == 4` (all items accounted for). |
| 412 | `test_submit_after_previous_completes` | **Strong** | Sequential submits with strict completed counts (1, 2). |
| 439 | `test_fifo_priority_drains_first_job_before_second` | **Strong** | Asserts EXACT dispatch order list — strongest possible cell for FIFO contract. |
| 481 | `test_outcome_counts_merged_to_tracker` | **Strong** | Pins outcome["generated"]==2 along with completed==2. |
| 501 | `test_merge_worker_outcome_aggregates_publishers_per_server` | **Strong** | D12 contract: pins exact aggregate counts AND server_type/server_name. Two-server × two-task matrix. |
| 547 | `test_drain_orphaned_fallback_routes_to_tracker` | **Strong** | Audit-fixed: tightened from `failed>=1` OR formula to exact `completed==0, failed==1`. Catches path-switch regression. |
| 582 | `test_cancel_passes_cancel_check_to_worker` | **Strong** | Pins `cancel_checks_received[0] is cancel_fn` (identity, not equality). Catches if dispatcher wraps/swaps the cb. |
| 612 | `test_cancelled_fallback_items_are_not_dispatched` | **Strong** | Pins `call_count[0]==1` exactly — proves no second CPU call after cancel. |

## TestDispatchLoopOrdering

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 661 | `test_first_worker_update_shows_busy_worker` | **Strong** | Pins `processing` in first snapshot — catches stale-idle-snapshot bug exactly. |

## TestInProgressFraction

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 693 | `test_fraction_from_busy_worker` | **Strong** | Pins both 0.5 for the right job AND 0.0 for other-job. Two cells. |
| 708 | `test_fraction_zero_when_idle` | **Strong** | Empty-worker pool → 0.0. |

## TestProgressBarMonotonicity

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 735 | `test_record_completion_includes_in_flight_fraction` | **Strong** | Direct D37 regression. Compares Path A's emitted percent to Path B's mirrored formula — catches the bar-bounce bug exactly. |

## TestProgressCallbackPercentOverride

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 789 | `test_progress_includes_in_flight_work` | **Strong** | Pins `percent_override is not None` for every completion call. The contract that keeps the two emit paths in sync. |

## TestReapRetrySkipThroughput

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 841 | `test_fast_items_complete_quickly` | **Strong (perf assertion)** | Pins `completed==10` AND wall time `< 0.5s`. Time-based assertion is sound: 10 items × ~5ms = 50ms minimum without reap-retry → 500ms cap is generous but catches a 10× regression. **Note:** the inline comment misstates the math (says `~10ms = ~100ms minimum` then sets cap to 500ms); the cap itself is fine, just the rationale is loose. |
| 870 | `test_slow_and_fast_items_mixed` | **Strong** | Pins `completed==5`. Could be tighter (e.g. assert call_order has all paths) but covers the throughput contract. |

## TestPoolReconciliationOnDispatch

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 919 | `test_pool_gains_workers_via_callback` | **Strong** | Pins workers count (2), worker_type=="GPU", AND completion count (4). Three checks in one. |

## TestInflightJobGuard

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 956 | `test_duplicate_call_is_skipped` | **Strong** | Pins `len(calls)==0` exactly — proves the guard prevents thread spawn. SpyThread pattern is precise. |

## Summary

- **26 tests** — all **Strong**
- 0 weak / bug-blind / tautological / bug-locking
- One audit-strengthened test (line 547-580) tightened OR-clause from `failed>=1` to strict counts
- D37 progress-monotonicity regression has a dedicated test that explicitly mirrors both emit-path formulas (line 735)
- D12 publisher aggregation tested with exact counts on a two-server × two-task matrix (line 501)
- Minor: rationale comment in `test_fast_items_complete_quickly` (line 865) does cap-vs-math math that doesn't quite check out (says minimum 100ms but caps at 500ms). The assertion is still meaningful — flag for human only if you want the comment cleaned up.

**File verdict: STRONG.** No code changes needed; comment polish on line 865 is optional.
