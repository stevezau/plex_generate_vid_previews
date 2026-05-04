# Audit: tests/test_routes.py — ~302 tests, 41 classes

Methodology note: this is a 5142-line file. Verdicts grouped by class for efficiency; specific weakness call-outs noted.

## TestPageRoutes (lines 126–217)

All 10 tests **Strong**: status code + content presence (assert specific HTML markers like `b"ejConnectPanel"`, `id="addServerModal"`); negative checks (modal absent on /setup) catch overzealous removal.

## TestLoginLogout (lines 224–268)

All 7 tests **Strong**: rate-limit count exact (5 → 6th=429, 10 → 11th=429); session state checked after login (not just redirect status); logout subsequent /api request bounces to /login.

## TestAuthAPI (lines 276–302)

All 5 tests **Strong**: status code + JSON body shape both verified.

## TestHealthCheck (line 313)

**Strong** — `status == "healthy"` strict.

## TestTokenEndpoints (lines 327–506)

All 13 tests **Strong**: `data["token"].startswith("****")` masking + `data["token"].endswith(new_token[-4:])` last-4 check + persisted-disk read-back (line 348). Mismatch/short rejections; env-controlled 409. Parametrized 5-cell wizard endpoint check at line 484 pins `status_code != 302` (the bug surface).

## TestJobsAPI (lines 514–1161)

