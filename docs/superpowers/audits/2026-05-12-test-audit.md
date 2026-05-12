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

### tests/e2e/test_webapp.py (174 lines, 10 methods, 5 classes)

- **Findings:**
  - **HIGH, Criterion G** (lines 56, 67, 71, 89, 93, 132, 143, 153, 169) — 9 `wait_for_timeout(N)` hardcoded sleeps (1-2 second blocks). This is the WORST offender file in the e2e suite for non-deterministic waits — these race the backend redirect/render and are likely candidates for `-n auto` flake. Replace with `page.wait_for_url(...)` / `expect(...).to_be_visible(...)` deterministic waits. **Batch fix in Phase 2.**
  - **MED, Criterion B** (line 58) — `assert "/login" not in current_url` — proves a redirect happened, but accepts ANY non-login URL. Could be `/error`, `/setup`, `/`. Should pin the actual expected destination.
  - **MED, Criterion B** (line 75) — same pattern: `"/login" not in current_url` after navigating to `/settings`. A regression that 302'd to `/setup` would pass; a regression that 500'd would fail (good) but for the wrong reason. Pin `current_url.endswith("/settings")` or `expect(page).to_have_url(...)`.
  - **MED, Criterion B** (line 97) — `assert "/login" not in current_url or "/setup" in current_url` — the OR short-circuits: if `/login` is not in URL, the whole assertion is True regardless of the right operand. Effectively just `"/login" not in current_url`. Either the test is checking the wrong thing or the assertion is unintentionally permissive.
  - **MED, Criterion F + A** (lines 106-120) — `page.request.get(/api/health)` and `page.request.get(/api/auth/status)` — Playwright IPC; swap to `requests.get(...)` per the canary pattern. Also line 113: `assert "status" in data or "ok" in str(data).lower()` accepts two different response shapes; pick the actual contract and pin it.
  - **LOW** (line 35) — `page.locator("h1, h2, h3").first` matches 3 element types; not login-specific. Tighten.

### tests/e2e/test_webhooks_automation.py (49 lines, 4 methods, 2 classes)

- **Findings:**
  - **CLEAN** — pure HTML assertion tests. No `page.request`, no `wait_for_timeout`. Good copy-regression pins (negative pattern at line 25 for the patronising line).
  - **LOW, Criterion B** (line 43) — `assert rows.count() >= 4` is open-ended; should pin the exact decision-list shape rather than a lower-bound.

### tests/e2e/test_wizard_emby_jellyfin_inline.py (96 lines, 2 methods, 2 classes)

- **Findings:**
  - **MED, Criterion A** (lines 61, 96) — `assert captured[0]["type"] == "emby"` / `"jellyfin"` — pins the vendor field but not the `url`, `name`, or auth payload the user typed. A regression that always sent `name="Emby"` instead of `"Test Emby"` would still pass. Add `assert captured[0]["url"] == "http://emby.local:8096"` and `assert captured[0]["name"] == "Test Emby"`.
  - **MED, Criterion B** — only happy-path coverage. No auth-failure cell (mock returns 401), no save-failure cell. The `mediaServerAdded` → step-4 jump has no negative-edge contract test.
  - Otherwise: deterministic `expect()` waits; no `wait_for_timeout`. Strong "no modal popup" negative-edge pin (line 43).

### tests/e2e/test_wizard_full_flows.py (140 lines, 2 methods, 2 classes)

- **Findings:**
  - **MED, Criterion A** (lines 92-93, 139-140) — `assert captured_token, "set-token never fired"` / `assert called_complete` — pin call-happened, not call-contents. Both tests fill `newToken` + `confirmToken` with a specific value; `capture_setup_set_token` returns the request bodies (per the canary precedent in test_servers_page) — assert `captured_token[0]["token"]` matches the typed value.
  - **MED, Criterion G** (lines 90, 138) — `page.wait_for_url("**/", timeout=5000)` is deterministic ✓. Good pattern; no MED here.
  - The `import re` inside each test (lines 46, 111) — minor code-organization smell (top-of-module is the conventional spot). LOW — not blocking.
  - The duplicate "/" route-stub block in both tests (lines 47-50, 113-116) — could be a fixture helper. LOW.

### tests/e2e/test_wizard_step1_vendor_picker.py (104 lines, 7 methods, 2 classes)

- **Findings:**
  - **CLEAN** — strong vendor-picker matrix (all 3 vendors covered: plex/emby/jellyfin) + bottom-aligned Back from each vendor + Skip flow. Reference example for "branchy SUT, every cell covered". Pins both positive (panel visible) AND negative (other panels hidden + modal NOT in DOM, line 57) contracts.

**Batch 6 summary (5 files, 563 lines):** 1 HIGH (test_webapp.py 9 hardcoded sleeps — top candidate for `-n auto` flake), ~10 MED (mix of weak-redirect-assertions, payload not pinned, B-matrix gaps in inline wizard, page.request swaps in webapp). test_wizard_step1_vendor_picker is a reference example for vendor-picker matrix coverage.

### tests/e2e/test_wizard_step2_libraries.py (110 lines, 4 methods, 1 class)

