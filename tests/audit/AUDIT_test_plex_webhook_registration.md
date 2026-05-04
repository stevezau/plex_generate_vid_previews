# Audit: tests/test_plex_webhook_registration.py — 19 tests, no class wrapping (module-level)

## Module-level tests

| Line | Test | Verdict |
|---|---|---|
| 21 | `test_register_adds_url_when_not_present` | **Strong** — `assert_called_once_with(<full URL with token>)` pins the exact arg sent to plex.tv. NOT bug-blind: arg is asserted. |
| 34 | `test_register_embeds_token_when_auth_provided` | **Strong** — `assert_called_once_with(...?token=abc)` — pins token-embedding contract. |
| 44 | `test_register_removes_legacy_plex_endpoint_when_targeting_incoming` | **Strong** — pins both `deleteWebhook(<old>)` AND `addWebhook(<new>)` with full URLs. Two-step migration covered. |
| 62 | `test_register_replaces_stale_url_with_old_token` | **Strong** — pins delete(OLD)+add(NEW). Token-rotation case. |
| 76 | `test_register_is_idempotent_when_url_already_present` | **Strong** — `assert_not_called()` on `addWebhook` (pins idempotency, no double-add). |
| 84 | `test_register_strips_trailing_slash` | **Strong** — pins normalized URL passed to `addWebhook`. |
| 93 | `test_register_missing_token_raises` | **Strong** — pins `exc_info.value.reason == "missing_token"` (not just that *some* exception fires; pins the reason code that the UI branches on). |
| 99 | `test_register_missing_url_raises` | **Strong** — pins `reason == "missing_url"`. Distinct error code. |
| 105 | `test_register_no_plex_pass_surfaces_clean_error` | **Strong** — pins `reason == "plex_pass_required"` (UI shows a distinct message for this). |
| 115 | `test_unregister_removes_existing_webhook_with_token` | **Strong** — pins `deleteWebhook` called with the registered URL (with `?token=`), even when caller passed the bare URL. |
| 125 | `test_unregister_url_not_present_is_noop` | **Strong** — `deleteWebhook.assert_not_called()` pins no-op. |
| 134 | `test_is_registered_returns_true_for_url_with_embedded_token` | **Strong** — pins True when stored URL has `?token=` even if probe URL doesn't. Catches the matching-with-token bug. |
| 143 | `test_build_authenticated_url_appends_token` | **Strong** — strict equality on `?token=abc`. |
| 148 | `test_build_authenticated_url_replaces_existing_token` | **Strong** — pins replacement of OLD with NEW. |
| 153 | `test_build_authenticated_url_empty_token_returns_base` | **Strong** — pins empty-token short-circuit (URL unchanged). |
| 158 | `test_is_registered_swallows_errors` | **Strong** — pins `False` on network failure (UI-probe contract: never raises). |
| 164 | `test_has_plex_pass_true_when_subscription_active` | **Strong** — pins True. |
| 171 | `test_has_plex_pass_false_when_no_subscription_and_webhooks_fail` | **Strong** — pins False — covers the multi-fallback (subscriptionActive False, hasPlexPass False, webhooks raise). |
| 180 | `test_has_plex_pass_swallows_missing_token` | **Strong** — pins False on empty token. |
| 184 | `test_list_webhooks_normalizes_urls` | **Strong** — strict equality on the normalized list (trailing slashes stripped). |

## Coverage gaps (informational, not failures)

- `_build_authenticated_url(..., server_id=...)` (the K6 multi-server feature, pwh.py:122) is **not** covered. `register(..., server_id=...)` keyword has no test. If a future change breaks the `server_id` query param, no test fires. Worth adding a row.

## Summary

- **19 tests** — 19 Strong, 0 Weak / Bug-blind / Tautological
- All `assert_called_*` calls use `_with(...)` — no D34-style call-count-only checks.
- All custom-exception tests pin `.reason` (not just exception type) — UI branches on this.
- **Coverage gap**: `server_id` parameter in `_build_authenticated_url` and `register` is untested. Not a failed test, just missing rows.

**File verdict: STRONG.** No fixes needed; consider adding `server_id` coverage rows.
