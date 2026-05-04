# Audit: tests/test_jobs.py — 39 tests, 11 classes

## TestJobLogPersistence

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 44 | `test_add_log_writes_to_file` | **Strong** | Asserts file exists, both lines present, AND exact newline count (2). Catches double-write or missing-flush bugs. |
| 62 | `test_get_logs_reads_from_file_after_restart` | **Strong** | Real round-trip via fresh JobManager; pins `len == 1` AND substring. Catches in-memory-only regressions. |
| 76 | `test_get_logs_returns_retention_message_when_file_missing_but_job_exists` | **Strong** | Strict `==` to module-level constant. Catches drifted message text. |
| 85 | `test_get_logs_returns_empty_when_job_does_not_exist` | **Strong** | Strict `== []` (vs raise/None). |

## TestLogRetentionEnforcement

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 96 | `test_enforce_retention_removes_expired_jobs` | **Strong** | Pins both `get_job is None` and log-file-removed in one shot. Backdate via attribute fixes wall-clock dependency. |
| 120 | `test_enforce_retention_keeps_recent_jobs` | **Strong** | Mirror cell — recent jobs survive both logically and on disk. |
| 138 | `test_enforce_retention_keeps_running_jobs` | **Strong** | Pins the "running jobs are immune to age" branch — the most production-critical guard. |
| 156 | `test_enforce_retention_removes_orphaned_log_files` | **Strong** | Catches orphan log-file leakage if a job was deleted bypass-the-API. |

## TestLogFileCleanup

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 175 | `test_delete_job_removes_log_file` | **Strong** | Asserts return value (`is True`) AND file gone. |
| 191 | `test_clear_completed_jobs_removes_log_files` | **Strong** | Pins return count (`== 1`) AND file removed. |
| 208 | `test_clear_logs_removes_file` | **Strong** | Plain file removal contract. |

## TestRetentionTimer

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 226 | `test_timer_starts_on_init` | **Strong** | `is not None` AND `daemon is True` — daemon flag matters for clean shutdown. |
| 234 | `test_timer_can_be_stopped` | **Strong** | Pins `_retention_timer is None` after stop. |

## TestCompleteJobWarning

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 245 | `test_complete_with_warning_sets_completed_status` | **Strong** | Strict status enum + exact error string. |
| 257 | `test_complete_with_error_sets_failed_status` | **Strong** | Mirror cell. |
| 269 | `test_complete_without_args_sets_completed_no_error` | **Strong** | Default cell — pins COMPLETED + `error is None`. |
| 281 | `test_error_takes_precedence_over_warning` | **Strong** | Pins precedence — UI behavior depends on it. |
| 293 | `test_warning_emits_job_completed_event` | **Strong** | Audit-fixed: exact-count + payload assertions. Used to be `>= 1`. |
| 316 | `test_error_emits_job_failed_event` | **Strong** | Pins both presence of `job_failed` AND absence of `job_completed`. |

## TestRequeueInterruptedJobs

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 335 | `test_no_interrupted_returns_empty` | **Strong** | Empty input → empty output, no side effects. |
| 346 | `test_stale_job_skipped` | **Strong** | Stale timestamp → not requeued. Pins both result and storage state. |
| 359 | `test_old_created_at_but_recent_started_at_revived` | **Strong** | Pins the "use started_at when present" branch — exactly the logic. Strict `is job` identity check + status reset. |
| 377 | `test_stale_started_at_also_skipped` | **Strong** | Mirror cell — both timestamps stale → skipped. |
| 391 | `test_pending_job_revived_in_place` | **Strong** | Pins all preserved fields after revive. |
| 407 | `test_failed_job_revived_in_place` | **Strong** | Pins ID/created_at preservation + status/error/completed_at/paused/percent reset. The full reset matrix. |
| 431 | `test_unparseable_date_skipped` | **Strong** | Bad timestamp → safely ignored, job preserved. |
| 445 | `test_list_cleared_after_revive` | **Strong** | Pins idempotency: second call returns `[]`. |

## TestPublishersAttribution

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 465 | `test_default_publishers_is_empty_list` | **Strong** | Pins both attr and `to_dict` shape. |
| 472 | `test_append_publishers_persists_through_restart` | **Strong** | Real disk round-trip via fresh manager — pins indexed values, not just len. |
| 511 | `test_append_publishers_noop_when_unknown_job` | **Strong** | Audit-strengthened: pins NO phantom job creation, exact-equality on count + None lookup. |
| 525 | `test_append_publishers_noop_when_rows_empty` | **Strong** | Strict `== []`. |
| 532 | `test_set_publishers_replaces_existing_rows` | **Strong** | Pins replace-not-append semantics with exact-equality on counts dict. The D12 contract. |
| 566 | `test_set_publishers_noop_when_unknown_job` | **Strong** | Mirror of append-noop. |
| 579 | `test_set_publishers_persists_through_restart` | **Strong** | Disk round-trip + names set comparison. |

## TestParentScheduleId

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 613 | `test_create_job_persists_parent_schedule_id` | **Strong** | In-memory + disk round-trip pins the field. |
| 624 | `test_create_job_defaults_parent_schedule_id_to_empty` | **Strong** | Pins both attr and to_dict default to `""`. |

## TestJobUnknownFieldTolerance

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 639 | `test_load_skips_unknown_kwarg_fields` | **Strong** | Future/removed field doesn't crash load — exactly the Phase H Fix-5 bug. Asserts loaded job is non-None AND library_name preserved. |

## TestSqliteJobsBackend

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 669 | `test_creates_jobs_db_on_first_start` | **Strong** | Pins file presence, storage non-None, AND row count==0. |
| 676 | `test_create_job_persists_one_row_not_a_full_file` | **Strong** | Pins exact row count (2) AND absence of jobs.json. The "structurally impossible" guarantee. |
| 690 | `test_state_survives_manager_recreation` | **Strong** | Real fresh manager — recovered status + library_name pinned. |
| 711 | `test_legacy_json_imports_then_renames` | **Strong** | Pins both job presence, status enum, AND that jobs.json was renamed to .imported.bak. |
| 736 | `test_legacy_import_skips_corrupt_records` | **Strong** | Pins "good rows survive, bad rows dropped" — both bad-status and bad-progress paths exercised. |
| 772 | `test_does_not_reimport_when_db_already_populated` | **Strong** | Pins row count UNCHANGED, stale row absent, AND legacy file preserved (NOT consumed). Three contracts in one. |
| 798 | `test_delete_removes_row` | **Strong** | Strict row count + None lookup. |

## TestRetryPreservesServerIdentity

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 818 | `test_retry_inherits_parent_server_triple` | **Strong** | K1 contract: server_id/name/type all pinned to specific values. Catches the "server=(all)" bug exactly. |
| 842 | `test_retry_when_parent_has_no_server_pin` | **Strong** | Mirror cell: parent None → retry None. Both halves of the matrix covered. |

## Summary

- **39 tests** — all **Strong**
- 0 weak / bug-blind / tautological / bug-locking
- Audit-strengthened tests (per inline comments): `test_warning_emits_job_completed_event` (lines 293-314) tightened from `>= 1` to exact-count; `test_append_publishers_noop_when_unknown_job` (511-523) tightened to assert no phantom job creation
- Strong matrix coverage on retention (running/recent/expired/orphan), publishers (append/set, known/unknown, empty rows), and SQLite backend (fresh DB, legacy import, corrupt-record skip, no-reimport guard)

**File verdict: STRONG.** No changes needed.
