# Test-suite manual audit — master tracker

Per-test deep audit of every test in `tests/`. Each test classified:
- **Strong** — would catch the bug it claims to test
- **Weak** — assertion too loose (truthy / substring / `is not None`)
- **Tautological** — tests the mock, not the SUT
- **Bug-blind** — `assert_called_once()` without arg checks (D34 paradigm)
- **Dead/redundant** — same coverage as another test
- **Framework trivia** — tests pytest/Flask/loguru we don't own
- **Bug-locking** — asserts what the buggy code currently does
- **Needs human** — can't judge; flagged for user review

**Scope:** 66 `tests/test_*.py` + 3 `tests/journeys/` + 2 `tests/e2e/` = **71 files**, **~2,340 tests**.
**Status: 71/71 files audited (100%).**

## Per-file results

| File | Tests | Audit doc | Verdict |
|---|---|---|---|
| test_api_server_auth.py | 13 | [link](AUDIT_test_api_server_auth.md) | ✅ STRONG |
| test_api_servers.py | 36 | [link](AUDIT_test_api_servers.md) | ✅ STRONG |
| test_app.py | 16 | [link](AUDIT_test_app.md) | ⚠ MIXED (1 bug-blind) |
| test_auth_external.py | 25 | [link](AUDIT_test_auth_external.md) | ✅ STRONG |
| test_auth_setup_guard.py | 20 | [link](AUDIT_test_auth_setup_guard.md) | ✅ STRONG |
| test_basic.py | 10 | [link](AUDIT_test_basic.md) | ✅ STRONG |
| test_bif_viewer.py | 41 | [link](AUDIT_test_bif_viewer.md) | ✅ STRONG (1 defensible weak smoke) |
| test_config.py | 107 | [link](AUDIT_test_config.md) | ✅ STRONG |
| test_dispatcher_kwargs_matrix.py | 18 | [link](AUDIT_test_dispatcher_kwargs_matrix.md) | ✅ STRONG |
| test_eta_calculation.py | 8 | [link](AUDIT_test_eta_calculation.md) | ✅ STRONG |
| test_file_results.py | 30 | [link](AUDIT_test_file_results.md) | ⚠ MIXED (3 weak) |
| test_full_scan_multi_server.py | ~30 | [link](AUDIT_test_full_scan_multi_server.md) | ⚠ MIXED (1 weak) |
| test_gpu_ci.py | 7 | [link](AUDIT_test_gpu_ci.md) | ✅ STRONG |
| test_gpu_detection_extended.py | ~205 | [link](AUDIT_test_gpu_detection_extended.md) | ✅ STRONG |
| test_headers.py | 2 | [link](AUDIT_test_headers.md) | ✅ STRONG |
| test_integration.py | 7 | [link](AUDIT_test_integration.md) | ✅ STRONG |
| test_job_dispatcher.py | 26 | [link](AUDIT_test_job_dispatcher.md) | ✅ STRONG |
| test_jobs.py | 39 | [link](AUDIT_test_jobs.md) | ✅ STRONG |
| test_logging_config.py | 24 | [link](AUDIT_test_logging_config.md) | ⚠ MIXED (1 weak) |
| test_media_processing.py | ~137 | [link](AUDIT_test_media_processing.md) | ✅ STRONG |
| test_notifications_api.py | 21 | [link](AUDIT_test_notifications_api.md) | ✅ STRONG |
| test_oauth_routes.py | 27 | [link](AUDIT_test_oauth_routes.md) | ⚠ MIXED (5 weak) |
| test_output_emby_sidecar.py | 9 | [link](AUDIT_test_output_emby_sidecar.md) | ✅ STRONG |
| test_output_jellyfin_trickplay.py | 11 | [link](AUDIT_test_output_jellyfin_trickplay.md) | ✅ STRONG |
| test_output_journal.py | 16 | [link](AUDIT_test_output_journal.md) | ✅ STRONG |
| test_output_plex_bundle.py | 15 | [link](AUDIT_test_output_plex_bundle.md) | ✅ STRONG |
| test_plex_client.py | ~70 | [link](AUDIT_test_plex_client.md) | ⚠ MIXED (1 bug-locking, 1 framework-trivia class, 1 weak) |
| test_plex_webhook_registration.py | 19 | [link](AUDIT_test_plex_webhook_registration.md) | ✅ STRONG |
| test_priority.py | 18 | [link](AUDIT_test_priority.md) | ✅ STRONG |
| test_processing.py | 30 | [link](AUDIT_test_processing.md) | ⚠ MIXED (4 weak/bug-blind) |
| test_processing_frame_cache.py | 32 | [link](AUDIT_test_processing_frame_cache.md) | ✅ STRONG (gold-standard) |
| test_processing_multi_server.py | 31 | [link](AUDIT_test_processing_multi_server.md) | ✅ STRONG |
| test_processing_outcome.py | 18 | [link](AUDIT_test_processing_outcome.md) | ✅ STRONG |
| test_processing_registry.py | 12 | [link](AUDIT_test_processing_registry.md) | ✅ STRONG |
| test_processing_retry_queue.py | 16 | [link](AUDIT_test_processing_retry_queue.md) | ✅ STRONG |
| test_processing_vendors.py | 31 | [link](AUDIT_test_processing_vendors.md) | ✅ STRONG |
| test_recent_added_scanner.py | 13 | [link](AUDIT_test_recent_added_scanner.md) | ✅ STRONG |
| test_retry_cascade.py | 11 | [link](AUDIT_test_retry_cascade.md) | ✅ STRONG |
| test_routes.py | ~302 | [link](AUDIT_test_routes.md) | ✅ STRONG |
| test_scheduler.py | 53 | [link](AUDIT_test_scheduler.md) | ✅ STRONG |
| test_security_fixes.py | 15 | [link](AUDIT_test_security_fixes.md) | ✅ STRONG |
| test_servers_base.py | 22 | [link](AUDIT_test_servers_base.md) | ⚠ MIXED (4 weak) |
| test_servers_emby.py | 38 | [link](AUDIT_test_servers_emby.md) | ⚠ MIXED (4 weak) |
| test_servers_emby_auth.py | 13 | [link](AUDIT_test_servers_emby_auth.md) | ⚠ MIXED (3 weak) |
| test_servers_features_backfill.py | 17 | [link](AUDIT_test_servers_features_backfill.md) | ✅ STRONG |
| test_servers_jellyfin.py | 34 | [link](AUDIT_test_servers_jellyfin.md) | ⚠ MIXED (4 weak) |
| test_servers_jellyfin_auth.py | 17 | [link](AUDIT_test_servers_jellyfin_auth.md) | ⚠ MIXED (1 weak) |
| test_servers_ownership.py | 22 | [link](AUDIT_test_servers_ownership.md) | ⚠ MIXED (1 weak) |
| test_servers_page.py | 3 | [link](AUDIT_test_servers_page.md) | ✅ STRONG |
| test_servers_plex.py | 39 | [link](AUDIT_test_servers_plex.md) | ⚠ MIXED (4 weak) |
| test_servers_registry.py | 15 | [link](AUDIT_test_servers_registry.md) | ⚠ MIXED (1 weak) |
| test_settings_manager.py | 51 | [link](AUDIT_test_settings_manager.md) | ⚠ MIXED (2 weak) |
| test_socketio.py | 10 | [link](AUDIT_test_socketio.md) | ⚠ MIXED (1 weak) |
| test_static_app_js.py | 10 | [link](AUDIT_test_static_app_js.md) | ✅ STRONG (rejects P3.7 delete-rec) |
| test_timezone.py | 4 | [link](AUDIT_test_timezone.md) | ✅ STRONG |
| test_upgrade.py | ~70 | [link](AUDIT_test_upgrade.md) | ✅ STRONG (exemplary) |
| test_utils.py | 36 | [link](AUDIT_test_utils.md) | ⚠ MIXED (5 weak) |
| test_version_check.py | 41 | [link](AUDIT_test_version_check.md) | ✅ STRONG (1 documented bug-locking) |
| test_webhook_router.py | 27 | [link](AUDIT_test_webhook_router.md) | ⚠ MIXED (1 weak) |
| test_webhook_secret_rotation.py | 6 | [link](AUDIT_test_webhook_secret_rotation.md) | ⚠ MIXED (1 broken assert) |
| test_webhooks.py | ~52 | [link](AUDIT_test_webhooks.md) | ⚠ MIXED (1 weak, 1 needs-human) |
| test_webhooks_plex.py | 27 | [link](AUDIT_test_webhooks_plex.md) | ⚠ MIXED (1 weak) |
| test_windows_compatibility.py | 9 | [link](AUDIT_test_windows_compatibility.md) | ✅ STRONG |
| test_worker.py | 49 | [link](AUDIT_test_worker.md) | ⚠ MIXED (3 weak, 1 tautological) |
| test_worker_concurrency.py | 22 | [link](AUDIT_test_worker_concurrency.md) | ⚠ MIXED (1 weak smoke) |
| test_worker_naming.py | 9 | [link](AUDIT_test_worker_naming.md) | ✅ STRONG |
| journeys/test_adapter_path_contract.py | 15 | [link](AUDIT_journeys_test_adapter_path_contract.md) | ✅ STRONG |
| journeys/test_journey_multi_server_partial_unreachable.py | 2 | [link](AUDIT_journeys_test_journey_multi_server_partial_unreachable.md) | ✅ STRONG |
| journeys/test_journey_sonarr_to_published.py | 3 | [link](AUDIT_journeys_test_journey_sonarr_to_published.md) | ✅ STRONG |
| e2e/test_ui_hover_defer.py | 2 | [link](AUDIT_e2e_test_ui_hover_defer.md) | ✅ STRONG |
| e2e/test_ui_workers_panel.py | 5 | [link](AUDIT_e2e_test_ui_workers_panel.md) | ✅ STRONG |