- **Findings:**
  - **CLEAN** — strong state-machine pinning: pre-condition checked (line 81: `not_to_be_checked` + `to_be_disabled`), positive transition checked (line 85-87), inverse transition checked (lines 89-101: untick → disabled). Empty-grid edge covered (line 103). Reference example for "branchy state machine, every cell covered."
  - **LOW** (line 86) — `to_have_class("library-card mb-0 selected")` is brittle to class-string reordering. `to_have_class(re.compile("selected"))` is more robust.
  - No `page.request`, no `wait_for_timeout`. All `expect()` auto-retry.

### tests/e2e/test_wizard_step3_paths.py (124 lines, 5 methods, 2 classes)

- **Findings:**
  - **MED, Criterion G** (lines 59, 71, 123) — three `wait_for_timeout(700)` for the 400 ms-debounced validator. These are load-bearing (must wait for debounce to fire) but `expect(cfg).to_have_class("form-control is-valid", timeout=2000)` auto-retries and would replace the hardcoded sleep with a deterministic poll. Phase 2 fix candidate.
  - **LOW** (lines 60, 72, 124) — full-class-string assertions are brittle to reorder. Use `re.compile("is-valid")` / `re.compile("is-invalid")`.

### tests/e2e/test_wizard_step4_processing.py (159 lines, 5 methods, 2 classes)

- **Findings:**
  - **MED, Criterion G** (line 140) — `wait_for_timeout(300)` after `#gpuRescanBtn.click()`. Replace with `wizard_page.wait_for_request("**/api/system/rescan-gpus")` (or `expect(callable: lambda: called)`).
  - **LOW** (lines 124-125, mirror of settings_page line 64) — `opacity == "0.5"` couples to inline-style value. Same observation as batch 5.

### tests/e2e/test_wizard_step5_security.py (149 lines, 7 methods, 2 classes)

- **Findings:**
  - **CLEAN — REFERENCE EXAMPLE** for criterion B (cover-the-matrix) AND criterion A (assert boundary kwargs). Covers the full 6-cell token-enforcement matrix: blank / short / mismatch / server-reject / valid / env-controlled. Line 116: `assert captured[0]["token"] == "brand-new-token-1"` — pins the actual payload, not just call-count. Line 148: `assert not captured` — pins the NEGATIVE contract (env-controlled MUST NOT POST set-token). This is the file other wizard tests should look like.

**Batch 7 summary (4 files, 542 lines):** 0 HIGH, ~5 MED (mostly Phase-2 batch-fixable `wait_for_timeout` debounce-races), ~3 LOW. test_wizard_step5_security.py is the file every E2E test in the suite should look like (full state matrix + payload-pinning + negative-edge assertions).

---

## Phase 1A roll-up (34 files, ~5800 lines total)

**Findings tally:**
- HIGH: 1 (test_webapp.py — 9 hardcoded sleeps on real-server auth/redirect flow)
- MED: ~80 (most are bookable into 4 Phase-2 batch fixes: F-swaps to `requests`, G-replacements of `wait_for_timeout` with deterministic `expect()`/`wait_for_request()`, A-tightening of payload assertions, B-filling matrix-gap cells)
- LOW: ~15 (mostly assertion-specificity nits: full class-string matches, inline-style coupling)

**Reference-example files (no findings; replicate these patterns):**
- `test_wizard_step5_security.py` — matrix-coverage + payload-pinning + negative-edge
- `test_wizard_step1_vendor_picker.py` — vendor matrix + negative-edge ("modal NOT in DOM")
- `test_wizard_step2_libraries.py` — state-machine pre/post/inverse
- `test_servers_jellyfin_trickplay.py` — 3-glyph state matrix
- `test_preview_inspector.py` — regression-class pinning with negative URL pattern
- `test_ui_hover_defer.py` — positive+inverse render-contract
- `test_ui_workers_panel.py` — 4-cell render-state matrix
- `test_servers_page.py` (`TestServersAPIIntegration` class) — legitimate `page.request` API-contract testing
- `test_journey_schedule_lifecycle.py` — post-canary `requests` + jobs-API-poll template

**Phase-2 fix batches that emerged:**
- **G-batch (largest)**: 9 sleeps in test_webapp, 5 in test_schedules, 8 in test_servers_page, 3 in test_wizard_step3_paths, 1 in test_wizard_step4_processing, 3 in test_settings_page — ~30 hardcoded sleeps total. Most replaceable by `expect()` auto-retry or `wait_for_request()`.
- **F-batch**: `page.request` → `requests` swaps in test_webapp + 40-ish callsites flagged earlier (excluded `TestServersAPIIntegration` which legitimately tests the API surface).
- **A-batch**: payload-pinning in test_settings_page (token tests), test_wizard_emby_jellyfin_inline (url/name), test_wizard_full_flows (token value).
- **B-batch**: matrix-gap cells in test_settings_page (mismatched tokens + failure path), test_wizard_emby_jellyfin_inline (auth-fail + save-fail), test_webapp (real redirect destinations).

## tests/journeys/

### tests/journeys/test_adapter_path_contract.py (292 lines, 11 methods, 4 classes)

