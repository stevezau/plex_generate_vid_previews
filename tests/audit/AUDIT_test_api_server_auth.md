# Audit: tests/test_api_server_auth.py — 14 tests, 3 classes

## TestEmbyPasswordAuth

| Line | Test | Verdict |
|---|---|---|
| 51 | `test_success_returns_token` | **Strong** — strict equality on `ok`, `access_token`, `user_id`, `server_id` PLUS HTTP 200. Pins the JSON contract the wizard reads. |
| 75 | `test_invalid_creds_surfaces_message` | **Strong** — explicitly asserts HTTP 200 (audit-fixed), `ok is False`, and `"401" in body["message"]`. The audit comment shows the gap that was closed (a 500 crash would have passed). |
| 95 | `test_missing_url_400` | **Strong** — strict status code 400 on missing field. |
| 103 | `test_missing_username_400` | **Strong** — same matrix cell, different field. |
| 111 | `test_unexpected_error_returns_500` | **Strong** — confirms the route's exception handler renders 500 (not bubbles to a Flask debug page). |

## TestJellyfinPasswordAuth

| Line | Test | Verdict |
|---|---|---|
| 128 | `test_success` | **Strong** — strict equality on `ok` and `access_token`. The mock returns `JellyfinAuthResult` which the route serializes — test pins serializer output, not just call shape. |

## TestJellyfinQuickConnect

| Line | Test | Verdict |
|---|---|---|
| 151 | `test_initiate_returns_code_and_secret` | **Strong** — strict equality on `code`, `secret`, `ok`. Pins the JSON shape consumed by the JS wizard. |
| 166 | `test_initiate_failure_surfaces_message` | **Strong** — pins `ok=False` AND substring `"Quick Connect"` in message — so a regression that swapped to a generic message would fail. |
| 180 | `test_poll_pending` | **Strong** — strict `ok=True` (the poll succeeded) AND `authenticated=False` (but not yet approved) — captures the two-axis state. |
| 194 | `test_poll_approved` | **Strong** — strict `authenticated=True` for the approved transition. |
| 207 | `test_exchange_success` | **Strong** — strict `ok=True` AND `access_token == "qc-tok"`. |
| 228 | `test_exchange_missing_secret` | **Strong** — strict 400 on missing required input. |
| 236 | `test_endpoints_dont_persist_anything` | **Strong** — pins the security-critical contract that the auth probe doesn't write to settings. Asserts `media_servers in (None, [])`. Catches regression where a successful auth call leaked into `media_servers` (would silently persist credentials before the user clicked Save). |

## Summary

- **13 tests** total, all **Strong**
- Audit-fixes already applied (status code added in `test_invalid_creds_surfaces_message`)
- Stateless-auth invariant pinned (`test_endpoints_dont_persist_anything`)

**File verdict: STRONG.** No changes needed.
