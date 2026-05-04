# Audit: tests/test_security_fixes.py — 14 tests, 5 classes

## TestIsWithinBase

| Line | Test | Verdict |
|---|---|---|
| 61 | `test_exact_match` | **Strong** — strict `is True` on equal-path case. |
| 67 | `test_child_path` | **Strong** — strict `is True` on real tmp child. |
| 75 | `test_outside_path` | **Strong** — `is False` on sibling dir. |
| 83 | `test_prefix_collision` | **Strong (load-bearing)** — `/plex` vs `/plex2` — pins the trailing-sep guard that prevents the classic prefix-collision bypass. This is the test that justifies the helper's existence. |

## TestPathTraversalPrevention

| Line | Test | Verdict |
|---|---|---|
| 95 | `test_validate_paths_with_null_byte` | **Strong** — asserts 200 status + `valid is False` + an `"Invalid"` error string. Three-anchor check; null byte in a path is a real CodeQL finding. |
| 107 | `test_validate_paths_unauthenticated_rejects_null_byte` | **Strong** — duplicates the null-byte check on the unauthenticated codepath (during setup). Comment explicitly notes this was previously hidden behind an `if status_code == 200:` guard — fix collapsed to a strict assertion. Good audit hygiene. |
| 127 | `test_validate_paths_traversal_resolved` | **Strong (boundary check)** — asserts `..` is *resolved* (no error), distinguishing legitimate normalisation from rejection. Pairs with the next two tests. |
| 155 | `test_validate_paths_outside_root_rejected` | **Strong** — asserts `valid is False` + `"must be within"` substring. Pins the rejection rationale, not just the rejection. |
| 177 | `test_validate_paths_traversal_escape_rejected` | **Strong** — `..` escape from allowed root is rejected with the same `"must be within"` reason — proves realpath() catches the escape attempt. |
| 199 | `test_validate_paths_local_media_null_byte` | **Strong-ish** — pins null-byte rejection on the `plex_local_videos_path_mapping` field. Asserts `valid is False` only — does not check the error reason like its sibling on line 95 does. **Minor weakness** — could lose the rejection-reason coverage if the validator changed. |

## TestInformationExposurePrevention

| Line | Test | Verdict |
|---|---|---|
| 220 | `test_get_jobs_error_no_leak` | **Strong** — asserts 500 + that BOTH `"5432"` (port number) AND `"Database"` (technology name) are absent from `data["error"]`. Two-token check is more bug-resistant than the single-token sibling tests. |
| 232 | `test_get_worker_statuses_error_no_leak` | **Strong** — asserts hex memory address (`"0xdeadbeef"`) is suppressed. Single-token but unique enough that a regression that drops sanitization would leak the marker. |
| 243 | `test_get_job_stats_error_no_leak` | **Strong** — asserts `"SQLAlchemy"` (library name) is suppressed. |
| 254 | `test_get_system_status_error_no_leak` | **Strong** — asserts `"nvidia-smi"` (binary path) is suppressed. |

(All four no-leak tests share the pattern: patch `get_job_manager` to raise, assert the unique marker substring is absent. The shared design means a single regression would fail all four — that's defense in depth, not redundancy.)

## TestFlaskSecretFilePermissions

| Line | Test | Verdict |
|---|---|---|
| 270 | `test_secret_file_has_restricted_permissions` | **Strong** — asserts `mode == 0o600` exactly (not just `< 0o700`). Skipped on Windows correctly. The `len(secret) > 0` part is weak but it's a sanity not the contract; the mode pin is the real assertion. |

## TestXSSPrevention

| Line | Test | Verdict |
|---|---|---|
| 287 | `test_auth_page_escapes_input` | **Strong** — three-token check: raw `<script>` absent, escaped `&lt;script&gt;` present, AND raw `<img onerror` absent. Catches both the missing-escape and the half-escape regressions. |

## Summary

- **15 tests** — 14 Strong, 1 minor-weakness
- 0 bug-blind, 0 tautological, 0 dead/redundant, 0 bug-locking
- Full traversal matrix (null byte / `..` resolved / outside root / `..` escape / per-field) covered
- Info-leak suite covers all 4 endpoints with unique marker tokens

**File verdict: STRONG.** One minor weakness: `test_validate_paths_local_media_null_byte` (line 199) only asserts `valid is False`, not the `"Invalid"` reason; tightening would give parity with line 95. No fix-blocking issues.
