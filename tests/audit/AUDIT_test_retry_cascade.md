# Audit: tests/test_retry_cascade.py — 11 tests, 3 classes

## TestRetryTierEnum

| Line | Test | Verdict |
|---|---|---|
| 18 | `test_all_known_tiers_present` | **Strong** — exact set equality on all 5 enum values. Catches both removal AND silent rename. |

## TestClassifyDvSafeRetryReason

| Line | Test | Verdict |
|---|---|---|
| 29 | `test_no_stderr_returns_none` | **Strong** — pins `None` (callers branch on this). |
| 32 | `test_dv_rpu_stderr_picks_dv_label` | **Strong** — strict equality on the user-visible label `"Dolby Vision RPU parsing error"` against a real upstream FFmpeg signature. |
| 39 | `test_zscale_stderr_picks_zscale_label` | **Strong** — strict equality on `"zscale colorspace conversion error"` from real FFmpeg shape. |
| 46 | `test_libplacebo_failure_only_fires_when_libplacebo_was_active` | **Strong** — pins the `use_libplacebo=True` branch produces the libplacebo label. |
| 52 | `test_libplacebo_failure_ignored_without_libplacebo` | **Strong** — mirror cell: same stderr + `use_libplacebo=False` → `None`. Together these pin the gating contract. |
| 56 | `test_dv_rpu_wins_over_libplacebo_catchall` | **Strong** — pins precedence: more specific (DV-RPU) beats catch-all (libplacebo) when both fire. Catches branch-order regression. |

## TestClassifyCpuFallbackReason

| Line | Test | Verdict |
|---|---|---|
| 76 | `test_no_signal_no_codec_no_hwaccel_returns_false` | **Strong** — pins both `(False, None)` tuple. |
| 81 | `test_codec_error_wins_first` | **Strong** — all three predicates true, but codec wins. Pins precedence ordering. |
| 89 | `test_hwaccel_runtime_error_wins_over_signal` | **Strong** — pins second-tier precedence (hwaccel beats signal). |
| 96 | `test_signal_kill_reports_signal_number` | **Strong** — pins exact reason string `"signal kill (signal 11)"` (rc 139 → 139-128). |
| 103 | `test_low_signal_number_does_not_subtract_128` | **Strong** — pins rc<=128 path: `signal 9` (no subtraction). Edge case, distinct cell. |

## Summary

- **11 tests** — 11 Strong, 0 Weak / Bug-blind / Tautological
- Full precedence matrix covered for both classifiers (both gate-on cells AND skip cells).
- Reason strings pinned by strict equality — UI/log strings won't drift undetected.

**File verdict: STRONG.** No changes needed.
