# Test Suite Audit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Audit every test file line-by-line for the existing project conventions + best practices, fix inline-safe issues on the spot, document the rest with severity flags. Leave the suite consistently polished, isolated from production tree where feasible, with no unnecessary mocks.

**Architecture:** Three phases. Phase 0 fixes the audit criteria document so every later judgement is against the same rubric. Phase 1 is a triage pass per directory (e2e → journeys → integration → root) — read every file, fix LOW-risk items inline, log MED/HIGH per file in `docs/superpowers/audits/2026-05-12-test-audit.md`. Phase 2 batches similar MED fixes across files (one commit per category, not per file). Phase 3 verifies the full suite stays green at `-n 0` / `-n 8` / `-n auto` after the cleanup.

**Tech Stack:** pytest, pytest-xdist, pytest-playwright, requests, the project's existing `.claude/rules/testing.md` rubric.

---

## Scope acknowledged

- 164 test files across `tests/` (93), `tests/e2e/` (34), `tests/integration/` (27), `tests/journeys/` (10)
- ~84,887 lines of test code
- 3 `conftest.py` files (root, e2e, integration)
- 8 reset helpers in production tree: `settings_manager.reset_settings_manager`, `retry_queue.reset_retry_scheduler`, `frame_cache.reset_frame_cache`, `dispatcher.reset_dispatcher`, `notifications.reset_session`, `job_gate.reset_job_gate`, `webhooks.reset_webhook_debounce`, plus the gated `api_test.py` blueprint

User decisions captured before planning:
- **Scope:** full audit — all 164 files
- **Output:** audit + fix inline where safe; surface MED/HIGH judgement calls before fixing
- **Cadence:** multi-session work, 2-4 days estimated, work in chunks with progress reports

## Audit criteria (from `.claude/rules/testing.md` + project rules)

Every test gets checked against:

**A. Naming conventions**
- File: `test_{module}.py`
- Class: `Test{ClassName}` or `Test{FunctionGroup}`
- Method: `test_{behavior}_when_{condition}`
- Body: Arrange / Act / Assert

**B. Bug-blind detection (HIGH severity)**
- A test that mocks a downstream call MUST assert the kwargs the SUT controls — not just `assert_called_once`
- Reference: `.claude/rules/testing.md:67-81` (the D34 dispatcher regression)
- Fix pattern: replace `mock.assert_called_once()` with `assert mock.call_args.kwargs["x"] == expected_value` for every SUT-controlled kwarg

**C. Cover-the-matrix gaps (MED severity)**
- Tests for branchy functions must cover every distinct cell
- Reference: `.claude/rules/testing.md:83-91`
- Fix pattern: list distinct branch values, multiply with other axes, add a row per cell (or a one-line comment explaining why a cell collapses)

**D. Mocking discipline (MED severity)**
- External deps (Plex API, FFmpeg, filesystem, network) MUST be mocked — check the HTTP boundary, not project-internal helpers
- Mocking a project-internal helper to "make the test pass" is the production-bug shape #7 antipattern (see `.claude/agents/architecture-review.md`)
- Fix pattern: move the mock to `_request` / `subprocess.run` / `Path.open` rather than `MyServer.query_items`

**E. Test isolation (MED severity)**
- No reliance on test order
- No module-global state surviving between tests
- The 19 `_reset_singletons` fixtures across the suite are testimony to this debt — flag but don't refactor in this audit (architectural change, out of scope per user)

**F. Playwright IPC usage in e2e (LOW severity, backlog)**
- Tests hitting backend APIs that don't need browser-cookie state should use `requests`, not `page.request`
- ~40 callsites across ~11 e2e files (verified pre-audit). The canary fix established the pattern; this audit can apply it broadly OR leave as backlog
- User decision needed per-file as I encounter them

**G. AAA structure (LOW severity)**
- Tests should read as Arrange / Act / Assert. Comment blocks marking the sections aren't required but help.

**H. Placeholder/lazy patterns (LOW severity)**
- "TBD", "TODO", "implement later", "test something works" without specific contract
- Empty assertions (`assert True`, `assert not None`)
- Commented-out code

**I. AAA-pattern + naming alignment (LOW severity, ruff-class)**
- Generic test names like `test_thing`, `test_works`
- Missing docstring on non-trivial test
- Mixed indentation, unused imports, deprecated typing (`List[X]` → `list[X]`)

## File structure

**No new production files.** This is a test-quality audit; production code is out of scope except where a test absolutely requires a small public-method addition (e.g., a single accessor to avoid mocking a `_private`).

