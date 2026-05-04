# Audit: tests/test_recent_added_scanner.py — 13 tests, no classes (module-level)

## Module-level tests (no class grouping)

| Line | Test | Verdict |
|---|---|---|
| 60 | `test_scan_submits_in_window_items` | **Strong** — submitted == 1 + `assert_called_once()` + asserts `args[0] == "recently_added"` AND `args[2] == "/data/movies/New.mkv"` (the path). Three-fold positional-arg check on the dispatch boundary. The args[2] check is the load-bearing one. |
| 79 | `test_scan_handles_episode_titles` | **Strong** — `assert args[1] == "Show S01E01"` — pins the human-readable title format used by webhook UI. Strict equality. |
| 101 | `test_scan_with_explicit_library_ids_scans_only_those_sections` | **Strong** — asserts call_count == 1 (only TV section walked) AND `args[2]` is the TV path (not the movies path). Catches a regression that ignored library_ids and submitted both. |
| 128 | `test_scan_empty_library_ids_falls_back_to_global_selected_libraries` | **Strong-ish** — pins call_count == 1, but does NOT assert WHICH section was matched (could be the wrong one). **Minor weakness** — adding `args[2] == matched-section-path` would harden this. Still catches the "no fallback at all" regression. |
| 148 | `test_scan_fractional_lookback_hours` | **Strong** — submitted == 1 AND `args[2]` == the in-window file path. Pins both the count AND the right item was chosen (vs the 20-min-ago decoy). |
| 166 | `test_scan_handles_unsupported_section_type` | **Strong** — Music section → 0 submitted + `assert_not_called`. Pins the type-filter contract. |
| 176 | `test_scan_handles_search_filter_unsupported` | **Strong (regression for older Plex)** — fakes `search()` raising on the addedAt filter, asserts the client-side fallback path still produces 1 submission. Pins the failover for older Plex servers that don't support addedAt as a filter. |
| 201 | `test_scan_skips_items_with_existing_bifs` | **Strong** — pre-creates a BIF on disk at the bundle path, asserts submitted == 0 AND `assert_not_called()`. Pins the de-dup contract: existing BIFs short-circuit dispatch. |
| 242 | `test_scan_submits_items_missing_bif` | **Strong** — mirror of above: BIF *not* created → submitted == 1 + `assert_called_once()`. Two-cell de-dup matrix is fully covered. |
| 275 | `test_scan_logs_history_when_items_submitted` | **Strong** — asserts `wh._webhook_history` contains an entry with `source == "recently_added"` AND `status == "queued"`. Two-key check on the audit-trail entry. |
| 320 | `test_scan_in_utc_minus_7_does_not_drop_in_window_items` | **Strong (issue #226 regression)** — uses `_force_pdt_timezone` fixture to actually set libc TZ to PDT, exercises the real `datetime.fromtimestamp()` path plexapi uses. Asserts `submitted == 1` with explicit error message naming the bug. The fixture itself does cleanup correctly (env restore + `tzset()` after restore — comment explains why) — that's a delicate detail done right. |
| 349 | `test_to_utc_naive_handles_naive_local_input` | **Strong** — strict equality on the round-trip with a host-tz-independent expected value (uses `datetime.fromtimestamp(unix_ts, tz=utc).replace(tzinfo=None)`). Catches "the helper accidentally returns input unchanged" regressions. |
| 363 | `test_to_utc_naive_passes_through_aware_input` | **Strong** — strict-equality on a hand-computed aware → naive UTC conversion. Catches offset-sign bugs. |

## Summary

- **13 tests** — 12 Strong, 1 minor-weakness (Strong-ish)
- 0 bug-blind, 0 tautological, 0 dead/redundant, 0 bug-locking
- Issue #226 (PDT host) regression is well-pinned (real libc TZ change, careful fixture cleanup)
- BIF-de-dup matrix complete (exists vs missing, both branches asserted)

**File verdict: STRONG.** Single nit: `test_scan_empty_library_ids_falls_back_to_global_selected_libraries` (line 128) could harden by also asserting which section was matched. Not fix-blocking.