## Roll-up

- **71/71 files audited.** Approx 2,340 tests reviewed.
- **44 STRONG** (no fixes needed).
- **27 MIXED** (one or more weak/bug-blind/tautological/bug-locking tests — fixable).
- **0 WEAK** (no whole-file failures).
- **1 needs-human** (`test_webhooks.py:1384` — naming confusion `server_id` vs `server_id_filter`).

## Fix queue (~32 individual tests across 27 files)

### Bug-locking (1 — must rewrite or delete)
- `test_plex_client.py:638` `test_path_mapping_partial_match_avoided` — actively asserts wrong behavior

### Framework-trivia (1 class, 8 tests — delete or rewrite)
- `test_plex_client.py:547-680` `TestPathMapping` — tests `str.replace`, not project code

### Bug-blind (1)
- `test_app.py:116` `test_creates_and_starts_job` — `assert_called_once()` with no kwarg checks

### Tautological (1)
- `test_worker.py:899` `test_worker_statistics` — tests Python's `sum()` builtin

### Weak — substring on message (~14)
- `test_servers_emby_auth.py:86, 100, 114` — `"401" in message` matches `"4015 widgets"`
- `test_servers_emby.py:183, 189, 195, 208`
- `test_servers_jellyfin_auth.py:215`
- `test_servers_jellyfin.py:105, 110, 115, 307`
- `test_servers_plex.py:73, 83, 92, 105`

