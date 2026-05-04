# Test-coverage audit + closing-the-gap roadmap

## Context

The user's hypothesis: "our tests do not accurately test the function of the app." The `tests/` tree has ~70 files and >1,500 tests at ~79% coverage, yet recurring production fixes (D31, D32, D34, D36, D38, etc.) keep landing for code paths that *did* have nominal test coverage. The pattern in our `.claude/rules/testing.md` already calls this out: "if removing a parameter from the SUT wouldn't break the test, the test isn't covering that parameter."

This audit reverse-engineers what the app *does* from the user's perspective (~120 flows), maps that against what we currently test, and produces a prioritised roadmap to close the real gaps — weighted by incidents that have already shipped.

**Goal of this plan**: not to grow the suite for vanity metrics, but to close gaps that have caused (or could cause) user-visible bugs. We aim to *delete* the over-tested trivia and *add* tests for the under-tested flows.

---

## Methodology

Three parallel exploration passes produced:

1. **Flow inventory** — 14 categories, ~120 user-facing operations, mapped from web routes + webhooks + scheduler + multi-server processor + worker pool + output adapters.
2. **Test inventory** — every `tests/*.py` and `tests/e2e/*.py` categorised by flow + test count.
3. **Incident archaeology** — 21 high-teaching-value `fix:` commits where a hindsight test would have prevented the ship. Cross-referenced against `.claude/rules/testing.md` patterns.

The diff is then weighted by:
- **P0 — already burned us in production** (incident archaeology hit + missing/weak test)
- **P1 — high blast-radius gap** (multi-server dispatch, GPU fallback, retry queue, webhook security)
- **P2 — single-vendor or single-mode gap** (Emby-only, Jellyfin-only, light path)
- **P3 — UI polish + edge cases**

---

## Findings: where current tests are blind

### A. Boundary-call assertion blindness (the D34 paradigm)

Many tests use `mock.assert_called_once()` without asserting **what kwargs the SUT controlled**. The dispatcher → `process_canonical_path` regression (D34, job d9918149) hid for months because the test only checked `kwargs["canonical_path"]` and ignored `kwargs["server_id_filter"]`. Likely candidates to audit (sample, not exhaustive):
- `test_processing_multi_server.py` — the publisher-loop tests need every kwarg the orchestrator sends asserted, not just the call.
- `test_webhook_router.py` — webhook → `create_vendor_webhook_job` boundary needs `title`, `server_id`, `item_id_by_server`, `regenerate` all asserted (the title-fallback bug we just fixed would have surfaced).
- `test_job_dispatcher.py` — `dispatch_items` → worker.assign_task boundary.

### B. Matrix-collapse blindness (the "tested Plex pin, not Emby pin" pattern)

Tests cover a happy path on one branch, then declare the function tested. Areas where the matrix is incomplete:
- `_resolve_publishers` with `server_id_filter`: tested for Plex, **not** for Emby/Jellyfin filters where the bug would behave differently.
- Retry backoff: tested for the first retry, not for `retry_attempt > 0` reaching `max_retries`.
- GPU fallback: tested for one codec, not for the matrix of `(codec, GPU vendor, fallback path taken)`.
- Auth methods: tested for token-in-header, not all four (`X-Auth-Token`, Bearer, Basic, `?token=`).

### C. Cross-module integration blindness

Each module is well unit-tested but the *seams* aren't:
- **Webhook → Job → Worker → Publisher** end-to-end: there's no single test asserting "Sonarr POST → 60s debounce → Job created with correct title + server pin → Worker picks up → publisher fanout → per-publisher chips correct".
- **Schedule → Job → server-pin inference**: the bug we shipped where scan jobs fanned out to all servers had no integration test.
- **Webhook prefix translation → owner pre-flight check**: D29-style prefix bugs only caught at the unit level on each side, not across the seam.
- **Pause → in-flight items + new spawns**: pause coverage is on the dispatcher main thread but not on the scan-loop sub-thread (the D-incident commit 886a2f4 found this gap the hard way).

### D. Vendor-specific surfaces under-tested

The Plex path is heavily covered (88 `test_plex_client.py` tests, 44 `test_servers_plex.py`). Emby + Jellyfin are thinner:
- Emby exact-path filter (`/Items?Path=`) added in `f842fc4` — only cached on the test that proved the perf win, not the correctness across *every code path* that calls `resolve_remote_path_to_item_id`.
- Jellyfin plugin's `ResolvePath` endpoint — only tested via the Python client; no contract test pinning the C# response shape.
- Jellyfin trickplay output layout — D38 fix added a path test; needs a parallel test for `compute_output_paths` so the same regression can't recur in Emby's analogous adapter.
- Vendor extraction toggle endpoints (`POST /api/servers/{id}/vendor-extraction`) — Plex-side test exists; Emby/Jellyfin path mostly a stub.

### E. UI race conditions

The kill-button flicker (5028fb6) and progress-bar bounce (af116e8) both shipped because:
- `test_static_app_js.py` only validates JS syntax / static analysis; no DOM-render race tests.
- E2E tests (`tests/e2e/`) are happy-path browser flows — no synthetic poll-during-hover or progress-emit-race tests.

This area is structurally hard to test, but a few well-chosen jsdom-based tests would have caught both.

### F. Auth + CSRF + rate-limit boundary tests are thin

- `test_security_fixes.py` — 16 tests, mostly CVE-specific patches.
- `test_headers.py` — 2 tests.
- `test_auth_external.py` — 26 tests, mostly token-derivation logic.
- **No** end-to-end test that POSTs to `/api/jobs` with credentials in the body and asserts they're stripped (D-style allow-list test).
- **No** test that POSTs to `/login` 11 times in a minute and asserts the 11th is rate-limited.
- **No** test asserting CSRF is enforced on every mutation route (a single missing decorator on a new route would silently bypass).

### G. "Code path doesn't exist yet but should" gaps

Things the user asked about previously and the audit confirms have no test:
- `appConfirm` modal contract: the deadlock we fixed in `8a89077` had no contract test for `void fn()` vs `fn()` Playwright pattern.
- Worker-card phase-string rendering (`current_phase`): no test asserts the JS picks up `current_phase` and renders it; the recent commit shipped without a JS-render test.
- Retry-waiting amber UX (`retry_eta`): no test asserts the JS converts `retry_eta` ISO timestamp into a countdown and the bar fills correctly.

---

## Coverage map: current tests by flow category

(Excerpt — full inventory in source agent reports.)

