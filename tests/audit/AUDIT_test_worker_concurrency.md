# Audit: tests/test_worker_concurrency.py — 21 tests, 7 classes

Real-thread concurrency tests for `Worker` and `WorkerPool` with `process_canonical_path` patched at the boundary. The SUT (worker scheduling, in-place CPU fallback, progress thread-safety) is exercised, not mocked.

## TestWorker

| Line | Test | Verdict |
|---|---|---|
| 104 | `test_worker_starts_available` | **Strong** — pins both `is_available()` and `not is_busy` initial state. Catches a refactor that flips the default. |
| 109 | `test_assign_task_marks_busy` | **Strong** — minimal but specific (`is_busy` after assign). Real subprocess boundary patched. |
| 115 | `test_assign_task_raises_if_busy` | **Strong** — `pytest.raises(RuntimeError, match="already busy")` pins the exception message text. Strong contract pin. |
| 123 | `test_check_completion_after_task_done` | **Strong** — three independent assertions: `check_completion() is True`, `is_available()`, `completed == 1`. Catches partial-state-update bugs. |
| 133 | `test_failed_task_increments_failed` | **Strong** — pins `failed==1 AND completed==0`. Catches a bug that increments both. |
| 142 | `test_find_available_prioritises_gpu` | **Strong** — `is gpu_w` (identity), not equality. Pins GPU-first scheduling contract. |
| 147 | `test_find_available_none_when_all_busy` | **Strong** — `is None` strict. |
| 154 | `test_shutdown_waits_for_thread` | **Strong** — pins `not thread.is_alive()` after shutdown. Catches a regression that returns from shutdown before the thread joins. |
| 162 | `test_get_progress_data_returns_dict` | **Weak** — `isinstance(data, dict)` + 2 key-presence checks. No value assertions. **Marginal value** — catches deletion of the method but not value corruption. Keep as smoke. |

## TestWorkerPoolInit

| Line | Test | Verdict |
|---|---|---|
| 178 | `test_creates_correct_worker_count` | **Strong** — strict equality on total + GPU + CPU counts. |
| 186 | `test_gpu_round_robin_assignment` | **Strong** — `indices == [0, 1, 0, 1]` strict-equality on the round-robin pattern. Catches a refactor that makes assignment sticky or random. |
| 193 | `test_cpu_only_pool` | **Strong** — pins worker_type for every entry. |
| 198 | `test_has_busy_workers_initially_false` | **Strong** — strict `not has_busy_workers()`. |
| 202 | `test_has_available_workers_initially_true` | **Strong** — mirror. |

## TestWorkerPoolProcessing

| Line | Test | Verdict |
|---|---|---|
| 215 | `test_process_all_items_headless` | **Strong** — `total_completed == 6` AND `progress_calls[-1] == (6, 6)`. The final-progress assertion catches the "drops final update" bug class. |
| 239 | `test_failed_items_tracked` | **Strong** — pins `failed==1 AND completed==0`. |
| 250 | `test_mixed_success_and_failure` | **Strong** — pins `completed + failed == 4` (not just total processed). Catches a bug that double-counts or drops items. |

## TestInPlaceCpuFallback

| Line | Test | Verdict |
|---|---|---|
| 283 | `test_codec_error_retries_on_cpu_in_place` | **Strong** — exemplary. `call_log == ["nvidia", None]` pins both the call ORDER and that the second call passes `gpu=None` (CPU). Plus `fallback_active is True` and substring on `fallback_reason`. Would catch a regression that retries on a different GPU instead of CPU. |

## TestWorkerPoolShutdown

| Line | Test | Verdict |
|---|---|---|
| 324 | `test_shutdown_completes_without_error` | **Strong** — pins `not thread.is_alive()` for every worker after shutdown. Catches the "shutdown returns early" bug. |

## TestProgressThreadSafety

| Line | Test | Verdict |
|---|---|---|
| 345 | `test_concurrent_progress_updates` | **Strong** — 4 threads × 100 updates with `errors == []` strict + range check on final state. Real race-condition smoke. |
| 375 | `test_get_progress_data_under_contention` | **Strong** — concurrent reader/writer; asserts no exceptions and all reads returned valid dicts. Pins the lock contract. |

## TestWorkerCallback

| Line | Test | Verdict |
|---|---|---|
| 415 | `test_worker_callback_called` | **Strong** — explicitly fixes the prior bug-blind comment ("may or may not fire"). Now asserts `worker_updates` non-empty AND `saw_busy` (an update with `status in ("processing","busy") AND current_title=="CB Test"`). The dual-pin closes the "callback fires but with empty list" failure mode. |
| 458 | `test_worker_callback_includes_remaining_time` | **Strong** — pins `remaining_time` field present AND of numeric type AND > 0. Catches a serializer that strips the field or returns "N/A". |

## Summary

- **22 tests** — 21 Strong, 1 Weak (smoke), 0 Bug-blind, 0 Tautological
- The CPU fallback test (line 283) is a model for boundary-call ordering pins
- The two TestWorkerCallback tests close a previously-acknowledged bug-blind pattern (the docstring at line 415 explicitly cites the original failure mode)
- Real threads + real `WorkerPool` + boundary-only mocking — high confidence

**File verdict: STRONG.** Only `test_get_progress_data_returns_dict` is loose (smoke-level), and it's cheap to keep. No fixes needed.
