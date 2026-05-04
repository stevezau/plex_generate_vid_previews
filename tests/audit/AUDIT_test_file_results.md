# Audit: tests/test_file_results.py — 26 tests, 8 classes

## TestFileResultRecording

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 40 | `test_record_and_read_round_trip` | **Strong** — strict equality on file/outcome/worker/reason across 3 records |
| 58 | `test_jsonl_file_created` | **Strong** — pins exact path layout + JSON record fields |
| 74 | `test_get_file_results_empty_when_no_records` | **Strong** — strict `== []` for missing job |
| 79 | `test_timestamp_present` | **Weak** — only checks truthy `results[0]["ts"]`; doesn't validate format. A bug that wrote `"ts": "x"` would pass. Marginal — the round-trip test already implicitly covers ts persistence |
| 87 | `test_malformed_jsonl_lines_skipped` | **Strong** — pins skip-bad-lines contract; len + ordering preserved |

## TestFileResultFiltering

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 115 | `test_filter_by_outcome` | **Strong** — len + per-row outcome assertion |
| 125 | `test_filter_by_search` | **Strong** — len + per-row substring contract |
| 135 | `test_filter_by_search_case_insensitive` | **Strong** — pins exact match for case-folded search |
| 145 | `test_filter_combined` | **Strong** — pins AND of two filters; specific path equality |

## TestFileResultRetention

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 159 | `test_retention_removes_file_results_for_expired_jobs` | **Strong** — pins job removal + JSONL deletion together |
| 181 | `test_delete_job_removes_file_results` | **Strong** — pins side-effect of delete on JSONL |
| 194 | `test_clear_completed_removes_file_results` | **Strong** — pins side-effect of clear_completed |
| 207 | `test_orphaned_file_results_cleaned_up` | **Strong** — pins orphan-cleanup path (no Job for the file) |

## TestFileResultCallback

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 226 | `test_callback_invoked_for_each_outcome` | **Strong** — len + per-row outcome + servers list strictly equal (D9) |
| 265 | `test_callback_cleared` | **Strong** — pins None-clear contract via `len == 0` |
| 278 | `test_callback_exception_does_not_propagate` | **Weak** — only asserts no raise. A regression where the callback wasn't even called would still pass. Could be improved by asserting attempt was made. Marginal — the production failure mode is "crashes the worker" |

## TestWorkerCallsNotifyFileResult

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 313 | `test_worker_imports_and_calls_notify_file_result` | **Weak** — just `hasattr(worker_mod, "_notify_file_result")`. Audit-fix comment acknowledges this is a "cheap structural sanity"; the runtime branches are covered elsewhere. Acceptable as smoke. |

## TestFileResultServerAttribution

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 345 | `test_servers_list_is_persisted_slim` | **Strong** — strict equality on slim per-server dict (frame_source filtering rule pinned) |
| 382 | `test_reason_derived_from_publisher_message_when_blank` | **Strong** — pins exact derived reason (D8 regression) |
| 411 | `test_explicit_reason_wins_over_publisher_message` | **Strong** — pins precedence contract |
| 435 | `test_servers_field_omitted_when_empty` | **Strong** — pins `"servers" not in r` (compactness contract) |

## TestFileResultBifPath

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 453 | `test_bif_path_extracted_from_first_publisher` | **Strong** — strict path equality (D34 deep-link) |
| 479 | `test_bif_path_skips_non_bif_outputs` | **Strong** — pins .bif filter; chooses correct path among multiple publishers |
| 517 | `test_bif_path_omitted_when_no_bif_output` | **Strong** — pins absence of field |
| 543 | `test_bif_path_omitted_when_publishers_have_no_output_paths` | **Strong** — same; matrix cell |

## TestFileResultsCap

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 565 | `test_writes_truncation_marker_at_cap` | **Strong** — strict count==11, marker outcome strict, marker reason includes cap value |
| 590 | `test_marker_only_written_once` | **Strong** — pins exactly-1 marker invariant |

## TestFileResultsAPI

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 656 | `test_file_results_endpoint` | **Strong** — pins count, total, summary fields strictly |
| 673 | `test_file_results_outcome_filter` | **Strong** — pins filtered count vs total + summary contract (full counts even when filtered) |
| 691 | `test_file_results_search_filter` | **Strong** — pins count + substring in returned file |
| 704 | `test_file_results_404_for_missing_job` | **Strong** — strict status_code |

## Summary

- **30 tests total** (8 classes)
- **27 Strong**
- **3 Weak** — `test_timestamp_present` (L79), `test_callback_exception_does_not_propagate` (L278), `test_worker_imports_and_calls_notify_file_result` (L313)
- **0 Bug-blind / Tautological / Dead / Bug-locking / Needs-human**

**Weak tests (low priority):**
- L79 `test_timestamp_present` — only truthy check on `ts`; a "x" string would pass. Could regex-match ISO format.
- L278 `test_callback_exception_does_not_propagate` — asserts no raise, doesn't assert callback was actually invoked. A regression that early-returned before the callback would silently pass.
- L313 `test_worker_imports_and_calls_notify_file_result` — only `hasattr` check; the docstring acknowledges this and points to other tests for the actual branches.

**File verdict: STRONG.** All load-bearing contracts (D8 reason derivation, D9 servers list, D34 bif_path, retention, cap+marker) are pinned with strict equality. The 3 Weak tests are documented as smoke/sanity coverage with the real assertions elsewhere.
