# Audit: tests/journeys/test_journey_multi_server_partial_unreachable.py — 2 tests

## Module-level

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 84 | `test_one_publisher_unreachable_others_succeed_aggregate_is_published` | **Strong** — aggregate `is PUBLISHED` (identity); per-publisher status dict checked for exactly 3 entries; both healthy publishers asserted PUBLISHED, Emby asserted FAILED; message content asserted to contain "could not write" or "preview file" so user gets diagnostic context |
| 199 | `test_all_publishers_fail_aggregate_is_failed` | **Strong** — bottom-edge mirror; aggregate FAILED + every publisher row FAILED via `all(...)` |

## Summary

- **2 tests** all **Strong**
- Real `process_canonical_path` invocation (not mocked) — only mocks at the `generate_images` + adapter `publish` boundaries (the right seam)
- Per-publisher status checked, not just count
- Message diagnostic content verified

**File verdict: STRONG.** No changes needed.
