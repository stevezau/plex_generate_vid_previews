# Regression-test verification — TEST_AUDIT.md Phase 6

This file is the gold-standard validation step from the test audit plan.
Each row maps a catalogued production-incident commit to the hindsight
test(s) that should fail if the fix is reverted. The audit's verification
contract:

> A future regression in any of the 21 catalogued incident classes
> triggers a test failure (manually verified by reverting one fix per
> class and confirming the new test fails).

**How to run a verification:**

```bash
# Pick a row from the table below.
git revert <fix-commit> --no-commit              # apply the revert
pytest <hindsight-test> --no-cov -v              # MUST FAIL
git reset --hard HEAD                            # restore
# Update "Last verified" + "Result" cells in the table.
```

If the hindsight test does NOT fail when the fix is reverted, the test is
**bug-blind** — write a stronger assertion or rewrite the test entirely.
That's the bar.

## Verification table

Status legend:
- **Verified ✅** — manually run: revert applied, hindsight test FAILED loudly, fix restored, test PASSES again
- **Verify** — hindsight test exists, expected to fail when reverted (manual verification recommended)
- **Pending** — no hindsight test yet
- **Audit** — debatable whether a test is warranted

| # | Incident commit | Hindsight test | Status | Notes |
|---|---|---|---|---|
| 1 | `d404f73` (D31 doubled-prefix) | `tests/test_output_plex_bundle.py` (existing) | Verify | Plex `/tree` URL building |
| 2 | `10be97c` (D32 Jelly not-in-library) | `tests/test_processing_multi_server.py::TestNotInLibraryRoutesToSkip` (Phase 1 P0.3) | Verify | Plex + Jellyfin variants |
| 3 | `d2c166c` (D33 source-missing retry) | `tests/test_processing_multi_server.py::TestSiblingMountProbe` | Verify | Existing |
| 4 | `a64030c` (D34 sub-second worker) | (no specific test — exists at worker UI level) | Audit | Consider adding worker-emit test |
| 5 | `dfc199a` (D34 per-GPU workers) | `tests/test_dispatcher_kwargs_matrix.py::TestGpuKwargsPropagate` (Phase 1 P0.1) | Verify | GPU kwargs forward |
| 6 | `b1022e2` (D35 sibling mount) | `tests/test_processing_multi_server.py::TestSiblingMountProbe` | Verify | rebind contract |
| 7 | `1e7403c → 0faf1cd` (D36 bundle-hash) | `tests/test_output_journal.py::TestOutputsFreshForSource` (existing 8 tests) | Verify | mtime/size invalidation |
| 8 | `af116e8` (D37 progress bounce) | (Phase 4 deferred — needs UI test) | Pending | UI render contract |
| 9 | `8409952` (D38 Jellyfin trickplay layout) | `tests/journeys/test_adapter_path_contract.py::TestJellyfinTrickplayAdapterPathLayout` (Phase 2 P1.6) | **Verified ✅** | Manually verified 2026-05-05: dropping spaces from `f"{w}-{tw}x{th}"` → 3 of 5 layout tests fail; restored → all pass |
| 10 | `4642387` (D40 plugin bridge) | `tests/test_servers_jellyfin.py::TestResolveRemotePathToItemIdViaPlugin` (existing) | Verify | JSON shape |
| 11 | `70275e9` (webhook prefix translation) | `tests/test_webhook_router.py::TestWebhookPrefixTranslationReachesOwnerCheck` (Phase 1 P0.4) | Verify | no silent 202-drop |
| 12 | `87c78b7` (vendor jobs bypass worker pool) | `tests/test_webhooks.py` Phase 0 + `test_dispatcher_kwargs_matrix.py` (Phase 1 P0.1) | Verify | dispatch through pool |
| 13 | `933a26d` (scheduler library scope) | `tests/test_app.py::TestRunScheduledJob` (Phase 1 P0.2) | Verify | inference test |
| 14 | `1873a23` (SocketIO upgrade) | `tests/test_socketio.py::TestSocketIOTransportConfig` (Phase 1 P0.6) | **Verified ✅** | Manually verified 2026-05-05: flipping `allow_upgrades=True` in app.py:456 → P0.6 test fails immediately; restored → passes |
| 15 | `1f09c3a` (90s gaps memoisation) | `tests/test_processing_multi_server.py::TestItemIdResolverMemoisation` (Phase 0 P0.5) | **Verified ✅** | Manually verified 2026-05-05: short-circuiting the cache check at multi_server.py:301 → 2 of 4 tests fail (cache-hit + cache-None); restored → all pass |
| 16 | `0092f8d` (regenerate checkbox) | `tests/test_full_scan_multi_server.py` Phase 0 + `test_dispatcher_kwargs_matrix.py` (Phase 1) | **Verified ✅** | Manually verified 2026-05-05: hard-coding `regenerate=False` at orchestrator.py:723 → both regenerate tests fail; restored → pass |
| 17 | `886a2f4` (pause short-circuit) | `tests/test_full_scan_multi_server.py::TestPauseGate` (existing) | Verify | spin-wait + cancel-precedence |
| 18 | `8c78074` (webhook fan-out) | `tests/journeys/test_journey_multi_server_partial_unreachable.py` (Phase 2 P1.4) | Verify | partial-failure aggregation |
| 19 | `5028fb6` (kill button race) | `tests/e2e/test_ui_hover_defer.py::TestActiveJobsHoverDefer` (Phase 4) | Verify | hover-defer contract |
| 20 | `ac5950b` (Jellyfin path-based refresh) | (no specific test) | Pending | could add to test_servers_jellyfin |
| 21 | `d92d1b8` (credential leak) | `tests/test_routes.py::test_create_job_ignores_credential_overrides` + `::test_get_settings_never_leaks_real_credentials_anywhere_in_response` (batch 1 + batch 5) | Verify | allow-list strips credentials |

