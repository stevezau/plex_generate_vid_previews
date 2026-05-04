# Audit: tests/test_scheduler.py — 53 tests, 10 classes

## TestScheduleCRUD

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 38 | `test_create_schedule_with_cron` | **Strong** | 7 strict equalities pin every persisted field of the create contract |
| 58 | `test_create_schedule_with_interval` | **Strong** | trigger_type=="interval" + value=="60" — pins interval branch |
| 72 | `test_create_schedule_disabled` | **Strong** | enabled=False → next_run is None pin (not a no-op) |
| 85 | `test_create_schedule_with_config` | **Strong** | config dict round-trips by equality |
| 98 | `test_create_schedule_no_trigger_raises` | **Strong** | `match=` regex on error text |
| 107 | `test_get_schedule` | **Strong** | id+name equality |
| 122 | `test_get_nonexistent_schedule` | **Strong** | None contract pinned |
| 126 | `test_get_all_schedules` | **Strong** | Set equality on names + length |
| 147 | `test_update_schedule_name` | **Strong** | Asserts name updated AND library_id preserved (covers partial-update bug class) |
| 162 | `test_update_schedule_trigger_cron_to_interval` | **Strong** | Trigger swap pinned |
| 176 | `test_update_schedule_trigger_interval_to_cron` | **Strong** | Mirror of above |
| 190 | `test_update_nonexistent_schedule` | **Strong** | None contract |
| 194 | `test_delete_schedule` | **Strong** | Returns True + get returns None — round-trip |
| 206 | `test_delete_nonexistent_schedule` | **Strong** | False contract |

## TestScheduleStopTime

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 219 | `test_create_with_stop_time_registers_stop_cron` | **Strong** | Inspects the actual APScheduler job IDs — pins the stop-cron registration |
| 236 | `test_create_without_stop_time_only_registers_start_cron` | **Strong** | Negative pin — stop-cron MUST NOT exist |
| 249 | `test_update_clearing_stop_time_removes_stop_cron` | **Strong** | Round-trip via get_schedule + jobstore inspection |
| 265 | `test_update_setting_stop_time_adds_stop_cron` | **Strong** | Mirror — adds the cron |
| 278 | `test_delete_removes_stop_cron` | **Strong** | Both IDs gone after delete |
| 292 | `test_interval_trigger_silently_drops_stop_time` | **Strong** | Pins silent-drop semantics + no stop-cron registered |
| 307 | `test_invalid_stop_time_raises` | **Strong** | ValueError pinned |
| 317 | `test_parse_hhmm_helper_edge_cases` | **Strong** | 8 strict-equality cases incl. valid + 4 raise paths |

## TestQuietHours

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 338 | `test_is_in_quiet_window_equal_times_disables` | **Strong** | Strict `is False` — disabled-by-equal-times pin |
| 344 | `test_is_in_quiet_window_same_day_window` | **Strong** | 5 boundary cells: start, mid, end-exclusive, before, after |
| 354 | `test_is_in_quiet_window_cross_midnight` | **Strong** | 6 cross-midnight boundary cells |
| 365 | `test_apply_quiet_hours_enabled_registers_both_crons` | **Strong** | Pins per-window IDs — D26 prefix preserved |
| 374 | `test_apply_quiet_hours_disabled_removes_both_crons` | **Strong** | Negative pin — both legacy AND D26 names absent |
| 383 | `test_apply_quiet_hours_equal_times_treated_as_disabled` | **Strong** | Pins "equal times = no cron" |
| 389 | `test_apply_quiet_hours_malformed_times_skipped` | **Strong** | Malformed → skip, not raise |
| 396 | `test_execute_scheduled_job_skipped_when_processing_paused` | **Strong** | Pins D21 — callback list stays empty |

