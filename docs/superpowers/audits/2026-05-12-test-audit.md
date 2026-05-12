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

_(filled in during Phase 1A)_

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
