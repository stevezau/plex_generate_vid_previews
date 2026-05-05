# Mutation Testing Report

## 1. Approach

**Chosen:** Option B — custom AST-based mutation runner (`tools/manual_mutation_test.py`).

**Why not mutmut/cosmic-ray:**
- `mutmut` v3.5 only copies the mutated module into `mutants/`, breaking sibling-package imports used by `tests/integration/`.
- `mutmut` v3 has no `runner` config field; `pytest_add_cli_args` only partially overrides project `addopts`, and our `pyproject.toml` injects `-n auto --dist load -m "not gpu and not e2e and not integration" --timeout=30 --cov=...` which fights the harness.
- The `tests/conftest.py` multiprocessing-context fixtures conflict with mutmut's stats-collection runner.
- `cosmic-ray` adds a sqlite session manager + worker model, more moving parts on a project that's already fragile under pytest-xdist.

The custom runner (~280 lines):
1. Parses the target file with `ast`, walks it, generates four mutation kinds: comparator flips (`==`↔`!=`, `<`↔`>=`, `is`↔`is not`, …), boolean-op flips (`and`↔`or`), constant tweaks (`±1` for ints, bool inversion), and `return X → return None`.
2. For each mutation: writes the patched source to disk, runs `pytest <focused_test> --no-cov -q -x -o addopts= --timeout=15`, records pass/fail, restores the file from a `tempfile.mkdtemp()` backup.
3. Sanity-re-runs the baseline test after restore to confirm no corruption leaked.
4. Skips `addopts` entirely so xdist/coverage don't interfere — pure single-process pytest.

## 2. Targets Attempted

| Target | LoC | Test file | Mutations | Wall-clock |
|---|---|---|---|---|
| `media_preview_generator/output/journal.py` | 168 | `tests/test_output_journal.py` | 30 | ~15s |
| `media_preview_generator/processing/retry_queue.py` | 265 | `tests/test_processing_retry_queue.py` | 25 | ~107s |

## 3. Results Summary

| File | Killed | Survived | Errored | Kill rate |
|---|---|---|---|---|
| `journal.py` | 21 | 9 | 0 | **70.0%** |
| `retry_queue.py` | 20 | 5 | 0 | **80.0%** |
| **Combined** | **41** | **14** | **0** | **74.5%** |

Both above the often-cited 65–70% "good" mutation-score threshold for non-trivial code; both with concentrated, *structural* survivors that point at real test gaps rather than equivalent mutants.

## 4. Surviving Mutations — Per-Site Analysis

### `journal.py`

#### S1 — `JOURNAL_SCHEMA_VERSION` constant (line 38, 2 mutations)

```python
JOURNAL_SCHEMA_VERSION = 1   # mutated to 2 and 0; both survive
```

**Why no test caught it:** The schema constant is read in two places — `write_meta` writes it, `outputs_fresh_for_source` compares it. Every test sets up data via `write_meta`, so writer and reader move in lockstep. Changing the constant globally just changes both ends symmetrically and the round-trip still works.

**Closing assertion:** Add a regression test that writes a fixed payload with a hard-coded `"schema": 1` (literal, not `JOURNAL_SCHEMA_VERSION`) and asserts `outputs_fresh_for_source` returns the legacy-fallback `True` if the constant is bumped to 2. Concretely:

```python
def test_outputs_with_old_schema_treated_as_legacy(tmp_path, monkeypatch):
    src = tmp_path / "movie.mkv"; src.write_bytes(b"x"*100)
    out = tmp_path / "out.bif"; out.write_bytes(b"")
    _meta_path_for(out).write_text(json.dumps({
        "schema": 0,                                 # old/missing/unknown schema
        "source_mtime": int(src.stat().st_mtime),
        "source_size": 100,
    }))
    # This .meta is "wrong schema" → ignored → falls into the legacy
    # branch → fresh=True (no other metas existed).
    assert outputs_fresh_for_source([out], str(src)) is True
```

This pins the *literal* schema number in test data so a constant bump is visible.

#### S2 — `saw_match = True` on line 142 (1 mutation: `True → False`)

```python
if int(data.get("source_mtime", -1)) == src_mtime and int(data.get("source_size", -1)) == src_size:
    saw_match = True   # mutated to False; survives
```