## TestQuietHoursMultiWindow

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 432 | `test_normalise_legacy_single_window_form` | **Strong** | Pins legacy→D26 migration including 7-day default |
| 443 | `test_normalise_multi_window_passes_through` | **Strong** | Pin per-window days arrays |
| 458 | `test_normalise_strips_unknown_day_names` | **Strong** | Pins "BOGUS" filtered out by exact list equality |
| 465 | `test_normalise_empty_days_list_falls_back_to_all_seven` | **Strong** | len==7 fallback contract |
| 471 | `test_normalise_handles_none_input` | **Strong** | None → empty contract |
| 477 | `test_is_now_in_any_quiet_window_respects_day_of_week` | **Strong** | Real datetimes — Mon True, Tue False matrix |
| 489 | `test_is_now_in_any_quiet_window_two_windows_either_active` | **Strong** | Three real datetimes spanning weekday/weekend/cross-midnight |
| 510 | `test_is_now_in_any_quiet_window_disabled_returns_false` | **Strong** | Disabled-flag pin |
| 521 | `test_apply_quiet_hours_two_windows_registers_two_pairs` | **Strong** | Pins window-index naming pattern |
| 537 | `test_apply_quiet_hours_rebuilds_cleanly_on_reapply` | **Strong** | Set equality — old jobs cleared on reapply (regression-class) |
| 560 | `test_apply_quiet_hours_window_with_no_valid_days_skipped` | **Strong** | Pins fall-through behaviour for bogus days |

## TestExecuteScheduleStop

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 581 | `test_pauses_only_jobs_from_this_schedule` | **Strong** | Asserts request_pause called with specific job-1 AND that other IDs NOT in any call (D34 paradigm — checks args, not just count) |

## TestScheduleEnableDisable

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 636 | `test_enable_schedule` | **Strong** | enabled=True pinned |
| 651 | `test_disable_schedule` | **Strong** | enabled=False pinned |
| 666 | `test_enable_nonexistent` | **Strong** | None contract |
| 670 | `test_disable_nonexistent` | **Strong** | None contract |

## TestScheduleRunNow

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 683 | `test_run_now_with_callback` | **Strong** | `assert_called_once_with` pins all 4 kwargs incl. D20's parent_schedule_id contract |
| 708 | `test_run_now_nonexistent` | **Strong** | False contract |
| 712 | `test_run_now_updates_last_run` | **Strong** | None → not None pin |

## TestSchedulePersistence

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 739 | `test_schedules_survive_restart` | **Strong** | Real persistence round-trip via two ScheduleManager instances |
| 767 | `test_handles_missing_file` | **Strong** | Empty list contract |
| 779 | `test_handles_corrupt_file` | **Strong** | Same after writing literal garbage |
| 794 | `test_schedules_re_register_with_apscheduler_after_jobstore_wipe` | **Strong** | D30 — wipes scheduler.db + asserts both enabled re-registered AND disabled NOT, AND next_run > now (pins UI staleness fix) |
| 867 | `test_schedules_re_register_preserves_stop_time_cron` | **Strong** | D30 stop-cron survives jobstore wipe |

## TestGetScheduleManager

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 908 | `test_returns_singleton` | **Strong** | `is` identity check |
| 921 | `test_sets_callback_on_existing` | **Strong** | Identity + callback equality |

## TestExecuteScheduledJobDispatch

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 944 | `test_dispatches_full_library_by_default` | **Strong** | Pins library_id, library_name, AND parent_schedule_id (D20 contract — explicit "removing the param fails the test" |
| 976 | `test_dispatches_recently_added_calls_multi_server_scan` | **Strong** | Asserts library_ids==["2"], lookback_hours==2.0 — multi-kwarg pin |
| 1021 | `test_dispatches_recently_added_with_no_library_passes_none` | **Strong** | Pins None propagation |
| 1055 | `test_recently_added_dispatch_clamps_invalid_lookback` | **Strong** | Garbage→1.0 default pin |
| 1087 | `test_recently_added_dispatch_updates_last_run` | **Strong** | None→not None |

## TestMultiLibrarySchedules

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 1122 | `test_create_schedule_with_multiple_libraries` | **Strong** | library_ids list + library_id None contract pinned |
| 1133 | `test_create_schedule_with_single_library_keeps_back_compat` | **Strong** | Single → mirrored back-compat |
| 1143 | `test_legacy_library_id_arg_still_works` | **Strong** | Two-direction mirror pinned |
| 1154 | `test_load_migrates_legacy_library_id_to_library_ids` | **Strong** | Real legacy JSON on disk → migrated on load |
| 1187 | `test_update_schedule_with_library_ids` | **Strong** | library_id cleared when going multi |

## Summary

- **53 tests** total
- **53 Strong** / 0 Weak / 0 Bug-blind / 0 Tautological / 0 Bug-locking / 0 Needs-human
- D5/D20/D21/D26/D30/D34 regression locks all present; UI-staleness pin (`next_run > now`) is particularly load-bearing
- Persistence tests use real disk round-trips (no mock-only verification)

**File verdict: STRONG.** No changes needed.
