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
