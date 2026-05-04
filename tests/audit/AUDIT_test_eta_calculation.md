# Audit: tests/test_eta_calculation.py — 8 tests, 3 classes

## TestJobProgressNoEta

| Line | Test | Verdict |
|---|---|---|
| 15 | `test_job_progress_default_has_no_eta` | **Strong** — defensive: asserts the `eta` field was REMOVED from JobProgress. Catches a refactor that re-introduces it (negative-space contract). |
| 21 | `test_job_progress_to_dict_omits_eta` | **Strong** — same defensive contract via the JSON serialization path that the API returns to the UI. |

## TestWorkerStatusEta

| Line | Test | Verdict |
|---|---|---|
| 30 | `test_worker_status_has_eta` | **Strong** — strict equality `== "2m 5s"` on dataclass + dict-roundtrip. |
| 35 | `test_worker_status_eta_default_empty` | **Strong** — pins default value `""` (NOT None — the JSON serialiser would render None vs "" differently in the UI). |

## TestFormatEtaWorkerDisplay

| Line | Test | Verdict |
|---|---|---|
| 43 | `test_seconds_only` | **Strong** — strict equality on output ("45s") |
| 46 | `test_minutes_and_seconds` | **Strong** — boundary case (125s → "2m 5s") |
| 49 | `test_hours_and_minutes` | **Strong** — boundary case (3700s → "1h 1m"); hour-formatting branch |
| 52 | `test_zero` | **Strong** — edge case (0 → "0s", not "" or "—") |

## Summary

- **8 tests** total
- **All Strong** — pure-function tests with strict equality assertions, complete matrix of (seconds-only, minutes+seconds, hours+minutes, zero) cells covered.
- **0 needs_human**

**File verdict: STRONG.** No changes needed.