## Manual verification log

Each row marked **Verified ✅** above was confirmed by:

1. Editing the production source to simulate the regression (single-line change)
2. Running the hindsight test → confirming it FAILED
3. `git checkout -- <file>` to restore
4. Re-running the test → confirming it PASSES again

**Verified-passing examples from 2026-05-05 manual run:**

- `media_preview_generator/web/app.py:456` `allow_upgrades=False → True`
  → `tests/test_socketio.py::TestSocketIOTransportConfig::test_allow_upgrades_is_false_on_underlying_engineio_server` FAIL
- `media_preview_generator/jobs/orchestrator.py:723` `regenerate=bool(getattr(...)) → regenerate=False`
  → `tests/test_full_scan_multi_server.py::test_regenerate_thumbnails_propagates_to_process_canonical_path` FAIL
  → `tests/test_dispatcher_kwargs_matrix.py::TestPlexNoPinFansOut::test_regenerate_true_propagates_when_config_set` FAIL
- `media_preview_generator/processing/multi_server.py:301` add `False and ` to cache check (force cache miss)
  → `TestItemIdResolverMemoisation::test_same_server_queried_repeatedly_hits_backend_once` FAIL
  → `TestItemIdResolverMemoisation::test_cache_remembers_none_for_not_in_library` FAIL
- `media_preview_generator/output/jellyfin_trickplay.py:147` drop spaces from sheet dir name
  → `TestJellyfinTrickplayAdapterPathLayout::test_sheet_dir_uses_width_space_dash_space_tilesxtiles` FAIL
  → `TestJellyfinTrickplayAdapterPathLayout::test_compute_output_paths_returns_sheet_zero_jpg` FAIL
  → `TestJellyfinTrickplayAdapterPathLayout::test_custom_width_propagates` FAIL

4 of 4 attempted reverts caused at least one hindsight test to fail loudly with a clear diagnostic message. **The tests catch the bugs they claim to.**

## Verified-passing baseline

After Phase 0-5 execution, the test suite stands at **2340 passing**
(baseline 2262 → +78 new tests across 6 batches). All ten of the audit's
P0 items have hindsight tests; 18 of 21 catalogued incidents have at
least one direct hindsight test in the suite.

## Open items (Phase 4 deferred)

Three incident classes still lack hindsight tests because they require
UI-render testing (jsdom or Playwright) and the project doesn't have
jsdom set up:

- D37 progress-bar bounce (`af116e8`)
- Kill-button hover race (`5028fb6`)
- Workers panel jitter (`e46e73c`)

Plan: add Playwright tests for these in a follow-up batch. Pattern is
established by existing `tests/e2e/test_dashboard.py`.

## What "verified" means in this file

- **Verify**: hindsight test exists, expected to fail when the fix is
  reverted. Manual verification recommended on next maintenance pass.
- **Pending**: no hindsight test yet — gap to close.
- **Audit**: the catalogued incident is debatable; verify whether a test
  is warranted (e.g. D34 sub-second worker visibility is hard to test
  without a working UI render layer).

## Audit + plan files

- Original audit: `TEST_AUDIT.md`
- Execution plan: `/home/data/.claude/plans/iridescent-churning-bear.md`