**Files modified (final state, post-audit):**
- All 164 test files — fixes applied inline per criterion
- `docs/superpowers/audits/2026-05-12-test-audit.md` — new audit report with per-file findings (MED/HIGH) and rationale for any production-touching judgement call
- `tests/conftest.py`, `tests/e2e/conftest.py`, `tests/integration/conftest.py` — touched if any fixture-level consistency fix is warranted

---

## Phase 0: Audit infrastructure (~30 min)

### Task 0.1: Create the audit report skeleton

**Files:**
- Create: `docs/superpowers/audits/2026-05-12-test-audit.md`

- [ ] **Step 1:** Write the report header + per-directory sections + severity legend

The report uses one section per directory (e2e, journeys, integration, root), and within each directory one subsection per file. Each file entry records: line count, classes/methods count, MED/HIGH findings with file:line + rationale + fix applied/deferred.

```markdown
# Test Suite Audit — 2026-05-12

## Severity legend
- **HIGH** — bug-blind test (no SUT contract pinned). Fix immediately.
- **MED** — matrix gap, internal-helper mock, isolation smell. Fix in batch (Phase 2).
- **LOW** — naming, AAA structure, placeholder. Fix inline during Phase 1.

## Summary (filled in post-audit)
| Directory | Files | LOW fixed inline | MED batched | HIGH fixed |
|---|---|---|---|---|
| tests/e2e/ | 34 | — | — | — |
| tests/journeys/ | 10 | — | — | — |
| tests/integration/ | 27 | — | — | — |
| tests/ (root) | 93 | — | — | — |

## tests/e2e/
### tests/e2e/test_<name>.py
- Line count:
- Findings:
  - LOW (fixed inline): ...
  - MED (Phase 2 batch X): ...
  - HIGH (fixed): ...

(repeat per file)
```

- [ ] **Step 2:** Commit the skeleton

```bash
git add docs/superpowers/audits/2026-05-12-test-audit.md
git commit -m "docs(test-audit): scaffold audit report skeleton"
```

### Task 0.2: Verify baseline test suite is green

- [ ] **Step 1:** Run full unit suite, e2e suite at `-n 0`, e2e suite at `-n 8`, e2e suite at `-n auto`. Record pass counts.

```bash
/home/data/.venv/bin/python -m pytest --no-cov 2>&1 | tail -2
/home/data/.venv/bin/python -m pytest -m e2e -n 0 --no-cov 2>&1 | tail -2
/home/data/.venv/bin/python -m pytest -m e2e -n 8 --no-cov 2>&1 | tail -2
/home/data/.venv/bin/python -m pytest -m e2e -n auto --no-cov 2>&1 | tail -2
```

Expected: ~2986 unit tests pass (with ~4 xdist-pollution flakes per project memory), 152 e2e at `-n 0`/`-n 8`/`-n auto`. Document the baseline numbers in the audit report's intro.

- [ ] **Step 2:** No commit — this is verification only.

---

## Phase 1: Triage audit — one batch per directory

Each batch: read every file in the directory line-by-line, apply LOW fixes inline, log MED/HIGH in the audit report.

### Phase 1A: tests/e2e/ (34 files)

This is where the recent fragility lived; highest-priority batch.

For each file in `tests/e2e/test_*.py`:

- [ ] **Step 1:** `Read` the entire file (no offset/limit).
- [ ] **Step 2:** Apply criterion A (naming) — rename test methods that don't match `test_{behavior}_when_{condition}` where the rename is unambiguous. Skip ambiguous ones (log as LOW).
- [ ] **Step 3:** Apply criterion H + I (placeholders, lazy patterns, dead imports, deprecated typing) — fix inline.
- [ ] **Step 4:** Check criterion B (bug-blind) — for every mock, verify a SUT-controlled kwarg is pinned. If not, log as HIGH with the specific assertion to add.
- [ ] **Step 5:** Check criterion C (matrix gaps) — for any test of a branchy function (vendor switch, retry stage, auth method), verify each cell is covered. Log gaps as MED.
- [ ] **Step 6:** Check criterion D (mocking discipline) — flag any project-internal helper mocked instead of the HTTP/filesystem boundary. Log as MED.
- [ ] **Step 7:** Check criterion F (Playwright IPC) — flag any `page.request.X()` backend API call that doesn't need browser-cookie state. Default: log as backlog LOW (this audit doesn't apply the swap; the canary fix established the pattern but the user explicitly scoped that to the canary).
- [ ] **Step 8:** Run the file's tests to verify inline fixes didn't regress.

