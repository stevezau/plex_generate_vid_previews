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
| 1 | `d404f73` (D31 doubled-prefix) | `tests/test_output_plex_bundle.py::TestComputeOutputPaths::test_does_not_double_prefix_url_when_item_id_is_full_path` | **Verified ✅** | 2026-05-05 (batch 19): replaced `bare_id = item_id_str.rsplit("/", 1)[-1]` with `bare_id = item_id_str` at servers/plex.py:880 → URL doubled to `/library/metadata//library/metadata/557676/tree`, test failed loudly with the doubled-prefix diagnostic; restored → passes |
| 2 | `10be97c` (D32 Jelly not-in-library) | `tests/test_processing_multi_server.py::TestNotInLibraryRoutesToSkip` (Phase 1 P0.3) | **Verified ✅** | Manually verified 2026-05-05: short-circuiting the `needs_server_metadata + item_id is None` branch at multi_server.py:536 → both Plex AND Jellyfin variants fail |
| 3 | `d2c166c` (D33 source-missing retry) | `tests/test_processing_multi_server.py::TestSourceMissing` (+ TestSiblingMountProbe::test_single_mount_falls_through_to_skipped) | **Verified ✅** | 2026-05-05 (batch 19): swapping `MultiServerStatus.SKIPPED_FILE_NOT_FOUND` → `MultiServerStatus.FAILED` at multi_server.py:774 → 2 tests fail (`TestSourceMissing` + `TestSiblingMountProbe::test_single_mount_falls_through_to_skipped`); restored → all pass |
| 4 | `a64030c` (D34 sub-second worker) | (NONE — confirmed missing) | Pending | Still no direct hindsight test for `_emit_worker_updates` state-change throttle bypass. Needed: a test that drives `JobDispatcher._emit_worker_updates()` twice with `worker.is_busy` flipped between calls, asserts the worker_callback fires both times even when `now - tracker._last_worker_update < 1.0` (i.e. throttle is bypassed when state changed). The dispatcher.py:574 `state_changed = current_busy != self._last_worker_busy_snapshot` line is the exact pin point. |
| 5 | `dfc199a` (D34 per-GPU workers) | `tests/test_dispatcher_kwargs_matrix.py::TestGpuKwargsPropagate` (Phase 1 P0.1) | **Verified ✅** | Manually verified 2026-05-05: dropping `server_id_filter=per_item_pin` at orchestrator.py:722 → 15 dispatcher kwargs tests fail across 2 files (full matrix) |
| 6 | `b1022e2` (D35 sibling mount) | `tests/test_processing_multi_server.py::TestSiblingMountProbe::test_finds_file_at_sibling_mount_when_canonical_stale` | **Verified ✅** | 2026-05-05 (batch 19): replacing `rebound_path = _probe_sibling_mounts(canonical_path, registry)` with `rebound_path = None` at multi_server.py:748 → rebind test fails (`status=SKIPPED_FILE_NOT_FOUND`, expected rebind to live path); restored → passes |
| 7 | `1e7403c → 0faf1cd` (D36 bundle-hash) | `tests/test_output_journal.py::TestOutputsFreshForSource` (8 tests) | **Verified ✅** | 2026-05-05 (batch 19): replacing the mtime+size equality check with `if True:` at journal.py:141 → 3 tests fail (`test_stale_when_source_replaced`, `test_stale_when_source_grew`, `test_mismatch_on_one_meta_invalidates_freshness`); restored → all 10 pass |
| 8 | `af116e8` (D37 progress bounce) | `tests/test_job_dispatcher.py::TestProgressBarMonotonicity::test_record_completion_includes_in_flight_fraction` (existing) | **Verified ✅** | 2026-05-05 (batch 19): hardcoding `fraction = 0.0` (skipping `in_progress_fraction_getter`) at dispatcher.py:151-156 → test fails with `record_completion=13.0, periodic=13.8` divergence (the exact bar-bounce); restored → passes. Note: previous Pending status was incorrect — there IS a hindsight test, not a UI one. |
| 9 | `8409952` (D38 Jellyfin trickplay layout) | `tests/journeys/test_adapter_path_contract.py::TestJellyfinTrickplayAdapterPathLayout` (Phase 2 P1.6) | **Verified ✅** | Manually verified 2026-05-05: dropping spaces from `f"{w}-{tw}x{th}"` → 3 of 5 layout tests fail; restored → all pass |
| 10 | `4642387` (D40 plugin bridge) | `tests/test_servers_jellyfin.py::TestResolveRemotePathToItemIdViaPlugin::test_uses_plugin_resolve_path_when_installed` | **Verified ✅** | 2026-05-05 (batch 19): changing `payload.get("itemId")` → `payload.get("id")` at jellyfin.py:222 → plugin-installed test fails (`got=None, expected="abc-123"`); restored → all 3 pass |
| 11 | `70275e9` (webhook prefix translation) | `tests/test_webhook_router.py::TestWebhookPrefixTranslationReachesOwnerCheck` | **Verified ✅** | 2026-05-05 (batch 19): re-introducing the buggy pre-flight (drop with `ignored_no_owners` when raw `canonical_path` doesn't match any local_prefix) at webhook_router.py:508 → both prefix-translation tests fail; restored → both pass |
| 12 | `87c78b7` (vendor jobs bypass worker pool) | `tests/test_dispatcher_kwargs_matrix.py::TestItemFieldsPropagate::test_item_id_by_server_hint_propagates` (+ test_webhooks.py vendor coverage) | **Verified ✅** | 2026-05-05 (batch 19): replacing `item_id_by_server=item.item_id_by_server or None` with `item_id_by_server=None` at orchestrator.py:716 → `test_item_id_by_server_hint_propagates` fails (`got=None, expected={'plex-only': 'rk-12345'}`); restored → all 18 pass |
| 13 | `933a26d` (scheduler library scope) | `tests/test_app.py::TestRunScheduledJob` (Phase 1 P0.2) | **Verified ✅** | Manually verified 2026-05-05: dropping the `_infer_server_from_library_id` call at app.py:72-78 → P0.2 test fails (job.server_id stays empty instead of "plex-tv") |
| 14 | `1873a23` (SocketIO upgrade) | `tests/test_socketio.py::TestSocketIOTransportConfig` (Phase 1 P0.6) | **Verified ✅** | Manually verified 2026-05-05: flipping `allow_upgrades=True` in app.py:456 → P0.6 test fails immediately; restored → passes |
| 15 | `1f09c3a` (90s gaps memoisation) | `tests/test_processing_multi_server.py::TestItemIdResolverMemoisation` (Phase 0 P0.5) | **Verified ✅** | Manually verified 2026-05-05: short-circuiting the cache check at multi_server.py:301 → 2 of 4 tests fail (cache-hit + cache-None); restored → all pass |
| 16 | `0092f8d` (regenerate checkbox) | `tests/test_full_scan_multi_server.py` Phase 0 + `test_dispatcher_kwargs_matrix.py` (Phase 1) | **Verified ✅** | Manually verified 2026-05-05: hard-coding `regenerate=False` at orchestrator.py:723 → both regenerate tests fail; restored → pass |
| 17 | `886a2f4` (pause short-circuit) | `tests/test_full_scan_multi_server.py::TestPauseGate` | **Verified ✅** | 2026-05-05 (batch 19): replacing the `while pause_check and pause_check():` spin gate with a single-shot `if … pass` no-op at orchestrator.py:620-623 → both pause tests fail (`process_canonical_path` invoked despite pause; `pause_then_cancel` dispatches mid-pause); restored → both pass |
| 18 | `8c78074` (webhook fan-out) | `tests/journeys/test_journey_multi_server_partial_unreachable.py` | **Verified ✅** | 2026-05-05 (batch 19): narrowing the per-publisher try/except at multi_server.py:601 to `except (NotImplementedError,)` (so `requests.ConnectionError` bubbles) → both partial-failure tests fail because the dispatcher aborts on Emby's exception instead of isolating per-publisher; restored → both pass |
| 19 | `5028fb6` (kill button race) | `tests/e2e/test_ui_hover_defer.py::TestActiveJobsHoverDefer::test_active_jobs_render_defers_when_container_is_hovered` | **Verified ✅** | 2026-05-05 (batch 19): prefixing the hover-guard with `if (false && …)` at app.js:1786 → Playwright test fails (sentinel wiped because container rebuilt mid-hover); restored → both hover-defer tests pass |
| 20 | `ac5950b` (Jellyfin path-based refresh) | `tests/test_servers_jellyfin.py::TestTriggerRefresh::test_path_based_nudge_when_no_item_id` (+ test_path_nudge_failure_falls_back_to_full_refresh) | **Verified ✅** | 2026-05-05 (batch 19): there ARE existing path-based-refresh tests in TestTriggerRefresh. Disabling the `/Library/Media/Updated` branch with `if False and remote_path:` at jellyfin.py:147 → 2 tests fail (`test_path_based_nudge_when_no_item_id`, `test_path_nudge_failure_falls_back_to_full_refresh`); restored → all 7 pass. Previous Pending status was incorrect. |
| 21 | `d92d1b8` (credential leak) | `tests/test_routes.py::TestJobsAPI::test_create_job_ignores_credential_overrides` + `TestSettingsAPI::test_get_settings_never_leaks_real_credentials_anywhere_in_response` | **Verified ✅** | 2026-05-05 (batch 19): bypassing the allow-list at api_jobs.py:355 (replaced filter with `dict(raw_config)`) → `test_create_job_ignores_credential_overrides` fails (`plex_token` leaks into overrides). Separately removing the `"****"` mask at api_settings.py:280 → `test_get_settings_never_leaks_real_credentials_anywhere_in_response` fails (sentinel string appears in response body). Restored → both pass. |

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
- `media_preview_generator/web/webhooks.py:389` replace ``or _clean_title_from_basename(basename)`` with ``or basename``
  → `test_create_vendor_webhook_job_uses_clean_title_when_title_omitted` FAIL
  → `test_create_vendor_webhook_job_uses_clean_title_for_movie_basename` FAIL
- `media_preview_generator/web/app.py:72-78` short-circuit the `_infer_server_from_library_id` call
  → `test_scheduled_job_infers_server_id_from_library_id` FAIL
- `media_preview_generator/processing/multi_server.py:536` short-circuit the `needs_server_metadata + item_id is None` branch
  → `test_jellyfin_returns_skipped_not_in_library_when_item_id_unresolvable` FAIL
  → `test_plex_returns_skipped_not_in_library_when_item_id_unresolvable` FAIL
- `media_preview_generator/jobs/orchestrator.py:722` replace `server_id_filter=per_item_pin` with `server_id_filter=None`
  → 15 dispatcher kwargs tests fail across `test_dispatcher_kwargs_matrix.py` + `test_full_scan_multi_server.py`

8 of 8 attempted reverts caused at least one hindsight test to fail loudly with a clear diagnostic message. **The tests catch the bugs they claim to** across SocketIO transport, regenerate kwarg propagation, item-id memoisation, Jellyfin trickplay layout, title fallback wiring, scheduler server-pin inference, Jellyfin SKIPPED_NOT_IN_LIBRARY, and the full dispatcher kwargs matrix.

## Phase 6 batch (2026-05-05) — full revert-verify of remaining 13 rows

Batch 19 closed the remaining `Verify` rows by surgically simulating the
incident in production code (one line per row) and confirming the
hindsight test fails. **20 of 21 incident rows now Verified ✅** — the
single outstanding row is D34 sub-second worker visibility (`a64030c`),
which still has no direct hindsight test (see row 4 notes for the
recommended test shape).

Notable correction: rows 8 (D37 progress bounce) and 20 (Jellyfin
path-based refresh) were previously listed as Pending — they actually DO
have direct hindsight tests (`TestProgressBarMonotonicity` and
`TestTriggerRefresh::test_path_based_nudge_when_no_item_id` respectively).
Both verified loudly on revert. Status corrected.

The kill-button hover race (row 19, `5028fb6`) was also verified via
Playwright (`tests/e2e/test_ui_hover_defer.py`) — runs in the standard
e2e marker, no jsdom required.

## Verified-passing baseline

After Phase 0-5 execution, the test suite stands at **2340 passing**
(baseline 2262 → +78 new tests across 6 batches). All ten of the audit's
P0 items have hindsight tests; **20 of 21 catalogued incidents now have
a direct hindsight test verified by manual revert** (only D34
sub-second-worker visibility remains unwritten).

## Open items (workers-panel-jitter follow-up)

Workers panel jitter (`e46e73c`) is not in the 21-incident catalogue but
is mentioned as a UI-render contract that could benefit from a Playwright
test. Pattern is established by `tests/e2e/test_ui_hover_defer.py`.

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
