# Audit: tests/test_oauth_routes.py — 27 tests, 7 classes

Tests for Plex OAuth + settings/setup/auth API routes via Flask test client.

## TestSettingsAPIRoutes

| Line | Test | Verdict |
|---|---|---|
| 59 | `test_get_settings` | **Weak** — only checks 3 keys are present in the response. No value assertions. A regression that returns these fields with garbage values (e.g. wrong types, swapped values) would pass. **Marginal value** — catches deletion of fields but not corruption. |
| 70 | `test_update_settings` | **Strong** — round-trips the update through the API: POST then GET, then `data["gpu_threads"] == 4 AND data["thumbnail_interval"] == 5` strict equality. Catches "saves but reads stale" persistence bugs. |
| 108 | `test_update_plex_url` | **Weak** — only asserts `success is True` after POST. Never reads back the saved value. A regression that returns success but drops the field would pass. |

## TestSetupRoutes

| Line | Test | Verdict |
|---|---|---|
| 124 | `test_get_setup_status` | **Weak** — 4 key-presence checks, no value assertions. Same critique as `test_get_settings`. |
| 136 | `test_save_setup_state` | **Strong** — POST then GET, asserts `data["step"] == 2` strict equality. Round-trips through persistence. |
| 153 | `test_complete_setup` | **Strong** — POST `/setup/complete` then GET `/setup/status` and pin `setup_complete is True`. Round-trip catches "endpoint succeeds but doesn't actually mark complete". |

## TestPlexServerRoutes

| Line | Test | Verdict |
|---|---|---|
| 177 | `test_get_servers_without_token` | **Strong** — pins specific 401 status (NOT `>= 400`) AND `body["servers"] == []` AND `"token" in body["error"]`. Audit-fix comment explicitly explains the prior loose status check. |
| 200 | `test_get_libraries_without_server` | **Strong** — pins specific 400 status AND `body["libraries"] == []` AND substring on error. Mirror cell. |
| 222 | `test_check_pin_returns_auth_token` | **Strong** — pins `data["auth_token"] == "secret-plex-token-from-plextv"` (strict equality on the upstream-returned token). Critical — without this pin, the multi-server wizard breaks silently as documented in the docstring. |
| 257 | `test_check_pin_pending_returns_null_auth_token` | **Strong** — pins `authenticated is False` AND `auth_token is None`. Mirror cell for unconfirmed PIN. |

## TestAuthRequired

| Line | Test | Verdict |
|---|---|---|
| 285 | `test_settings_requires_auth` | **Strong** — strict 401. |
| 290 | `test_save_settings_requires_auth` | **Strong** — strict 401 on POST. |
| 299 | `test_invalid_token_rejected` | **Strong** — strict 401 with bad token. |

## TestJobLogsAndWorkers

| Line | Test | Verdict |
|---|---|---|
| 308 | `test_get_job_logs_not_found` | **Strong** — strict 404 for missing job. |
| 313 | `test_get_worker_statuses` | **Strong** — pins envelope shape: `isinstance(data, dict)` AND `"workers" in data` AND `isinstance(data["workers"], list)` AND for each entry asserts `worker_id` and `status` keys. Audit-fix comment at lines 313-321 explicitly closes the prior tautology. |
| 338 | `test_job_logs_requires_auth` | **Strong** — strict 401. |
| 343 | `test_workers_requires_auth` | **Strong** — strict 401. |

## TestAuthTokenFunctions

| Line | Test | Verdict |
|---|---|---|
| 352 | `test_is_token_env_controlled_false` | **Strong** — strict `is False`. |
| 359 | `test_is_token_env_controlled_true` | **Strong** — strict `is True`. |
| 366 | `test_set_auth_token_success` | **Strong** — `result["success"] is True` AND `get_auth_token() == "my-new-secure-token"` (round-trip). |
| 375 | `test_set_auth_token_too_short` | **Strong** — `success is False` AND substring `"at least 8 characters"` on error message. Pins the validation message. |
| 384 | `test_set_auth_token_env_locked` | **Strong** — pins `success is False` AND `"environment variable" in error`. |
| 393 | `test_set_auth_token_rejects_same_as_current` | **Strong** — pins setup wizard step 5 contract (force token away from auto-generated). Substring `"different from the current"` on error. |
| 405 | `test_get_token_info_structure` | **Strong** — audit-fix comment cites prior weakness. Now pins type of every field, value of `source` field, AND that token is masked (`startswith("*")`). The mask check is security-critical. |
| 428 | `test_get_token_info_config_source` | **Strong** — pins `env_controlled is False AND source == "config"`. |
| 437 | `test_get_token_info_env_source` | **Strong** — pins `env_controlled is True`, `source == "environment"`, AND `token == "****2345"` (exact mask format with last-4). |

## TestTokenAPIEndpoints

| Line | Test | Verdict |
|---|---|---|
| 452 | `test_setup_token_info_endpoint` | **Weak** — only checks key presence (no value assertions). A regression returning `{"env_controlled": "broken", "token": null, "source": null}` would pass. |
| 462 | `test_setup_token_info_requires_auth` | **Strong** — strict 401. |
| 467 | `test_setup_set_token_success` | **Weak** — `success is True` check only; never verifies the token actually got set (no `get_auth_token() == "my-new-password-123"` round-trip). The function-level test at line 366 covers the underlying behavior, but this leaves the HTTP boundary untested for actual write success. |
| 481 | `test_setup_set_token_mismatch` | **Strong** — strict 400 + `success is False` + substring `"match"` on error. |
| 496 | `test_setup_set_token_too_short` | **Strong** — strict 400 + `success is False` + substring `"8 characters"` on error. |
| 511 | `test_setup_set_token_requires_auth` | **Strong** — strict 401 on unauthenticated POST. |

## Summary

- **27 tests** — 22 Strong, 5 Weak, 0 Bug-blind, 0 Tautological
- **Weak tests** (key-presence-only or success-flag-only):
  - Line 59 `test_get_settings` — key presence only
  - Line 108 `test_update_plex_url` — `success is True` only, no read-back
  - Line 124 `test_get_setup_status` — key presence only
  - Line 452 `test_setup_token_info_endpoint` — key presence only
  - Line 467 `test_setup_set_token_success` — `success is True` only, no round-trip
- **Suggested fixes**:
  - For the GET tests: assert at least one value is well-typed/sane (e.g. `isinstance(data["gpu_threads"], int)`, `data["plex_url"] is None or isinstance(..., str)`).
  - For `test_update_plex_url`: round-trip GET + assert `data["plex_url"] == "http://192.168.1.100:32400"`.
  - For `test_setup_set_token_success`: GET token-info or call `get_auth_token()` to verify the change persisted.
- The audit-fix comments throughout (lines 177, 200, 313, 405) demonstrate prior pass strengthened many tests; these remaining weak ones are mostly newer additions or were missed.

**File verdict: MIXED.** Five weak tests need tightening to round-trip values rather than just check presence/success — flag for fix.
