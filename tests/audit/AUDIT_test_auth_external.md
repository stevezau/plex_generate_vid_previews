# Audit: tests/test_auth_external.py — 21 tests, 6 classes

## TestGetAuthMethod

| Line | Test | Verdict |
|---|---|---|
| 59 | `test_default_is_internal` | **Strong** — strict equality after `delenv`. Pins the safe default (don't accidentally start in external mode). |
| 63 | `test_external_lowercase` | **Strong** — strict equality. |
| 67 | `test_external_uppercase` | **Strong** — pins the `.lower()` normalisation. |
| 71 | `test_external_mixed_case` | **Strong** — same matrix cell. |
| 75 | `test_external_with_whitespace` | **Strong** — pins `.strip()` normalisation. |
| 79 | `test_internal_explicit` | **Strong** — explicit "internal" round-trips. |
| 83 | `test_invalid_value_falls_back_to_internal` | **Strong** — security default: anything unrecognised → safe (internal). Catches a regression that defaulted to external on garbage. |
| 87 | `test_empty_string_falls_back_to_internal` | **Strong** — empty env var → internal. |

## TestIsAuthExternal

| Line | Test | Verdict |
|---|---|---|
| 95 | `test_false_by_default` | **Strong** — pins helper composition. |
| 99 | `test_true_when_external` | **Strong** — strict bool. |
| 103 | `test_false_when_internal` | **Strong** — strict bool. |

## TestExternalAuthRouteBehavior

| Line | Test | Verdict |
|---|---|---|
| 115 | `test_dashboard_accessible_without_token` | **Strong** — pins HTTP 200 (not 302 to login) when external auth is active. The bug class this protects against is "external auth env set but decorator still redirects" — exact production-shipping risk. |
| 123 | `test_api_jobs_accessible_without_token` | **Strong** — same contract for `/api/jobs`. |
| 131 | `test_api_settings_accessible_without_token` | **Strong** — same contract for `/api/settings`. |
| 139 | `test_api_auth_status_reports_external` | **Strong** — strict `authenticated is True` AND `auth_method == "external"` (a UI-consumed contract). |
| 147 | `test_login_page_redirects_to_dashboard` | **Strong** — strict 302 AND `Location.endswith("/")`. Pins the redirect target precisely. |

## TestInternalAuthUnchanged

| Line | Test | Verdict |
|---|---|---|
| 164 | `test_api_jobs_requires_auth` | **Strong** — strict 401 without token. The complement of the external test — proves toggling `AUTH_METHOD` actually changes behaviour. |
| 172 | `test_api_jobs_with_token_succeeds` | **Strong** — strict 200 with valid token. |
| 180 | `test_api_auth_status_reports_internal` | **Strong** — strict `auth_method == "internal"`. |

## TestWebhookAuthNotBypassed

| Line | Test | Verdict |
|---|---|---|
| 194 | `test_radarr_webhook_requires_token` | **Strong** — security-critical: webhook tokens are NOT bypassed by external mode. Strict 401. |
| 203 | `test_sonarr_webhook_requires_token` | **Strong** — same matrix cell for sonarr. |
| 212 | `test_custom_webhook_not_auto_authenticated` | **Strong** — audit-fixed: enumerates `(302, 401, 403)` instead of the original `!= 200` (which would have passed on a 500). The audit comment explains the fix in detail. |
| 239 | `test_radarr_webhook_succeeds_with_valid_token` | **Strong** — pins the positive case: with valid token, webhook works even in external mode. |

## TestExternalAuthReenableInternal

| Line | Test | Verdict |
|---|---|---|
| 253 | `test_switch_from_external_to_internal` | **Strong** — pins state transition: env-var change between requests is honoured (no caching issue). Two distinct status code assertions (200 then 401). |

## TestTokenInfoIncludesAuthMethod

| Line | Test | Verdict |
|---|---|---|
| 270 | `test_token_info_internal` | **Strong** — strict equality on returned dict field. |
| 277 | `test_token_info_external` | **Strong** — strict equality on returned dict field. |

## Summary

- **25 tests** total, all **Strong**
- Security boundary fully covered: env parsing matrix (8 cases), helper composition (3), route-decorator bypass (5), internal-still-strict (3), webhook-NOT-bypassed (4), state transition (1), introspection (2)
- Audit-fixed `test_custom_webhook_not_auto_authenticated` previously had a `!= 200` assertion that would have hidden 500s; now enumerates acceptable rejection codes

**File verdict: STRONG.** No changes needed. The webhook-not-bypassed tests in particular pin a security-critical contract that's easy to regress.
