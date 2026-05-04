# Audit: tests/test_processing_frame_cache.py — 32 tests, 6 classes

## TestFrameCacheBasics

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 48 | `test_get_returns_none_when_empty` | **Strong** — strict `is None` + `len == 0` |
| 53 | `test_put_and_get_roundtrip` | **Strong** — pins frame_count + frame_dir equality |
| 69 | `test_frame_dir_for_is_deterministic` | **Strong** — strict equality (pins hash-determinism) |
| 75 | `test_invalidate_removes_entry` | **Strong** — pins both in-memory removal AND on-disk dir gone |
| 89 | `test_clear_drops_everything` | **Strong** — pins `len == 0` after clear |

## TestCacheValidity

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 104 | `test_mtime_change_invalidates` | **Strong** — pins real mtime-bump invalidation (slow-marked) |
| 119 | `test_ttl_expiry` | **Strong** — pins ttl=0 returns None on subsequent get |
| 131 | `test_missing_frame_dir_invalidates` | **Strong** — pins None + `len == 0` (eviction on lookup) |
| 147 | `test_missing_source_file_invalidates` | **Strong** — strict `is None` after unlink |
| 158 | `test_sub_second_mtime_drift_still_hits` | **Strong** — pins NFS-tolerance window contract (0.5s hit, 1.5s miss — both boundary cells) |

## TestLruEviction

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 208 | `test_oldest_entry_evicted_when_full` | **Strong** — pins LRU eviction order |
| 221 | `test_get_promotes_entry_to_most_recently_used` | **Strong** — pins MRU promotion contract (a survives; b evicted) |
| 243 | `test_eviction_removes_on_disk_frames` | **Strong** — pins disk-cleanup on eviction (catches the leak regression) |
| 266 | `test_max_entries_default_is_generous` | **Strong** — pins default cap >= 50 (catches regression to legacy 32 cap) |
| 282 | `test_eviction_at_size_cap_does_not_strand_generation_locks` | **Strong** — pins lock-identity preserved across entry eviction |
| 302 | `test_generation_lock_actually_serializes_same_path` | **Strong** — real threads + Event-based mutual-exclusion test (catches race-condition regressions) |
| 354 | `test_generation_lock_distinct_paths_do_not_serialize` | **Strong** — pins per-path lock granularity (catches global-lock regression) |

## TestSingletonAccessor

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 383 | `test_returns_same_instance_with_matching_args` | **Strong** — `is` identity check |
| 388 | `test_returns_same_instance_when_second_call_omits_args` | **Strong** — `is` identity check (pins None=use existing) |
| 393 | `test_reconfigure_with_different_base_dir_raises` | **Strong** — strict raise + match string |
| 398 | `test_reset_clears_singleton` | **Strong** — `is not` identity check |

## TestDispatcherIntegration

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 411 | `test_second_call_hits_cache_and_skips_ffmpeg` | **Strong** — pins gen.call_count==1 across two dispatches + status checks + BIF exists (cornerstone assertion called out in comment) |
| 472 | `test_regenerate_bypasses_cache` | **Strong** — pins gen.call_count==2 with regenerate=True |
| 522 | `test_use_frame_cache_false_uses_adhoc_tmp` | **Strong** — pins `len(cache) == 0` + bif file exists |

## TestConfigurableFrameReuse

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 576 | `test_default_ttl_covers_one_hour` | **Strong** — pins 45-min cache hit (default 60-min TTL contract) |
| 604 | `test_legacy_ten_minute_ttl_when_disabled` | **Strong** — strict `ttl == 600` |
| 615 | `test_settings_block_drives_ttl` | **Strong** — strict equality on TTL and disk |
| 631 | `test_settings_treats_zero_ttl_as_missing_and_uses_default` | **Strong** — pins 0/None/"" → default (3 cells of falsy matrix) |
| 671 | `test_settings_clamps_pathological_small_disk_cap` | **Strong** — strict `disk == 64` (clamp floor) |
| 690 | `test_settings_returns_defaults_when_block_not_a_dict` | **Strong** — strict equality on both defaults |
| 709 | `test_settings_returns_defaults_when_manager_raises` | **Strong** — strict equality on defaults under exception |
| 729 | `test_disk_cap_evicts_when_over` | **Strong** — pins exactly-1 entry remaining + per-old-entry None check (loops through 5) |
| 778 | `test_get_frame_cache_reads_settings_on_first_construction` | **Strong** — pins `_ttl_seconds` + `_max_disk_bytes` from settings |
| 792 | `test_settings_changes_apply_without_restart` | **Strong** — pins same singleton + live TTL+cap update (regression catcher for the "singleton cached forever" bug) |

## Summary

- **32 tests total** (6 classes)
- **32 Strong**
- **0 Weak / Bug-blind / Tautological / Dead / Bug-locking / Needs-human**

**File verdict: STRONG.** Exemplary file. Notable coverage:
- Full validity matrix: ttl, mtime change, sub-second drift (NFS rounding tolerance with both boundary cells), missing source, missing frame dir.
- Real concurrency tests for `generation_lock` — actual threads, Event-based mutual-exclusion (covers the race the lock exists to prevent, not just lock identity).
- Dispatcher integration uses real `process_canonical_path` against a real registry; cache-hit assertion is `gen.call_count == 1` (cornerstone).
- Settings matrix covers enabled/disabled, ttl=0/None/""/120, malformed-block, settings-manager-raises — every cell that produces different downstream behaviour is explicitly tested.
- Live-update test (`test_settings_changes_apply_without_restart`) pins the regression where settings used to require restart.