```bash
/home/data/.venv/bin/python -m pytest -m e2e -n 0 --no-cov -p no:rerunfailures tests/e2e/test_<file>.py 2>&1 | tail -3
```

- [ ] **Step 9:** Append the file's entry to `docs/superpowers/audits/2026-05-12-test-audit.md`.
- [ ] **Step 10:** Commit per-file or per-batch-of-5-files (whichever is cleaner).

```bash
git add tests/e2e/test_<file>.py docs/superpowers/audits/2026-05-12-test-audit.md
git commit -m "test-audit(e2e): inline LOW fixes for <file>, log MED/HIGH"
```

**Estimated effort: 6-8 hours for 34 files.**

### Phase 1B: tests/journeys/ (10 files)

Same playbook as Phase 1A. Journey tests cross multiple modules and are particularly prone to bug-blind mocking (they wire many internal pieces together).

**Estimated effort: 2-3 hours.**

### Phase 1C: tests/integration/ (27 files)

Same playbook. Integration tests may hit real services (`@pytest.mark.integration` / `@pytest.mark.gpu`); confirm mock discipline at the network boundary, not internal helpers.

**Estimated effort: 4-6 hours.**

### Phase 1D: tests/ (root, 93 files)

The bulk. Same playbook. These are mostly unit tests, so mock discipline + bug-blind detection are the dominant criteria. Matrix gaps are common in pure-unit branchy-function tests.

**Estimated effort: 12-16 hours.**

### Task 1.5: Audit the three conftest.py files explicitly

**Files:**
- Modify (if needed): `tests/conftest.py`
- Modify (if needed): `tests/e2e/conftest.py`
- Modify (if needed): `tests/integration/conftest.py`

Conftests don't live by the per-test rubric; they live by:
- Fixtures should have docstrings explaining what they provide
- Fixtures should be scoped correctly (function vs session)
- No leaked state across test boundaries (the 19 `_reset_singletons` smell is HERE)
- Consistency: similar fixtures should follow similar patterns