### Weak — under-asserted dataclass / round-trip (~7)
- `test_servers_base.py:56, 68, 79, 226`
- `test_servers_registry.py:76` (round-trip 3 of 10 fields — risk: silent credential loss)
- `test_settings_manager.py:140, 511`

### Weak — looser-than-required assertions (~10)
- `test_socketio.py:245`, `test_servers_ownership.py:113`
- `test_oauth_routes.py:59, 108, 124, 452, 467`
- `test_logging_config.py:99`
- `test_utils.py:29, 41, 53, 91, 230`
- `test_webhooks_plex.py:306`, `test_webhooks.py:357`
- `test_full_scan_multi_server.py:1126`
- `test_file_results.py:79, 278, 313`
- `test_processing.py:471, 748, 901, 948`
- `test_webhook_router.py:740`
- `test_worker.py:203, 836, 952`
- `test_worker_concurrency.py:162` (cheap smoke — keep)

### Broken assertion (1)
- `test_webhook_secret_rotation.py:254-260` — tuple expression instead of `assert`

### Needs-human (1)
- `test_webhooks.py:1384` `test_create_vendor_webhook_job_server_id_filter_pins_publishers`

## Standouts (positive examples worth referencing)

- `test_processing_frame_cache.py` — gold-standard. Real-thread concurrency tests for `generation_lock` via `Event`, full validity matrix, dispatcher integration.
- `test_dispatcher_kwargs_matrix.py` — exemplary D34-paradigm closure (12-kwarg matrix × 3 server types × pin states).
- `test_upgrade.py` — full v2→v11 schema-migration matrix, idempotency, edge cases.
- `test_servers_features_backfill.py` — every `assert_called_once_with` pins kwargs, strict status codes throughout.
- `test_routes.py:1487` — defense-in-depth: scans entire response body for sentinel secrets.
- `test_app.py::TestPrewarmCaches` — explicitly cites and closes a prior tautology in its docstring.
- `test_static_app_js.py` — clever cross-file static checks (template scan + servers.js block extraction).