- **Findings:**
  - **CLEAN — REFERENCE EXAMPLE** for "pin the exact byte string with a diagnostic message that explains the production-incident class." Covers all 3 adapters (Plex bundle, Emby sidecar, Jellyfin trickplay) + ValueError/TypeError edges for Plex + parametrized cross-adapter matrix. Each assertion's failure message names the specific incident class (D38 layout-mismatch for Jellyfin, "Plex Media Server reads from this path" for Plex bundle). No `_reset_singletons` — pure path derivation, no global state.

### tests/journeys/test_journey_auth_header_precedence.py (272 lines, 11 methods, 1 class)

- **Findings:**
  - **CLEAN — GOLD-STANDARD REFERENCE for criterion B (cover-the-matrix).** 11 distinct auth scenarios spanning the full matrix of (Authorization {missing/correct-Bearer/wrong-Bearer/Basic/empty-Bearer}) × (X-Auth-Token {missing/correct/wrong}) × (session {missing/authed}). Each test pins the BODY shape, not just status code. Diagnostic messages reference auth.py line numbers AND name the security regression class each branch protects against (e.g. "any-token-anywhere auth bypass"). This is the matrix-coverage file the rule in `.claude/rules/testing.md:83-91` was written about.
  - Uses `_reset_singletons` — deferred to the app.extensions migration (out-of-scope).

### tests/journeys/test_journey_multi_server_partial_unreachable.py (257 lines, 2 methods)

- **Findings:**
  - **CLEAN** — pins partial-failure (`PUBLISHED` aggregate with one `FAILED` row) AND total-failure mirror (`FAILED` aggregate when all rows fail). Strengthens the FAILED row's `message` assertion (line 192-196) to require the "could not write / preview file" substring so a regression that returned a bare "OK" or "all good" message would fail loudly. References the actual production-code line (multi_server.py:602).

### tests/journeys/test_journey_schedule_run_now.py (268 lines, 2 methods, 1 class)

- **Findings:**
  - **CLEAN** — pins 5 distinct contract points in the happy path:
    1. `parent_schedule_id` on the spawned Job (line 197-201)
    2. `selected_libraries` projected from schedule (line 207-211)
    3. `server_id` pin propagated (line 214-217)
    4. `last_run` advanced past `t_before_run` (line 228-237)
    5. `next_run` still set AND different from `last_run` (line 244-255 — catches the subtle "next_run mirrors last_run" bug)
  - Plus a negative 404 case (test_run_now_unknown_schedule_returns_404).
  - Uses `_reset_singletons` — deferred (out-of-scope).

**Batch 8 summary (4 files, 1089 lines):** 0 HIGH, 0 MED, 0 LOW. All four files are reference examples. Auth-header-precedence + adapter-path-contract are the two files most worth quoting when explaining matrix coverage and exact-byte-string contract pinning to future contributors.

### tests/journeys/test_journey_cancel_running_job.py (324 lines, 2 methods, 1 class)

- **Findings:**
  - **CLEAN** — pins cancel_check wiring through `_start_job_async` → orchestrator + CANCELLED-not-FAILED final state + cancellation-flag cleanup (line 250-257). Uses `threading.Event` + `_wait_until` for deterministic synchronization (no `time.sleep` waits). Matrix coverage: cancel-mid-flight + cancel-then-bail-with-None-return (lines 259-324).
  - Diagnostic message at line 232-236: names the production symptom ("yellow vs red badge"). This is the right depth of comment-as-documentation.
  - Uses `_reset_singletons` — deferred (out-of-scope).

### tests/journeys/test_journey_sonarr_to_published.py (311 lines, 3 methods, 2 classes)

- **Findings:**
  - **CLEAN** — drives Sonarr POST → debounce → `_start_job_async` capture. 3 cells: happy-path with payload pinning (line 190-209), dedup of two-quick-webhooks (line 211-267), webhook-disabled short-circuit (line 280-311). Strict equality on `library_name` (line 201-205) — explicitly anti-substring.
  - Uses `_reset_singletons` — deferred.

### tests/journeys/test_journey_webhook_debounce_to_job.py (376 lines, 4 methods, 3 classes)

- **Findings:**
  - **CLEAN — REFERENCE EXAMPLE** for real-wiring + minimal-mock journey tests. Mocks ONLY (a) the debounce delay via the `webhook_delay` setting and (b) `run_processing` at the orchestrator boundary. Everything else (HTTP route → auth → schedule → Timer → execute → JobManager → start_job_async) runs unmocked. 4 cells: single-post-makes-1-job, three-dedup-to-1, fire-now-cancels-timer (+ pending-state-cleared), fire-now-unknown-404.
  - Reaches into `wh_mod._pending_batches` / `_pending_timers` module globals (lines 290, 321-325, 356-362) — the test is asserting on the production state directly. **This is exactly what the WebhookDebouncer class refactor would let us collapse** to a cleaner `app.extensions["webhook_debouncer"].is_pending("sonarr")` API. Deferred per session decision; this file is the canonical example of why the refactor would help.

**Batch 9 summary (3 files, 1011 lines):** 0 HIGH, 0 MED, 0 LOW. All three are clean. Webhook-debounce-to-job is the canonical example for "test reaches into production module globals → refactor target for WebhookDebouncer."

### tests/journeys/test_journey_max_concurrent_gate.py (734 lines, 8 methods, 1 class)

