# Audit: tests/test_webhook_secret_rotation.py — 6 tests, 2 classes

## TestReregisterAfterSecretRotation

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 110 | `test_re_registers_every_plex_server` | **Strong** — `mock_register.call_count == 2` strict, AND for-loop verifies each call's `kwargs.get("auth_token") == "rotated-token"`. Per-call kwarg verification (not just count). The exact D34-paradigm closure. |
| 140 | `test_skips_non_plex_servers` | **Strong** — strict `call_count == 1` (only the Plex server, Emby skipped). Has explanatory message. |
| 161 | `test_skips_plex_server_without_webhook_public_url` | **Strong** — strict `call_count == 1`. Pins the URL-required gate. |
| 184 | `test_per_server_failure_does_not_block_other_servers` | **Strong** — custom `side_effect` raises for `plex-broken`; asserts `call_count["n"] == 2` (both ATTEMPTED). Catches the "bail on first failure" regression explicitly called out in the message. |
| 216 | `test_no_register_calls_when_no_plex_servers_configured` | **Strong** — strict `call_count == 0` for pure Emby install. |

## TestPostSettingsTriggersReregistration

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 238 | `test_post_webhook_secret_triggers_reregister_hook` | **BUG-BLIND (broken assertion)** — see below. The "assertion" at line 254-260 is a tuple expression, not an assert statement. `mock_hook.assert_called_once()` returns None, then a tuple `(None, "msg")` is constructed and immediately discarded. If the hook never fires, the test still passes. **NEEDS FIX.** |

### The bug at line 254-260

```python
(
    mock_hook.assert_called_once(),
    (
        "Posting webhook_secret to /api/settings must trigger the re-register hook. "
        "Without this wiring, rotated secrets silently break Plex webhooks."
    ),
)
```

This builds a 2-tuple as an expression statement. The `assert_called_once()` call DOES execute (so if the mock has zero calls AND mock raises, the test would catch it) — BUT:
1. There's no `assert` keyword wrapping it. If `assert_called_once()` raises `AssertionError`, the test still fails (so the call-count branch is partially covered).
2. The "message" string isn't connected to anything — it's just the second tuple element, doing nothing.
3. More importantly, the pattern reads like an attempt at `assert call(), "msg"` — which is almost certainly what the author intended. As-written, if `mock.assert_called_once()` is replaced or refactored to NOT raise (e.g. switched to `assert mock.called`), the message would silently disappear AND so would any AssertionError carrying useful diagnostics.

**Verdict: Functionally Strong-ish (the call_once check still raises) but Bug-blind in spirit — anyone reading this assumes both lines are part of a single assert.** The recommended fix is:

```python
assert mock_hook.call_count == 1, (
    "Posting webhook_secret to /api/settings must trigger the re-register hook. "
    "Without this wiring, rotated secrets silently break Plex webhooks."
)
```

## Summary

- **6 tests** total
- **5 Strong, 1 Broken-assertion** (`test_post_webhook_secret_triggers_reregister_hook` — tuple-expression "assertion" with no `assert` keyword; the call-count check IS still happening via `assert_called_once()` raising, but the message is dead and the pattern is a foot-gun)
- 0 tautological / dead

**File verdict: MIXED — one broken-assertion needs fixing.**

### Recommended fix

`tests/test_webhook_secret_rotation.py:254-260` — replace the tuple expression with a real `assert`:
```python
assert mock_hook.call_count == 1, (
    "Posting webhook_secret to /api/settings must trigger the re-register hook. "
    "Without this wiring, rotated secrets silently break Plex webhooks."
)
```
