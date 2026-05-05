# Audit: tests/test_scheduler.py — ~55 tests (re-audit, batch 6)

Tests for ScheduleManager (CRUD, persistence, enable/disable, run_now), per-schedule stop_time (D20), single-window + multi-window quiet hours (D21/D26), execute_scheduled_job dispatch branching (full_library vs recently_added), multi-library schedules (H7), D30 jobstore-wipe re-registration.

## TestScheduleCRUD

| Line | Test | Verdict | Note |
|---|---|---|---|
| 38 | `test_create_schedule_with_cron` | Strong | Pins all 8 returned fields (id, name, library_id/name, trigger_type/value, enabled, last_run, next_run presence). |
| 58 | `test_create_schedule_with_interval` | Strong | Pins trigger_type and value. |
| 72 | `test_create_schedule_disabled` | Strong | Pins enabled=False AND next_run=None. |
| 85 | `test_create_schedule_with_config` | Strong | Pins exact config dict equality. |
| 98 | `test_create_schedule_no_trigger_raises` | Strong | Pins ValueError + match substring. |
| 107 | `test_get_schedule` | Strong | Pins retrieved id and name. |
| 122 | `test_get_nonexistent_schedule` | Strong | Pins None. |
| 126 | `test_get_all_schedules` | Strong | Pins len==2 + name set. |
| 147 | `test_update_schedule_name` | Strong | Pins new name AND library_id preserved. |
| 162 | `test_update_schedule_trigger_cron_to_interval` | Strong | Pins trigger_type AND value. |
| 176 | `test_update_schedule_trigger_interval_to_cron` | Strong | Same pattern reversed. |
| 190 | `test_update_nonexistent_schedule` | Strong | Pins None. |
| 194 | `test_delete_schedule` | Strong | Pins True + post-delete get None. |
| 206 | `test_delete_nonexistent_schedule` | Strong | Pins False. |

## TestScheduleStopTime

| Line | Test | Verdict | Note |
|---|---|---|---|
| 219 | `test_create_with_stop_time_registers_stop_cron` | Strong | Pins stop_time field + both APScheduler job ids registered. |
| 236 | `test_create_without_stop_time_only_registers_start_cron` | Strong | Pins start id present + stop id absent + stop_time empty. |
| 249 | `test_update_clearing_stop_time_removes_stop_cron` | Strong | Pins stop id removed + stop_time empty. |
| 265 | `test_update_setting_stop_time_adds_stop_cron` | Strong | Pins stop id added + stop_time value. |
| 278 | `test_delete_removes_stop_cron` | Strong | Pins both ids removed. |
| 292 | `test_interval_trigger_silently_drops_stop_time` | Strong | Pins stop_time empty + stop id absent. |
| 307 | `test_invalid_stop_time_raises` | Strong | Pins ValueError. |
| 317 | `test_parse_hhmm_helper_edge_cases` | Strong | Multi-row matrix incl. all error rows. |

## TestQuietHours

| Line | Test | Verdict | Note |
|---|---|---|---|
| 338 | `test_is_in_quiet_window_equal_times_disables` | Strong | Pins False for two cells. |
| 344 | `test_is_in_quiet_window_same_day_window` | Strong | 5-cell matrix with boundary checks. |
| 354 | `test_is_in_quiet_window_cross_midnight` | Strong | 6-cell matrix incl. boundaries. |
| 365 | `test_apply_quiet_hours_enabled_registers_both_crons` | Strong | Pins both `__qh_pause_0` and `__qh_resume_0` job ids. |
| 374 | `test_apply_quiet_hours_disabled_removes_both_crons` | Strong | Pins absence of any qh_pause/qh_resume + legacy ids absent. |
| 383 | `test_apply_quiet_hours_equal_times_treated_as_disabled` | Strong | Pins absence of qh ids. |
| 389 | `test_apply_quiet_hours_malformed_times_skipped` | Strong | Pins absence of qh ids on malformed. |
| 396 | `test_execute_scheduled_job_skipped_when_processing_paused` | Strong | Pins `called == []` when settings_manager.processing_paused=True. |

## TestQuietHoursMultiWindow

| Line | Test | Verdict | Note |
|---|---|---|---|
| 432 | `test_normalise_legacy_single_window_form` | Strong | Pins enabled, len, start/end, days set defaults to all 7. |
| 443 | `test_normalise_multi_window_passes_through` | Strong | Pins per-window days exactly. |
| 458 | `test_normalise_strips_unknown_day_names` | Strong | Pins exact `["mon", "fri"]` (BOGUS dropped). |
| 465 | `test_normalise_empty_days_list_falls_back_to_all_seven` | Strong | Pins len==7. |
| 471 | `test_normalise_handles_none_input` | Strong | Pins exact dict shape. |
| 477 | `test_is_now_in_any_quiet_window_respects_day_of_week` | Strong | Two cells with named day asserts. |
| 489 | `test_is_now_in_any_quiet_window_two_windows_either_active` | Strong | Three cells (two windows × inside/outside). |
| 510 | `test_is_now_in_any_quiet_window_disabled_returns_false` | Strong | Pins False. |
| 521 | `test_apply_quiet_hours_two_windows_registers_two_pairs` | Strong | Pins all 4 cron ids. |
| 537 | `test_apply_quiet_hours_rebuilds_cleanly_on_reapply` | Strong | Pins exact set `{"__qh_pause_0", "__qh_resume_0"}` after reapply (no leak). |
| 560 | `test_apply_quiet_hours_window_with_no_valid_days_skipped` | Strong | Pins both pause ids present (BOGUS-only window falls back to all 7 days). |

