# Test Suite Audit — 2026-05-12

Companion to `docs/superpowers/plans/2026-05-12-test-suite-audit.md`. One section per directory; one entry per file within. Severity legend below.

## Severity legend

- **HIGH** — bug-blind test (no SUT contract pinned, only call-count). Fix immediately when found.
- **MED** — matrix gap, internal-helper mock at wrong layer, isolation smell, test-pattern bug that would mask real regressions. Fix in Phase 2 batch.
- **LOW** — naming convention, AAA structure, placeholder copy, unused imports, deprecated typing. Fix inline during Phase 1 read-through.

## Pre-audit baseline (captured 2026-05-12, before any edits)

| Run | Result |
|---|---|
| Full unit suite (default pytest) | 2972 passed, **1 failed** — `tests/test_routes.py::TestJobConfigPathMappings::test_start_job_library_ids_override_sets_plex_library_ids`. Known xdist-pollution flake; passes in isolation. |
| `pytest -m e2e -n 0` | **152 passed** (clean) |
| `pytest -m e2e -n 8` | **152 passed** (clean) |
| `pytest -m e2e -n auto` run 1 | **31 failed, 122 passed** |
| `pytest -m e2e -n auto` run 2 | **29 failed, 123 passed** |
| `pytest -m e2e -n auto` run 3 | **16 failed, 136 passed** |
| Canary `test_run_now_creates_job_in_active_panel` at `-n auto`, 5 runs | **5/5 passed** |

