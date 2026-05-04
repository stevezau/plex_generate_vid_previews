# Audit: tests/test_output_journal.py — 16 tests, 4 classes

## TestMetaPath

| Line | Test | Verdict |
|---|---|---|
| 17 | `test_meta_path_appends_meta_suffix` | **Strong** — strict equality on full filename (`movie-320-10.bif.meta`). |
| 21 | `test_meta_path_handles_no_extension` | **Strong** — strict equality on the no-extension edge case. |

## TestWriteMeta

| Line | Test | Verdict |
|---|---|---|
| 27 | `test_writes_one_meta_per_output` | **Strong** — reads back JSON, pins all four required fields (`source_size`, `source_path`, `publisher`, `schema`). Catches a serializer regression that drops a field. |
| 45 | `test_silently_skips_when_source_missing` | **Strong** — pins the no-meta-written outcome (instead of crashing). |
| 51 | `test_failure_to_write_one_meta_does_not_block_others` | **Strong** — pins partial-failure tolerance: good output stamped, bad path skipped. |

## TestOutputsFreshForSource

| Line | Test | Verdict |
|---|---|---|
| 67 | `test_fresh_when_meta_matches` | **Strong** — pins `True` when source unchanged. |
| 76 | `test_stale_when_source_replaced` | **Strong** — pins `False` after Sonarr quality upgrade (size + mtime change). Real-world bug class. |
| 89 | `test_stale_when_source_grew` | **Strong** — pins `False` after append-only growth (different signature than full replace). Distinct cell. |
| 101 | `test_legacy_outputs_with_no_meta_treated_as_fresh` | **Strong** — pins migration semantics (`True` for pre-journal outputs). Bug-locking-shaped — but the docstring explicitly justifies this as the *intended* upgrade behaviour. **Bug-locking only if the spec changes**; today this is a contract pin. |
| 115 | `test_not_fresh_when_output_missing` | **Strong** — pins `False` when output file doesn't exist. |
| 122 | `test_not_fresh_when_source_missing` | **Strong** — pins `False` when source file doesn't exist. Distinct cell from row above. |
| 127 | `test_handles_corrupt_meta_as_legacy` | **Strong** — pins corrupt-meta → treated as legacy (`True`). Conservative fallback semantics intentionally pinned. |
| 136 | `test_one_match_is_enough_when_others_have_no_meta` | **Strong** — pins the partial-stamp tolerance contract: `True` if at least one matching meta + others legacy. |
| 147 | `test_mismatch_on_one_meta_invalidates_freshness` | **Strong** — pins `False` if any meta says stale (conservative). Mirror of the row above for the mismatch direction. |
| 165 | `test_not_fresh_when_no_outputs` | **Strong** — pins `False` for empty list. Important: callers check this to skip work. |

## TestClearMeta

| Line | Test | Verdict |
|---|---|---|
| 172 | `test_removes_existing_metas` | **Strong** — pins meta deleted AND output file *not* deleted (critical: avoids data loss). |
| 185 | `test_silent_on_missing_metas` | **Strong** — pins no-exception on missing metas (idempotent cleanup contract). |

## Summary

- **16 tests** — 16 Strong, 0 Weak / Bug-blind / Tautological
- `outputs_fresh_for_source` matrix is complete: source missing/present × output missing/present × meta missing/matching/mismatching/corrupt × all-match vs partial.
- `test_legacy_outputs_with_no_meta_treated_as_fresh` is a *contract pin* not bug-locking — the docstring explicitly justifies migration semantics.

**File verdict: STRONG.** No changes needed.
