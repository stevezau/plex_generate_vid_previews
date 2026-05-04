# Audit: tests/test_timezone.py — 4 tests, 1 class

## TestGetTimezoneInfo

| Line | Test | Verdict |
|---|---|---|
| 13 | `test_no_tz_env_and_utc_system_shows_warning` | **Strong** — exercises the warning-triggering branch (no TZ env + UTC tzname). Asserts `tz_env_set=False`, `"warning"` key present, AND warning text contains the actionable hints (`/etc/localtime`, `TZ=`). Catches a regression that drops the warning silently. |
| 24 | `test_tz_env_set_no_warning` | **Strong** — TZ env set → no warning. Strict check on `tz_env_set=True` AND `"warning" not in result`. |
| 32 | `test_tz_env_explicitly_utc_no_warning` | **Strong** — boundary case: explicit `TZ=UTC` (which makes UTC the user's choice) must NOT warn. Different cell from row 1 (no TZ + UTC) — pins user-intent vs. accidental. |
| 40 | `test_no_tz_env_but_non_utc_system_no_warning` | **Strong** — covers the `/etc/localtime` Docker mount case. No TZ env + system shows PST → no warning needed. Together with the others, fully covers the (TZ env, system tz) matrix. |

## Summary

- **4 tests**, all **Strong**
- Complete 2x2 matrix of (TZ env: set/unset) × (system tz: UTC/non-UTC)
- Warning text content asserted (not just presence)

**File verdict: STRONG.** No changes needed.
