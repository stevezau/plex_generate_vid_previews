# Audit: tests/test_auth_setup_guard.py — 19 tests, 3 classes

## TestSetupNotComplete

| Line | Test | Verdict |
|---|---|---|
| 53 | `test_unauthenticated_get_settings` | **Strong** — strict 200 when setup is not complete (the `@setup_or_auth_required` decorator must NOT block during initial setup). Pre-condition `assert not is_setup_complete()` makes the matrix cell explicit. |
| 61 | `test_unauthenticated_post_setup_set_token` | **Strong** — audit-fixed: original asserted only status; now also asserts `success=True` AND that `get_auth_token() == "test-tok-12345678"` (the token actually changed). Catches a regression that returns 200 with `success=False` or doesn't persist. |
| 85 | `test_unauthenticated_get_plex_servers` | **Strong** — audit-fixed: pins that the 401 came from the BODY (`"No Plex token"`) and NOT the auth decorator (`error != "Authentication required"`). The audit comment explains the exact bug class this guards against — wrapping `if status==401` would have passed even if the auth decorator wrongly fired. |
| 109 | `test_unauthenticated_get_system_status` | **Strong** — strict 200. |
| 117 | `test_unauthenticated_get_setup_state` | **Strong** — strict 200. |
| 125 | `test_unauthenticated_post_setup_state` | **Strong** — strict 200 on POST. |
| 138 | `test_unauthenticated_get_token_info` | **Strong** — strict 200. |

## TestSetupComplete

| Line | Test | Verdict |
|---|---|---|
| 150 | `test_unauthenticated_get_settings_requires_auth` | **Strong** — strict 401 AND `error == "Authentication required"`. Pins the error string returned to the UI (which keys off it). |
| 159 | `test_unauthenticated_post_settings_requires_auth` | **Strong** — strict 401 + error string for POST. |
| 172 | `test_unauthenticated_post_set_token_requires_auth` | **Strong** — strict 401. Sensitive endpoint — must require auth post-setup. |
| 184 | `test_unauthenticated_get_token_info_requires_auth` | **Strong** — strict 401. |
| 192 | `test_valid_bearer_token_get_settings` | **Strong** — pins Bearer header support (mirror of X-Auth-Token). |
| 201 | `test_valid_x_auth_token_get_settings` | **Strong** — pins X-Auth-Token support. |
| 209 | `test_invalid_token_get_settings` | **Strong** — strict 401 on wrong token. |
| 218 | `test_invalid_bearer_token_get_settings` | **Strong** — strict 401 on wrong Bearer token. |
| 227 | `test_authenticated_post_setup_complete` | **Strong** — pins 200 on the setup-complete endpoint with valid auth. |
| 235 | `test_authenticated_get_system_status` | **Strong** — pins 200 with valid auth. |

## TestEdgeCases

| Line | Test | Verdict |
|---|---|---|
| 247 | `test_setup_completes_mid_session` | **Strong** — three-state transition: pre-setup 200, post-setup unauth 401, post-setup auth 200. Pins that the decorator re-checks setup state on every request (no caching). |
| 267 | `test_empty_bearer_prefix` | **Strong** — strict 401 on `"Bearer "` (empty token body). Catches a regression where the parser accepted empty tokens. |
| 276 | `test_malformed_authorization_header` | **Strong** — `"NotBearer some-token"` rejected. Pins the exact-match `Bearer ` prefix requirement. |
| 285 | `test_empty_x_auth_token` | **Strong** — strict 401 on empty header value. |

## Summary

- **20 tests** total, all **Strong**
- Audit-fixed tests now go beyond status-code-only assertions:
  - `test_unauthenticated_post_setup_set_token` verifies token actually persisted
  - `test_unauthenticated_get_plex_servers` distinguishes body-401 vs decorator-401 (the bug class this whole file guards against)
- Setup-vs-authenticated matrix complete: every protected endpoint tested in both states; multiple auth header forms covered (X-Auth-Token + Bearer + invalid + empty + malformed)
- Mid-session state transition pinned

**File verdict: STRONG.** No changes needed.