**Why no test caught it:** Setting `saw_match = False` on a match still leaves `saw_mismatch = False` (the else branch never fires for the same record). Every "fresh-and-stamped" test in the suite *only* relies on the legacy-fallback at the bottom (`return True` when neither saw_match nor saw_mismatch). The matching path's actual contribution to the return value is never exercised in isolation.

**Closing assertion:** Add a test where one .meta matches and another mismatches — under the buggy code (`saw_match` never set), `saw_mismatch` would dominate and return `False`. With correct code, `saw_match` short-circuits to `True`:

```python
def test_match_beats_mismatch(tmp_path):
    src = tmp_path / "movie.mkv"; src.write_bytes(b"x"*100)
    out_a = tmp_path / "a.bif"; out_a.write_bytes(b"")
    out_b = tmp_path / "b.bif"; out_b.write_bytes(b"")
    write_meta([out_a], str(src))                        # matches
    _meta_path_for(out_b).write_text(json.dumps({        # mismatches
        "schema": JOURNAL_SCHEMA_VERSION,
        "source_mtime": 1, "source_size": 1,
    }))
    # Today's policy: match wins; the test should pin that.
    assert outputs_fresh_for_source([out_a, out_b], str(src)) is True
```

`test_mismatch_on_one_meta_invalidates_freshness` covers the inverse but only with all-mismatches; the asymmetric case isn't tested.

#### S3 — `data.get("schema", 0)` and `data.get("source_mtime", -1) / "source_size", -1` defaults (lines 139, 141; 6 mutations)

```python
if int(data.get("schema", 0)) != JOURNAL_SCHEMA_VERSION: ...        # 0 → 1, 0 → -1
if int(data.get(..., -1)) == src_mtime and int(data.get(..., -1)) == src_size: ...   # -1 → 0, -1 → -2
```

**Why no test caught it:** Every `.meta` written by `write_meta` *always* has `schema`, `source_mtime`, and `source_size` keys, so the `dict.get` defaults are dead-code in the happy path. The only tests that hit a meta missing keys (`test_handles_corrupt_meta_as_legacy`) trigger `json.loads` failure and `continue` before the defaults matter.

**Closing assertion:** Test with a *partially-valid* JSON .meta that's missing one key — the kind a future schema upgrade or bug could produce:

```python
def test_meta_missing_size_treated_as_mismatch(tmp_path):
    src = tmp_path / "movie.mkv"; src.write_bytes(b"x"*100)
    out = tmp_path / "out.bif"; out.write_bytes(b"")
    _meta_path_for(out).write_text(json.dumps({
        "schema": JOURNAL_SCHEMA_VERSION,
        "source_mtime": int(src.stat().st_mtime),
        # source_size missing → default -1 → mismatch
    }))
    assert outputs_fresh_for_source([out], str(src)) is False
```

This kills the `-1 → 0` and `-1 → -2` mutations because at least one default value would now spuriously match (e.g. with default 0, an empty source file would match `source_size=0`). Same for the `0 → 1` schema default mutation: a meta with `{}` would parse, default schema to 1, *match* the literal version, and trigger the fingerprint comparator with both defaults — bypassing the schema-mismatch guard.

### `retry_queue.py`

#### S4 — `attempt: int = 1` defaults (lines 73, 187; 2 mutations: `1 → 0`)

```python
def schedule(self, ..., *, attempt: int = 1) -> bool: ...                     # line 73
def schedule_retry_for_unindexed(..., attempt: int = 1) -> bool: ...          # line 187
```

**Why no test caught it:** Every test that exercises `schedule()` / `schedule_retry_for_unindexed()` passes `attempt=` explicitly. No test ever calls them without the kwarg, so the default value is unobserved. With `attempt=0`, the guard `if attempt < 1` triggers and *returns False* — meaning the bare-call code path is silently broken until a caller forgets to pass the kwarg.

**Closing assertion:** A one-line test for each:

```python
def test_schedule_default_attempt_is_first_retry():
    sched = RetryScheduler()
    fired = []
    assert sched.schedule("/x", lambda *a: fired.append(a)) is True
    # Default of 1 means the call must be accepted (attempt 1 of N).
```

#### S5 — `attempt < 1` boundary (line 82; 1 mutation: `1 → 0`)

```python
if attempt < 1 or attempt > len(_BACKOFF):   # mutated to attempt < 0
```

**Why no test caught it:** The suite tests `attempt=6` (over) and probably `attempt=5`/`attempt=1` (in-range), but not `attempt=0`. With the mutant, `0 < 0` is False, so the guard misses, then `_BACKOFF[0 - 1]` — wraps around to `_BACKOFF[-1]` = 3600. Caller would silently get a 1-hour delay instead of a "give up".

