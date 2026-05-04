# Audit: tests/test_app.py — 16 tests, 7 classes

Tests for the Flask app factory: CORS resolution, secret derivation, scheduled-job callback, WSGI module shape, startup requeue, and prewarm caches.

## TestGetCorsOrigins

| Line | Test | Verdict |
|---|---|---|
| 53 | `test_default_returns_wildcard` | **Strong** — strict equality on `"*"` AND `is_default is True`. Catches a regression that flips the default. |
| 59 | `test_env_override` | **Strong** — strict equality on env value AND `is_default is False`. Mirror cell. |

## TestDeriveSecret

| Line | Test | Verdict |
|---|---|---|
| 69 | `test_deterministic` | **Strong** — same seed + same salt → same secret. Pins HMAC determinism. |
| 75 | `test_different_salt_produces_different_secret` | **Strong** — different salt → different secret. Pins salt-sensitivity. The pair fully covers the HMAC contract. |

## TestGetOrCreateFlaskSecret

| Line | Test | Verdict |
|---|---|---|
| 85 | `test_env_variable_takes_priority` | **Strong** — env value wins; strict equality on returned secret. |
| 90 | `test_generates_and_persists_seed` | **Strong** — pins `seed_file.exists()` AND non-empty secret string. |
| 98 | `test_reuses_existing_seed` | **Strong** — same secret across two calls. Catches a "regenerate every call" bug. |
| 104 | `test_seed_file_has_restrictive_permissions` | **Strong** — `mode == 0o600` strict equality. Security-critical pin (catches `0o644` regression). |

## TestRunScheduledJob

| Line | Test | Verdict |
|---|---|---|
| 116 | `test_creates_and_starts_job` | **Bug-blind** — only asserts `mock_start.assert_called_once()` with no kwarg checks. This is the exact D34 paradigm — passes regardless of which job_id, config, library_name was actually forwarded. **NEEDS FIX**: also assert `mock_start.call_args.args[0]` is a job id (string) AND that the job manager has a job with `library_name=="Movies"`. |
| 143 | `test_includes_library_id_in_config` | **Strong** — pins `selected_library_ids == ["1"]` OR `selected_libraries == ["1"]` with explanatory comment. The disjunction handles the dispatcher's legacy field-name choice. The audit fix comment at lines 164-171 documents the prior weak substring assertion. |
| 179 | `test_scheduled_job_infers_server_id_from_library_id` | **Strong** — exemplary. Two-server fixture (so the test could fail if matching logic were broken), then `assert job.server_id == "plex-tv"` with full explanatory message tied to incident 933a26d. Cell distinct from the explicit test below. |
| 260 | `test_scheduled_job_explicit_server_id_overrides_inference` | **Strong** — both servers own library_id=42, only explicit `server_id="emby-explicit"` would pick correctly. Strict `job.server_id == "emby-explicit"`. Pins precedence. |

## TestWsgiModule

| Line | Test | Verdict |
|---|---|---|
| 327 | `test_wsgi_importable` | **Strong** — actually `import_module`s, asserts `hasattr(wsgi, "app")` AND `callable(wsgi.app)`. Audit-fix comment at line 332-335 explicitly closes the prior `find_spec is not None` weakness. |

## TestRequeueInterruptedOnStartup

| Line | Test | Verdict |
|---|---|---|
| 359 | `test_string_false_disables_requeue` | **Strong** — `mock_get_job_manager.assert_not_called()` AND `mock_start_job.assert_not_called()`. Pins the "string 'false' is treated as falsy" contract (an easy bug). |
| 374 | `test_string_true_requeues_jobs` | **Strong** — `requeue_interrupted_jobs.assert_called_once_with(max_age_minutes=45)` (note: "45" string coerced to int) AND `mock_start_job.assert_called_once_with("job-123", {"foo": "bar"})`. Pins both the int-coercion AND the per-job dispatch with full args. |
| 391 | `test_processing_paused_cleared_on_startup` | **Strong** — pins `sm.processing_paused is False` post-call. Catches the bug where requeued jobs are dispatched but the pause flag is never cleared so they sit idle. |

## TestPrewarmCaches

| Line | Test | Verdict |
|---|---|---|
| 424 | `test_create_app_triggers_real_prewarm` | **Strong** — patches at the BOUNDARIES (vulkan, version, GPU helpers), not the SUT. The audit-fix comment at lines 412-421 explicitly explains the move from "mock _prewarm_caches" tautology to boundary-mocking. Asserts each boundary was called once. Exemplary. |
| 484 | `test_prewarm_calls_gpu_and_version` | **Strong** — direct call to `_prewarm_caches()` with the same boundary mocks. Slightly redundant with line 424 but covers the unit (not just the integration) — keep both. |

## Summary

- **16 tests** — 15 Strong, 1 Bug-blind
- **Bug-blind**: `TestRunScheduledJob::test_creates_and_starts_job` (line 116) — `assert_called_once()` with no kwarg check. **Needs fix**: should at least pin `mock_start.call_args.args[0]` is a job_id string AND that the job carries `library_name=="Movies"` via job_manager lookup (mirroring the pattern used at lines 250-253). This is the same D34-paradigm mistake the project explicitly calls out in `.claude/rules/testing.md`.
- The `TestRunScheduledJob::test_scheduled_job_infers_server_id_from_library_id` test (line 179) is a model adversarial test — two servers in fixture, only one is correct, with explicit narrative tied to incident.
- The `TestPrewarmCaches::test_create_app_triggers_real_prewarm` test is exemplary boundary-mocking.

**File verdict: MIXED.** One bug-blind test (line 116) needs strengthening — flag for fix.
