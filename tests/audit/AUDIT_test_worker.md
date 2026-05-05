# Audit: tests/test_worker.py — 51 tests (re-audit, batch 6)

Tests for `Worker` class and `WorkerPool` — initialization, task assignment, GPU→CPU codec fallback, fallback state reset, cancellation, completion bookkeeping, dynamic worker add/remove + reconcile, per-job thread scoping (D5), `_build_selected_gpus`.

## TestWorker

| Line | Test | Verdict | Note |
|---|---|---|---|
| 26 | `test_worker_initialization` | Strong | Pins all 8 init fields. |
| 40 | `test_worker_is_available` | Strong | Pins True→False transition on busy flip. |
| 49 | `test_worker_assign_task` | Strong | Pins `is_busy is True`, `current_task`, `media_title`, `title_kind == "video"` (display heuristic). |
| 74 | `test_worker_assign_task_when_busy` | Strong | Pins `RuntimeError` on mid-task assign. |
| 91 | `test_worker_assign_task_accepts_pre_claimed_state` | Strong | Pins assign-after-pre-claim does not raise (closes atomic-claim race regression). |
| 109 | `test_worker_check_completion` | Strong | Pins `is_busy True → False` after completion. |
| 131 | `test_worker_progress_data` | Strong | Audit-fixed: pins round-trip values + REQUIRED contract keys for fresh worker (avoids tautology). |
| 169 | `test_worker_find_available` | Strong | Multi-state matrix: all-available, two-busy, all-busy. |
| 192 | `test_worker_shutdown_waits_for_longer_timeout` | Strong | Pins `join.assert_called_once_with(timeout=60)`. |
| 203 | `test_worker_format_gpu_name` | Strong | Audit-fixed: pins length AND brand substring per branch (NVIDIA/AMD/Intel). Closes prior weak `len == 10` only. |
| 237 | `test_worker_thread_execution` | Strong | Pins `current_thread.is_alive()` AND `call_count == 1` AND `worker.completed == 1`. |
| 268 | `test_last_task_outcome_delta` | Strong | Pins per-task delta keys + zeros for non-fired keys. |
| 294 | `test_worker_gpu_codec_error_retries_on_cpu_in_place` | Strong | Pins exact 2 calls + first kwargs `gpu='NVIDIA'`, second `gpu=None, gpu_device_path=None` + worker fields (completed, failed, fallback_active, fallback_reason). |
| 335 | `test_worker_gpu_cpu_fallback_records_failure_when_cpu_retry_fails` | Strong | Pins `len == 2`, completed==0, failed==1, fallback_active. |
| 361 | `test_worker_cpu_handles_codec_error_as_failure` | Strong | Pins failed==1, completed==0. |

## TestPerJobThreadScoping

| Line | Test | Verdict | Note |
|---|---|---|---|
| 402 | `test_thread_registered_to_one_job_isnt_thread_for_another` | Strong | Pins `is_job_thread_for(tid, "job-A") is True` AND `is_job_thread_for(tid, "job-B") is False`. D5 contract pin. |
| 417 | `test_unregister_clears_only_calling_thread` | Strong | Pins post-unregister returns False. |
| 433 | `test_concurrent_workers_each_scoped_to_own_job` | Strong | Real-thread test; pins per-thread `_self True / _cross False`. Excellent matrix. |
| 484 | `test_unowned_registration_doesnt_match_any_real_job` | Strong | Pins False for any job_id including empty. |

## TestWorkerPool

| Line | Test | Verdict | Note |
|---|---|---|---|
| 508 | `test_worker_pool_initialization` | Strong | Pins `len == 6` AND per-index worker_type. |
| 527 | `test_worker_pool_gpu_assignment` | Strong | Pins per-worker `gpu_index` (round-robin). |
| 544 | `test_worker_pool_process_items` | Strong | Pins `total_completed == 2`. |
| 570 | `test_worker_pool_has_busy_workers` | Strong | Pins False→True transition. |
| 579 | `test_worker_pool_has_available_workers` | Strong | Pins True→False transition (all busy). |
| 589 | `test_worker_pool_shutdown` | Strong | Audit-fixed: pins per-worker `is_running is False or _stopped is True`. Closes prior `assert True` floor. |
| 606 | `test_worker_pool_add_and_remove_workers` | Strong | Pins added==2 + len==4 + result dict shape (removed/scheduled/unavailable). |
| 622 | `test_remove_workers_schedules_busy_and_retires_when_idle` | Strong | Pins exact result dict + post-retirement count. |
| 637 | `test_dynamic_remove_does_not_stall_completion` | Strong | Pins `elapsed < 2.0` AND `completed + failed == len(items)`. |
| 669 | `test_dynamic_gpu_removal_does_not_stall_completion` | Strong | Same pattern for GPU removal. |
| 708 | `test_worker_pool_pause_check_blocks_dispatch` | Strong | Pins `completed == 2` AND `elapsed >= 0.2`. |
| 738 | `test_no_dispatch_while_paused` | Strong | Pins `first_assign_time >= 0.9 * pause_duration` AND `completed == 2`. |
| 782 | `test_worker_pool_stats_are_per_library` | Strong | Pins per-library completed/failed counts independently. |
| 820 | `test_worker_pool_task_completion` | Strong | Pins all 4 items completed. |
| 852 | `test_worker_pool_error_handling` | Strong | Audit-fixed: pins exactly `failed == 2` AND `completed == 2` for deterministic mock (was loose `> 0`). |
| 900 | `test_worker_pool_progress_updates` | Strong | Audit-fixed: pins `add_task.call_count == 1` AND `remove_task.call_count == 1` (1:1 pair, no leak). |
| 936 | `test_worker_pool_cpu_fallback_on_codec_error` | Strong | Pins exact `call_order == [(key1, NVIDIA), (key1, None)]` AND worker stats. |
| 974 | `test_mixed_workload_with_gpu_cpu_fallback` | Strong | Pins all 3 keys attempted + conditional invariant for key2 (GPU-then-CPU OR CPU-only) + key1/key3 single calls. Sophisticated nondeterministic-scheduling handling. |
| 1050 | `test_codec_error_fails_when_cpu_retry_also_fails` | Strong | Pins failed==1, completed==0, fallback_active. |

