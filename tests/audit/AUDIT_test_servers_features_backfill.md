# Audit: tests/test_servers_features_backfill.py — 14 tests, 4 classes

Phase 5 backfill targeting newly-shipped server features (vendor extraction toggle/status, health check, plugin install). Each class wraps a Flask route + Jellyfin server method patch. Boundaries are correct (HTTP layer in, server method out).

## TestVendorExtractionToggle

| Line | Test | Verdict |
|---|---|---|
| 121 | `test_disable_extraction_returns_per_library_results` | **Strong** — `mock_set.assert_called_once_with(scan_extraction=False)` pins the kwarg the SUT controls, plus strict-equality on `ok`, `scan_extraction`, `results`, `ok_count`, `error_count`, `total`. Catches the "silently re-enabled" inversion bug the docstring calls out. |
| 149 | `test_enable_extraction_passes_true_to_backend` | **Strong** — mirror cell with `scan_extraction=True`. The pair pins both branches. |
| 168 | `test_partial_failure_reports_ok_false_with_per_library_breakdown` | **Strong** — pins `ok=False`, `ok_count=1`, `error_count=1`, plus per-library detail. Catches a regression that always returns `ok=True` regardless of error rows. |
| 196 | `test_invalid_body_returns_400` | **Strong** — two distinct bad bodies (missing key, wrong type). Strict 400 (not `>= 400`). |
| 207 | `test_unknown_server_id_returns_404` | **Strong** — strict 404 status pin. |

## TestVendorExtractionStatus

| Line | Test | Verdict |
|---|---|---|
| 227 | `test_status_returns_counts` | **Strong** — strict-equality on the documented count fields AND pins `vendor=="jellyfin"` (UI conditionally renders CTA copy on this). |
| 254 | `test_status_probe_failure_returns_502` | **Strong** — strict 502 + substring on the propagated error message. 502 (not 500) is the right semantic — backend probe failure, not our bug. |

## TestServerHealthCheck

| Line | Test | Verdict |
|---|---|---|
| 283 | `test_returns_issues_in_documented_shape` | **Strong** — strict equality on every documented per-issue field (`library_id`, `flag`, `severity`, `fixable`, `current`, `recommended`). This is exactly the "documented contract shape" pattern. |
| 322 | `test_no_issues_returns_empty_list` | **Strong** — pins `[] not None`, plus 0 counts. UI's "all good" rendering depends on the empty-list shape, not 404/null. |
| 340 | `test_probe_failure_returns_502` | **Strong** — same 502 contract as vendor extraction. |
| 354 | `test_unknown_server_returns_404` | **Strong** — strict 404. |

## TestServerHealthCheckApply

| Line | Test | Verdict |
|---|---|---|
| 368 | `test_apply_no_body_fixes_all_flagged` | **Strong** — `mock_apply.assert_called_once_with(flags=None)` pins the "no flags = fix everything" contract via the kwarg. |
| 390 | `test_apply_with_specific_flags_passes_through` | **Strong** — `mock_apply.assert_called_once_with(flags=["Foo", "Bar"])`. List forwarded verbatim — catches a bug-blind `assert_called_once()` that would miss order or content. |
| 409 | `test_apply_partial_failure_reports_ok_false` | **Strong** — pins `ok=False` for mixed results plus per-flag breakdown. |
| 430 | `test_apply_empty_results_is_ok_true_not_failure` | **Strong** — pins the "{} = nothing to fix = success" contract called out in the docstring. Catches the inversion. |
| 449 | `test_invalid_flags_type_returns_400` | **Strong** — strict 400 on `flags=str` instead of list. |

## Summary

- **17 tests** — 17 Strong, 0 Weak, 0 Bug-blind, 0 Tautological, 0 Needs-human
- Every `assert_called_once_with(...)` pins the kwargs the SUT is responsible for forwarding (no bare `assert_called_once()` D34-paradigm calls)
- Strict status codes (200/400/404/502) with no `>= 400` slop
- Both error and success branches covered for every endpoint
- The `vendor` field assertion in `test_status_returns_counts` is a particularly good catch — UI CTA copy depends on it

**File verdict: STRONG.** No changes needed. Notable model file for "test the route + assert the per-issue shape" pattern.