All ~38 tests **Strong**:
- Credential strip test (line 684) inspects `mock_start.call_args[0][1]` for forbidden keys — closes the D34-class bug; explicit "audit fix" comment cites prior assert_called_once weakness
- Server-id inference matrix: 4 cells (single → infer, multi → don't, explicit → wins, libraries 100 vs 200 → correct mapping) with strict `body["server_id"] == ...` checks
- Pagination: `data["page"]`, `data["per_page"]`, `data["pages"]` all asserted with strict equality
- Pause/resume verifies BOTH the response AND the underlying `sm.processing_paused` (the comment explicitly notes "the response payload alone could lie")
- Worker scaling: response keys + `pool.add_workers.assert_called_once_with("CPU", 2)` (exact args)

## TestManualTriggerAPI (lines 1169–1298)

All 8 tests **Strong**:
- Line 1172: assert `webhook_paths == [str(test_file)]` AND `force_generate is False` (audit fix — comment cites prior weakness)
- Line 1200: matrix cell — `force_regenerate=True` propagates as `force_generate is True`
- Path traversal returns 400 with "outside" in error
- Multiple paths label includes "2 files"

## TestSettingsAPI (lines 1306–1705)

All ~22 tests **Strong**:
- Line 1487: `test_get_settings_never_leaks_real_credentials_anywhere_in_response` is a defense-in-depth scan for sentinel strings in the entire response body — would catch any future field forwarding the secret
- Token+webhook redaction handling: 3 cells per secret (placeholder ****, empty string, real new value)
- gpu_config validation rejects non-list (400), filters invalid entries
- log-level setter calls `setup_logging.assert_called_once()`

## TestJobConfigPathMappings (lines 1714–1974)

All 6 tests **Strong**:
- Settings path_mappings flow into `captured_configs[0].path_mappings == expected` (exact normalize round-trip)
- Both negative tests (path_mappings + webhook_paths NOT accepted as overrides) pin the security contract
- `test_run_processing_returning_none_marks_job_failed` (line 1884) asserts `state["status"] == "failed"` AND `"aborted" in state["error"]` — closes ConnectionError silent-success bug

## TestSetupWizardAPI (lines 1982–2078)

All 7 tests **Strong**:
- Line 1985 `test_get_setup_status_no_auth` strengthened (audit-noted) to assert `isinstance(.., bool)` for both fields with type message
- Line 2046 `test_complete_setup` pre-clears setup_complete then asserts the toggle, not just response success

## TestQuietHoursAPI (lines 2086–2163)

All 6 tests **Strong**: legacy single-window migrated to all-week (len 7), multi-window round-trip with day list strict equality, invalid time/day rejection with 400 + error message context (`"Window #1"`, `"moonday"`).

## TestSchedulesAPI / TestSchedulesCRUD / TestReprocessJob (lines 2166–2729)

All ~17 tests **Strong**: 404/400/409/200/201 status codes per CRUD operation; cron malformed → 400 + "error" in body; reprocess of running job → 409.

## TestSystemAPI (lines 2218–2383)

All 6 tests **Strong**: `data["gpus"] == []`, `running_job is None`, `pending_jobs == 0`, response field token masked `== "****"`, type asserts on int fields. Media-server status: per-row status string ("connected"/"disabled"/"unauthorised") strict, 30s cache flag, 401 classification fix called out (was bucketed as "unreachable" before).

## TestPathValidation / TestValidatePathsBranches (lines 2391–3019)

All ~16 tests **Strong**: each substring on specific error key ("Folder not found", "invalid path", "Local Media Path is required", etc.), valid-structure test verifies "valid structure" in info list.

## TestAuthMethods / TestAuthRejection (lines 2524–2560)

All 4 tests **Strong**: status + body shape (`isinstance(body, dict) and "jobs" in body`) — explicit comment notes a status-only check would mask short-circuit regression.

## TestWorkerScalingValidation (lines 2737–2867)

All 7 tests **Strong**: 0/invalid type → 400; no-pool → 409; non-numeric → 400 with "integer" in error.

## TestPageRoutesAdditional (lines 3027–3119)

All 10 tests **Strong**: redirect Locations checked exactly (preserves query string, fragment); /setup with already-configured → 302 to /; index without setup → 302 to /setup.

## TestLogHistoryAPI (lines 3127–3321)

All 7 tests **Strong**: `data["lines"] == []` for missing file, exact-count `len() == 1` after level filter, `before` cursor filtering returns the older entry, malformed before → 400, limit slices correctly.

## TestLibrariesAPI / TestPlexTestConnection / TestPlexLibrariesAPI (lines 3329–3811)

All ~14 tests **Strong**: HTTP boundary mocking (requests.get) verified — the comment explicitly explains why mocking the helper would be the D31 anti-pattern. URL/headers/X-Plex-Token captured and asserted. Each error type (401/404/SSLError/Timeout/ConnectionError/ValueError) gets a specific error-message substring check.

## TestPlexWebhookLoopbackGuard (lines 3599–3732)

All 4 tests **Strong**: docker+localhost → no outbound call (`mock_post.assert_not_called()`); D31 doubled-prefix regression guard explicit (`"/api/webhooks/plex/api/webhooks/plex" not in target_url`); IPv4 + IPv6 loopback both covered.

## TestFetchLibrariesViaHTTP (lines 3819–3861)

Both tests **Strong**: filter result shape, `verify=True/False` kwarg pinned.

## TestParamToBool (lines 3864–3907)

All 5 tests **Strong**: parametrized truthy/falsy/None/passthrough.

## TestLibraryCache (lines 3915–3998)

All 3 tests **Strong**: `mock_get.call_count == 1/2`, second call URL inspected for cache bypass.

## TestClassifyLibraryType (lines 4006–4048)

All 8 tests **Strong**: strict equality (`== "movie"`/`"sports"`/`"show"`) per case.

## TestGetVersionInfo (lines 4056–4252)

All 9 tests **Strong**: install_type + current_version + latest_version + update_available all checked; cache TTL pin via `call_count[0] == 1` after 2 calls.

## TestGetPlexServersConnectionList / TestBifSearchPhases / TestFolderBrowse (lines 4259–4722)

All tests **Strong**: Plex server list derivation (host/local/ssl pinned), search phase 1+2 dispatch verified by URL inspection (e.g. `assert any(u.endswith("/library/metadata/100/allLeaves") for u in called_urls)`), folder browse safety (relative→400, denylist→403, dot-dirs hidden by default).

## TestValidatePlexConfigFolder / TestPerServerPlexWebhook / TestBackupRestore / TestSettingsManagerWebhookMigration (lines 4730–5142)

All tests **Strong**: shard count exact, error message contains plex_root path AND `-v` example, per-server token forwarded (`seen["token"] == "TOKEN-A"`), backup ordering newest-first verified by exact filename list, restore writes match exact contents, migration moves keys onto correct server.

## Summary

- **~302 tests** across **41 classes**
- All **Strong**
- 0 bug-blind / tautological / bug-locking / weak finds requiring fix
- Multiple "audit fix" comments throughout cite prior weak assertions and explain the strengthening rationale (great pattern)
- The credential-strip + webhook_paths-strip + path_mappings-strip tests close the D34-class bug surface comprehensively
- HTTP boundary mocking is consistent (requests.get, not the in-module helper) — D31 regression guards baked in

**File verdict: STRONG.** No changes needed.
