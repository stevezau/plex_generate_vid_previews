# Audit: tests/test_worker.py — 49 tests, 8 classes

## TestWorker

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 26 | `test_worker_initialization` | **Strong** | 9 strict-equality field pins covering full Worker constructor contract |
| 40 | `test_worker_is_available` | **Strong** | True/False matrix on is_busy |
| 49 | `test_worker_assign_task` | **Strong** | Pins is_busy, current_task path, media_title, title_kind ("video" derivation) |
| 74 | `test_worker_assign_task_when_busy` | **Strong** | RuntimeError on mid-task reassign |
| 91 | `test_worker_assign_task_accepts_pre_claimed_state` | **Strong** | Pre-claim atomic-claim race fix pinned: is_busy=True+current_task=None must accept |
| 109 | `test_worker_check_completion` | **Strong** | Returns True + is_busy False after thread join |
| 131 | `test_worker_progress_data` | **Strong** | Audit-fixed — round-trip + contract shape (required keys present on fresh worker) |
| 169 | `test_worker_find_available` | **Strong** | 3 cells: all-avail / first-two-busy / all-busy → None |
| 192 | `test_worker_shutdown_waits_for_longer_timeout` | **Strong** | `assert_called_once_with(timeout=60)` — pins exact timeout value |
| 203 | `test_worker_format_gpu_name` | **Weak (substring)** | Asserts `len(name)==10` (strong) but content via `"RTX" in name or "NVIDIA" in name` for NVIDIA only; AMD/Intel only check length. A regression that returned `"PADDING12 "` for AMD would pass. **Marginal — AMD/Intel content not pinned.** |
| 221 | `test_worker_thread_execution` | **Strong** | Asserts thread started, call_count==1, completed==1 |
| 252 | `test_last_task_outcome_delta` | **Strong** | Asserts delta["generated"]==1 + all OTHERS == 0; second task delta resets — pins per-task semantics |
| 278 | `test_worker_gpu_codec_error_retries_on_cpu_in_place` | **Strong** | 7 assertions: 2 calls, kwargs.gpu first, kwargs.gpu None second, gpu_device_path None, completed=1, fallback_active=True, fallback_reason substring |
| 319 | `test_worker_gpu_cpu_fallback_records_failure_when_cpu_retry_fails` | **Strong** | 2 calls + completed=0 + failed=1 + fallback_active |
| 345 | `test_worker_cpu_handles_codec_error_as_failure` | **Strong** | CPU worker: failed=1, completed=0 |

## TestPerJobThreadScoping

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 386 | `test_thread_registered_to_one_job_isnt_thread_for_another` | **Strong** | D5 leak pin — sibling job_id MUST be False |
| 401 | `test_unregister_clears_only_calling_thread` | **Strong** | Round-trip register→unregister→False |
| 417 | `test_concurrent_workers_each_scoped_to_own_job` | **Strong** | Real threads cross-checking — pins both self_match True AND cross_match False |
| 468 | `test_unowned_registration_doesnt_match_any_real_job` | **Strong** | Empty-string back-compat pin: never matches any job_id, including "" |

## TestWorkerPool

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 492 | `test_worker_pool_initialization` | **Strong** | Length + per-index worker_type pin (GPU first, CPU last) |
| 511 | `test_worker_pool_gpu_assignment` | **Strong** | Round-robin gpu_index pinned (0,1,0,1) |
| 528 | `test_worker_pool_process_items` | **Strong** | total_completed == 2 |
| 554 | `test_worker_pool_has_busy_workers` | **Strong** | False/True matrix |
| 563 | `test_worker_pool_has_available_workers` | **Strong** | True/False matrix |
| 573 | `test_worker_pool_shutdown` | **Strong** | Audit-improved — asserts every worker reports is_running=False/_stopped=True (was "doesn't crash" floor) |
| 590 | `test_worker_pool_add_and_remove_workers` | **Strong** | added==2, len==4, removed/scheduled/unavailable dict-equality |
| 606 | `test_remove_workers_schedules_busy_and_retires_when_idle` | **Strong** | Full dict equality + retired count + length check |
| 621 | `test_dynamic_remove_does_not_stall_completion` | **Strong** | elapsed < 2.0s + total accounted (completed+failed == 8) |
| 653 | `test_dynamic_gpu_removal_does_not_stall_completion` | **Strong** | Same shape for GPU removal |
| 692 | `test_worker_pool_pause_check_blocks_dispatch` | **Strong** | Both completed==2 AND elapsed >= 0.2 (proves the wait actually happened) |
| 722 | `test_no_dispatch_while_paused` | **Strong** | first_assign_time >= pause_duration*0.9 — proves no early-dispatch |
| 766 | `test_worker_pool_stats_are_per_library` | **Strong** | Two separate process_items calls — first.completed==2, second.completed==1 (per-call scoping) |
| 804 | `test_worker_pool_task_completion` | **Strong** | total_completed == 4 |
| 836 | `test_worker_pool_error_handling` | **Weak** | Only `total_failed > 0`. Doesn't pin which/how many failed. With 4 items and every-other failing, expected==2, but test passes for any positive count (1,2,3,4). **Recommend** changing to `assert total_failed == 2` and `total_completed == 2`. |
| 872 | `test_worker_pool_progress_updates` | **Strong** | Audit-improved — add_task.call_count == 1, remove_task.call_count == 1 (1:1 pairing pinned, not just `.assert_called()`) |
| 899 | `test_worker_statistics` | **Tautological (mild)** | Sets w.completed=5 then asserts sum==5+3=8. Tests Python sum() — not the SUT. **Marginal value** — could be deleted but harmless. |
| 914 | `test_worker_pool_cpu_fallback_on_codec_error` | **Strong** | Pins call_order==[(key1, NVIDIA), (key1, None)] — exact ordered pair |
| 952 | `test_mixed_workload_with_gpu_cpu_fallback` | **Weak** | Only `total_completed == 3`. Doesn't pin call_order per item — a regression that put key1 on CPU first would pass. The single-item variant pins call_order; this 3-item variant should too. |
| 989 | `test_codec_error_fails_when_cpu_retry_also_fails` | **Strong** | failed=1, completed=0, fallback_active=True |

