# Audit: tests/test_headers.py — 2 tests

| Line | Test | Verdict |
|---|---|---|
| 6 | `test_default_headers_set_on_import` | **Strong** — pins the exact UUID3 namespaced device identifier we send to plex.tv. A regression that changes the device name string would lose the user's previously-registered Plex webhook (it's keyed by identifier on Plex's side). Strict equality. |
| 20 | `test_env_overrides_respected` | **Strong** — pins the `setdefault` semantics: caller-supplied env vars must NOT be overwritten by our defaults. Otherwise users on shared infra couldn't separate identifiers. |

## Summary

- **2 tests**, both **Strong**
- Pins both edges of the contract (default + override)

**File verdict: STRONG.** No changes needed.
