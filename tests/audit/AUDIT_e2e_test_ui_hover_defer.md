# Audit: tests/e2e/test_ui_hover_defer.py — 2 tests, 1 class

## TestActiveJobsHoverDefer

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 42 | `test_active_jobs_render_defers_when_container_is_hovered` | **Strong** — sentinel-attribute approach: tags DOM, monkeypatches `matches(':hover')`, re-renders, then asserts sentinel survived. Real production guard contract pinned (commits 5028fb6, 0df1cc3). Diagnostic message is explicit about which file:line should change if it fails. |
| 114 | `test_active_jobs_render_DOES_rebuild_when_NOT_hovered` | **Strong** — mirror test for the contract floor. Asserts `firstContainsLibraryA`, `secondContainsLibraryB`, AND `secondDoesNotContainLibraryA` (3 cells). Catches "always-defer" regression which would mask the first test. |

## Summary

- **2 tests** all **Strong**
- Pins both edges: defer-when-hovered AND rebuild-when-not-hovered
- Uses real Playwright + JS evaluate to drive the production `updateActiveJobs(...)` function
- Sentinel-survival pattern is clever — robust against any innerHTML-replacement

**File verdict: STRONG.** No changes needed.