- [ ] Read each in full
- [ ] Document the 19 `_reset_singletons` situation in the audit report with a "deferred — out of audit scope" note (per user's earlier decision not to refactor module globals to `app.extensions`)
- [ ] Apply LOW fixes (docstrings, dead fixtures, naming)
- [ ] Commit

---

## Phase 2: MED-fix batches

Group MED findings by category across all 164 files. Apply each category as a single commit so the rationale is documented once.

### Task 2.1: Bug-blind tests — add SUT-kwarg assertions

For every HIGH (already fixed in Phase 1) and any MED bug-blind-adjacent issues, batch the assertion additions.

- [ ] Group findings from the audit report under "category: bug-blind kwarg assertions"
- [ ] For each, replace `mock.assert_called_once()` with the explicit kwarg checks per `.claude/rules/testing.md:71-79`
- [ ] Run the affected tests
- [ ] Commit

```bash
git commit -m "test(audit): pin SUT-controlled kwargs on N mock-asserted tests (was bug-blind)"
```

### Task 2.2: Matrix-gap rows

For every MED matrix-gap, add the missing-cell row.

- [ ] Pick the SUT function with gaps
- [ ] Enumerate the branch axes from the audit report
- [ ] Add the missing row(s) — each must pin the actual contract for that cell, not just "runs without raising"
- [ ] Run affected tests
- [ ] Commit per SUT function (small commits)

### Task 2.3: Internal-helper-mock → HTTP-boundary-mock

For every MED "mock at wrong layer" finding, refactor the test to mock at the actual external boundary.

- [ ] For each affected test: identify the actual HTTP/subprocess call site
- [ ] Replace `@patch('module.helper')` with `@patch('module._request')` or equivalent
- [ ] Verify the test still pins the right contract — sometimes lowering the mock layer reveals a bug-blind test that was hiding behind the helper mock
- [ ] Commit per file or per category

### Task 2.4: Test-isolation smells (only the cheap ones)

For tests where isolation is broken by something other than the documented module-global pattern (which is out of scope):

- [ ] Identify any test that depends on a sibling test running first
- [ ] Either make the test self-sufficient (set up its own state) or mark it explicitly with a docstring "depends on TestX.test_y" + use a fixture
- [ ] Out of scope: refactoring `app.extensions`-class singletons. That's a separate effort.

---

## Phase 3: Verification

### Task 3.1: Full suite green check

- [ ] **Step 1:** Run full unit suite, e2e at `-n 0`, e2e at `-n 8`, e2e at `-n auto` × 3 consecutive runs.

```bash
/home/data/.venv/bin/python -m pytest --no-cov 2>&1 | tail -3
for n in 0 8 auto; do
    echo "=== -n $n ==="
    /home/data/.venv/bin/python -m pytest -m e2e -n $n --no-cov -p no:rerunfailures 2>&1 | tail -3
done
for i in 1 2 3; do
    echo "=== -n auto run $i ==="
    /home/data/.venv/bin/python -m pytest -m e2e -n auto --no-cov -p no:rerunfailures 2>&1 | tail -3
done
```

Expected: same pass count as Phase 0 baseline. If any test fails post-audit that was passing pre-audit, fix at root (not by reverting the audit change).

### Task 3.2: Lint + format check

- [ ] **Step 1:** `ruff check tests/` — all green
- [ ] **Step 2:** `ruff format --check tests/` — all green
- [ ] **Step 3:** Fix any new lint warnings introduced by the audit

```bash
/home/data/.venv/bin/python -m ruff check tests/ 2>&1 | tail -5
/home/data/.venv/bin/python -m ruff format --check tests/ 2>&1 | tail -5
```

### Task 3.3: Audit report finalization

- [ ] **Step 1:** Fill in the summary table at the top of the audit report
- [ ] **Step 2:** Add a "what was left out of scope" section listing items intentionally deferred (the 19 `_reset_singletons` refactor, the 40 `page.request` swaps in e2e tests outside the canary, etc.)
- [ ] **Step 3:** Commit the finalised report

```bash
git commit -m "docs(test-audit): finalise audit report with summary + deferred-scope list"
```

### Task 3.4: Architecture review on the cumulative diff

Per CLAUDE.md, dispatch the Architecture Review agent on the full diff of test changes before pushing.

- [ ] **Step 1:** Dispatch agent on the cumulative diff across Phase 1 + Phase 2 commits
- [ ] **Step 2:** Address any HIGH findings before push; document MED disposition

### Task 3.5: Push

- [ ] **Step 1:** `git push origin dev`
- [ ] **Step 2:** Verify CI passes

---

## Out of scope (do not touch in this audit)

- Migrating module-global singletons to `app.extensions` (the 19 `_reset_singletons` fixtures). User explicitly declined this refactor (~3-5 day cost vs benefit). The audit will flag the smell once with a pointer to the deferred decision; per-file occurrences won't be re-flagged.
- Migrating the remaining ~40 `page.request.X()` callsites in e2e tests to `requests`. The canary's fix established the pattern; broader application is backlog scope.
- Production code changes other than the rare "add a tiny public method to avoid mocking `_private`" case, which is itself rare and flagged for user decision before applying.
- The 8 reset helpers in production tree. Each exists for a documented reason (test infra reaching in to module globals). User accepted them as test-infra patterns earlier in this session.

## Self-review (running it now)

1. **Spec coverage:** User wanted full audit of all 164 files, fix inline where safe, follow best practices, no unnecessary mocks. Plan covers all 164 across Phase 1A/B/C/D, defines best-practice criteria with file:line citations, has explicit MED batch for unneeded mocks. ✓

2. **Placeholder scan:** No "TBD" / "implement later" — every step has the actual action it requires.

3. **Type consistency:** Plan doesn't define new types/functions.

4. **Scale honesty:** 2-4 days estimated. Per-phase effort breakdown given. The user signed up for this scale with eyes open.

## Effort estimate (honest)

| Phase | Effort | Cadence |
|---|---|---|
| Phase 0 | 30 min | Single session |
| Phase 1A (e2e, 34 files) | 6-8 hr | 1 session |
| Phase 1B (journeys, 10) | 2-3 hr | 1 session |
| Phase 1C (integration, 27) | 4-6 hr | 1 session |
| Phase 1D (root, 93) | 12-16 hr | 2-3 sessions |
| Phase 2 (MED batches) | 2-4 hr | 1 session |
| Phase 3 (verify + push) | 1 hr | 1 session |
| **Total** | **28-39 hr** | **2-4 working days** |

## Commits per phase (target)

- Phase 0: 1 commit (audit-skeleton)
- Phase 1A: 6-8 commits (one per 5-file batch in e2e/)
- Phase 1B: 2 commits
- Phase 1C: 5-6 commits
- Phase 1D: 18-20 commits
- Phase 2: 3-4 commits (one per MED category)
- Phase 3: 2-3 commits (final report + Architecture Review fixes if any)
- **Total: ~40 commits over 2-4 days**
