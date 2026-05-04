# Audit: tests/test_servers_emby_auth.py — 12 tests, 2 classes

## TestAuthorizationHeader

| Line | Test | Verdict |
|---|---|---|
| 17 | `test_includes_required_fields` | **Strong** — pins all 4 required field markers (`Client=`, `Device=`, `DeviceId="abc123"`, `Version=`) AND the leading scheme (`MediaBrowser `). Substring checks but on tokens that *Emby/Jellyfin reject without* — contract pins. |

## TestAuthenticateEmbyWithPassword

| Line | Test | Verdict |
|---|---|---|
| 28 | `test_success_returns_token_and_user_id` | **Strong** — pins `ok=True`, `access_token`, `user_id`, `server_id` by strict equality. NOT tautological — boundary is at `requests.post`, not at the SUT. |
| 51 | `test_calls_correct_endpoint_with_correct_body` | **Strong** — pins URL path (`/Users/AuthenticateByName`), full JSON body (`{"Username": "admin", "Pw": "pw"}`), and Authorization header content (scheme + DeviceId). Catches arg-shape regressions (D34-class). |
| 71 | `test_strips_trailing_slash_from_base_url` | **Strong** — strict URL equality after slash strip. |
| 86 | `test_401_returns_specific_message` | **Weak — substring only** — asserts `"401" in result.message`. The status code in the message is the user-visible signal but the assertion would pass for any message containing "401" (e.g., "got 4011 errors"). Acceptable today but lookups via `==` would be tighter. Keep as-is unless tightening pass. |
| 100 | `test_403_returns_specific_message` | **Weak — substring only** — same shape as 401. Acceptable. |
| 114 | `test_other_4xx_5xx_returns_status_in_message` | **Weak — substring only** — `"500" in message`. Same shape. |
| 128 | `test_missing_access_token_treated_as_failure` | **Strong** — pins `ok=False` AND `"AccessToken" in message` (the missing field name surfaces to the user). |
| 143 | `test_invalid_json_treated_as_failure` | **Strong** — pins `ok=False` AND `result.message` truthy (per the inline audit-fix note: catches the regression that returns `ok=False` with empty message → blank UI error). |
| 164 | `test_timeout_returns_specific_message` | **Strong** — pins `ok=False` AND `"timed out" in message.lower()`. The substring is the load-bearing user signal. |
| 177 | `test_ssl_error_returns_specific_message` | **Strong** — pins `ok=False` AND `"ssl" in message.lower()`. |
| 190 | `test_missing_url` | **Strong** — pins `ok=False` AND `"url" in message.lower()`. |
| 199 | `test_missing_username` | **Strong** — pins `ok=False` AND `"username" in message.lower()`. |

## Summary

- **13 tests** — 10 Strong, 3 Weak (substring-only on numeric status codes in messages)
- Weak rows (lines 86, 100, 114): assertions like `"401" in result.message` would pass on accidental message changes (e.g. `"4015 widgets failed"`). The intent is clear; the assertion isn't airtight. To tighten, assert the full message OR use `re.search(r"\b401\b", message)`.
- The boundary discipline is correct everywhere: mocks at `requests.post`, not at the SUT.
- The `test_invalid_json` row was already strengthened by an earlier audit pass (per inline comment).

**File verdict: MIXED — mostly Strong, 3 Weak rows worth tightening.**

### Suggested fixes (do not apply now per task scope)
- Lines 98, 112, 126: replace `assert "<code>" in result.message` with `assert re.search(rf"\b{code}\b", result.message)` or assert exact expected message text.
