# Audit: tests/test_servers_jellyfin_auth.py — 17 tests, 5 classes

## TestPasswordAuth

| Line | Test | Verdict |
|---|---|---|
| 19 | `test_success` | **Strong** — strict equality on `ok`, `access_token`, `user_id`. Mocks `requests.post` at the boundary, exercises real `authenticate_jellyfin_with_password` parsing of Jellyfin's response shape (`AccessToken` / `User.Id`). Not tautological. |
| 40 | `test_uses_authenticatebyname_endpoint` | **Strong** — pins the URL path (`/Users/AuthenticateByName`). Catches an accidental endpoint rename (vendor API contract). |
| 54 | `test_unauthorized_returns_specific_message` | **Strong** — `not result.ok` AND `"401" in result.message`. Substring is meaningful (status code in error message). |
| 67 | `test_missing_url_short_circuits` | **Strong** — audit-fixed: asserts `requests.post` was NOT called (no wasted round-trip), AND error message non-empty. The original was bug-blind on the network call; now it pins the short-circuit contract. |
| 82 | `test_missing_username_short_circuits` | **Strong** — same short-circuit assertion for the username field. |

## TestInitiateQuickConnect

| Line | Test | Verdict |
|---|---|---|
| 92 | `test_success_returns_code_and_secret` | **Strong** — `isinstance` + strict code/secret equality + non-empty message. |
| 105 | `test_401_explains_quick_connect_disabled` | **Strong** — `initiation is None` AND substring `"Quick Connect"` in message — ensures the operator-facing message references Quick Connect specifically, not a generic 401. |
| 114 | `test_missing_url` | **Strong** — `initiation is None` AND `"url"` in message lowercase — operator gets a useful diagnostic. |
| 119 | `test_response_missing_fields` | **Strong** — pins the validation that a missing `Secret` field is rejected. Catches a regression where the function would return a half-populated `QuickConnectInitiation`. |

## TestPollQuickConnect

| Line | Test | Verdict |
|---|---|---|
| 131 | `test_pending_returns_false` | **Strong** — strict `False` AND non-empty message. |
| 145 | `test_approved_returns_true` | **Strong** — strict `True`. |
| 158 | `test_404_handled` | **Strong** — `False` AND substring `"expired"` OR `"not found"` in message — pins user-facing diagnostic for an expired secret. |

## TestExchangeQuickConnect

| Line | Test | Verdict |
|---|---|---|
| 172 | `test_success` | **Strong** — strict equality on `ok` and `access_token`. |
| 190 | `test_401_explains_not_yet_approved` | **Strong** — substring `"approved"` in message — operator-facing diagnostic pinned. |
| 202 | `test_uses_authenticatewithquickconnect_endpoint` | **Strong** — pins the URL path. Catches endpoint rename. |
| 215 | `test_missing_secret` | **Weak (could be stronger)** — only asserts `not result.ok`. Doesn't assert the message references the missing secret, AND doesn't pin that `requests.post` is not called. A regression that hit the API with empty secret and got back 400 → `ok=False` would still pass. **Note for fixing**: add `post.assert_not_called()` and a substring on the message. |

## TestQuickConnectBlocking

| Line | Test | Verdict |
|---|---|---|
| 221 | `test_returns_token_when_approved_first_poll` | **Strong** — strict `ok=True` AND `access_token == "tok"`. Mocks `poll` and `exchange` at the right boundary; exercises the real `quick_connect_blocking` orchestration. |
| 242 | `test_times_out_when_never_approved` | **Strong** — `not result.ok` AND substring `"deadline"` in message. Pins both the failure status AND the user-facing diagnostic. |

## Summary

- **17 tests** total — 16 Strong, 1 Weak (`test_missing_secret` at line 215)
- Audit-fixed short-circuit tests now pin the no-network-call contract
- Vendor API endpoint paths pinned (catches accidental URL drift)

**File verdict: STRONG (one weak edge — `test_missing_secret` should also pin no-network-call and a useful error message).**

Recommended fix: tighten `test_missing_secret` to assert `requests.post` was not called and that the message mentions "secret" — mirroring the pattern used in the `test_missing_url_short_circuits` audit fix above it.
