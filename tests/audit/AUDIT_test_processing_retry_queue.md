# Audit: tests/test_processing_retry_queue.py — 16 tests, 6 classes

## TestBackoffSchedule

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 30 | `test_schedule_grows_geometrically` | **Strong** | Pins monotonic-increase property across all gaps. Catches a re-tuning to a flat schedule. |
| 37 | `test_first_delay_under_a_minute` | **Strong** | Strict `<= 60` upper bound — pins fast first retry. Acceptable as an inequality (the contract IS "≤ 60s"). |
| 41 | `test_public_alias_is_same_object` | **Strong** | `is` identity check + strict tuple equality `== (30, 120, 300, 900, 3600)`. Pins both the public alias AND the exact schedule values (D15 contract). |

## TestRetrySchedulerSchedule

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 54 | `test_schedule_fires_callback_after_delay` | **Strong** | Strict equality `captured == [("/canonical", 1)]` AND `pending_count() == 0` cleanup check AND `schedule(...) is True` return. Triple contract pin. |
| 76 | `test_schedule_replaces_existing_timer_for_same_path` | **Strong** | Pins `first == 0, second == 1` — old timer was cancelled, new fired exactly once. Real time-based test verifying replacement semantics. |
| 100 | `test_schedule_returns_false_after_max_attempts` | **Strong** | Loops every attempt 1..len(_BACKOFF)+1 and pins True/False boundary. Matrix-style coverage. |

## TestRetrySchedulerCancel

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 122 | `test_cancel_pending_retry` | **Strong** | Three-way: `cancel(...) is True`, `cb_calls == 0`, `pending_count() == 0`. Pins all sides. |
| 141 | `test_cancel_returns_false_when_nothing_pending` | **Strong** | Strict `is False` for unknown path. |

## TestSchedulerSingleton

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 148 | `test_get_retry_scheduler_is_singleton` | **Strong** | `is` identity check across calls. |
| 153 | `test_reset_drops_singleton_and_cancels_pending` | **Strong** | Pins `cb_calls == 0` after reset — the cancellation contract is enforced. |

## TestScheduleRetryForUnindexed

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 176 | `test_callback_invokes_process_canonical_path` | **Strong** | 5 strict-equality kwarg pins: canonical_path, item_id_by_server, registry (`is`), AND the critical `schedule_retry_on_not_indexed is False` (anti fork-bomb pin). Plus `len(captured) == 1`. Exemplary contract test. |
| 228 | `test_chained_retry_fires_when_still_unindexed` | **Strong** | Pins `call_count == 2` — chain-fires-once-then-resolves. |
| 301 | `test_chain_terminates_after_max_attempts` | **Strong** | Strict `call_count == len(_BACKOFF)` with explanatory message. Pins termination boundary. |
| 353 | `test_callback_swallows_exceptions` | **Strong** | The contract is "test reaches end without crash" — the `ran.set()` confirms callback executed and the test surviving proves the timer thread didn't die. The comment-style assertion (reaching the end) is documented. Acceptable. |

## TestProcessCanonicalPathIntegration

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 429 | `test_skipped_not_indexed_triggers_retry_schedule` | **Strong** | Pins multi-aspect contract: `result.status is MultiServerStatus.SKIPPED_NOT_INDEXED` (D13 bucket), `len(publishers) == 1`, `adapter_name == "plex_bundle"`, `len(schedule_calls) == 1`, `args[0] == "/x.mkv"` (canonical preserved end-to-end), `attempt == 1`. D31-style "spy at boundary not at SUT" pattern explicitly documented. |
| 514 | `test_skipped_not_indexed_no_retry_when_disabled` | **Strong** | Audit-fixed test: explicitly checks `schedule_calls == []` AND status starts with `SKIPPED` (proves the not-indexed branch DID run, not silently short-circuited). The audit-fix comment documents the bug class this guards against. |

## Summary

- **16 tests** — all **Strong**, 0 weak/bug-blind/tautological/dead/needs-human

**File verdict: STRONG.**

This file is exemplary — every test pins multiple contracts, callback args are inspected (not just call counts), the D13/D15/D31 design contracts are explicitly covered, and integration tests use real `_publish_one` paths via `LibraryNotYetIndexedError` injection rather than stubbing same-module helpers. The audit-fix comment at L514 demonstrates the team's awareness of the "absence-only assertion is bug-blind" pattern.

No changes needed.