| Flow category | Test files | Tests | Strength |
|---|---|---|---|
| Setup/wizard | `test_auth_setup_guard.py` + 8 e2e | ~50 | **Strong (UI heavy)** |
| Server management | 11 files (`test_servers_*.py` + `test_api_servers.py` + e2e) | ~290 | **Strong for Plex, thinner for Emby/Jellyfin** |
| Webhooks | `test_webhooks.py`, `test_webhooks_plex.py`, `test_webhook_router.py`, `test_plex_webhook_registration.py` | ~131 | **Strong unit, weak end-to-end** |
| Scheduling | `test_scheduler.py` + 1 e2e | ~69 | **Strong CRUD, weak server-pin inference + quiet hours** |
| Jobs lifecycle | `test_jobs.py`, `test_job_dispatcher.py`, retry tests | ~102 | **Strong individually, no full-lifecycle integration** |
| Multi-server orchestration | `test_processing_multi_server.py`, `test_processing_outcome.py`, `test_full_scan_multi_server.py`, `test_processing.py` | ~111 | **Coverage exists; assertions thin per D34 pattern** |
| Worker pool / GPU | `test_worker.py`, `test_worker_concurrency.py`, `test_gpu_*.py` | ~269 | **GPU detection exhaustive; pool capacity matrix sparse** |
| Output adapters | 4 files | ~55 | **Plex strong; Emby/Jellyfin contract tests missing** |
| Web UI / API routes | `test_routes.py` (264) + 14 others + 14 e2e | ~470 | **Largest bucket; many trivial / declarative tests** |
| Settings / config | `test_config.py`, `test_settings_manager.py`, `test_upgrade.py` | ~240 | **Strong** |
| Auth / security | `test_auth_external.py`, `test_security_fixes.py`, `test_headers.py` | ~44 | **Thin** |
| BIF format | `test_media_processing.py`, `test_processing_vendors.py` | ~126 | **Strong** |
| Frame cache | `test_processing_frame_cache.py` | ~27 | **Strong** |
| **Background services** (recent_added_scanner, version_check) | scattered | ~57 | **Adequate** |

**Bucket totals:** ~70 test files, ~2,000+ test functions. The numbers look healthy. The *distribution* is the issue.

---

## The diff: what an "ideal" suite has that we don't

### Missing test categories entirely

1. **Cross-module integration suite** — one file per major user journey:
   - `test_journey_sonarr_to_published.py` (Sonarr POST → debounce → Job → Worker → Plex bundle on disk)
   - `test_journey_plex_webhook_to_published.py` (Plex `library.new` → Job → fanout to all owning servers)
   - `test_journey_scheduled_scan_to_published.py` (cron fires → Job created → server pin honoured → no fanout)
   - `test_journey_retry_to_eventual_success.py` (file not yet indexed → backoff → retry → publish)
   - `test_journey_gpu_fallback_to_cpu.py` (CodecNotSupportedError → CPU retry → publish)
   - `test_journey_multi_server_with_one_unreachable.py` (Plex up, Jellyfin down → publish to Plex, soft-skip Jellyfin, no FAILED)

2. **UI render-contract tests** — small jsdom suite that asserts:
   - Active-jobs card with `retry_eta` renders amber pill + countdown bar that fills.
   - Worker card with `current_phase` renders the phase string instead of "Working…".
   - Job table during hover defers wholesale rebuild (kill button stays clickable).
   - `appConfirm` modal: `void fn()` doesn't deadlock the Playwright test.

3. **Auth + security boundary tests** — closing the D-style gaps:
   - `/api/jobs` POST strips credential fields from body (D-style allow-list).
   - `/login` rate-limit fires after N attempts/min.
   - Every mutation route has `@api_token_required` (introspect the blueprint, fail loudly on a new route that forgot).
   - All four webhook auth methods accepted; combinations rejected.
   - `webhook_secret` regenerate updates every registered Plex webhook URL.

4. **Adapter contract tests** — a single parametrized matrix asserting `compute_output_paths` returns the *exact* path the vendor reads from:
   - Plex: `{config}/Media/localhost/{hash}/Indexes/index-sd.bif`
   - Emby: `{source_dir}/{stem}-320-10.bif` next to source
   - Jellyfin: `{source_dir}/{stem}.trickplay/{width}/{tile}-{cols}x{rows}/{frame}.jpg` (D38 layout)
   - Negative test: each vendor's wrong-layout path fails.

5. **Plugin contract tests for Jellyfin C# bridge** — pin the response JSON shape of `ResolvePath`, `RegisterTrickplay`, etc. so a plugin update can't silently break the Python client. (Lightweight: just JSON schemas, not a running plugin.)

### Missing test cells (matrix-completion gaps)

- `_resolve_publishers` with `server_id_filter` — add Emby + Jellyfin variants.
- `_make_item_id_resolver` memoisation — assert the cache hits on the 2nd+ call **per server** (the existing test only checks one).
- Retry backoff — assert behaviour at `retry_attempt = max_retries` (no further retry scheduled).
- Pause check — assert it short-circuits the multi-server scan loop (886a2f4 hindsight).
- Sibling-mount probe — assert `process_canonical_path` rebinds when source moved within the same logical prefix (b1022e2).
- Bundle-hash cache — assert invalidation when source mtime/size changes (D36 root cause).
- Webhook prefix translation — add a test where the pre-flight check needs translation (70275e9).

### Tests we should *delete* or thin out

The audit also identifies over-testing that adds noise:
- `test_routes.py` (264 tests) — many tests are 3-line "asserts route exists and returns 200." Collapse to a single parametrized "all routes return non-500" smoke test, freeing space for journey tests.
- `test_gpu_detection_extended.py` (173 tests) — exhaustive permutations of GPU name strings; the underlying parser is stable. Trim to one test per vendor + one regression test.
- `test_basic.py` — package import + version format. Fold into `test_app.py`.
- `test_static_app_js.py` — JS syntax validation; redundant with the linter. Replace with the new render-contract suite proposed above.

---

## Prioritised roadmap

### **P0 — close the gaps that already shipped bugs** (target: this week)

| # | Test to add | Catches |
|---|---|---|
| P0.1 | `test_dispatcher_kwargs_matrix.py` — assert every kwarg the dispatcher sends to `process_canonical_path` for every (server type × pin state × retry stage) cell | D34 dispatcher bug class |
| P0.2 | `test_journey_scheduled_scan_to_published.py` — schedule with `library_id=2` produces Job with `server_id` inferred and only that server's publishers fire | 933a26d server-pin gap |
| P0.3 | `test_jellyfin_not_in_library_skipped.py` — Jellyfin missing item_id → `SKIPPED_NOT_IN_LIBRARY` (not exception, not FAILED) | D32 (10be97c) |
| P0.4 | `test_journey_webhook_with_prefix_translation.py` — webhook with `webhook_prefixes` configured passes pre-flight | 70275e9 silent 202 drop |
| P0.5 | `test_make_item_id_resolver_memoisation.py` — same server queried 5× per dispatch hits cache 4× | 1f09c3a 90s gaps |
| P0.6 | `test_socketio_no_websocket_upgrade.py` — pin `allow_upgrades=False` so a refactor can't lose it again | 1873a23 frozen UI |
| P0.7 | `test_create_vendor_webhook_job_title_fallback.py` — extend `test_clean_title_from_basename` to assert the helper is *actually wired in* (not just exists) | the bug we shipped today |
| P0.8 | `test_credential_strip_on_jobs_post.py` — POST with `plex_token` in body never reaches Config | d92d1b8 credential leak |
| P0.9 | `test_pause_short_circuits_scan_loop.py` — pause flag stops new `process_canonical_path` spawns within the multi-server scan | 886a2f4 |
| P0.10 | `test_regenerate_kwarg_propagation.py` — `regenerate=True` from UI reaches `process_canonical_path(regenerate=True)` | 0092f8d silent skip |

### **P1 — high blast-radius integration tests** (target: next week)