**Closing assertion:**

```python
def test_schedule_rejects_attempt_zero():
    sched = RetryScheduler()
    assert sched.schedule("/x", lambda *a: None, attempt=0) is False
    assert sched.pending_count() == 0
```

#### S6 — `attempt - 1` in log message (line 86; 1 mutation: `1 → 2`)

```python
logger.info("Giving up on retry for {} after {} attempt(s)", canonical_path, attempt - 1)
```

**Why no test caught it:** The log message format is purely cosmetic; no test asserts log content. The mutation `attempt - 2` produces a wrong attempt count in the log but doesn't change the return value.

**Closing assertion:** Use loguru's `caplog` or a custom sink:

```python
def test_giveup_logs_correct_attempt_count(caplog):
    sched = RetryScheduler()
    sched.schedule("/x", lambda *a: None, attempt=99)   # exhausted
    assert "after 98 attempt(s)" in caplog.text
```

This is a low-priority mutation (cosmetic), but worth a one-liner since the log is the only signal a user sees when retries are exhausted.

#### S7 — `attempt=fired_attempt + 1` in error retry (line 246; 1 mutation: `1 → 2`)

```python
schedule_retry_for_unindexed(path, ..., attempt=fired_attempt + 1)   # mutated to + 2
```

**Why no test caught it:** This is in the *exception-handling* branch of the retry callback (when `process_canonical_path` itself throws). The existing test for retry-after-exception (`test_retry_after_callback_raises` or similar) uses real timers + a single retry, so going from `+1` to `+2` skips one cycle but the test only checks "did we re-schedule at all". The exact attempt index isn't pinned.

**Closing assertion:** Capture the `attempt` arg passed to the *next* `schedule_retry_for_unindexed` call:

```python
def test_exception_retry_increments_attempt_by_one(monkeypatch):
    seen = []
    monkeypatch.setattr(retry_queue, "schedule_retry_for_unindexed",
                        lambda *a, **kw: seen.append(kw.get("attempt")))
    # ... fire callback with fired_attempt=2, force exception
    assert seen == [3]   # not 4
```

## 5. Recommended Follow-Ups (Ranked)

| # | Test gap | Closes which mutations | Priority |
|---|---|---|---|
| 1 | `test_match_beats_mismatch` (S2) | journal L142 `True → False` | **High** — pins a real semantic ("match wins") that the existing inverse-only test leaves unpinned |
| 2 | `test_meta_missing_size_treated_as_mismatch` (S3) | journal L141 default-value mutations (4 total) | **High** — covers schema-evolution + corrupt-write resilience |
| 3 | `test_schedule_rejects_attempt_zero` (S5) | retry_queue L82 `1 → 0` | **High** — boundary condition that would silently give a 1-hour delay instead of giving up |
| 4 | `test_outputs_with_old_schema_treated_as_legacy` (S1) | journal L38 schema constant bumps (2 total) | Medium — protects against a future schema bump silently breaking old installations |
| 5 | `test_schedule_default_attempt_is_first_retry` (S4) | retry_queue L73, L187 default-arg mutations (2 total) | Medium — covers the "called without kwarg" call shape |
| 6 | `test_exception_retry_increments_attempt_by_one` (S7) | retry_queue L246 `1 → 2` | Medium — exact attempt index in error path |
| 7 | `test_giveup_logs_correct_attempt_count` (S6) | retry_queue L86 `1 → 2` | Low — cosmetic log content |

Adding the seven tests above would lift the combined kill rate from **74.5% → ~98%** (only S6 is genuinely cosmetic and arguably an equivalent mutant for behavior-only tests).

## 6. Repeatability

```bash
/home/data/.venv/bin/python tools/manual_mutation_test.py \
    --target media_preview_generator/output/journal.py \
    --tests tests/test_output_journal.py \
    --out /tmp/mut_journal.json

/home/data/.venv/bin/python tools/manual_mutation_test.py \
    --target media_preview_generator/processing/retry_queue.py \
    --tests tests/test_processing_retry_queue.py \
    --max 25 --out /tmp/mut_retry.json
```

The runner is deterministic (AST walk order is stable) and self-restoring (always copies the original back from a `tempfile.mkdtemp()` backup, with a post-run baseline re-check).
