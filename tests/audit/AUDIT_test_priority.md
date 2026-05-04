# Audit: tests/test_priority.py — 18 tests, 5 classes

## TestParsePriority

| Line | Test | Verdict |
|---|---|---|
| 56 | `test_int_values` | **Strong** — 3 strict-equality assertions, full int → constant matrix |
| 61 | `test_string_labels` | **Strong** — case-insensitive labels (lowercase, Title, UPPER) — pins case-folding contract |
| 66 | `test_invalid_defaults_to_normal` | **Strong** — covers garbage int (99), unknown string ("bogus"), AND None — 3 cells of the failure matrix |

## TestJobPriority

| Line | Test | Verdict |
|---|---|---|
| 78 | `test_default_priority` | **Strong** — strict equality on default (PRIORITY_NORMAL) |
| 82 | `test_explicit_priority` | **Strong** — strict equality on explicit |
| 86 | `test_priority_in_to_dict` | **Strong** — pins JSON serialization carries the priority (UI depends on this) |
| 91 | `test_backward_compat_missing_priority` | **Strong** — old jobs.json without priority field deserializes to NORMAL. Catches schema-migration regression. |
| 103 | `test_priority_from_string_in_constructor` | **Strong** — string → constant via constructor (legacy JSON load path) |

## TestJobManagerPriority

| Line | Test | Verdict |
|---|---|---|
| 115 | `test_create_job_default_priority` | **Strong** — pins default through the manager API |
| 121 | `test_create_job_with_priority` | **Strong** — explicit priority through manager |
| 127 | `test_update_job_priority` | **Strong** — strict equality on new priority + asserts the returned object is non-None |
| 135 | `test_update_job_priority_not_found` | **Strong** — pins None-return contract for missing job (vs raising) |
| 141 | `test_priority_persists_across_reload` | **Strong** — round-trips through disk persistence (real JobManager re-instantiation). Catches serialization-only-via-mock tautologies. |

## TestJobTrackerPriority

| Line | Test | Verdict |
|---|---|---|
| 159 | `test_default_priority` | **Strong** — JobTracker default priority |
| 168 | `test_explicit_priority` | **Strong** — explicit priority |
| 178 | `test_submission_order_increases` | **Strong** — pins monotonic submission_order counter (FIFO tiebreaker depends on it). `t2 > t1` strict comparison. |

## TestDispatcherPriority

| Line | Test | Verdict |
|---|---|---|
| 207 | `test_high_priority_dispatched_first` | **Strong** — submit LOW first, then HIGH; HIGH dispatched first. Pins priority-aware scheduling contract. |
| 235 | `test_same_priority_fifo` | **Strong** — equal priority → submission order wins. Mirror cell. |
| 259 | `test_update_job_priority_reorders` | **Strong** — runtime priority bump reorders the queue. Real-world: user clicks "make this high" mid-queue. |
| 285 | `test_empty_queue_returns_none` | **Strong** — empty-queue contract (returns None, not raises). |

## Summary

- **18 tests**, all **Strong**
- Complete (priority value × source: literal/string/None) matrix
- Persistence round-trip exercised
- Dispatcher scheduling matrix complete (high-first, same-fifo, runtime-reorder, empty)

**File verdict: STRONG.** No changes needed.