## TestExecuteScheduleStop

| Line | Test | Verdict | Note |
|---|---|---|---|
| 581 | `test_pauses_only_jobs_from_this_schedule` | Strong | Pins `assert_called_once_with("job-1")` AND negative assertions for sibling/already-paused jobs. Multi-invariant. |

## TestScheduleEnableDisable

| Line | Test | Verdict | Note |
|---|---|---|---|
| 636 | `test_enable_schedule` | Strong | Pins not None + enabled True. |
| 651 | `test_disable_schedule` | Strong | Pins not None + enabled False. |
| 666 | `test_enable_nonexistent` | Strong | Pins None. |
| 670 | `test_disable_nonexistent` | Strong | Pins None. |

## TestScheduleRunNow

| Line | Test | Verdict | Note |
|---|---|---|---|
| 683 | `test_run_now_with_callback` | Strong | Pins True + `assert_called_once_with(library_id=, library_name=, config={}, parent_schedule_id=)`. D20 contract pin. |
| 708 | `test_run_now_nonexistent` | Strong | Pins False. |
| 712 | `test_run_now_updates_last_run` | Strong | Pins last_run starts None and is set after run_now. |

## TestSchedulePersistence

| Line | Test | Verdict | Note |
|---|---|---|---|
| 739 | `test_schedules_survive_restart` | Strong | Pins schedule fields after manager2 reload. |
| 767 | `test_handles_missing_file` | Strong | Pins `[]` empty list. |
| 779 | `test_handles_corrupt_file` | Strong | Pins `[]` empty list (graceful). |
| 794 | `test_schedules_re_register_with_apscheduler_after_jobstore_wipe` | Strong | D30: pins enabled schedules in `get_jobs()`, disabled NOT, next_run_time refreshed in JSON, future timestamp. Multi-invariant. |
| 867 | `test_schedules_re_register_preserves_stop_time_cron` | Strong | D30 + D20: pins both start and `__stop` ids restored after jobstore wipe. |

## TestGetScheduleManager

| Line | Test | Verdict | Note |
|---|---|---|---|
| 908 | `test_returns_singleton` | Strong | Pins `m1 is m2` (identity). |
| 921 | `test_sets_callback_on_existing` | Strong | Pins `m1 is m2` AND callback identity. |

## TestExecuteScheduledJobDispatch

| Line | Test | Verdict | Note |
|---|---|---|---|
| 944 | `test_dispatches_full_library_by_default` | Strong | Pins library_id, library_name AND parent_schedule_id (D20 audit-fix). |
| 976 | `test_dispatches_recently_added_calls_multi_server_scan` | Strong | Pins `library_ids == ["2"]` + `lookback_hours == 2.0`. |
| 1021 | `test_dispatches_recently_added_with_no_library_passes_none` | Strong | Pins `library_ids is None` + `lookback_hours == 1.0`. |
| 1055 | `test_recently_added_dispatch_clamps_invalid_lookback` | Strong | Pins `lookback_hours == 1.0` (default) for garbage input. |
| 1087 | `test_recently_added_dispatch_updates_last_run` | Strong | Pins last_run None pre, not None post. |

## TestMultiLibrarySchedules

| Line | Test | Verdict | Note |
|---|---|---|---|
| 1122 | `test_create_schedule_with_multiple_libraries` | Strong | Pins library_ids list AND library_id is None for multi. |
| 1133 | `test_create_schedule_with_single_library_keeps_back_compat` | Strong | Pins both library_ids AND library_id mirrored. |
| 1143 | `test_legacy_library_id_arg_still_works` | Strong | Pins library_id AND library_ids derived. |
| 1154 | `test_load_migrates_legacy_library_id_to_library_ids` | Strong | Pins migrated library_ids AND original library_id preserved. |
| 1187 | `test_update_schedule_with_library_ids` | Strong | Pins library_ids set AND library_id cleared (multi). |

**File verdict: STRONG.** Re-audit found ZERO weak/bug-blind/tautological tests. The previous audit's "STRONG" verdict holds up — every test pins multiple invariants, the matrix coverage for quiet-hours windows / day filters is exemplary, the D20 (stop_time) and D30 (jobstore-wipe re-registration) contract pins are tight, and the recently_added dispatch tests close the audit-fix `parent_schedule_id` propagation gap.

## Fix queue

(empty — no remaining issues)