## TestReconcileGpuWorkers

| Line | Test | Verdict | Note |
|---|---|---|---|
| 1086 | `test_reconcile_removes_idle_workers_immediately` | Strong | Pins removed==2 + deferred==0 + `len == 1`. |
| 1099 | `test_reconcile_defers_busy_workers` | Strong | Pins removed==0 + deferred==2 + `len == 3` + `len(deferred) == 2`. |
| 1116 | `test_reconcile_mixed_idle_and_busy` | Strong | Pins removed==2 + deferred==1 + post-len==2 + kept==1. |
| 1132 | `test_pending_removal_prevents_task_assignment` | Strong | Pins `not is_available()` even after busy→idle transition. |
| 1152 | `test_deferred_worker_retired_after_completion` | Strong | Pins retired is True + `len == 1` + worker no longer in pool. |
| 1171 | `test_deferred_workers_cleaned_by_apply_deferred_removals` | Strong | Pins retired==2 + final len==1. |
| 1189 | `test_reconcile_disabled_device_defers_busy` | Strong | Pins removed==1 + deferred==1 + `_pending_removal is True`. |

## TestWorkerProgressCount

| Line | Test | Verdict | Note |
|---|---|---|---|
| 1207 | `test_progress_not_double_counted_on_gpu_cpu_fallback` | Strong | Pins `completed_counts[-1] == 1` (NOT 2) on fallback. H2 boundary contract. |
| 1247 | `test_fallback_state_resets_on_new_task` | Strong | Pins fallback_active/reason values across two tasks. |

## TestWorkerCancellation

| Line | Test | Verdict | Note |
|---|---|---|---|
| 1285 | `test_worker_cancellation_does_not_fallback_to_cpu` | Strong | Pins failed==1, completed==0, fallback_active==False, `mock_process.call_count == 1`. |
| 1307 | `test_worker_passes_cancel_check_to_process_item` | Strong | Pins `cancel_check is cancel_fn` (identity, not equality). |

## TestBuildSelectedGpus

| Line | Test | Verdict | Note |
|---|---|---|---|
| 1351 | `test_enabled_gpu_returned_with_config_values` | Strong | Pins `len == 1` + tuple unpack + workers + ffmpeg_threads. |
| 1366 | `test_disabled_gpu_is_skipped` | Strong | Pins `== []`. |
| 1374 | `test_zero_workers_is_skipped` | Strong | Pins `== []`. |
| 1382 | `test_failed_gpu_is_skipped` | Strong | Pins device list contains "cuda:0" + NOT contains "cuda:1". |
| 1403 | `test_undetected_gpu_gets_default_config` | Strong | Pins workers==1 + ffmpeg_threads==2 defaults. |
| 1418 | `test_empty_cache_returns_empty_list` | Strong | Pins `== []`. |
| 1426 | `test_mixed_enabled_and_disabled` | Strong | Pins exact device set. |

**File verdict: STRONG.** Significant improvement from prior "MIXED (3 weak, 1 tautological)" — the 4 previously flagged tests have all been audit-fixed: tautological `test_worker_statistics` was deleted (now an explanatory comment at line 927), `test_worker_progress_data` strengthened to pin contract shape, `test_worker_format_gpu_name` strengthened to pin brand substrings, `test_worker_pool_shutdown` strengthened to pin `is_running is False`, `test_worker_pool_error_handling` strengthened to pin exact failed/completed counts, `test_worker_pool_progress_updates` strengthened to pin 1:1 add_task/remove_task pairing.

Re-audit found NO new weak/bug-blind/tautological tests. The fallback / cancellation / reconcile / dynamic-remove threading tests are all strongly anchored with multi-invariant assertions.

## Fix queue

(empty — no remaining issues)