| # | Test to add | Closes |
|---|---|---|
| P1.1 | `test_journey_sonarr_to_published.py` — Sonarr POST → debounce → Job → Plex publish on disk | webhook integration seam |
| P1.2 | `test_journey_retry_to_eventual_success.py` — not-indexed → backoff → retry → publish | retry queue end-to-end |
| P1.3 | `test_journey_gpu_fallback_to_cpu.py` — CodecNotSupportedError mid-encode → CPU retry → publish | GPU fallback seam |
| P1.4 | `test_journey_multi_server_partial_unreachable.py` — Plex up, Jellyfin down → Plex publishes, Jellyfin soft-skips | reliability under partial failure |
| P1.5 | `test_appconfirm_promise_contract.py` — Playwright contract: `void fn()` returns immediately, OK click resolves | future appConfirm callers |
| P1.6 | `test_adapter_path_contract.py` — parametrized matrix pinning every adapter's exact output path layout | D38 layout regression class |
| P1.7 | `test_resolve_publishers_filter_matrix.py` — `server_id_filter` on Plex / Emby / Jellyfin / unset | matrix-collapse gap |
| P1.8 | `test_login_rate_limit.py` — 11 POSTs/min returns 429 on the 11th | unguarded brute-force vector |

### **P2 — fill vendor + edge gaps** (target: 2-3 weeks)

| # | Test to add | Closes |
|---|---|---|
| P2.1 | `test_jellyfin_plugin_contract.py` — JSON shape of `ResolvePath`, `RegisterTrickplay` pinned | plugin-Python drift |
| P2.2 | `test_emby_exact_path_filter.py` — every code path that calls `resolve_remote_path_to_item_id` exercises the new `?Path=` filter | Emby perf path |
| P2.3 | `test_vendor_extraction_toggle_emby.py` + `_jellyfin.py` — full-flow test of disable + re-enable + status | vendor extraction surface |
| P2.4 | `test_quiet_hours_window.py` — schedule with quiet-hours blocks dispatch in window, resumes outside | scheduling edge case |
| P2.5 | `test_sibling_mount_rebind.py` — file moved between mounts under same prefix → process_canonical_path rebinds | b1022e2 |
| P2.6 | `test_bundle_hash_cache_invalidation.py` — source mtime/size change invalidates bundle hash cache | D36 root cause |
| P2.7 | `test_webhook_secret_rotation_propagates.py` — rotating secret re-registers every Plex server's webhook URL | secret rotation gap |

### **P3 — UI render contracts + cleanup** (target: 3-4 weeks)

| # | Action | Why |
|---|---|---|
| P3.1 | Add `test_ui_render_active_job_card.py` (jsdom) — retry-waiting state | recent UX commits had no test |
| P3.2 | Add `test_ui_render_worker_card.py` (jsdom) — `current_phase` rendering | recent UX commits had no test |
| P3.3 | Add `test_ui_render_kill_button_hover_defer.py` (jsdom) — wholesale rebuild defers under hover | 5028fb6 |
| P3.4 | Trim `test_routes.py` from 264 trivial tests → ~30 meaningful + 1 parametrized smoke | reduce maintenance noise |
| P3.5 | Trim `test_gpu_detection_extended.py` from 173 → ~20 | reduce CI runtime |
| P3.6 | Delete `test_basic.py`, fold into `test_app.py` | reduce file count |
| P3.7 | Replace `test_static_app_js.py` syntax checks with the P3.1-3.3 contract tests | redundant with linter |

---

## Files to read before executing each phase

When starting **P0**, read these for context:
- `media_preview_generator/processing/multi_server.py` — the `_make_item_id_resolver` + `_resolve_publishers` + `process_canonical_path` triangle (most P0 tests live around this).
- `media_preview_generator/web/webhooks.py` + `webhook_router.py` — title fallback path + dispatch.
- `media_preview_generator/web/scheduler.py` + `routes/api_jobs.py:_infer_server_from_library_id` — the schedule-pin pattern.
- `tests/conftest.py` — existing fixtures (`mock_config`, `mock_plex_server`, `_pi`, `_ms` builders) you'll reuse.
- `.claude/rules/testing.md` — boundary-call assertion patterns to follow.

When starting **P1**, additionally read:
- `media_preview_generator/jobs/orchestrator.py` + `worker.py` — the dispatcher and worker entry points (for journey tests).
- `media_preview_generator/processing/retry_queue.py` — backoff schedule + reaching `max_retries`.

When starting **P2**, additionally read:
- `jellyfin-plugin/Api/TrickplayBridgeController.cs` — endpoint contracts to pin.
- `media_preview_generator/servers/emby.py` — `_uncached_resolve_remote_path_to_item_id` (the new `?Path=` path).

---

## Verification

After each phase, run:

```bash
# P0: focused incident regression suite
pytest tests/test_dispatcher_kwargs_matrix.py tests/test_journey_*.py \
       tests/test_jellyfin_not_in_library_skipped.py \
       tests/test_make_item_id_resolver_memoisation.py \
       tests/test_socketio_no_websocket_upgrade.py \
       tests/test_credential_strip_on_jobs_post.py \
       tests/test_regenerate_kwarg_propagation.py \
       --no-cov -v

# P1: full integration journeys (slower)
pytest tests/test_journey_*.py -m integration --no-cov -v

# Whole suite (parallel, with coverage gate)
pytest

# E2E (Playwright, serial)
pytest -m e2e -n 0 --no-cov
```

Success criteria for the audit-as-a-whole:
- All 10 P0 tests written and passing.
- A future regression in any of the 21 catalogued incident classes triggers a test failure (manually verified by reverting one fix per class and confirming the new test fails).
- Coverage % stays at or above current ~79% (we're not chasing the metric — we're trading trivial tests for meaningful ones).
- CI runtime doesn't increase by more than 30% (P3 cleanup offsets P0–P2 additions).

---

## Estimate