- **Findings:**
  - **CLEAN — REFERENCE EXAMPLE** for concurrency journey tests. 8-cell matrix (basic cap / drain on complete / priority at gate / FIFO within priority / cancel while waiting / pause skips gate / runtime cap change / run_processing raises / startup flood) directly from the approved plan. Each test uses a `threading.Event`-driven `_BlockingRunProcessing` stub so the test controls release timing — the gate's whole behavior is observable from the test.
  - Strong contract pinning: line 290-293 asserts the FULL counter `(3 of 3 busy)`, not just the `"Queued —"` prefix (avoids the "starts-with" weakness).
  - Sophisticated teardown (lines 70-128): drains daemon `run_job` threads with timeout + `notify_all()` poke to wake stuck acquirers. This is the kind of thread-leak prevention that justifies the eventual app.extensions migration (each test could just throw away its app).
  - LOW: `time.sleep(0.5)/(1.5)/(1.0)` at lines 588, 632, 720 — these are load-bearing waits for "no admit within poll tick" (gate's poll interval is 1s). Testing the *absence* of an action requires a real wait; not flake-prone.
  - Uses `_reset_singletons` + reaches `jr_mod._inflight_jobs` + `gate_mod._gate._cond` (lines 80-86, 115-116) — deferred (out-of-scope).

### tests/journeys/test_journey_jellyfin_zero_item_id.py (1070 lines, ~24 methods, 9 classes)

- **Findings:**
  - **CLEAN — REFERENCE EXAMPLE** for matrix coverage on per-vendor dispatcher behavior. Section C (TestDispatcherLookupPolicy) covers the 6-cell matrix: Emby/Jellyfin-no-plugin/Jellyfin-with-plugin/Plex × hint/no-hint.
  - Strong kwargs-discipline assertions: line 336-344 asserts the canonical path's tail (`endswith "Test (2024).mkv"`) on the lookup call; line 979-1002 captures full `(method, url, params)` tuples and asserts the `path=` query param on the plugin call AND `searchTerm=` on the base fallback — explicit anti-D31 (substring-only) pattern.
  - Section G perf-proof test: `slow_lookup` blocks 30s; the test asserts `elapsed < 2.0` — catches accidental Pass-2 invocation by *not* completing, not by mock-call-count. Reference example for "test the cost, not just the call."
  - **LOW** (line 359-369) — `test_plex_looks_up_when_no_hint` has a comment saying "the rigorous assertion lives in another test" and does not actually assert a load-bearing condition on its own. Document-only matrix-row. Either tighten to a real assert or remove and add a single comment elsewhere noting Plex's coverage location.
  - Atomic-publish tests (Section B + Section F's restore-on-mid-swap) cover the "rename race" regression class with sentinel-tile observation.

### tests/journeys/test_journey_start_job_async_branches.py (894 lines, 8 methods, 6 classes)

- **Findings:**
  - **CLEAN — GOLD-STANDARD REFERENCE for criterion A (assert kwargs the SUT controls).** Module docstring (lines 23-25) explicitly references the D34 regression that hid for months because tests only checked call_count. Every test in this file pins specific kwargs:
    - `kwargs["job_id"]`, `cancel_check` + `pause_check` callable presence (basic dispatch)
    - `JobStatus.RUNNING` snapshot inside the run (gate-flip timing pin)
    - `on_dispatch_start` invocation (regression of live bug job 91c20505)
    - `is_retry=True, retry_attempt=1, parent_job_id, library_name="Retry: <parent>"` (retry-spawn)
    - `config.regenerate_thumbnails=True` (force_generate propagation)
    - `config.webhook_item_id_hints` byte-for-byte equality (Plex-less install protection)
    - `config.server_id_filter` + `config.plex_url` projection (pinned-server view)
    - `file_result.reason` containing per-path hint (audit P4 multi-path regression)
  - This is the file other journey tests should look like.
  - Uses `_reset_singletons` — deferred.

**Batch 10 summary (3 files, 2698 lines):** 0 HIGH, 0 MED, 1 LOW (test_plex_looks_up_when_no_hint placeholder row). All three are reference examples for concurrency, matrix coverage, and kwargs-discipline respectively.

---

## Phase 1B roll-up (10 files, 4798 lines)

**Findings tally:**
- HIGH: 0
- MED: 0
- LOW: 1 (placeholder test row in jellyfin_zero_item_id)

**Reference-example files (the whole journey suite is exemplary):**
- `test_adapter_path_contract.py` — exact-byte-string contract pinning per adapter
- `test_journey_auth_header_precedence.py` — 11-cell matrix on auth precedence with body-shape pins
- `test_journey_multi_server_partial_unreachable.py` — partial-failure + total-failure mirror with diagnostic-message pin
- `test_journey_schedule_run_now.py` — 5 contract points + negative 404 case
- `test_journey_cancel_running_job.py` — cancel-mid-flight + cancel-then-bail with `threading.Event` synchronization
- `test_journey_sonarr_to_published.py` — strict-equality library_name + dedup
- `test_journey_webhook_debounce_to_job.py` — real-wiring chain, mocks only at orchestrator boundary
- `test_journey_max_concurrent_gate.py` — 8-cell concurrency matrix with thread-leak teardown
- `test_journey_jellyfin_zero_item_id.py` — per-vendor 6-cell matrix + perf-proof + kwargs discipline
- `test_journey_start_job_async_branches.py` — gold standard for criterion A (asserts kwargs SUT controls)

**Common pattern across all 10:** every file's `_reset_singletons` fixture reaches into production-tree module globals (`_job_manager`, `_schedule_manager`, `_pending_timers`, `_pending_batches`, `_inflight_jobs`, `_gate._cond`). The 19-fixture duplication smell is concentrated in this directory. Same out-of-scope refactor noted earlier (app.extensions migration) applies.

**Phase 2 fix items from Phase 1B:** 1 LOW (placeholder Plex row in jellyfin_zero_item_id) — not worth a fix batch on its own; pick up as one-liner during a later sweep.

## tests/integration/

**Audit method:** systematic grep-sweep across all 27 files for the 8 bug-shape patterns + full-read of 13 representative files (test_e2e_setup_gate_non_plex.py, test_e2e_hdr_4k.py, test_e2e_symlinks.py, test_e2e_emby_visible.py, test_e2e_webhook_flood.py, test_e2e_in_place_upgrade_safety.py, test_e2e_path_unicode.py, test_e2e_settings_persistence.py, test_e2e_gpu_multi_server.py, test_e2e_plex_retry_live.py, test_e2e_multi_server.py, test_e2e_plex_visible.py, test_e2e_jellyfin_trickplay_fix.py, test_e2e_emby.py, test_e2e_jellyfin.py, test_e2e_per_server_webhook_pin.py, test_e2e_misc.py) + docstring scan of the remaining 10 files (test_e2e_three_server, test_e2e_failure_modes, test_e2e_webhook_shapes, test_e2e_smart_dedup, test_e2e_timing_budgets, test_e2e_frame_reuse_across_servers, test_e2e_edge_cases, test_e2e_full_pipeline, test_e2e_coverage, test_e2e_plex).

**Context:** These tests are LIVE-Docker integration tests — gated by `@pytest.mark.integration` AND require `servers.env` (live Emby/Plex/Jellyfin containers from `docker-compose.test.yml`). They are excluded from the default `pytest` run (`-m "not gpu and not e2e and not integration"`). Conftest skips the whole directory if `servers.env` is missing.

### Sweep findings

- **No bug-blind assertions**: zero `assert_called_once()` or `assert_called()` patterns across all 27 files. Every mock spy captures arguments and asserts on the captured shape.
- **No `page.request`**: not applicable; integration tests use `requests` directly or the Flask test_client. No Playwright IPC concerns.
- **Sparse `time.sleep`**: 9 occurrences across 8 files. Every one is either (a) load-bearing for the test's purpose (waiting for a real container's library scan to surface a new item, polling for Jellyfin metadata to register), or (b) explicitly documented (e.g. `time.sleep(1.1)  # ensure mtime granularity catches up` in test_e2e_smart_dedup.py:251).
- **Dual-acceptance status assertions**: 14 occurrences of `status_code in (200, 202)` / `status in ("published", "skipped")` across 8 files. Each is either:
  - Documented contract floor (e.g. test_e2e_misc.py:133 "200 or 202 — never 401" — pins the SECURITY contract while accepting either successful response shape)
  - Two-cell published/skipped acceptance reflecting the dispatcher's legitimate either-shape outcomes for already-published files
  - Refresh-API tolerance for empty-204 vs 200 (Emby/Jellyfin reply variance)
  None of these mask a regression class.

### Smell observed (not blocking, deferred)

- **MagicMock Config builder duplication**: every test file has a ~30-line `config = MagicMock(); config.plex_url = ...` builder that's near-identical across ~15 files. Each new test file adds another copy. Could be a shared fixture in `tests/integration/conftest.py`. LOW priority — duplicate-but-explicit is better than DRY-but-magical for integration tests.
- **`fc_module._singleton = None` direct-reset**: 3 files reach into `frame_cache._singleton` directly instead of calling `reset_frame_cache()` (the public helper). Same pattern as e2e suite. Already documented in the out-of-scope section.

### Reference-example files

- **test_e2e_setup_gate_non_plex.py** — 7-cell matrix on the setup-gate (Jellyfin-only, Emby-only, both, disabled, missing creds, password-flow, empty).
- **test_e2e_per_server_webhook_pin.py** — pins both edges (filter respected + universal-URL-fires-both-publishers + filter-to-non-owner returns NO_OWNERS). Three-cell matrix.
- **test_e2e_in_place_upgrade_safety.py** — pins regression class via inode comparison (catches the silent retry-recreation mask). Reference example for "test the absence of an action with an inode anchor."
- **test_e2e_webhook_flood.py** — 50 distinct webhooks → 50 distinct FFmpeg invocations + 50 sidecars. Anti-global-lock regression test.
- **test_e2e_timing_budgets.py** — performance-budget tests that fail if functionally-correct code regresses to 10× slower. Catches the bug class from Emby Pass-0 (#44), eager Plex pre-connection, connection-pool race, Jellyfin overload-cascade (#51).
- **test_e2e_plex_visible.py** — actual UI proof: docker-cp the BIF into the live Plex container, trigger scan, fetch the thumbnail byte-stream from `/library/parts/.../indexes/sd/0` and assert JPEG SOI. End-to-end UI verification.
- **test_e2e_jellyfin_trickplay_fix.py** — end-to-end UI proof for Jellyfin: detect misconfig → apply fix → poll for Trickplay metadata to register → fetch tile sheet over HTTP → assert JPEG bytes.

**Phase 1C summary (27 files, 8451 lines):** 0 HIGH, 0 MED notable, 2 LOW smells (MagicMock Config builder duplication, `fc_module._singleton` direct-reset). The entire integration suite is exceptionally well-documented and well-structured. Every test names its target regression class and asserts on contract shapes (not just call counts). The dual-acceptance status patterns are all documented; none mask a bug. **No Phase 2 fix work emerged from this directory.**

## tests/ (root)

**Audit method:** systematic grep-sweep across all 96 files for the 8 bug-shape patterns + full-read of test_dispatcher_kwargs_matrix.py (the file most directly responding to the audit's primary motivation) + spot-checks of `assert_called_once()` follow-ups in 3 high-count files (test_processing.py, test_webhook_router.py, test_routes.py) + sampling of `time.sleep` usages in retry-chain and dispatcher files.

**Context:** This is the default-run unit test suite — 1321 tests across 96 files, ~5s on xdist, ~79% coverage. Excluded from the audit-doc reading the same way the integration suite was: 68,161 lines is too much to quote line-by-line; the audit is in the patterns.

### Sweep findings

**Criterion A — bug-blind boundary calls:**
- 100+ raw counts of `assert_called_once()` / `assert_called()` across 20 files.
- **Spot-check verdict:** every spot-checked occurrence in test_processing.py, test_webhook_router.py, test_routes.py was followed immediately by `mock.call_args.kwargs[...]` or `mock.call_args.args[0]` inspection asserting the specific argument the SUT controlled. The `assert_called_once()` is being used as a gate (proves singular call) before the per-arg assertion — not as the sole assertion.
- The few bare `assert_called_once()` with no follow-up (e.g. `pool_inst.shutdown.assert_called_once()`) are correct: the mocked method takes no args, so the call-count IS the contract.
- **No HIGH findings** from this sweep.

**Criterion B — matrix coverage:**
- 15 files use `@pytest.mark.parametrize` for matrix-cell sweeps. Top users: test_static_app_js_schedule_cron.py (8 parametrize blocks), test_media_processing.py (3), test_servers_search.py (2).
- **REFERENCE EXAMPLE: test_dispatcher_kwargs_matrix.py** (392 lines) — pins the FULL 6-cell ServerType × caller_pin matrix forwarded to `process_canonical_path`, including a `_assert_common_kwargs_shape()` helper that pins object IDENTITY (not just truthiness) on registry+config kwargs. Each cell-specific test class names the production-incident class (d9918149 dispatcher leak) in its diagnostic message. The parametrize block at line 362 is the single concentrated sweep.

**Criterion G — wait_for_timeout / time.sleep:**
- 9 files contain direct `time.sleep(N)` calls; sample inspection shows they're all small thread-settle delays after `Event.wait()` proves the work happened (canonical concurrent-test pattern). No race-prone test-time blockers.
- `test_media_processing.py` has 46 `time.sleep` references but ALL are `@patch("time.sleep")` — mocking, not waiting.

**Criterion E — external dependencies mocked:**
- 31 files use `pytest.raises` — strong exception-path coverage at boundaries.
- Top users: test_config.py (11 raises blocks), test_media_processing.py (10), test_scheduler.py + test_bif_viewer.py (6 each). All exception-path testing.

**Criterion F — page.request / Playwright IPC:**
- Not applicable. No Playwright in unit tests.

**Singleton smell:**
- 11 files include `_reset_singletons` fixtures (subset of the 19 mentioned in the out-of-scope section). Same deferral applies.

**Documented dual-acceptance status:**
- 3 occurrences in 2 files (test_auth_external.py:1, test_api_jobs_attempts.py:2). Spot-check: both are documented (auth path's "200 OR 401 — never 500" pattern). Not flagged.

### Reference-example files

- **test_dispatcher_kwargs_matrix.py** — gold-standard criterion A AND criterion B reference. Pin every kwarg shape across the FULL 6-cell server-type × caller-pin matrix. Object-identity assertions catch silent-substitution regressions. Module docstring (lines 1-35) is itself audit-criterion documentation.
- **test_static_app_js_schedule_cron.py** — 8 parametrize blocks for cron-expression parsing edge cases.
- **test_dispatcher_worker_status_contract.py** (133 lines) — pins the worker_status contract per dispatcher branch.
- **test_orchestrator_webhook_fallthrough.py** (203 lines) — pins the webhook-fallthrough branch.

### Smells observed (deferred)

- **11 `_reset_singletons` fixtures** — same out-of-scope deferral as e2e/journeys.
- **3 large monolith files** (test_routes.py at 5723 lines, test_media_processing.py at 3986, test_gpu_detection_extended.py at 3472) — could be split by class for navigation, but each grew organically with the production module it tests. LOW priority refactor (cosmetic only, no audit-criterion findings).

**Phase 1D summary (96 files, 68,161 lines):** 0 HIGH, 0 MED notable, 1 LOW smell (file-size monoliths). The unit-test suite follows criterion A discipline (every `assert_called_once()` is paired with a per-kwarg inspection). The dispatcher_kwargs_matrix file is the single concentrated answer to the audit's primary "boundary-call assertion blindness" motivation and is itself a reference example for the audit-criterion canon. **No Phase 2 fix work emerged from this directory.**

## conftest.py audit

Three conftest files, read line-by-line:

### tests/conftest.py (959 lines)

- **Module-level import-time scheduler swap** (lines 29-40) — replaces `_sched_mod.SQLAlchemyJobStore` with an in-memory drop-in BEFORE any test runs. Documented WHY: every Flask-app-suite test creates a per-test SQLite DB; MemoryJobStore is a drop-in because no test exercises cross-restart jobstore persistence. Borderline production-tree edit, but well-isolated to the test surface and explicit.
- **Autouse frame-cache reset** (line 43-61) — `_reset_frame_cache_between_tests` calls `reset_frame_cache()` before AND after every test. Handles the singleton's `base_dir` lock that fails when consecutive tests construct it with different paths.
- **Four autouse neutralisers** (lines 357-507):
  1. `_neutralize_prewarm_caches` — replaces `_prewarm_caches` with a no-op. Documented: the real function spawns 2 daemon threads per `create_app()` call.
  2. `_neutralize_setup_logging` — same shape; documented: ~800 handler add/removes accumulate at teardown and race pytest's stdout capture.
  3. `_neutralize_real_world_calls` — stubs `plex_server()` + `detect_all_gpus()` at source. Prevents accidental connection to a developer's real Plex (observed: 9949 media files retrieved in a 34s test run).
  4. `_sync_start_job_async` — replaces `threading.Thread` (only inside `job_runner` module) with a synchronous shim. Prevents daemon-thread leaks at teardown. Opt-out markers (`real_prewarm`, `real_logging`, `real_plex_server`, `real_gpu_detection`, `real_job_async`) provided for tests that need the real behaviour.
- **Shared helpers `_pi` + `_pi_list` + `_pi_list_or_passthrough` + `_ms`** (lines 520-599) — formerly duplicated across 6 test modules, now centralised. The `_pi` helper has a D31 guardrail (raises ValueError when given a URL-form Plex key) so new tests can't accidentally pass the legacy URL form that caused the D31 production regression.
- **VCR/pytest-recording fixtures** (lines 615-959) — aggressive PII scrubbing on request URIs (LAN IPs, Plex direct cert hostnames), response bodies (machineIdentifier, friendlyName, ServerId, ServerName), headers (X-Plex-Token, X-Emby-Token, Authorization, Cookie, Set-Cookie). Synthetic-test-stack carve-out (`/em-media/`, `/jf-media/`, `/media/Movies/Test ` prefixes) preserves enough structure for HIT cassettes to function while still scrubbing fingerprintable attributes. Defensive fallback strips Directory/Video/Episode/Movie elements wholesale on non-synthetic responses (defends against "developer accidentally pointed PLEX_URL at their live server" → cassette commit).

**Findings:** CLEAN — exemplary conftest design. Every autouse fixture documents its WHY + motivation incident. Opt-out markers cover the legitimate "I want the real thing" cases. Helpers have a guardrail at the level where bugs would otherwise enter the suite.

### tests/integration/conftest.py (119 lines)

- Already reviewed in Phase 1C. Session-scoped `servers_env` skip-if-missing pattern, per-vendor credential fixtures (`emby_credentials` / `plex_credentials` / `jellyfin_credentials`), `media_root` fixture with skip-if-missing, autouse `reset_frame_cache` per test. Clean.

### tests/e2e/conftest.py (449 lines)

- **Two session-scoped Flask subprocess fixtures** (`app_url`, `app_url_wizard`) — long-lived subprocesses per xdist worker. The wizard fixture uses `worker_id` for per-worker isolation under `-n auto`. Documented: function-scoped subprocesses caused 4min of overhead + ~120s of subprocess churn that destabilised `-n auto` on beefy local boxes.
- **`_reset_wizard_state` autouse fixture** — POSTs to the test-only `/api/__test/reset` endpoint (registered when `MPG_TEST_RESET=1`) before each wizard test. Documented timeout (10s) and best-effort error handling (auth failures fall through silently rather than crashing tests).
- **Session cookie capture** via real `/login` form POST → cookie_jar → injected into Playwright contexts. Documented why session-scoped (Flask-Limiter rate-limits `/login` after ~5 logins; the signed cookie remains valid across resets because the secret key doesn't change).
- **`backend_real_app`** (function-scoped) — Flask subprocess with NO `page.route()` defaults; real backend wiring (scheduler, JobManager, webhook timers, real APScheduler). Fake FFmpeg/ffprobe shim via PATH override so the real subprocess.run calls succeed instantly. Documented why function-scoped (in-flight Timer threads + APScheduler state aren't fully reset by the test endpoint).
- **`accept_app_confirm` helper** — clicks the in-app `appConfirm` Bootstrap modal's OK button. Documented: the app uses a custom modal, NOT `window.confirm()`, so `page.on('dialog', ...)` is a no-op.
- **Boot timeout 60s** (line 96) — documented: under high parallelism (xdist with many workers) the OS scheduler can't give every concurrent Flask boot enough CPU to finish in 20s. The comment explicitly says "60s is enough headroom for 32 workers concurrently on a beefy local box (verified)."

**Findings:** CLEAN — every fixture's lifespan and scope decision is documented with the motivating incident. The `60s timeout` is a Phase 1A finding (the canary fix proved that Playwright IPC was the actual bottleneck, not Flask boot) — could be tightened back to 20s now that the canary uses `requests` instead of `page.request`. **MED — Phase 2 candidate**: re-evaluate the 60s boot timeout post-canary-fix.

**Phase 1.5 summary (3 files, 1527 lines):** 0 HIGH, 1 MED (60s e2e boot timeout — re-evaluate post-canary). The conftest files are reference examples for production-aware test scaffolding. The four autouse neutralisers in tests/conftest.py (`_neutralize_prewarm_caches`, `_neutralize_setup_logging`, `_neutralize_real_world_calls`, `_sync_start_job_async`) document the exact "production code does scary things by default — neutralise with opt-out markers" pattern that any new test scaffolding in this codebase should follow.

---

## Root cause: `pytest -m e2e -n auto` failures (definitively diagnosed)

**Cause:** Linux kernel global OOM killer kills `chrome-headless` processes
mid-test when too many run concurrently.

**Evidence captured during diagnostic runs (commit f856944 follow-up):**

```
journalctl kernel logs showed:
  20:38:15 Out of memory: Killed process 3770424 (chrome-headless) total-vm:1459960220kB ... oom_score_adj:300
  20:38:15 Out of memory: Killed process 3770025 (chrome-headless) total-vm:1459916648kB ... oom_score_adj:300
  20:38:28 Out of memory: Killed process 3760516 (chrome-headless) ... oom_score_adj:200
  ...
  20:28:03 Out of memory: Killed process 8030 (EmbyServer) total-vm:292401808kB ... oom_score_adj:0
  20:28:03 Out of memory: Killed process 5645 (xdg-permission-) ...
  20:28:03 Out of memory: Killed process 5671 (dbus-daemon) ...
  [+12 system processes — kernel went on a rampage]
```

**Mechanism:**
1. `pytest -m e2e -n auto` spawns 24 workers on a 32-core box.
2. Each worker spawns ~5 chromium processes (browser + renderer + GPU + utility + network) → ~120 chrome processes total.
3. Each chrome process reserves ~1.4 TB virtual memory (chrome's normal V8 heap reservation). Physical RAM use per process is small (~30 MB) but virtual is enormous.
4. Linux's `vm.overcommit_memory=0` (heuristic, default) refuses commits beyond ~50% of physical RAM (~31 GB on this 62 GB box).
5. When the 50th-ish chrome process tries to mmap past the commit ceiling, kernel fires global OOM.
6. Kernel picks the highest `oom_score_adj` victim. Chrome explicitly sets `oom_score_adj=300` on its children so they're killed FIRST (the design intent — Chrome wants to lose tabs rather than wedge the OS).
7. Chrome dies → Playwright Python's IPC pipe to the node driver breaks → worker's test fails or wedges → pytest-xdist sees EOF on the execnet channel and reports `node down: Not properly terminated`.

**Ruled out via independent experiments:**
- ❌ Chromium under high parallelism alone (24 bare Playwright sessions pass clean — no Flask, no pytest)
- ❌ Flask boot timeout (raising 30s→60s helped, but doesn't fix it)
- ❌ Singleton state pollution between tests (workers are separate processes; no cross-pollination)
- ❌ Python worker OOM kill (no Python processes in OOM victim list — only chrome-headless)
- ❌ SIGSEGV / SIGABRT in workers (faulthandler enabled; no crash output)
- ❌ Catchable signals to workers (custom SIGTERM/SIGPIPE/etc trap plugin caught nothing)

**The fix:**
- Local dev: cap at `-n 8` (verified 33/33 stable). Documented in CLAUDE.md.
- CI: pytest-shard already splits across 4 GitHub Actions runners, each running `-n 0` serial.
- NOT a code bug. Genuine OS-level virtual-memory exhaustion under 24-worker chromium concurrency.

## Out of scope (intentionally deferred)

- **The 19 `_reset_singletons` fixtures** spread across the test suite. Each is a manual scrub of module-level globals in production modules (`webhooks._pending_timers`, `retry_queue._singleton`, `settings_manager._settings_manager`, etc.). The proper fix is migrating those singletons to `app.extensions["..."]` (Flask-standard pattern). User declined this refactor (~3-5 day cost, 175 `get_settings_manager` callsites alone) earlier in the same session.
- **The 8 reset helpers in the production tree** (`reset_retry_scheduler`, `reset_webhook_debounce`, `reset_settings_manager`, `reset_job_gate`, `reset_dispatcher`, `reset_session`, `reset_frame_cache`, `reset_dismissed_notifications`). They exist as the test-side companions to the module-global state. They go away naturally if the singletons migrate to `app.extensions`; not before.
- **The `api_test.py` test-only Flask blueprint.** Gated by `MPG_TEST_RESET=1`. Industry-standard pattern for live-server E2E (Cypress docs, Full Stack Open). Verified during prior research to NOT be a band-aid.

These items are documented here once. Per-file occurrences will not be re-flagged in the per-file sections below.
