# Audit: tests/test_socketio.py — 10 tests, 4 classes

## TestSocketIOConnection

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 131 | `test_authenticated_client_connects` | **Strong** — strict `is_connected` check after the auth fixture sets `session["authenticated"] = True`. Pins the connect-handler accept path. |
| 134 | `test_unauthenticated_client_rejected` | **Strong** — strict `not is_connected`. Pins the connect-handler reject path; if the handler stops calling `disconnect()` on auth failure, the test fires. |
| 140 | `test_disconnect_works` | **Strong (mostly framework)** — calls `.disconnect()` then `not is_connected`. Mostly tests flask-socketio's test client, but cheap to keep as a smoke for the namespace plumbing. |

## TestSubscription

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 153 | `test_subscribe_to_job` | **Strong** — in-file audit comment notes the prior tautology (`isinstance(received, list)`) was replaced. Now asserts `received == []` (silent), `is_connected` after, AND retests with empty payload `{}` to hit the `if job_id:` early-return branch. Two-cell coverage. |
| 172 | `test_unsubscribe_from_job` | **Strong** — also called out in-file as previously tautological; now asserts silent + still-connected + idempotent (calling unsubscribe twice doesn't crash). |

## TestJobEvents

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 228 | `test_job_created_event` | **Strong** — waits for `job_created`, asserts payload `id == job.id` AND `library_name == "Movies"`. Specific equality on payload content, not just "event fired". |
| 245 | `test_job_started_event` | **Weak (event-only)** — asserts `"job_started" in event_names` but does NOT inspect payload. A regression that emitted `job_started` with the wrong job_id or stale status would still pass. Not catastrophic since the start contract is thin, but inconsistent with `test_job_created_event` and `test_progress_update_event` which DO check payload. **Could be tightened to assert `payload["id"] == job.id`** but not bug-blind in the D34 sense (no boundary kwargs are asserted-via-`assert_called_once`). |
| 259 | `test_progress_update_event` | **Strong** — in-file audit comment confirms a deliberate strengthening pass. Asserts `payload["job_id"] == job.id`, `progress["percent"] == 50.0`, `processed_items == 5`, `total_items == 10`, `current_item == "Episode 5"`. Full payload pin. |
| 298 | `test_job_completed_event` | **Strong** — asserts the event AND `args[0]["status"] == "completed"`. Pins the status field on the wire. |

## TestSocketIOTransportConfig

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 339 | `test_allow_upgrades_is_false_on_underlying_engineio_server` | **Strong** — introspects the actual engineio server (the real source of truth). Pins the 1873a23 regression (WebSocket-upgrade pinning a gunicorn thread). Asserts `eio_server.allow_upgrades is False` strictly. |
| 359 | `test_async_mode_is_threading` | **Strong** — strict `socketio.async_mode == "threading"` with explanatory message tying back to GitHub #154 (eventlet/gevent breaks subprocess). |

## Summary

- **10 tests** total
- **9 Strong, 1 Weak** (`test_job_started_event` checks event presence only — could match the rigor of its sibling tests by inspecting `payload["id"]`)
- 0 bug-blind / tautological / dead

**File verdict: STRONG (one Weak that should be tightened to match its siblings).**

### Recommended fix

`tests/test_socketio.py:245` `test_job_started_event` — add payload assertion:
```python
started_events = [r for r in received if r["name"] == "job_started"]
assert started_events[0]["args"][0]["id"] == job.id
```
to mirror the rigor of `test_job_created_event` / `test_progress_update_event`.