## TestReconcileGpuWorkers

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 1025 | `test_reconcile_removes_idle_workers_immediately` | **Strong** | Strict equality on removed/deferred + length |
| 1038 | `test_reconcile_defers_busy_workers` | **Strong** | Dict equality + len(deferred)==2 |
| 1055 | `test_reconcile_mixed_idle_and_busy` | **Strong** | Pins removed=2, deferred=1, kept count |
| 1071 | `test_pending_removal_prevents_task_assignment` | **Strong** | Pins is_available()==False BOTH while busy AND after idle (pending flag persists) |
| 1091 | `test_deferred_worker_retired_after_completion` | **Strong** | Identity check `not in pool.workers` after retire |
| 1110 | `test_deferred_workers_cleaned_by_apply_deferred_removals` | **Strong** | retired==2 + len==1 |
| 1128 | `test_reconcile_disabled_device_defers_busy` | **Strong** | _pending_removal True pin |

## TestWorkerProgressCount

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 1145 | `test_progress_not_double_counted_on_gpu_cpu_fallback` | **Strong** | H2 regression lock — completed_counts[-1] == 1 (would catch ==2 double-count bug) |
| 1186 | `test_fallback_state_resets_on_new_task` | **Strong** | After 2nd assign: fallback_active==False, fallback_reason is None (state reset pin) |

## TestWorkerCancellation

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 1224 | `test_worker_cancellation_does_not_fallback_to_cpu` | **Strong** | failed=1, completed=0, fallback_active=False, mock_process.call_count==1 (no CPU retry) |
| 1246 | `test_worker_passes_cancel_check_to_process_item` | **Strong** | call_kwargs["cancel_check"] is cancel_fn (identity check) |

## TestBuildSelectedGpus

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 1290 | `test_enabled_gpu_returned_with_config_values` | **Strong** | Pins gpu_type, device, workers, ffmpeg_threads via strict equality |
| 1305 | `test_disabled_gpu_is_skipped` | **Strong** | Empty list pin |
| 1313 | `test_zero_workers_is_skipped` | **Strong** | Empty list pin |
| 1321 | `test_failed_gpu_is_skipped` | **Strong** | Pins cuda:0 IN, cuda:1 NOT — both directions |
| 1342 | `test_undetected_gpu_gets_default_config` | **Strong** | Pins workers=1, ffmpeg_threads=2 defaults |
| 1357 | `test_empty_cache_returns_empty_list` | **Strong** | Empty list pin |
| 1365 | `test_mixed_enabled_and_disabled` | **Strong** | Set equality on devices |

## Summary

- **49 tests** total
- **45 Strong / 3 Weak / 1 Tautological / 0 Bug-blind / 0 Bug-locking / 0 Needs-human**

**Weak finds (need fixing):**
1. **Line 203 `test_worker_format_gpu_name`** — AMD and Intel branches only assert `len(name)==10` without content checks; only NVIDIA branch pins content. Recommend adding `"RX" in name or "AMD" in name` for AMD; `"INTEL" in name or "Arc" in name or "UHD" in name` for Intel.
2. **Line 836 `test_worker_pool_error_handling`** — `assert total_failed > 0` is loose. With 4 items and every-other failing, the deterministic answer is 2. Recommend `assert total_failed == 2 and total_completed == 2`.
3. **Line 952 `test_mixed_workload_with_gpu_cpu_fallback`** — Only checks `total_completed == 3`. Doesn't pin call_order per item, so a regression that ran a non-codec-error item through CPU fallback would pass. Recommend pinning a per-item gpu/None pattern (key1 GPU, key2 GPU+CPU, key3 GPU).

**Tautological find (marginal):**
- **Line 899 `test_worker_statistics`** — Sets `w.completed = 5` then asserts `sum(...) == 5+3`. Tests Python's `sum()`, not the SUT. Marginal value; can be kept (harmless smoke) or deleted.

**File verdict: MIXED.** 3 Weak tests need strengthening to catch regressions, 1 Tautological test is harmless smoke.