- P0: ~10 tests, ~1.5 days of work. Each test is small (one boundary, one assertion that wasn't there).
- P1: ~8 tests, ~3 days. Journey tests need careful fixture composition.
- P2: ~7 tests, ~2 days. Mostly vendor-specific, parallelisable.
- P3: ~7 actions, ~1.5 days. Mostly deletion + 3 jsdom files.

**Total: ~8 working days** to take the suite from "looks complete on paper" to "actually catches the bugs that have shipped."

---

# APPENDICES — raw evidence the audit was built from

The body above is the synthesized conclusion. The three appendices below are the raw evidence so a reviewer can verify the diff, spot anything I omitted, and reuse the inventories independently.

---

## Appendix A — Full user-facing flow inventory (~120 flows)

Source-file references in `path:line` form so each flow can be inspected. "(inferred)" tags flows whose call site I could see but didn't open the file for to pin a line.

### Setup wizard
- First-run redirect to setup when `is_setup_complete=false` (`pages.py:14–18`)
- Token generation or custom-token entry on first login (`api_jobs.py:149–155`, `api_settings.py:937–955`)
- Plex Multi-Server Discovery: Plex.tv OAuth sign-in returns all accessible servers; user selects multiple to add (inferred)
- Setup wizard step progression: state persistence across steps (`api_settings.py:883–897`)
- Skip setup button for Emby/Jellyfin users (`api_settings.py:921–934`)
- Webhook URL liveness check in setup; Docker loopback warning (`api_settings.py:33–49`)
- Path validation: plex_config_folder structure check + local media folder readability (`api_settings.py:1079–1139`)
- Complete setup → redirect to dashboard (`api_settings.py:900–909`)

### Server management
- **List/read**: GET /api/servers returns all configured servers with credentials redacted (`api_servers.py:320–365`)
- **Create**: POST /api/servers with type/name/url/auth; server_identity auto-probed (`api_servers.py:566–611`)
- **Edit**: PUT/PATCH /api/servers/{id}; auth fields merged to retain secrets on round-trip (`api_servers.py:614–653`)
- **Delete**: DELETE /api/servers/{id}; best-effort Plex webhook deregistration from plex.tv (`api_servers.py:656–700`)
- **Test connection**: POST /api/servers/test-connection (transient) or /api/servers/{id}/test-connection (saved) (`api_servers.py:703–812`)
- **Refresh libraries**: POST /api/servers/{id}/refresh-libraries queries live server's library list, preserves per-lib toggles (`api_servers.py:441–558`)
- **Toggle server enabled**: PATCH /api/servers/{id}/enabled (`api_servers.py:854–882`)
- **Vendor extraction toggle**: POST /api/servers/{id}/vendor-extraction; flips Plex `scannerThumbnailVideoFiles`, Emby `Extract*ImagesDuringLibraryScan`, Jellyfin `ExtractTrickplayImagesDuringLibraryScan` per library (`api_servers.py:885–949`)
- **Vendor extraction status**: GET /api/servers/{id}/vendor-extraction/status returns count of extracting/stopped libraries (`api_servers.py:952–989`)
- **Health check (settings audit)**: GET /api/servers/{id}/health-check returns per-library fixable issues (Jellyfin trickplay flag, etc.) (`api_servers.py:992–1067`)
- **Apply health fixes**: POST /api/servers/{id}/health-check/apply auto-fixes flagged settings (`api_servers.py:1070–1119`)
- **Install Jellyfin plugin**: POST /api/servers/{id}/install-plugin one-click Media Preview Bridge install (`api_servers.py:815–851`)
- **Output status check**: GET /api/servers/{id}/output-status verifies BIF files exist on disk per-server (`api_servers.py:1122–1258`)
- **Path ownership resolve**: GET /api/servers/owners?path=X finds which servers own a file (`api_servers.py:396–438`)

### Webhook ingestion
- **Unified webhook router**: POST /api/webhooks/incoming (single endpoint for all vendors) (`webhook_router.py`)
- **Plex library.new webhook**: Auto-discovered via Plex Pass detection; user registers from Settings → Webhooks (`api_plex_webhook.py`)
- **Sonarr/Radarr/Tdarr download webhooks**: Custom webhook URL accepts vendor-specific payloads (`webhooks.py`)
- **Emby/Jellyfin native webhooks**: Inbound POST payloads routed to unified handler (`webhook_router.py`)
- **Webhook auth**: X-Auth-Token header, Authorization Bearer, Basic auth, or ?token= query param (`webhooks.py:95+`)
- **Debounce**: Rapid imports of same file batched; configurable webhook_delay (default 60s) before job creation (`webhooks.py:28–31`)
- **Retry on not-yet-indexed**: Webhook triggers job that retries if Plex hasn't indexed the file yet (30s → 2m → 5m → 15m → 60m backoff) (`jobs.py` inferred)
- **Webhook history**: Last 100 events persisted to webhook_history.json and surfaced on /automation page (`webhooks.py:41–93`)
- **Plex direct webhook registration**: Settings page register/unregister buttons; state probed from plex.tv (`api_plex_webhook.py:143+`)
- **Secret rotation**: Rotating webhook_secret re-registers every Plex server's webhook so Plex picks up new token (`api_settings.py:453–498`)

### Scheduling
- **Create schedule**: POST /api/schedules with name, cron_expression or interval_minutes, optional library scope (`api_schedules.py:37–88`)
- **Edit schedule**: PUT /api/schedules/{id} with partial payload; cron validation on save (`api_schedules.py:91–133`)
- **Delete schedule**: DELETE /api/schedules/{id} (`api_schedules.py:136–143`)
- **Enable/disable schedule**: POST /api/schedules/{id}/enable or /disable (`api_schedules.py:146–165`)
- **Run schedule now**: POST /api/schedules/{id}/run immediately triggers job (`api_schedules.py:168–175`)
- **Quiet hours (pause windows)**: Multi-window schedule with per-day-of-week filters; GET current quiet window state (`api_schedules.py:190–198`)
- **Priority per schedule**: high/normal/low affects queue dispatch order (`api_schedules.py` inferred)
- **Per-library scope pinning**: Schedule targets specific libraries or "all" (`api_schedules.py:59–61`)

### Job lifecycle
- **Create manual job**: POST /api/jobs/manual with file_paths list and force_regenerate flag (`api_jobs.py:371–433`)
- **Create library job**: POST /api/jobs with library_ids/library_names and optional server_id (`api_jobs.py:293–368`)
- **Job state**: RUNNING → (completion) → COMPLETED/FAILED/CANCELLED (`api_jobs.py:225–239`)
- **List jobs**: GET /api/jobs with pagination (default 50/page, up to 200); sorted running→pending→terminal (`api_jobs.py:211–279`)
- **Get job detail**: GET /api/jobs/{id} (`api_jobs.py:282–290`)
- **Cancel job**: POST /api/jobs/{id}/cancel (`api_jobs.py:436–448`)
- **Delete job**: DELETE /api/jobs/{id} (`api_jobs.py:951–958`)
- **Reprocess job**: POST /api/jobs/{id}/reprocess creates new job with same params; restores full file set if original was retry (`api_jobs.py:961–1027`)
- **Get per-file results**: GET /api/jobs/{id}/files with outcome filter + search; outcome values: generated/failed/cpu_fallback/skipped/already_exists (`api_jobs.py:751–804`)
- **Get job logs**: GET /api/jobs/{id}/logs with offset/limit pagination (`api_jobs.py:702–748`)
- **Clear completed jobs**: POST /api/jobs/clear by status (`api_jobs.py:1030–1049`)
- **Get job stats**: GET /api/jobs/stats returns counters (`api_jobs.py:1052–1066`)

### Processing control
- **Global processing pause**: POST /api/processing/pause halts dispatch after current tasks complete (`api_jobs.py:514–527`)
- **Global processing resume**: POST /api/processing/resume resumes all running jobs + starts pending (`api_jobs.py:530–549`)
- **Get processing state**: GET /api/processing/state returns paused flag (`api_jobs.py:504–511`)
- **Add workers**: POST /api/workers/add or /api/jobs/{id}/workers/add (`api_jobs.py:552–579`, `634–666`)
- **Remove workers**: POST /api/workers/remove or /api/jobs/{id}/workers/remove; can schedule deferred removal (`api_jobs.py:582–607`, `669–699`)
- **Worker status**: GET /api/jobs/workers returns all workers with current task/progress/speed/ETA (`api_jobs.py:807–856`)
- **Set job priority**: POST /api/jobs/{id}/priority updates queue order (`api_jobs.py:471–501`)

### Multi-server orchestration
- **Canonical path → publishers**: Single FFmpeg run produces output for all servers owning the file (inferred from `multi_server.py`)
- **Sibling-BIF reuse**: One-pass output generation; Plex/Emby/Jellyfin each receive their format (`output/` adapters)
- **Item-id resolution**: Plex items resolved by path hash; Emby/Jellyfin by item_id (`api_bif.py` inferred)
- **Per-publisher output**: Plex bundles written to /Media/localhost/{hash}/Indexes/; Emby/Jellyfin sidecars next to source (`output/` adapters)

### GPU handling
- **GPU detection**: Probed on startup; NVIDIA/AMD/Intel/Apple cached in _gpu_cache (`routes/_helpers.py` inferred)
- **Per-GPU enable/workers/threads**: Settings → Workers panel edits gpu_config (`api_settings.py:290–292`, `434–448`)
- **GPU worker reconciliation**: Edited gpu_config synced to live WorkerPool without restart (`api_settings.py:52–76`)
- **CPU fallback**: GPU worker automatically retries file on CPU if decode fails (`jobs/worker.py` inferred)
- **HDR tone-mapping**: tonemap_algorithm setting (none/hable/linear) applied by processor (`api_settings.py:296`)
- **Codec detection**: CodecNotSupportedError triggers CPU fallback (CLAUDE.md:71)

### Output adapters
- **Plex bundle**: FFmpeg → JPEG frames → packed BIF; published to `plex_config_folder/Media/localhost/{hash}/Indexes/index-sd.bif` (CLAUDE.md:93)
- **Emby sidecar**: BIF file written next to source video as `{title}-320-5.bif` (`api_bif.py` inferred)
- **Jellyfin trickplay**: BIF + manifest JSON under source directory; auto-flips Jellyfin library settings (`api_servers.py:888–905`)
- **Cross-server BIF reuse**: Same canonical path generates once, output adapted per-server (`processing/multi_server.py` inferred)

### Web UI dashboard
- **Active jobs panel**: Real-time job count + status breakdown (`index.html` driven by /api/jobs)
- **Worker cards**: Per-worker GPU/CPU type, current file, progress %, speed, ETA (GET /api/jobs/workers)
- **Job queue table**: Paginated list of running/pending/completed jobs with library + server labels (GET /api/jobs)
- **Stats counters**: Files generated/failed/retried today/this week/all-time (GET /api/jobs/stats)
- **Servers panel**: Per-server card showing name, type (Plex/Emby/Jellyfin), library count, enabled toggle, edit/test buttons (`pages.py:109–120`)
- **Recent activity**: Job creation timestamps + outcome summaries in activity log (`jobs.py` inferred)
- **SocketIO live updates**: Job progress, worker status, log lines pushed to connected clients (`socketio_handlers.py:26–96`)
- **Logs viewer**: Real-time log stream; per-level filtering (DEBUG/INFO/WARNING/ERROR) (`pages.py:54–58`, `socketio_handlers.py:67–95`)

### Settings & persistence
- **Settings load**: GET /api/settings returns all settings; per-server Plex fields projected from media_servers[0] (`api_settings.py:257–320`)
- **Settings save**: POST /api/settings persists validated updates; routes legacy plex_* fields into media_servers (`api_settings.py:565–599`)
- **settings.json migration**: Env vars (PLEX_TOKEN, PLEX_URL, etc.) one-time seeded to settings.json on first load; thereafter .json is source of truth (CLAUDE.md:69)
- **Backup/restore**: Last N versions of settings.json/schedules.json/webhook_history.json kept as timestamped .bak files; one-click restore (`api_settings.py:1236–1313`)
- **Auth token regenerate**: POST /api/token/regenerate generates new random token; clears all sessions (`api_jobs.py:148–155`)
- **Auth token set**: POST /api/token/set allows custom token post-setup if WEB_AUTH_TOKEN env not set (`api_jobs.py:158–203`)
- **Log level hot-reload**: PUT /api/settings/log-level updates loguru level without restart (`api_settings.py:602–627`)
- **Frame cache TTL**: Settings → Performance panel configures frame_reuse (enabled, ttl_minutes, max_cache_disk_mb) (`api_settings.py:317–318`)
- **Job history retention**: job_history_days setting controls how old completed jobs are kept (`api_settings.py:300`)
- **Log rotation**: log_rotation_size + log_retention_count (`api_settings.py:298–299`)

### Auth & security
- **Login/logout**: POST /api/auth/login (rate-limited 10/min) + POST /api/auth/logout (`api_jobs.py:126–145`)
- **Login page**: GET /login when not authenticated (`pages.py:22–37`)
- **Session creation**: Persistent session on successful token validation (`api_jobs.py:133–135`)
- **API token requirement**: @api_token_required decorator on all mutation routes (routes, e.g. `api_jobs.py:148+`)
- **Setup-only routes**: @setup_or_auth_required allows access during setup or with valid token (`api_servers.py:321+`)
- **CSRF**: Flask session-based (implicit)
- **Rate limiting**: /login POST (5/min), /api/auth/login (10/min) (`pages.py:23`, `api_jobs.py:127`)
- **Webhook secret**: Optional webhook_secret rotated separately from API token; embedded in registered Plex webhook URLs (`api_settings.py:305`)

### Background services
- **Job dispatcher**: Pulls pending jobs from queue, assigns to worker pool based on priority (`jobs/dispatcher.py` inferred)
- **Recently-added scanner**: Polling fallback when Plex Pass not available; finds new files without webhook (`recent_added_scanner.py` inferred)
- **Schedule executor**: APScheduler runs cron/interval jobs at trigger time (`scheduler.py` inferred)
- **Version check**: Background GitHub release polling (CLAUDE.md:41)
- **Gunicorn worker reload**: DEV_RELOAD env flag for development (CLAUDE.md:12)

### Observability & troubleshooting
- **Per-job logs**: Captured via loguru per-job sink; paginated with offset/limit (`api_jobs.py:702–748`)
- **Activity log**: Webhook history (last 100 events) persisted + viewable on /automation (`webhooks.py:41–93`)
- **Toast notifications**: SocketIO events emit status changes to UI (`socketio_handlers.py` + `jobs.py` inferred)
- **Retry-waiting state**: Job config tracks retry attempt + next-retry timestamp; visible in UI (`jobs.py` inferred)
- **BIF thumbnail viewer**: /bif-viewer page lets users inspect generated previews per-server (`pages.py:102–106`)
- **Worker status panel**: Live worker cards show task + progress; GPU fallback badge when CPU retry occurs (`api_jobs.py:807–856`)
- **Webhook test UI**: Settings → Webhooks → Test Connection probes inbound URL reachability (`api_plex_webhook.py` inferred)
- **Server health check**: Audit per-library settings (Jellyfin trickplay enabled, Plex metadata extraction, etc.) (`api_servers.py:992–1067`)

---

## Appendix B — Full test-file inventory (~70 files, ~2,000+ tests)

Format: `path — what it covers (N tests, M classes)`.

### Setup/wizard
- `tests/test_auth_setup_guard.py` — Setup flow auth guard, post-complete access restriction (3 classes, 21 tests)
- `tests/e2e/test_wizard_full_flows.py` — End-to-end happy-path wizard sequencing per vendor (2 classes, 2 tests)
- `tests/e2e/test_wizard_step1_vendor_picker.py` — Vendor selection UI, branching logic (2 classes, 7 tests)
- `tests/e2e/test_wizard_step2_libraries.py` — Library list fetching & multi-select (1 class, 4 tests)
- `tests/e2e/test_wizard_step3_paths.py` — Path mapping config capture (2 classes, 5 tests)
- `tests/e2e/test_wizard_step4_processing.py` — Processing settings (threads, GPU, quality) (2 classes, 6 tests)
- `tests/e2e/test_wizard_step5_security.py` — Auth token setup (2 classes, 6 tests)
- `tests/e2e/test_wizard_emby_jellyfin_inline.py` — Inline password auth in wizard (2 classes, 2 tests)

### Server management
- `tests/test_servers_base.py` — Server registry, base interface, type classification (10 classes, 23 tests)
- `tests/test_servers_plex.py` — PlexServer wrapper, connection, library list (10 classes, 44 tests)
- `tests/test_servers_emby.py` — EmbyServer integration, item queries (13 classes, 44 tests)
- `tests/test_servers_jellyfin.py` — JellyfinServer integration, query building (12 classes, 40 tests)
- `tests/test_servers_emby_auth.py` — Emby password authentication (2 classes, 13 tests)
- `tests/test_servers_jellyfin_auth.py` — Jellyfin quick-connect, token exchange (5 classes, 18 tests)
- `tests/test_servers_ownership.py` — Server ownership resolution for paths (5 classes, 25 tests)
- `tests/test_servers_registry.py` — Server registry CRUD, config persistence (6 classes, 16 tests)
- `tests/test_api_servers.py` — Server list/get/refresh APIs, path ownership (9 classes, 43 tests)
- `tests/test_api_server_auth.py` — Emby/Jellyfin password auth, quick-connect flow (3 classes, 13 tests)
- `tests/e2e/test_servers_page.py` — Server management UI, add server modal (5 classes, 15 tests)

### Webhooks
- `tests/test_webhooks.py` — Webhook auth, event routing, debounce timers, history (57 tests)
- `tests/test_webhooks_plex.py` — Plex webhook event parsing & item resolution (28 tests)
- `tests/test_plex_webhook_registration.py` — Plex webhook registration/renewal (20 tests)
- `tests/test_webhook_router.py` — Vendor-agnostic webhook dispatcher (10 classes, 26 tests)
- `tests/e2e/test_webhooks_automation.py` — Webhook trigger in UI (2 classes, 4 tests)

### Scheduling
- `tests/test_scheduler.py` — ScheduleManager CRUD, cron/interval, enable/disable (11 classes, 66 tests)
- `tests/e2e/test_schedules.py` — Schedule UI creation/edit (2 classes, 3 tests)

### Jobs lifecycle
- `tests/test_jobs.py` — JobManager log persistence, retention, status tracking (11 classes, 46 tests)
- `tests/test_job_dispatcher.py` — Job queue, async dispatch, inflight guards (9 classes, 28 tests)
- `tests/test_processing_retry_queue.py` — Retry backoff, retry gate, slow queue (6 classes, 16 tests)
- `tests/test_retry_cascade.py` — Transient failure cascading through retry levels (3 classes, 12 tests)

### Multi-server orchestration / processing
- `tests/test_processing_multi_server.py` — process_canonical_path, multi-adapter fanning (15 classes, 28 tests)
- `tests/test_processing_outcome.py` — Result merging, dedup, conflict resolution (6 classes, 18 tests)
- `tests/test_full_scan_multi_server.py` — Library scans across multiple servers (12 classes, 29 tests)
- `tests/test_processing.py` — run_processing orchestration, library queries (10 classes, 36 tests)

### Worker pool / GPU
- `tests/test_worker.py` — Worker class, WorkerPool, threading, task assignment (7 classes, 57 tests)
- `tests/test_worker_concurrency.py` — Worker pool capacity, scheduling, fairness (7 classes, 23 tests)
- `tests/test_worker_naming.py` — Worker ID & name generation (3 classes, 9 tests)
- `tests/test_gpu_detection_extended.py` — NVIDIA/AMD/Apple/Intel GPU detection (33 classes, 173 tests)
- `tests/test_gpu_ci.py` — GPU detection CI-specific tests (2 classes, 7 tests)

### Output adapters
- `tests/test_output_plex_bundle.py` — Plex BIF publication, bundle metadata (4 classes, 16 tests)
- `tests/test_output_emby_sidecar.py` — Emby sidecar XML generation (4 classes, 9 tests)
- `tests/test_output_jellyfin_trickplay.py` — Jellyfin trickplay tile packing (3 classes, 13 tests)
- `tests/test_output_journal.py` — Job result journaling (4 classes, 17 tests)
- `tests/e2e/test_servers_jellyfin_trickplay.py` — Jellyfin trickplay e2e (1 class, 2 tests)

### Web UI / API routes
- `tests/test_routes.py` — Flask routes (login, settings, jobs, health) (38 classes, 264 tests)
- `tests/test_oauth_routes.py` — Plex OAuth & settings API (7 classes, 32 tests)
- `tests/test_notifications_api.py` — Notification list/clear API (5 classes, 22 tests)
- `tests/test_socketio.py` — WebSocket event dispatch (3 classes, 9 tests)
- `tests/e2e/test_webapp.py` — App homepage, navigation (5 classes, 11 tests)
- `tests/e2e/test_dashboard.py` — Dashboard job list, stats (4 classes, 6 tests)
- `tests/e2e/test_dashboard_modals.py` — Dashboard modal dialogs (4 classes, 6 tests)
- `tests/e2e/test_login_page.py` — Login flow UI (1 class, 3 tests)
- `tests/e2e/test_logs_page.py` — Job logs display (1 class, 2 tests)
- `tests/e2e/test_settings_page.py` — Settings form UI (4 classes, 10 tests)
- `tests/e2e/test_settings_steppers.py` — Stepper input controls (1 class, 3 tests)
- `tests/e2e/test_folder_picker.py` — Path picker dialog (1 class, 5 tests)
- `tests/e2e/test_preview_inspector.py` — BIF preview viewer (2 classes, 4 tests)
- `tests/e2e/test_theme_toggle.py` — Dark/light mode toggle (1 class, 2 tests)
- `tests/test_bif_viewer.py` — BIF frame endpoint, info endpoint, search (12 classes, 45 tests)

### Settings / config / persistence
- `tests/test_config.py` — Config loading, path mappings, validation (15 classes, 92 tests)
- `tests/test_settings_manager.py` — Settings CRUD, persistence, defaults (9 classes, 60 tests)
- `tests/test_upgrade.py` — Migration from old to new config format (18 classes, 88 tests)

### Auth / security
- `tests/test_auth_external.py` — External auth sources, token derivation (7 classes, 26 tests)
- `tests/test_security_fixes.py` — CVE patches, injection tests (5 classes, 16 tests)
- `tests/test_headers.py` — HTTP security headers (2 tests)

### BIF format
- `tests/test_media_processing.py` — BIF generation, FFmpeg, HDR/DV detection (25 classes, 110 tests)
- `tests/test_processing_vendors.py` — Per-vendor frame extraction & post-processing (6 classes, 16 tests)

### Frame cache
- `tests/test_processing_frame_cache.py` — Frame cache lifecycle, disk I/O (6 classes, 27 tests)

### Utility / misc
- `tests/test_basic.py` — Package import, version format (3 classes, 10 tests)
- `tests/test_app.py` — Flask app creation, route registration (7 classes, 16 tests)
- `tests/test_logging_config.py` — Logging setup, handlers (4 classes, 24 tests)
- `tests/test_version_check.py` — Update availability check (8 classes, 44 tests)
- `tests/test_plex_client.py` — Plex connection, retry, library queries (11 classes, 88 tests)
- `tests/test_processing_registry.py` — Vendor registry, processor selection (4 classes, 12 tests)
- `tests/test_utils.py` — Misc utility functions (8 classes, 38 tests)
- `tests/test_timezone.py` — Timezone handling (1 class, 4 tests)
- `tests/test_eta_calculation.py` — Progress & ETA calculation (3 classes, 8 tests)
- `tests/test_priority.py` — Item priority sorting (5 classes, 20 tests)
- `tests/test_file_results.py` — Processing result file I/O (9 classes, 31 tests)
- `tests/test_windows_compatibility.py` — Windows path handling (5 classes, 11 tests)
- `tests/test_integration.py` — Full end-to-end integration (3 classes, 7 tests)
- `tests/test_static_app_js.py` — Frontend JS validation (5 classes, 10 tests)
- `tests/test_recent_added_scanner.py` — Recent items discovery (13 tests)

### Shared fixtures (`tests/conftest.py`)
- `mock_config`, `mock_plex_server/section/movie/episode`, `media_fixture` (SDR/HDR10/DV8 clips)
- `mock_ffmpeg_success/failure`, `mock_mediainfo_*` (HDR / DV variants)
- `create_mock_ffmpeg_process`, `create_mock_mediainfo` factories
- `_pi`, `_pi_list`, `_pi_list_or_passthrough`, `_ms` ProcessableItem builders
- Autouse: frame cache reset, logging/prewarm/GPU neutralisation, job_async sync shim

### Shared fixtures (`tests/e2e/conftest.py`)
- `app_url`, `app_url_wizard` (subprocess Flask)
- `session_cookie`, `session_cookie_wizard`
- `authed_page`, `wizard_page` (Playwright)
- `accept_app_confirm` modal helper
- `_mocks.py` route intercepts

### pytest config (`pyproject.toml`)
- Markers: integration, real_plex_server, real_gpu_detection, gpu, plex, slow, ci, e2e, real_prewarm, real_logging, real_job_async
- Default invocation: `-m "not gpu and not e2e and not integration"` parallel via xdist, 30s/test timeout
- Coverage threshold: 70% on `media_preview_generator/`

### Notable gaps already visible at a glance
1. No explicit log-rotation daemon-thread tests (retention policy covered, daemon not).
2. No cross-vendor library conflict tests (same item owned by multiple servers in parallel processing).
3. No Plex library secret/PIN handling tests.
4. No webhook rate-limiting tests (debounce present, no per-IP/per-vendor throttling).
5. No large-scale load tests (worker pool tests use small counts; no 100+ concurrent job scenario).
6. No disk-space-exhaustion handling tests.
7. No network-interruption-mid-processing tests (FFmpeg timeout present, no graceful partial-BIF recovery).
8. No settings auto-migration-on-startup test (upgrade tests cover migration but not in-app reload behaviour).

---

## Appendix C — Production-incident archaeology (21 incidents)

Each entry: **Commit + title** — what shipped wrong, root cause, hindsight test.

### Webhooks & multi-server coordination

**1. D31: Doubled URL prefix in Plex resolution (`d404f73`)**
- Wrong: Sonarr/Radarr webhooks silently failed for ~3 days even though Plex had the file.
- Cause: `get_media_items_by_paths` stored PlexAPI's URL form (`/library/metadata/557676`) as item_id instead of bare ratingKey. Downstream `get_bundle_metadata` did `f"/library/metadata/{item_id}/tree"` → `/library/metadata//library/metadata/557676/tree` (404). Caught at DEBUG only.
- Hindsight: end-to-end test of `PlexBundleAdapter` mocking Plex /tree call, asserting URL has no doubled prefix when item_id is a full URL.

**2. D38: Jellyfin trickplay directory format mismatch (`8409952`)**
- Wrong: Jellyfin 10.10+ never rendered tiles even though files existed on disk.
- Cause: adapter wrote to `<dir>/trickplay/<basename>-<width>/`, Jellyfin reads `<media_dir>/<basename>.trickplay/<width>-<tileW>x<tileH>/`.
- Hindsight: unit test asserting `compute_output_paths` returns paths matching Jellyfin's actual `GetTrickplayDirectory` layout.

**3. Webhook paths not applied to pre-flight check (`70275e9`)**
- Wrong: users with prefix translations got silent 202 "no_owners" drops.
- Cause: pre-flight `no_owners` check ran against raw webhook path without applying `webhook_prefixes`.
- Hindsight: unit test that webhook with prefix translation passes pre-flight (not 202-reject).

### GPU & worker pool dispatch

**4. D34: Worker progress invisible for sub-second tasks (`a64030c`)**
- Wrong: cache-skip jobs showed zero worker activity even though dispatcher logs proved a worker picked it up.
- Cause: 1Hz emit throttle coalesced busy→idle into one window; user saw idle without "processing" flash.
- Hindsight: test running cacheable item with mocked FFmpeg showing zero-latency skip, asserting at least one "worker busy" message reaches UI.

**5. D34: Per-GPU worker count silently ignored (`dfc199a`)**
- Wrong: 2 GPUs × 2 workers gave parallelism=2, not 4; FFmpeg never saturated even with correct settings.
- Cause: `getattr(gpu_info_dict, "workers", 1)` on a dict returns the default, not the dict key.
- Hindsight: matrix test [1 GPU, 2 GPUs] × [1 worker, 2 workers/GPU] asserting peak concurrent worker count equals slots.

**6. Vendor webhook jobs bypassed worker pool (`87c78b7`)**
- Wrong: Sonarr/Radarr webhooks ran on CPU on NVIDIA boxes; Workers UI showed zero rows; jobs SIGTERM'd mid-encode.
- Cause: vendor-hint short-circuit called `_dispatch_webhook_paths_multi_server` synchronously (no GPU assignment, no UI worker rows).
- Hindsight: test asserting vendor webhook jobs with hints flow through `dispatch_items` (same path Plex uses), not synchronous bypass.

**7. Pause didn't stop new FFmpeg spawns on multi-server scans (`886a2f4`)**
- Wrong: user clicked Pause; SIGSTOP/SIGCONT halted running processes but ThreadPoolExecutor kept spawning new ones for ~6 min.
- Cause: multi-server full-scan path silently ignored `pause_check` at the item-fetch loop.
- Hindsight: test with pause_check returning True mid-scan, asserting no new `process_canonical_path` calls spawn.

### Multi-server ownership & routing

**8. Webhooks only published to Plex despite Emby/Jellyfin ownership (`8c78074`)**
- Wrong: file owned by 3 servers only published to Plex; logs said "Owners resolved: 1 server(s)."
- Cause: orchestrator built ServerRegistry from legacy Plex config only; multi-server settings existed but never reached dispatch.
- Hindsight: unit test for `run_processing` with multi-server config, asserting `_resolve_publishers` returns all 3 servers.

**9. Scheduled library-scoped jobs fanned out to all servers (`933a26d`)**
- Wrong: scheduled "TV Daily" ran 20 min for 202 items, only 1 ran FFmpeg. Rest were redundant lookups.
- Cause: scheduler dispatch path didn't infer `server_id` from `library_id` like the manual /api/jobs POST does.
- Hindsight: test for scheduled job with `library_id=2` (Plex TV), asserting only TV-owning servers queried.

**10. Sibling-mount probe missing for moved files (`b1022e2`)**
- Wrong: file moved between disks under same logical prefix → infinite SKIPPED_FILE_NOT_FOUND retries.
- Cause: no self-healing when same file exists at alternative mount under same prefix.
- Hindsight: test with multi-mount paths, moving file to sibling mount, asserting `process_canonical_path` rebinds.

### Item lookup & indexing

**11. D32: Jellyfin not-in-library reported as FAILED (`10be97c`)**
- Wrong: Jellyfin publish for unindexed file crashed with "publish-time bookkeeping ValueError"; marked FAILED.
- Cause: no `PublisherStatus.SKIPPED_NOT_IN_LIBRARY` path; exception thrown instead of graceful skip.
- Hindsight: test for multi-server publish where Jellyfin has no item_id hint, asserting `SKIPPED_NOT_IN_LIBRARY` (not exception).

**12. Jellyfin refresh fallback thrashing (`ac5950b`)**
- Wrong: season-pack import triggers one full /Library/Refresh per file, pinning Jellyfin for minutes.
- Cause: no path-based refresh equivalent; cooldown was a workaround, not a fix.
- Hindsight: test asserting `trigger_refresh` uses `/Library/Media/Updated` path-based endpoint when item_id missing but remote_path exists.

**13. 90s silent gaps in multi-phase Jellyfin lookups (`1f09c3a`)**
- Wrong: file showing "Querying library..." for 90s with no log progress.
- Cause: `_resolve_item_id_for` called 5+ times per file across sub-phases with no memoisation; each Jellyfin miss = ~30s cold enumeration.
- Hindsight: test with mocked slow server, asserting per-dispatch memoiser cache hits on 2nd+ call for same server/path.

### UI & state sync

**14. Progress bar bounced (12% → 30% → 12%) (`af116e8`)**
- Wrong: job progress bar visibly jumped up every 3s then snapped down on completion.
- Cause: two emit paths used different formulas (completion: `completed/total`; periodic: `(completed+in_flight_fraction)/total`).
- Hindsight: test with multiple workers mid-FFmpeg emitting via both paths, asserting same percent value.

**15. Worker progress stayed at 0.0% during pre-FFmpeg phases (`933a26d`)**
- Wrong: worker card showed "0.0% / 0.0x" during item-id lookup, indistinguishable from "FFmpeg stuck at 0%."
- Cause: no `ffmpeg_started` flag surfaced to UI.
- Hindsight: test with worker in pre-FFmpeg phase asserting UI renders "Working…" (not "0.0%"), switching only after `ffmpeg_started=True`.

**16. Kill button race on refresh (`5028fb6`)**
- Wrong: user hovers Cancel; UI poll-refresh rebuilds DOM; click registers on stale node; no cancel fires.
- Cause: wholesale `tbody.innerHTML = html` rebuild during hover destroys nodes under cursor.
- Hindsight: jsdom test for loadJobs rebuild logic with simulated hover, asserting tbody defers rebuild while `:hover` is active.

**17. Webhook log showed "none match" for paths that do match (`55d1c81`)**
- Wrong: user sees "Resolving 1 webhook path(s) — none match" even though dispatcher resolved them.
- Cause: breadcrumb check vs dispatcher used different path forms; breadcrumb never applied prefix translation.
- Hindsight: test asserting `_log_webhook_owning_servers` with `webhook_prefixes` shows matching libraries (not "none match").

### SocketIO & API

**18. Failed to fetch / frozen UI (`1873a23`)**
- Wrong: "Failed to fetch" on /api/pause after 20-min jobs; HTTP 500 on simple GET /api/jobs.
- Cause: SocketIO upgraded to WebSocket by default, dead clients pinned gunicorn threads. Previous fix (`allow_upgrades=False`) silently lost in package rename.
- Hindsight: regression test explicitly asserting `allow_upgrades=False` is set on SocketIO server init.

**19. Regenerate checkbox did nothing (`0092f8d`)**
- Wrong: UI checkbox set; job silently skipped FFmpeg; user thought it worked.
- Cause: checkbox set `config.regenerate_thumbnails=True` but no call site wired it to `process_canonical_path`'s `regenerate=` kwarg.
- Hindsight: test for manual trigger with regenerate=True, asserting `process_canonical_path` called with `regenerate=True`.

**20. D36: Bundle-hash cache reverted (`1e7403c → 0faf1cd`)**
- Wrong: perf optimisation shipped, broke prod days later, had to be reverted.
- Cause: no test coverage for cache correctness across sessions; pytest disabled cache via `PYTEST_CURRENT_TEST` env var (fragile); per-item mocks hid stale-entry serving.
- Hindsight: integration test pre-populating cache, running full-library scan, re-analyzing on Plex, re-running scan, asserting cache invalidated.

### Security

**21. Credential leak in /api/jobs (`d92d1b8`)**
- Wrong: /api/jobs accepted `plex_token`, `plex_url`, `plex_config_folder` in body and flowed into worker Config via `setattr`.
- Cause: missing allow-list; any body key merged into config without validation.
- Hindsight: test for /api/jobs POST with credential fields in body, asserting they are stripped (not persisted).

### Tests added that filled gaps after incidents (for reference)
- `tests/conftest.py` — D31-shape guardrails for boundary call assertions (kwargs not just call count).
- `tests/test_output_plex_bundle.py` — end-to-end tests that don't mock `get_bundle_metadata` (D31).
- `tests/test_full_scan_multi_server.py` — matrix coverage for GPU worker slots, multi-server fan-out, pause+cancel flows (D32–D34).
- `tests/test_servers_jellyfin.py` — path-based refresh fallback, trigger_refresh ordering (D32, D38).
- `tests/test_utils.py` — security boundary tests (D31): traversal, symlink, null-byte protection in `_safe_resolve_within`.

### Recurring gap themes
1. **Mocking masks parameter bugs** — tests pass because mock was called once but don't assert *kwargs* (D31, D34 paradigm in `testing.md`).
2. **Single happy-path tests miss matrix cells** — Plex-pin tested but not Emby-pin; "slow path" tested but not "skip-cached path" (D34, D33).
3. **UI race conditions on refresh cycles** — wholesale DOM rebuilds during hover, state changes mid-poll (kill button, progress bar bounce).
4. **Multi-server dispatch bottlenecks** — redundant reverse-lookups not memoised; registry build path didn't include all configured servers.