The `-n auto` failures are **non-deterministic** — different tests fail on each run. The canary holds. This matches the documented Playwright IPC stall pattern (playwright#26739, playwright-python#1039) that the canary fix addressed; the remaining ~40 `page.request.X()` backend API callsites across the e2e suite have not been migrated and are presumed susceptible to the same stall under `-n auto` load.

**Verification baseline for the audit:** `pytest -m e2e -n 8` (152/152, stable). `-n auto` results will be reported but not gated on pre-existing flakiness.

## Audit criteria (operational)

Each file is read line-by-line and checked against:

- **A. Naming conventions** — `test_{module}.py`, `Test{ClassName}`, `test_{behavior}_when_{condition}`, AAA pattern. (LOW)
- **B. Bug-blind detection** — mock asserts a call but doesn't pin the SUT-controlled kwargs. (HIGH)
- **C. Matrix-coverage gaps** — branchy SUT function tested with only one cell. (MED)
- **D. Mock layer** — mocks at the project-internal helper layer instead of the HTTP/subprocess boundary. (MED)
- **E. Test isolation** — relies on test order, leaks state. The 19 `_reset_singletons` fixtures are recognised but DEFERRED (out of scope per user). (MED)
- **F. Playwright IPC for backend APIs** — `page.request.X()` used for backend calls that don't need browser cookie state. The canary fix established the swap-to-`requests` pattern; this audit applies it inline where safe. (MED — was LOW-backlog in plan; promoted because pre-audit baseline shows 16-31 `-n auto` failures from this class.)
- **G. Placeholder / lazy patterns** — TBD, "test something works", commented-out code, `assert True`. (LOW)
- **H. Lint / type modernisation** — deprecated `typing.List`, unused imports, ruff warnings. (LOW)

## Summary (filled in post-audit)

| Directory | Files | LOW fixed inline | MED batched | HIGH fixed |
|---|---|---|---|---|
| `tests/e2e/` | 34 | — | — | — |
| `tests/journeys/` | 10 | — | — | — |
| `tests/integration/` | 27 | — | — | — |
| `tests/` (root) | 93 | — | — | — |

---

## tests/e2e/

### tests/e2e/test_dashboard.py (118 lines, 6 methods, 4 classes)

- **Findings:**
  - **MED** (line 106) — `assert any("cpu_threads" in (c or {}) for c in captured)` pins the *key* but not the *value*. After clicking the `+`, `cpu_threads` should be `2`. A regression that sent `cpu_threads: 0` would pass this test. Fix: also assert `c["cpu_threads"] == 2`. Batch in Phase 2.1 (bug-blind kwarg).
  - **MED** (line 105) — hardcoded `wait_for_timeout(300)` for "POST to land". Race-y under load. Should `wait_for_request` instead. Batch in Phase 2.3-equivalent (deterministic waits). Common pattern in e2e file; will be batched suite-wide.
  - **LOW** — none worth fixing inline; file is clean.

### tests/e2e/test_dashboard_modals.py (266 lines, 6 methods, 4 classes)

- **Findings:**
  - **MED** (line 81) — `assert captured, "POST /api/jobs never fired"` pins the call happened, not the body. A regression that sent an empty body would pass. Fix: assert `body.get("library_ids")` or similar (cf. the Jellyfin test at line 167-175 which DOES pin specifics — use that as the template).
  - **MED** (lines 71, 80, 150, 165, 236, 262) — multiple `wait_for_timeout(N)` hardcoded sleeps. Same class as test_dashboard.py:105. Batch suite-wide.
  - **MED** (line 250-266 `test_manual_trigger_dropdown_shows_vendor_in_option_text`) — matrix-coverage gap: only Plex + Jellyfin tested. Emby cell omitted with no docstring explaining why. Either add Emby row or add comment per `.claude/rules/testing.md:90`.
  - Good practice example: lines 167-175 (Jellyfin full-scan test) — pin specific values (`library_ids == ["lib-1"]`, `"server_id" not in body`). This is the template for fixing the line 81 finding.

### tests/e2e/test_folder_picker.py (103 lines, 5 methods, 1 class)

- **Findings:**
  - **MED** (line 92) — `expect(...).not_to_have_value("/")` passes for ANY non-`"/"` value. Vague. The test name says "drills in" so the value should match the clicked folder's path. Tighten to `expect(...).to_have_value("/data")` or similar.
  - **MED** (lines 67, 78, 89, 91) — `wait_for_timeout(300)` × 4. Same race pattern as test_dashboard*. Batch.
  - **LOW** — clean otherwise.

### tests/e2e/test_journey_bif_viewer_with_real_frames.py (261 lines, 2 methods, 1 class)

- **Findings:**
  - **MED, Criterion F** (lines 160-167, 249-253) — uses `page.request.get(...)` for backend API calls (`/api/bif/info`, `/api/bif/frame`). Both pass `X-Auth-Token` header — no browser cookie state needed. Same Playwright IPC stall class as the canary. Fix: swap to `requests`. Batch in Phase 2.F.
  - **Strong assertions throughout**: JPEG magic-byte check, `naturalWidth > 0`, frame count = 5 are all SUT-specific contracts. Excellent test design — use as a reference for what good e2e looks like.

### tests/e2e/test_journey_cancel_kill_running_job.py (191 lines, 3 methods, 1 class)

- **Findings:**
  - **MED, Criterion F** (lines 24-31, 41-44, 81-84, 123-126, 138-141, 173-176) — six `page.request.X()` backend API calls. None need browser cookie state. Swap to `requests`. Batch in Phase 2.F.
  - **Strong assertions**: line 147 pins exact log-line text ("Cancellation requested by user"); line 186 pins exact DOM element absence. Good contracts.

**Batch 1 summary (5 files, 939 lines):** 0 HIGH, 11 MED, 0 LOW. The dominant patterns are (a) hardcoded `wait_for_timeout` sleeps and (b) `page.request.X()` for backend API calls. No inline LOW fixes warranted in this batch — files are already clean on naming, AAA, imports, types.

### tests/e2e/test_journey_edit_existing_server.py (200 lines, 3 methods, 1 class)

- **Findings:**
  - **MED, Criterion F** (line 135-138) — `page.request.get(/api/servers/{id}, X-Auth-Token)` — backend API call, no browser-cookie dependency. Swap to `requests` in Phase 2.F.
  - **Strong on-disk persistence assertions**: lines 150-159 + 190-197 read `settings.json` directly to prove the PUT actually flushed to disk. Excellent — catches the "PUT updated in-memory state but never wrote to disk" bug class explicitly.
- **LOW**: none.

### tests/e2e/test_journey_jellyfin_wizard_full.py (112 lines, 1 method, 1 class)

- **Findings:**
  - **CLEAN** — heavy use of `_mocks` helpers (HTTP boundary mocking), specific value assertions (`type == "jellyfin"`, `name`, exact token), good docstrings explaining the test's signal vs. its limitations.
  - No `page.request` backend API calls (uses `capture_*` helpers via `page.route`).
- **LOW**: none.

### tests/e2e/test_journey_live_job_lifecycle.py (206 lines, 2 methods, 1 class)

- **Findings:**
  - **MED, Criterion F** (lines 80, 141, 161, 179) — 4 `backend_real_page.request.X()` backend API callsites. Same Playwright IPC risk as canary. Swap in Phase 2.F.
  - **MED, Criterion F-SocketIO** (lines 58-69) — opens a Playwright-driven SocketIO subscription (`page.evaluate(io('/jobs', ...))`) before posting jobs. This is the exact pattern the canary fix eliminated. Under `-n auto` this is risky. The canary moved to polling `GET /api/jobs?page=0`; this test could too, but it specifically verifies the SocketIO emit which has its own value. **Decision needed**: keep SocketIO observation (and accept `-n auto` flakiness) OR add a parallel `requests` poll for the job-state check.
  - Strong assertions: line 138 (job id match), line 147 (stats.total >= 1). Good.

### tests/e2e/test_journey_notifications_lifecycle.py (218 lines, 5 methods, 1 class)

- **Findings:**
  - **MED, Criterion F** (lines 66, 93, 99, 106, 130, 148, 207, 214) — 8 `backend_real_page.request.X()` callsites. None need browser-cookie state. Swap in Phase 2.F.
  - **Strong on-disk + endpoint cross-check** (lines 138-149): tests that permanent dismiss writes to `settings.json` AND that the GET endpoint filters dismissed entries. Both contracts pinned in one test.
  - **MED, Criterion B** (lines 102-103) — `dismiss_resp.json().get("ok") is True` is good. But could also pin response shape more completely (id of dismissed item echoed in the response would be a stronger contract). Optional polish.

### tests/e2e/test_journey_pause_resume_scan.py (224 lines, 4 methods, 1 class)

- **Findings:**
  - **MED, Criterion F** (lines 36, 44, 53, 71, 75, 83, 112, 118, 131, 153, 189, 211) — 11 `backend_real_page.request.X()` callsites. None need browser-cookie state. Largest concentration in any single file so far. Swap in Phase 2.F.
  - **MED, Criterion F-SocketIO** (lines 173-187) — SocketIO subscription pattern, same as test_journey_live_job_lifecycle.
  - **Strong contracts**: lines 198-202 pin `paused=True` event payload; lines 219-222 pin `paused=False` for resume. Good.
  - **Strong negative assertion**: line 145 (`last_status != "running"`) — pins the "must not be stuck running" contract specifically. The docstring explains why this exact negative shape catches the bug.

**Batch 2 summary (5 files, 960 lines):** 0 HIGH, ~36 MED (dominated by page.request callsites — 31 in this batch alone), 0 LOW. Pattern is clear: every backend-real test that interacts with API endpoints uses `page.request.X()` instead of `requests`. The Phase 2.F batch will mass-swap these. No inline LOW fixes warranted; files are well-structured otherwise.

### tests/e2e/test_journey_schedule_lifecycle.py (286 lines, 4 methods, 1 class)

- **Findings:**
  - **CLEAN** — this is the canary, already migrated to `requests` in commit `43a9db7`. Module-level `_AUTH_HEADERS` + `_API_TIMEOUT` + verbose comment block explaining the migration rationale. **Reference template** for the Phase 2.F batch — every other backend-real file should adopt the same shape.
  - **LOW** (lines 96-100, 214-218, 248-252) — three non-canary tests still inject `backend_real_page` as a fixture but no longer use it (the test bodies use `requests` directly via the helpers). Could be removed for symmetry with `test_run_now_creates_job_in_active_panel` (line 137-140 which dropped the fixture). Defer to Phase 2 batch (unused-fixture cleanup) — low priority.
  - Strong negative assertion: line 282 (`status_code == 404` for double-delete). Good.

### tests/e2e/test_journey_schema_migration_boot.py (261 lines, 3 methods, 1 class)

- **Findings:**
  - **CLEAN** — uses `subprocess.Popen` directly (custom fixture, not `backend_real_app`), then verifies via on-disk `settings.json` read + `urllib.request` for the `/api/servers` cross-check (lines 196-208). NO `page.request` use at all.
  - **Strong contract pinning**: every test asserts specific schema migration outcomes (frame_reuse defaults, media_servers synthesis, ttl_minutes preserved on no-op boot). Best-practice template for migration testing.
  - Negative-case test (`test_boot_with_already_current_schema_is_a_noop`) deliberately seeds a non-default value (ttl_minutes=999) and asserts it survives — pins the "off-by-one re-runs migration" bug class explicitly.
  - **LOW**: none.

### tests/e2e/test_journey_settings_save_reload.py (136 lines, 2 methods, 1 class)

- **Findings:**
  - **MED, Criterion F** (line 106-113) — `backend_real_page.request.put(...)` for the log-level endpoint. Swap to `requests` in Phase 2.F.
  - **Strong on-disk poll** (lines 53-75): polls the file until all three values appear, with a detailed failure message including the file contents. This is the right approach for "did the write actually flush" testing.
  - **LOW** (line 116): `if resp.status == 404: pytest.skip(...)` — silent skip when endpoint missing. Reasonable but a comment explaining when this branch is expected to fire would help. Not blocking.

### tests/e2e/test_journey_webhook_to_dashboard.py (207 lines, 2 methods, 1 class)

- **Findings:**
  - **MED, Criterion F** (lines 31, 91, 113, 143) — 4 `page.request.X()` callsites. None need browser-cookie state. Swap.
  - **MED, Criterion F-SocketIO** (lines 64-78, 168-182) — SocketIO subscription pattern, same as test_journey_live_job_lifecycle. The TEST PURPOSE here is verifying SocketIO emit for webhook-spawned jobs, so dropping it isn't trivial. Decision deferred to Phase 2 — see batch 2 notes.
  - **Strong contracts**: pins `file_count == 1`, `source == "sonarr"`, terminal state, debounce key extraction. The second test (`test_natural_debounce_fires_without_explicit_fire_now`) explicitly tests the silent-failure path where the Timer is started but the callback never runs. Excellent.

### tests/e2e/test_login_page.py (29 lines, 3 methods, 1 class)

- **Findings:**
  - **MED, Criterion B** (line 15) — `assert focused in (...) or page.locator("#token").is_visible()` — the `or` clause makes this pass when *either* the autofocus check OR the locator is visible. Effectively tests "the token input exists somewhere on the page" which is the wrong contract for a test named `test_token_input_is_autofocused`. Fix: assert the autofocus specifically (`assert focused == "token"`) and let the test fail loudly if autofocus regressed.
  - **MED, Criterion B** (line 22) — `to_contain_text("didn", ...)` is a vague substring match. Probably matches "didn't match" but would also pass on "didn", "didnt", "didns" — any string with that fragment. Tighten to the actual expected string ("didn't match" or whatever the canonical copy is).
  - **LOW**: file is very short; finds are about assertion specificity, not structure.

**Batch 3 summary (5 files, 919 lines):** 0 HIGH, ~10 MED (mostly page.request swaps deferred to Phase 2.F + 2 assertion-specificity issues in login_page), 1 LOW (unused-fixture cleanup in schedule_lifecycle). Notable wins: schedule_lifecycle is the reference template post-canary-fix; schema_migration_boot is a model for migration testing.

### tests/e2e/test_logs_page.py (35 lines, 2 methods, 1 class)

- **Findings:**
  - **MED, Criterion B** (line 27) — `assert authed_page.locator("h1, h2, h3, .container-fluid").first.is_visible()` — the CSS selector matches any of 4 elements; the test passes if ANY of `<h1>`/`<h2>`/`<h3>`/`.container-fluid` is visible. The test is named `test_logs_page_loads` but doesn't actually pin a logs-specific element. Tighten to `#logsContent` or whatever the page-specific identifier is.
  - **Not Criterion F**: line 31's `page.request.get(/logs)` hits a *page* route (HTML response), not an API. Auth-redirect test; legitimate use.

### tests/e2e/test_preview_inspector.py (202 lines, 5 methods, 3 classes)

- **Findings:**
  - **CLEAN** — all routing via `authed_page.route(...)` (HTTP boundary mocks). Strong contract pinning at lines 173-179: explicitly asserts the URL contains `index-sd.bif` AND NOT `.mkv`, pinning the exact regression class (Plex click-through sending the wrong path).
  - **Cover-the-matrix**: covers Plex (line 38), multi-vendor 3-cell (line 64). Click-through test is Plex-specific because the regression was Plex-specific — appropriate scoping.

### tests/e2e/test_schedules.py (202 lines, 3 methods, 2 classes)

- **Findings:**
  - **MED** (lines 121, 128, 169, 176, 196) — 5 `wait_for_timeout(N)` hardcoded sleeps. Same race pattern; batch suite-wide.
  - **Cover-the-matrix**: line 200-202 covers all 3 vendor cells (PLEX, EMBY, JELLYFIN). Non-Plex schedule tests cover JF + Emby (2 cells; Plex covered separately in the wizard tests). Good.
  - **Strong contracts**: server_id pinning (lines 132, 180), job_type pinning (lines 134, 183) — pins both the SUT-controlled fields.
  - All routing via `authed_page.route(...)` ✓. No `page.request` backend API calls.

### tests/e2e/test_servers_jellyfin_trickplay.py (116 lines, 3 methods, 1 class)

- **Findings:**
  - **CLEAN** — covers all 3 glyph states (critical/recommended/ok) in 3 tests; complete matrix coverage with specific class-name assertions (`text-danger`, `text-warning`, `text-success`). Tooltip content also pinned. Reference example for "branchy SUT, every cell covered" per `.claude/rules/testing.md:83-91`.

### tests/e2e/test_servers_page.py (253 lines, 15 methods, 5 classes)

- **Findings:**
  - **MED** (lines 62, 109, 120, 130, 209, 221, 229, 252) — 8 `wait_for_timeout(N)` sleeps. Same pattern.
  - **Not Criterion F** (lines 146, 160, 168) — three `page.request.X()` calls in `TestServersAPIIntegration` *are testing the API surface itself*, not using it for setup. This is the legitimate use of `page.request` — direct API contract verification. Could migrate to `requests` for IPC-stall safety but not strictly wrong.
  - **MED** (line 175) — `assert response.status in (400, 404)` accepts two distinct contracts. The docstring justifies it ("both prove the route exists with sane validation") which is reasonable, but ideally the test would seed a real server so the validation step is reached deterministically. Borderline — not blocking.
  - **MED** (line 64-65) — `if value: assert "/api/webhooks/incoming" in value` — only asserts when the input is non-empty. A regression that returns an empty string would silently pass. Should fail loudly: drop the `if value:` guard and let the empty case fail visibly.
  - **Strong contracts** throughout the Add-Server flows: pins type + url after each save.

**Batch 4 summary (5 files, 808 lines):** 0 HIGH, ~15 MED (mix of wait_for_timeout + 2 assertion-specificity), 0 LOW. test_servers_jellyfin_trickplay.py is a reference example for matrix coverage of a 3-state branch. test_preview_inspector.py pins regression class with negative-pattern assertions.

### tests/e2e/test_settings_page.py (161 lines, 10 methods, 4 classes)

- **Findings:**
  - **MED, Criterion A** (lines 113, 132, 158-161) — `mock_token_set` / `mock_token_regenerate` / `capture_settings_backups_restore` return a *truthy-list* sentinel. `assert captured, "POST … never fired"` only proves the call happened; it doesn't pin the body payload. `test_set_custom_token_matching_succeeds` doesn't assert that the body's `token` field equals `"brand-new-tok-1"` — a regression that sends an empty/wrong token would still pass call-count. The settings/backups test DOES pin `file` + `backup` (lines 159-161); the token tests should do the same.
  - **MED, Criterion B** (lines 93-113) — `test_set_custom_token_matching_succeeds` is the only auth test. Matrix gaps: (a) tokens that don't match across the two fields, (b) `mock_token_set(ok=False)` failure path, (c) regenerate-when-cancelled. Three uncovered cells for a security-sensitive code path.
  - **MED, Criterion G** (lines 112, 131, 157) — three `wait_for_timeout(500)` after `evaluate('void X()')`. Replace with `expect(callable: lambda: bool(captured)).to_be_truthy()` polling, or use `authed_page.wait_for_request()` to deterministically catch the POST.
  - **LOW** (line 64) — `assert opacity == "0.5"` couples to a CSS inline-style value. If production switches to a class-based fade, this breaks. Minor.

### tests/e2e/test_settings_steppers.py (71 lines, 3 methods, 1 class)

- **Findings:**
  - **CLEAN** — parametrized smoke + 2 increment tests. Deterministic `expect(...).to_have_value(...)` assertions. No `page.request`, no `wait_for_timeout`. Reference example for parametrized smoke + per-target verification.

### tests/e2e/test_theme_toggle.py (40 lines, 2 methods, 1 class)

- **Findings:**
  - **LOW, Criterion G** (lines 27, 38) — two `wait_for_timeout(200)`. Could be `expect(authed_page.locator("html")).to_have_attribute("data-bs-theme", ...)` but the 200ms is tiny; not load-bearing for parallel-flake.
  - **LOW, Criterion B** — second test asserts `stored in ("light", "dark")` — both branches accepted, doesn't pin which one resulted from the click. Mostly OK because the *first* test pins the flip; this is the persistence test. Borderline.

### tests/e2e/test_ui_hover_defer.py (168 lines, 2 methods, 1 class)

- **Findings:**
  - **CLEAN** — pins the production guard at app.js:1786 (hover-defer for the Active Jobs container) with a sentinel-survival check AND the contract-floor mirror (rebuild DOES happen when not hovered). Reference example for pinning a positive AND negative contract.
  - Heavy JS injection but every block is commented with WHY (matches `.claude/rules/commenting.md` policy).

### tests/e2e/test_ui_workers_panel.py (295 lines, 5 methods, 3 classes)

- **Findings:**
  - **CLEAN** — covers all four cells of the worker-card matrix (in-place update, vanish-by-key, pre-FFmpeg phase rendering, FFmpeg-started normal, fallback-active badge). Each test pins both the positive contract AND its inverse where applicable. Reference example for branchy-SUT coverage per `.claude/rules/testing.md:83-91`.

**Batch 5 summary (5 files, 735 lines):** 0 HIGH, ~6 MED (4 in settings_page, 2 LOW in theme_toggle), 0 LOW notable. test_ui_hover_defer + test_ui_workers_panel are gold-standard reference files for render-contract pinning. test_settings_page has the only real assertion-specificity gap (token tests don't pin payload).

## tests/journeys/

_(filled in during Phase 1B)_

## tests/integration/

_(filled in during Phase 1C)_

## tests/ (root)

_(filled in during Phase 1D)_

## conftest.py audit

_(filled in during Phase 1.5)_

---

## Out of scope (intentionally deferred)

- **The 19 `_reset_singletons` fixtures** spread across the test suite. Each is a manual scrub of module-level globals in production modules (`webhooks._pending_timers`, `retry_queue._singleton`, `settings_manager._settings_manager`, etc.). The proper fix is migrating those singletons to `app.extensions["..."]` (Flask-standard pattern). User declined this refactor (~3-5 day cost, 175 `get_settings_manager` callsites alone) earlier in the same session.
- **The 8 reset helpers in the production tree** (`reset_retry_scheduler`, `reset_webhook_debounce`, `reset_settings_manager`, `reset_job_gate`, `reset_dispatcher`, `reset_session`, `reset_frame_cache`, `reset_dismissed_notifications`). They exist as the test-side companions to the module-global state. They go away naturally if the singletons migrate to `app.extensions`; not before.
- **The `api_test.py` test-only Flask blueprint.** Gated by `MPG_TEST_RESET=1`. Industry-standard pattern for live-server E2E (Cypress docs, Full Stack Open). Verified during prior research to NOT be a band-aid.

These items are documented here once. Per-file occurrences will not be re-flagged in the per-file sections below.
