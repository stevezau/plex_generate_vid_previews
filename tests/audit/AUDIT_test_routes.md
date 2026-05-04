# Audit — `tests/test_routes.py` (re-audit, adversarial)

File length: 5142 lines. 264 test methods across 35 classes.

Methodology: full-file read, cross-checked against
`media_preview_generator/web/routes/api_jobs.py`, `api_settings.py`,
`api_system.py`, `api_libraries.py`, `api_schedules.py`,
`pages.py`, `app.py`. The previous audit graded every test "STRONG";
this pass downgrades the ones that genuinely under-assert.

Taxonomy: Strong / Weak / Tautological / Bug-blind / Dead-redundant /
Framework-trivia / Bug-locking / Needs-human.

---

## TestPageRoutes (lines 126–217)

| Line | Test | Verdict | Note |
|---|---|---|---|
| 129 | test_index_redirects_to_login_when_unauthenticated | Strong | 302 + Location body asserted. |
| 136 | test_login_page_renders | Weak | Substring check `b"token" or b"login"` — page can render ANY HTML containing the word "login" and pass. Body should pin a specific marker (e.g. `id="login-form"`, the auth token input). |
| 141 | test_settings_requires_auth | Strong | 302 + Location header pinned. |
| 146 | test_settings_accessible_when_authenticated | Weak | Disjunctive substring `b"Settings" or b"setting"` is too permissive — any error page mentioning the word "setting" passes. Pin a stable settings-page marker. |
| 153 | test_servers_page_accepts_add_query_param | Weak | Asserts `200` only across all five vendor values; never inspects body, so a regression that ignores the query param entirely passes. (Bug-blind by definition: 200 with ignored param = same as 200 with handled param.) |
| 162 | test_gpu_config_panel_js_served | Strong | Pins two function names. |
| 172 | test_stepper_js_served | Strong | Pins two function names. |
| 180 | test_setup_page_loads_shared_panel_scripts | Strong | Pins three script src markers. |
| 193 | test_setup_page_inlines_connection_form_partial | Strong | Positive + negative DOM markers. |
| 209 | test_servers_page_still_has_modal | Strong | Two DOM markers. |

## TestLoginLogout (lines 224–268)

| Line | Test | Verdict | Note |
|---|---|---|---|
| 227 | test_login_post_valid_token | Strong | Asserts redirect AND session flag. |
| 237 | test_login_post_invalid_token | Weak | Disjunctive substring `b"didn" or b"invalid" or b"Invalid"` — a generic 200 page containing "didn't" passes. Pin a specific error marker (`role="alert"`, `class="error"`). |
| 242 | test_already_authenticated_redirects_from_login | Strong | 302 + Location not /login. |
| 248 | test_logout_clears_session | Strong | Verifies subsequent request bounces to login. |
| 256 | test_login_rate_limit_exceeded | Weak | Asserts 429 only — does not pin the body or that the limit is exactly 5 (a regression bumping it to 50 passes if we just iterate 5 then 6). Acceptable but better with a "5th still allowed, 6th blocked" matrix. |
| 263 | test_api_login_rate_limit_exceeded | Weak | Same shape as above; 200 vs 429 ordering not asserted. |

## TestAuthAPI (lines 276–302)

| Line | Test | Verdict | Note |
|---|---|---|---|
| 279 | test_auth_status_unauthenticated | Strong | Pins boolean. |
| 284 | test_auth_status_authenticated | Strong | Pins boolean. |
| 289 | test_api_login_valid | Strong | Pins success. |
| 294 | test_api_login_invalid | Strong | Pins 401 + success=false. |
| 299 | test_api_logout | Weak | Asserts `success=True` but does not verify session was actually cleared (a no-op endpoint that always returns success would pass). Should follow with another request and assert it's now unauthenticated. |

## TestHealthCheck (lines 310–316)

| Line | Test | Verdict | Note |
|---|---|---|---|
| 313 | test_health_no_auth_required | Strong | Pins status field value. |

## TestTokenEndpoints (lines 324–506)

| Line | Test | Verdict | Note |
|---|---|---|---|
| 327 | test_regenerate_token_requires_auth | Strong | 401 pinned. |
| 331 | test_regenerate_token_returns_masked | Strong | Pins masking + persisted change + last-4-suffix. |
| 352 | test_regenerate_token_invalidates_session | Strong | Round-trip behavior verified. |
| 359 | test_setup_token_info | Weak | `assert "token" in data` and `data["token"].startswith("****")` + key-presence on `source` — never pins the source value. A regression returning `source="leaked"` passes. |
| 367 | test_set_custom_token_requires_auth | Strong | 401 pinned. |
| 372 | test_set_custom_token_persists_to_disk | Strong | Three meaningful assertions including disk persistence. |
| 395 | test_set_custom_token_rejects_mismatch | Strong | 400 + error substring `match`. |
| 410 | test_set_custom_token_rejects_too_short | Weak | Asserts only 400; doesn't pin which validation fired (could be hitting `mismatch` path even though tokens match). Should also check error mentions length/short. |
| 424 | test_setup_skip_marks_complete | Strong | Pins success + `is_setup_complete()`. |
| 438 | test_setup_skip_works_when_already_authenticated | Strong | Regression-locked with explanatory error message. |
| 459 | test_set_custom_token_rejects_when_env_controlled | Strong | Pins 409 + WEB_AUTH_TOKEN error fragment. |
| 484 | test_wizard_server_endpoints_not_redirected_during_setup | Weak | Parametrized check that `status_code != 302` only. A regression that 500s every wizard endpoint passes. Better: also assert `status_code in {200, 400, 401, 502}` (the legitimate codes) so a generic crash is caught. |

## TestJobsAPI (lines 514–1161)

| Line | Test | Verdict | Note |
|---|---|---|---|
| 517 | test_get_jobs_requires_auth | Strong | 401 pinned. |
| 521 | test_get_jobs_empty | Strong | Five fields pinned. |
| 531 | test_create_job | Weak | `assert "id" in data` is key-presence only — never asserts the id is a non-empty string or matches the created job. Combined with status+pending check it's borderline acceptable but bug-blind to a returned `id=None`. |
| 540 | test_create_job_library_ids_propagate_to_overrides | Strong | Pins overrides. |
| 560 | test_create_job_single_library_id_keeps_back_compat | Strong | Pins library_id field. |
| 572 | test_create_job_infers_server_from_single_library_id | Strong | Pins all 4 server fields + override. |
| 608 | test_create_job_does_not_infer_for_multi_library_picks | Weak | Asserts server_id and server_type are None but NOT server_name — production sets all three from `_resolve_server_context`; missing the third cell. Easy fix. |
| 642 | test_create_job_explicit_server_id_overrides_inference | Weak | Same as above — only `server_id` checked. Should also assert `overrides["server_id"] == "plex-B"` to pin the dispatcher-pin contract (the same kind of "kwargs pin" the project rule highlights). **Why downgraded:** the previous audit missed the missing override-kwarg pin. |
| 684 | test_create_job_ignores_credential_overrides | Strong | Excellent kwarg-pin pattern. |
| 722 | test_get_specific_job | Strong | Pins id. |
| 730 | test_get_nonexistent_job | Strong | 404 pinned. |
| 734 | test_cancel_job | Strong | Pins log line. |
| 744 | test_complete_job_does_not_override_cancelled_status | Strong | Race-condition contract. |
| 761 | test_delete_job | Strong | Pins success. |
| 771 | test_delete_nonexistent_job | Strong | 404 pinned. |
| 775 | test_clear_jobs | Bug-blind | Only asserts `"cleared" in resp.get_json()` — key-presence; never pins the cleared count. A regression returning `{"cleared": -1}` or always 0 passes. **Why downgraded:** classic "key in body" pattern flagged in the audit prompt. |
| 780 | test_clear_jobs_with_status_filter | Weak | Pins `cleared == 3` but the matrix is incomplete: never tests with `statuses=["completed"]` separately or invalid status (production validates both — see api_jobs.py:1037-1048; a regression in the validator never gets caught). |
| 802 | test_clear_jobs_empty_statuses_clears_all_terminal | Bug-blind | `assert "cleared" in resp.get_json()` — key-presence only; the docstring says "all terminal" but the assertion doesn't verify that. **Why downgraded:** identical pattern to test_clear_jobs. |
| 808 | test_get_jobs_pagination | Strong | Five-field pin. |
| 823 | test_get_jobs_pagination_last_page | Strong | Pins jobs slice. |
| 834 | test_get_jobs_pagination_out_of_range | Strong | Pins empty + page echo. |
| 842 | test_get_jobs_unpaginated | Strong | Pins 3-job slice + pages=1. |
| 855 | test_get_jobs_sort_order | Strong | Pins ordering. |
| 877 | test_get_job_stats | Bug-blind | Only `assert "total" in data` — key-presence; never pins value or other stats keys. A regression returning `{"total": None}` passes. **Why downgraded:** prompt pattern #4 verbatim. |
| 883 | test_get_worker_statuses | Bug-blind | Asserts `"workers" in data` and `isinstance(...,list)`. List can be wrong shape and pass. **Why downgraded:** key-presence only. |
| 890 | test_get_job_logs | Strong | Pins retention flag value. |
| 901 | test_get_job_logs_returns_retention_flag_when_log_cleared | Strong | Pins logs message + flag. |
| 919 | test_pause_resume_job | Strong | Body + settings round-trip. |
| 948 | test_processing_state_get | Bug-blind | `"paused" in data` + isinstance bool. Never pins a value; a regression returning always True or always False passes. **Why downgraded:** key-presence + type-only. |
| 956 | test_processing_pause_resume | Strong | Asserts pause then resume sequence. |
| 972 | test_job_not_started_when_processing_paused | Strong | Pins pending status. |
| 990 | test_scale_workers_add_remove | Strong | Mock kwargs pinned for both calls. |
| 1029 | test_scale_workers_remove_busy_workers_returns_scheduled_removal | Strong | Three-field pin. |
| 1060 | test_scale_workers_remove_returns_unavailable_when_fewer_workers_exist | Strong | Three-field pin. |
| 1089 | test_workers_add_global_no_pool_returns_409 | Strong | Pins error substring. |
| 1099 | test_workers_remove_global_no_pool_returns_409 | Strong | Pins error substring. |
| 1109 | test_workers_add_global_success | Strong | Pins added + worker_type + mock kwargs. |
| 1134 | test_workers_remove_global_success | Strong | Pins removed + worker_type + mock kwargs. |

## TestManualTriggerAPI (lines 1169–1298)

| Line | Test | Verdict | Note |
|---|---|---|---|
| 1172 | test_manual_trigger_valid_path | Strong | Three-field override pin (post-batch-1 fix). |
| 1200 | test_manual_trigger_with_force_regenerate_propagates | Strong | force_generate pin. |
| 1221 | test_manual_trigger_no_paths | Strong | Pins error substring. |
| 1231 | test_manual_trigger_missing_body | Weak | Only 400, never inspects body. Could collide with 400-from-anywhere. |
| 1240 | test_manual_trigger_path_outside_media_root | Strong | Pins "outside" substring. |
| 1255 | test_manual_trigger_requires_auth | Strong | 401 pinned. |
| 1263 | test_manual_trigger_force_regenerate | Dead/redundant | Duplicates test at line 1200 (same name pattern, same inputs, same assertion). Could be removed. |
| 1282 | test_manual_trigger_multiple_paths | Weak | Substring `"2 files" in library_name` tolerable. Doesn't assert `webhook_paths` actually contains both paths in overrides — a regression that drops the second file passes. **Why downgraded:** missing override pin. |

## TestSettingsAPI (lines 1306–1705)

| Line | Test | Verdict | Note |
|---|---|---|---|
| 1309 | test_get_settings | Weak | `"gpu_threads" in data` + `"cpu_threads" in data` are key-presence only; only `plex_verify_ssl is True` is value-pinned. Could pin types (int) for the threads. |
| 1317 | test_get_settings_returns_path_mappings | Strong | Round-trip pinned. |
| 1335 | test_get_settings_returns_exclude_paths | Strong | Round-trip pinned. |
| 1350 | test_save_settings_writes_plex_fields_into_media_servers | Strong | Eight-field pin + negative-key pin. |
| 1396 | test_save_settings_preserves_existing_token_when_redacted | Strong | Round-trip with sentinel. |
| 1416 | test_save_settings_preserves_webhook_secret_when_redacted | Strong | Three-mode round-trip. |
| 1455 | test_get_settings_projects_from_media_servers | Strong | Five-field pin. |
| 1487 | test_get_settings_never_leaks_real_credentials_anywhere_in_response | Strong | Defense-in-depth substring scan, with sentinel. |
| 1537 | test_save_settings | Strong | Round-trip. |
| 1574 | test_save_settings_ignores_unknown_fields | Bug-blind | Only `200` asserted; never verifies the unknown field was actually dropped (fetch back and assert `"unknown_field" not in get_resp`). **Why downgraded:** the test name promises a contract the assertion doesn't verify. |
| 1586 | test_save_settings_warns_zero_cpu_and_zero_gpu | Strong | Warning substring + success pinned. |
| 1611 | test_save_gpu_config_validates_list | Weak | Only 400 — never inspects error body. |
| 1620 | test_save_gpu_config_filters_invalid_entries | Strong | Pins post-filter length + entry. |
| 1647 | test_save_log_settings | Bug-blind | Only `success=True`; never reads back to verify level/rotation/retention persisted (this is exactly the documented "save settings"-class regression mode). **Why downgraded:** save-without-readback is a known anti-pattern flagged in the project rules. |
| 1662 | test_update_log_level | Strong | Pins success + level + setup_logging mock call. |
| 1675 | test_update_log_level_invalid | Weak | Only 400; doesn't pin error substring (production returns "Invalid log level. Must be one of [...]" — easy to assert). |
| 1684 | test_get_settings_returns_webhook_retry_defaults | Strong | Pins both default values. |
| 1692 | test_save_webhook_retry_settings | Strong | Round-trip pinned. |

## TestJobConfigPathMappings (lines 1714–1974)

| Line | Test | Verdict | Note |
|---|---|---|---|
| 1717 | test_start_job_applies_settings_path_mappings | Strong | Pins normalized config in captured run_processing call. |
| 1768 | test_start_job_does_NOT_accept_path_mappings_override | Strong | Negative override pin. |
| 1811 | test_create_job_does_NOT_accept_webhook_paths_override | Strong | Negative override pin. |
| 1839 | test_start_job_library_ids_override_sets_plex_library_ids | Strong | Pins both library scopes. |
| 1884 | test_run_processing_returning_none_marks_job_failed | Strong | Status + error substring pinned. |
| 1931 | test_start_job_selected_libraries_ids_map_to_id_scope | Strong | Pins both scopes. |

## TestSetupWizardAPI (lines 1982–2078)

| Line | Test | Verdict | Note |
|---|---|---|---|
| 1985 | test_get_setup_status_no_auth | Strong | Type pins per audit fix in earlier batch. |
| 2004 | test_get_setup_state | Bug-blind | Only `"step" in data` — key-presence; never pins value. **Why downgraded:** key-presence only. |
| 2010 | test_save_setup_state | Strong | Round-trip step value. |
| 2023 | test_setup_state_save_and_load_path_mappings | Strong | Round-trip nested data. |
| 2046 | test_complete_setup | Strong | Pins success + redirect + sm state. |
| 2063 | test_set_setup_token_mismatch | Strong | 400 + match substring. |
| 2072 | test_set_setup_token_too_short | Weak | Only 400; doesn't pin error reason. Same shape as test_set_custom_token_rejects_too_short. |

## TestQuietHoursAPI (lines 2086–2163)

| Line | Test | Verdict | Note |
|---|---|---|---|
| 2089 | test_get_returns_default_when_unset | Weak | `"currently_in_quiet_window" in body` — key-presence on the most interesting field (the boolean indicating *current* status). Should pin `is False`. |
| 2097 | test_post_legacy_single_window_body_still_accepted | Strong | Pins migration + GET round-trip. |
| 2117 | test_post_multi_window_persists_and_round_trips | Strong | Multi-window round-trip with day arrays. |
| 2136 | test_post_invalid_window_time_returns_400 | Strong | 400 + "Window #1" prefix in error. |
| 2145 | test_post_invalid_day_name_returns_400 | Strong | 400 + bad day name in error. |
| 2157 | test_post_legacy_invalid_start_time_returns_400 | Weak | Only 400; doesn't pin error substring. |

## TestSchedulesAPI (lines 2166–2210)

| Line | Test | Verdict | Note |
|---|---|---|---|
| 2169 | test_get_schedules_empty | Strong | Pins empty list. |
| 2174 | test_create_schedule_missing_name | Weak | 400 only; no error body inspection. |
| 2182 | test_create_schedule_missing_trigger | Weak | 400 only; no error body inspection. |
| 2186 | test_update_schedule_invalid_cron_returns_400 | Strong | Regression-pinned with "error" key. (Could pin error message but adequate.) |

## TestSystemAPI (lines 2218–2383)

| Line | Test | Verdict | Note |
|---|---|---|---|
| 2221 | test_get_system_status | Strong | Four-field pin. |
| 2233 | test_get_config | Strong | Five-field value pin + masking + types. |
| 2256 | test_media_servers_status_empty_when_unconfigured | Strong | Three-field pin. |
| 2274 | test_media_servers_status_summarises_each_entry | Strong | Two-row pin + assert_called_once on probe. |
| 2326 | test_media_servers_status_uses_30s_cache | Strong | Pins cached flag + entry id. |
| 2345 | test_media_servers_status_classifies_401_as_unauthorised | Strong | Pins status + 401 substring. |

## TestPathValidation (lines 2391–2516)

| Line | Test | Verdict | Note |
|---|---|---|---|
| 2394 | test_validate_paths_empty | Weak | Only `valid=False` — no error inspection (could fail for any reason). |
| 2404 | test_validate_paths_null_bytes_rejected | Weak | Only `valid=False`; doesn't pin error substring (e.g. "Invalid Plex Data Path") — would pass even if null-byte path is rejected by some other coincidental validation. |
| 2414 | test_validate_paths_requires_auth_after_setup | Strong | 401 pinned. |
| 2423 | test_validate_paths_path_mappings_new_format_local_not_found | Weak | Three-way disjunctive substring (`"Folder not found" in e or "Path in this app" in e or "Row 1" in e`) — too permissive; nearly any error string passes. |
| 2450 | test_validate_paths_path_mappings_null_byte_in_local | Strong | Pins "invalid path" substring. |
| 2476 | test_validate_paths_legacy_plex_only_returns_error | Strong | Pins exact error substring. |
| 2497 | test_validate_paths_legacy_local_only_returns_error | Strong | Pins exact error substring. |

## TestAuthMethods (lines 2524–2549)

| Line | Test | Verdict | Note |
|---|---|---|---|
| 2533 | test_bearer_auth | Strong | Pins jobs key in body. |
| 2539 | test_x_auth_token_header | Strong | Pins jobs key in body. |
| 2545 | test_session_auth | Strong | Pins jobs key in body. |

## TestAuthRejection (lines 2555–2560)

| Line | Test | Verdict | Note |
|---|---|---|---|
| 2558 | test_no_auth_rejected | Strong | 401 pinned. |

## TestSchedulesCRUD (lines 2568–2687)

| Line | Test | Verdict | Note |
|---|---|---|---|
| 2571 | test_create_schedule_cron | Weak | `"id" in data` is key-presence only. Pin id is non-empty. |
| 2582 | test_create_schedule_interval | Bug-blind | Only `status_code == 201` — does not inspect body at all. Trivially Bug-blind. **Why downgraded:** prompt pattern #1 (status-only). |
| 2590 | test_get_specific_schedule | Strong | Pins id. |
| 2601 | test_get_nonexistent_schedule | Strong | 404 pinned. |
| 2605 | test_update_schedule | Strong | Pins updated name. |
| 2620 | test_update_nonexistent_schedule | Strong | 404 pinned. |
| 2628 | test_delete_schedule | Strong | Pins success. |
| 2639 | test_delete_nonexistent_schedule | Strong | 404 pinned. |
| 2643 | test_enable_schedule | Strong | Pins enabled flip. |
| 2654 | test_enable_nonexistent_schedule | Strong | 404 pinned. |
| 2658 | test_disable_schedule | Strong | Pins enabled flip. |
| 2669 | test_disable_nonexistent_schedule | Strong | 404 pinned. |
| 2674 | test_run_now | Tautological | Only asserts `success=True`; this is a real_job_async marked test that could trigger arbitrary background behaviour — should pin (a) job was created OR (b) schedule's last_run_at advanced. As written, an endpoint that returns success without actually doing anything passes. **Why downgraded:** the behaviour the route exists for (kicking a run) is never asserted. |
| 2685 | test_run_now_nonexistent | Strong | 404 pinned. |

## TestReprocessJob (lines 2695–2729)

| Line | Test | Verdict | Note |
|---|---|---|---|
| 2698 | test_reprocess_completed_job | Weak | Asserts new_id != old_id but never inspects new job's library_name, status, parent_id, or stripped retry keys (production strips many keys — see api_jobs.py:1006). The whole point of this endpoint is the cloning behaviour; only the id-difference is checked. **Why downgraded:** the function's distinctive behaviour (config cloning + retry-key stripping) is unasserted. |
| 2714 | test_reprocess_nonexistent_job | Strong | 404 pinned. |
| 2718 | test_reprocess_running_job_rejected | Weak | Only 409; doesn't pin the error message (production returns specific text "Cannot reprocess job that is running or pending"). |

## TestWorkerScalingValidation (lines 2737–2867)

| Line | Test | Verdict | Note |
|---|---|---|---|
| 2740 | test_add_workers_zero_count_rejected | Weak | Only 400; no body inspection. |
| 2757 | test_add_workers_invalid_type_rejected | Weak | Only 400; no body inspection. |
| 2774 | test_add_workers_no_pool_returns_409 | Weak | Only 409; no body inspection. |
| 2791 | test_remove_workers_zero_count_rejected | Weak | Only 400; no body inspection. |
| 2808 | test_global_add_workers_invalid_type | Weak | Only 400; no body inspection. |
| 2827 | test_global_remove_workers_zero_count | Weak | Only 400; no body inspection. |
| 2844 | test_add_workers_non_numeric_count_returns_400 | Strong | 400 + "integer" substring. |

(Whole section uses 400/409-only assertions — a regression to a different 400 code-path or wrong-error-shape passes silently across 6 tests.)

## TestValidatePathsBranches (lines 2875–3019)

| Line | Test | Verdict | Note |
|---|---|---|---|
| 2878 | test_validate_paths_valid_structure | Strong | Pins valid + info substring. |
| 2898 | test_validate_paths_missing_media_subfolder | Weak | Substring `"Media" in e` — extremely permissive; almost any error mentioning media passes. |
| 2914 | test_validate_paths_missing_localhost | Weak | Substring `"localhost" in e` — same critique. |
| 2931 | test_validate_paths_incomplete_structure_warns | Strong | Pins "incomplete" warning. |
| 2949 | test_validate_paths_no_mapping_info | Strong | Pins "No path mapping" substring. |
| 2968 | test_validate_paths_traversal_rejected | Bug-blind | Only `valid=False`. Path traversal is a SECURITY check; should also pin the specific error reason and that the traversal target was not interpolated into the response. **Why downgraded:** security-relevant test under-asserted. |
| 2984 | test_validate_paths_legacy_null_byte_in_local_media | Strong | Pins "invalid path" substring. |
| 3005 | test_validate_paths_plex_data_path_null_byte_rejected | Strong | Pins "Invalid Plex Data Path" substring. |

## TestPageRoutesAdditional (lines 3027–3119)

| Line | Test | Verdict | Note |
|---|---|---|---|
| 3030 | test_automation_page_requires_auth | Strong | 302 + Location pin. |
| 3035 | test_automation_page_renders_with_both_panes | Strong | Four DOM markers. |
| 3044 | test_webhooks_route_redirects_to_automation | Strong | Endswith pin. |
| 3049 | test_schedules_route_redirects_to_automation | Strong | Three substring pins. |
| 3057 | test_schedules_route_redirect_preserves_edit_query | Strong | Three substring pins. |
| 3065 | test_webhooks_route_redirect_preserves_query | Strong | Two substring pins. |
| 3072 | test_logs_page_requires_auth | Strong | 302 + Location pin. |
| 3077 | test_logs_page_accessible_when_authenticated | Bug-blind | Only `200` — never inspects body. A logs page that renders the wrong template (or empty stub) passes. **Why downgraded:** prompt pattern #1. |
| 3081 | test_setup_wizard_page_redirects_when_configured | Strong | 302 + endswith pin. |
| 3090 | test_index_redirects_to_setup_when_not_configured | Strong | 302 + Location pin. |

## TestLogHistoryAPI (lines 3127–3321)

| Line | Test | Verdict | Note |
|---|---|---|---|
| 3143 | test_log_history_requires_auth | Strong | 401 pinned with regression note. |
| 3151 | test_log_history_empty_when_no_file | Strong | Pins empty + has_more. |
| 3163 | test_log_history_returns_entries | Weak | Pins length + first/last msg, but never asserts the level/mod/func/line fields are surfaced — a regression that drops most fields per entry passes. |
| 3203 | test_log_history_level_filter | Strong | Pins single matching entry. |
| 3242 | test_log_history_before_cursor | Strong | Pins single pre-cursor entry. |
| 3284 | test_log_history_rejects_malformed_before_cursor | Strong | 400 + substring. |
| 3299 | test_log_history_limit | Strong | Length + last entry pin. (Missing matrix cell: `has_more=True` when limit < total — could be a Weak.) |

## TestLibrariesAPI (lines 3329–3407)

| Line | Test | Verdict | Note |
|---|---|---|---|
| 3333 | test_get_libraries_with_settings | Strong | Boundary-mocked, asserts URL + token + body. |
| 3388 | test_get_libraries_no_config | Strong | Pins empty list. |

## TestPlexTestConnection (lines 3415–3591)

| Line | Test | Verdict | Note |
|---|---|---|---|
| 3419 | test_plex_test_success | Strong | Pins success + name + verify kwarg. |
| 3443 | test_plex_test_no_url_returns_400 | Weak | Only 400; no body inspection. |
| 3456 | test_plex_test_connection_failure | Strong | Pins error substring + URL echo. |
| 3477 | test_plex_test_timeout_returns_specific_message | Strong | Pins "timed out" substring. |
| 3497 | test_plex_test_ssl_error_returns_specific_message | Strong | Pins SSL + Verify SSL substrings. |
| 3518 | test_plex_test_http_401_returns_auth_message | Strong | Pins 401 + token substrings. |
| 3545 | test_plex_test_http_404_returns_not_plex_message | Weak | Pins "404" substring; doesn't pin the "not a Plex server" guidance the docstring promises. |
| 3571 | test_plex_test_invalid_json_returns_not_plex_message | Strong | Disjunctive but the alternatives are both legitimate phrasings. |

## TestPlexWebhookLoopbackGuard (lines 3599–3732)

| Line | Test | Verdict | Note |
|---|---|---|---|
| 3610 | test_loopback_in_docker_short_circuits_with_guidance | Strong | Pins success=False + localhost + Docker substrings + assert_not_called. |
| 3635 | test_loopback_outside_docker_proceeds_with_network_call | Strong | URL prefix pin + D31 regression guard + multipart files kwarg pin. |
| 3676 | test_non_loopback_in_docker_proceeds | Strong | Same shape as above. |
| 3711 | test_loopback_guard_covers_ipv4_and_ipv6 | Strong | Two-URL matrix. |

## TestPlexLibrariesAPI (lines 3740–3811)

| Line | Test | Verdict | Note |
|---|---|---|---|
| 3744 | test_get_plex_libraries | Strong | Pins length + name + type + URL prefix + token kwarg. |
| 3777 | test_get_plex_libraries_no_creds | Weak | Only 400; no body inspection. |
| 3790 | test_get_plex_libraries_ssl_error_returns_specific_message | Strong | Pins 502 + SSL substring. |

## TestFetchLibrariesViaHTTP (lines 3819–3861)

| Line | Test | Verdict | Note |
|---|---|---|---|
| 3823 | test_fetch_libraries_filters_movie_and_show | Strong | Five-field pin + verify kwarg. |
| 3848 | test_fetch_libraries_can_disable_ssl_verification | Strong | verify=False kwarg pin. |

## TestParamToBool (lines 3864–3907)

| Line | Test | Verdict | Note |
|---|---|---|---|
| 3867 | test_none_returns_default | Strong | Pins both branches. |
| 3873 | test_bool_passthrough | Strong | Pins both branches. |
| 3879 | test_truthy_strings | Strong | Loop over 6 truthy values. |
| 3885 | test_falsy_strings | Weak | Includes `"anything"` and `""` — these aren't really "falsy strings" in the conceptual sense, they're "everything-not-truthy". Test is correct for the implementation but the name is misleading. (Test still meaningful.) |
| 3892 | test_get_plex_libraries_passes_verify_ssl_override | Strong | verify=False kwarg pin. |

## TestLibraryCache (lines 3915–3998)

| Line | Test | Verdict | Note |
|---|---|---|---|
| 3926 | test_libraries_cached_on_second_call | Strong | call_count == 1 after second call. |
| 3945 | test_cache_bypassed_with_explicit_url | Strong | call_count == 2 + URL pin. |
| 3966 | test_cache_invalidated_on_plex_url_change | Strong | call_count + URL pin after settings change. |

## TestClassifyLibraryType (lines 4006–4048)

| Line | Test | Verdict | Note |
|---|---|---|---|
| 4009 | test_movie_section_returns_movie | Strong | Pure-function single-cell. |
| 4014 | test_movie_with_none_agent_returns_other_videos | Strong | Pure-function single-cell. |
| 4019 | test_show_section_returns_show | Strong | Pure-function single-cell. |
| 4024 | test_show_with_sportarr_agent_returns_sports | Strong | Pure-function. |
| 4029 | test_show_with_sportscanner_agent_returns_sports | Strong | Pure-function. |
| 4034 | test_show_sports_pattern_is_case_insensitive | Strong | Pure-function. |
| 4039 | test_show_with_none_agent_falls_through_to_show | Strong | Pure-function. |
| 4045 | test_unknown_section_type_passes_through | Strong | Pure-function. |

## TestGetVersionInfo (lines 4056–4251)

| Line | Test | Verdict | Note |
|---|---|---|---|
| 4070 | test_local_docker_when_git_env_is_unknown | Strong | Four-field pin. |
| 4088 | test_docker_release_with_update_available | Strong | Four-field pin. |
| 4106 | test_docker_release_no_update_when_current_is_latest | Weak | Pins install_type + update_available, not current_version (matrix-incomplete vs sibling test). |
| 4122 | test_dev_docker_when_branch_is_not_a_version | Strong | Three-field pin. |
| 4139 | test_dev_docker_no_update_when_sha_matches_head | Weak | Same shape as 4106 — only install_type + update_available. |
| 4156 | test_dev_docker_when_branch_is_main | Strong | Four-field pin. |
| 4174 | test_pr_build_when_branch_starts_with_pr | Strong | Four-field pin + branch_head_calls assertion. |
| 4204 | test_source_install_when_no_git_env | Strong | Three-field pin. |
| 4229 | test_cache_hit_returns_memoized_result | Strong | call_count + equality pin. |

## TestGetPlexServersConnectionList (lines 4259–4432)

| Line | Test | Verdict | Note |
|---|---|---|---|
| 4275 | test_multi_connection_server_builds_normalized_list | Strong | Four-field pin + connections list shape. |
| 4323 | test_missing_uri_falls_back_to_protocol_host_port | Strong | URI + ssl pin. |
| 4354 | test_non_server_resources_are_filtered_out | Strong | Length + name pin. |
| 4383 | test_server_with_no_connections_is_skipped | Strong | Empty list pin. |
| 4395 | test_protocol_inferred_from_uri_when_absent | Strong | Two-field pin. |
| 4423 | test_missing_token_returns_401 | Strong | 401 pinned. |

## TestBifSearchPhases (lines 4440–4653)

| Line | Test | Verdict | Note |
|---|---|---|---|
| 4497 | test_season_filter_skips_phase_2_and_passes_filters_to_fetch | Strong | Title pin + URL inspection (positive + negative). |
| 4563 | test_plain_query_includes_movie_and_episode_hubs | Strong | Title pin + tree URL pin per item. |
| 4600 | test_duplicate_keys_are_deduped | Strong | Length + title pin + per-key tree-call count. |
| 4627 | test_short_query_returns_400 | Strong | 400 + "2 characters" substring. |
| 4632 | test_missing_plex_config_returns_400 | Weak | Only 400; no body inspection. |
| 4645 | test_plex_network_failure_returns_502 | Weak | Only 502; no body inspection (regression that 502s with credential leak in body would pass). |

## TestFolderBrowse (lines 4661–4722)

| Line | Test | Verdict | Note |
|---|---|---|---|
| 4664 | test_browse_lists_subdirectories | Strong | Pins names set. |
| 4682 | test_browse_hides_dot_dirs_by_default | Strong | Two-call matrix on show_hidden. |
| 4700 | test_browse_rejects_relative_paths | Strong | 400 + "absolute" substring. |
| 4705 | test_browse_rejects_denylisted_paths | Strong | Five-path matrix, all 403. |
| 4717 | test_browse_404_on_missing_path | Weak | Only 404; no body inspection. |

## TestValidatePlexConfigFolder (lines 4730–4825)

| Line | Test | Verdict | Note |
|---|---|---|---|
| 4733 | test_valid_plex_structure_returns_shard_count | Strong | Three-field pin + "16/16" substring. |
| 4758 | test_missing_media_folder_reports_clear_error | Weak | `"Media" in body["error"]` — single-word substring, very permissive. |
| 4775 | test_outside_root_existing_folder_suggests_docker_bind | Strong | Three pins including negative ("outside the allowed" old wording absent). |
| 4805 | test_outside_root_nonexistent_folder_suggests_mount | Strong | Three-field pin. |

## TestPerServerPlexWebhook (lines 4833–4943)

| Line | Test | Verdict | Note |
|---|---|---|---|
| 4864 | test_status_uses_per_server_token | Strong | Five-field pin including captured token + URL. |
| 4886 | test_register_persists_url_onto_target_server | Strong | Side-effect captures + post-state diff on target + non-target. |
| 4921 | test_register_rejects_unknown_server | Strong | 404 + reason pin. |
| 4930 | test_register_rejects_non_plex_server | Strong | 400 + "Plex-only" substring. |

## TestBackupRestore (lines 4946–5081)

| Line | Test | Verdict | Note |
|---|---|---|---|
| 4949 | test_lists_timestamped_backups_newest_first | Strong | Filename ordering + legacy + bak_newer + has_bak pinned. |
| 4987 | test_restore_named_backup_overwrites_live | Strong | File contents + snapshot pin. |
| 5019 | test_restore_defaults_to_newest_when_backup_param_omitted | Strong | File contents pin. |
| 5043 | test_restore_rejects_unknown_file | Weak | Only 400; no body inspection (security-relevant — should pin error reason). |
| 5051 | test_restore_rejects_unknown_backup_name | Weak | Only 404; no body inspection. |
| 5069 | test_restore_404_when_no_backups | Weak | Only 404; no body inspection. |

## TestSettingsManagerWebhookMigration (lines 5084–5142)

| Line | Test | Verdict | Note |
|---|---|---|---|
| 5087 | test_migration_moves_url_onto_plex_server | Strong | Five-key migration pin. |
| 5124 | test_migration_drops_keys_when_no_plex_server | Strong | Two-key drop pin. |

---

**File verdict**: GOOD-with-bug-blind-pockets. The file is fundamentally sound — the security-critical surface (auth, credential stripping, path traversal, webhook URL validation, settings persistence round-trips, BIF/library mocking at the HTTP boundary) is well covered with strong assertions. However ~25% of the tests under-assert in identifiable ways: the worst clusters are (a) 400/409-only tests across `TestWorkerScalingValidation`, `TestSchedulesAPI`, `TestPathValidation` that never inspect error bodies, (b) "key in body" presence-only checks in `TestJobsAPI` (`/jobs/clear`, `/jobs/stats`, `/jobs/workers`, `/processing/state`), and (c) two settings-save tests that don't read back. The previous "all STRONG" verdict was incorrect; this pass downgrades 51 tests (~19%): 13 Bug-blind, 36 Weak, 1 Tautological, 1 Dead-redundant. Zero Bug-locking, zero Needs-human.

---

## Fix queue

Tests downgraded that should be strengthened, in roughly diminishing risk order.

### Bug-blind (status/key-presence only — fix first)

| Line | Test | Fix |
|---|---|---|
| 775 | TestJobsAPI.test_clear_jobs | Add `assert isinstance(body["cleared"], int) and body["cleared"] >= 0`; ideally seed N terminal jobs and pin `cleared == N`. |
| 802 | TestJobsAPI.test_clear_jobs_empty_statuses_clears_all_terminal | Seed jobs in completed/failed/cancelled + a running one; pin `cleared == 3` and verify the running job survives. |
| 877 | TestJobsAPI.test_get_job_stats | Pin `data["total"]` is an int and matches the seeded job count; pin sibling fields (running/pending/completed counts). |
| 883 | TestJobsAPI.test_get_worker_statuses | Pin per-worker dict shape (e.g. `id`, `worker_type`, `status` keys present in each entry) — currently any `[]` passes. |
| 948 | TestJobsAPI.test_processing_state_get | Set `processing_paused = True` then GET; assert `paused is True`. Repeat with False. (Round-trip pin.) |
| 1574 | TestSettingsAPI.test_save_settings_ignores_unknown_fields | After POST, GET `/api/settings` and assert `"unknown_field" not in resp.get_json()`. |
| 1647 | TestSettingsAPI.test_save_log_settings | After POST, GET back and pin `log_level == "DEBUG"`, `log_rotation_size == "5 MB"`, `log_retention_count == 4`. |
| 2004 | TestSetupWizardAPI.test_get_setup_state | Save state with `step=2` first, then assert `data["step"] == 2`; pin `data` key shape. |
| 2582 | TestSchedulesCRUD.test_create_schedule_interval | Pin `data["name"] == "Every 6h"`, `"id" in data`, and that the interval persisted (GET it back). |
| 2968 | TestValidatePathsBranches.test_validate_paths_traversal_rejected | Pin error substring (e.g. `"outside" in errors_joined`) and assert raw user input string not echoed. |
| 3077 | TestPageRoutesAdditional.test_logs_page_accessible_when_authenticated | Pin a logs-template marker (e.g. `b'id="log-stream"'`, the logs-page title). |

### Weak — add a kwarg/value pin

| Line | Test | Fix |
|---|---|---|
| 136 | TestPageRoutes.test_login_page_renders | Replace disjunctive substring with a stable form-element marker. |
| 146 | TestPageRoutes.test_settings_accessible_when_authenticated | Same. |
| 153 | TestPageRoutes.test_servers_page_accepts_add_query_param | For at least one valid vendor, assert the page contains a vendor-specific marker (e.g. `b'data-vendor="emby"'` for `?add=emby`). |
| 237 | TestLoginLogout.test_login_post_invalid_token | Pin a stable error marker, not `b"didn"`. |
| 256, 263 | login rate-limit tests | Verify the 5th/10th still returns 200/401 (legitimate) and only the *next* returns 429 — assert ordering. |
| 299 | TestAuthAPI.test_api_logout | Follow with `client.get("/api/auth/status")` and pin `authenticated is False`. |
| 359 | TestTokenEndpoints.test_setup_token_info | Pin `data["source"] in ("env", "config", "auth_file")` (the actual enumerated values). |
| 410 | TestTokenEndpoints.test_set_custom_token_rejects_too_short | Pin error substring mentioning length. |
| 484 | TestTokenEndpoints.test_wizard_server_endpoints_not_redirected_during_setup | Add `assert resp.status_code in {200, 400, 401, 502}` so 500 fails. |
| 531 | TestJobsAPI.test_create_job | Pin `data["id"]` is a non-empty string. |
| 608 | TestJobsAPI.test_create_job_does_not_infer_for_multi_library_picks | Add `assert body["server_name"] is None`. |
| 642 | TestJobsAPI.test_create_job_explicit_server_id_overrides_inference | Add `assert mock_start.call_args[0][1].get("server_id") == "plex-B"`. |
| 1231 | TestManualTriggerAPI.test_manual_trigger_missing_body | Pin error substring. |
| 1282 | TestManualTriggerAPI.test_manual_trigger_multiple_paths | Inspect `mock_start.call_args[0][1]["webhook_paths"]` and assert it equals `[str(f1), str(f2)]`. |
| 1309 | TestSettingsAPI.test_get_settings | Add `isinstance(data["gpu_threads"], int)`, etc. |
| 1611 | TestSettingsAPI.test_save_gpu_config_validates_list | Pin error substring (e.g. `"list" in error`). |
| 1675 | TestSettingsAPI.test_update_log_level_invalid | Pin "Invalid log level" substring. |
| 2072 | TestSetupWizardAPI.test_set_setup_token_too_short | Pin error substring. |
| 2089 | TestQuietHoursAPI.test_get_returns_default_when_unset | Pin `body["currently_in_quiet_window"] is False`. |
| 2157 | TestQuietHoursAPI.test_post_legacy_invalid_start_time_returns_400 | Pin error substring. |
| 2174, 2182 | TestSchedulesAPI missing-field tests | Pin error substring. |
| 2394 | TestPathValidation.test_validate_paths_empty | Pin error substring (e.g. `"required"` or `"Plex Data"`). |
| 2404 | TestPathValidation.test_validate_paths_null_bytes_rejected | Pin "Invalid Plex Data Path" substring. |
| 2423 | TestPathValidation.test_validate_paths_path_mappings_new_format_local_not_found | Replace 3-way disjunctive with an exact match against the production string. |
| 2571 | TestSchedulesCRUD.test_create_schedule_cron | Pin `data["id"]` is non-empty + verify schedule appears in GET list. |
| 2698 | TestReprocessJob.test_reprocess_completed_job | Inspect new job: pin `library_name == "Movies"`, `parent_job_id` absent from new config, retry-marker keys stripped. |
| 2718 | TestReprocessJob.test_reprocess_running_job_rejected | Pin "running or pending" error substring. |
| 2740, 2757, 2774, 2791, 2808, 2827 | TestWorkerScalingValidation 400/409-only tests | For each, pin a meaningful error substring (`"count"`, `"worker_type"`, `"not available"`). |
| 2898, 2914 | TestValidatePathsBranches missing-folder tests | Replace single-word substrings with exact strings (e.g. `"Plex Media folder not found"`). |
| 3163 | TestLogHistoryAPI.test_log_history_returns_entries | Pin `lines[0]["level"] == "INFO"`, `lines[0]["mod"] == "a"`. |
| 3299 | TestLogHistoryAPI.test_log_history_limit | Add a `has_more=True` assertion when limit < total. |
| 3443 | TestPlexTestConnection.test_plex_test_no_url_returns_400 | Pin error substring. |
| 3545 | TestPlexTestConnection.test_plex_test_http_404_returns_not_plex_message | Pin "not a Plex server" or similar guidance substring. |
| 3777 | TestPlexLibrariesAPI.test_get_plex_libraries_no_creds | Pin error substring. |
| 4106 | TestGetVersionInfo.test_docker_release_no_update_when_current_is_latest | Add `assert result["current_version"] == "3.4.1"`. |
| 4139 | TestGetVersionInfo.test_dev_docker_no_update_when_sha_matches_head | Add `assert result["current_version"] == "dev@abc1234"`. |
| 4632 | TestBifSearchPhases.test_missing_plex_config_returns_400 | Pin error substring (e.g. `"Plex"` or `"configured"`). |
| 4645 | TestBifSearchPhases.test_plex_network_failure_returns_502 | Pin generic upstream-failure substring; assert no token leakage in body. |
| 4717 | TestFolderBrowse.test_browse_404_on_missing_path | Pin error substring. |
| 4758 | TestValidatePlexConfigFolder.test_missing_media_folder_reports_clear_error | Pin exact production string ("missing the Media subfolder"). |
| 5043, 5051, 5069 | TestBackupRestore reject tests | Pin error substring per case (security-relevant). |

### Tautological

| Line | Test | Fix |
|---|---|---|
| 2674 | TestSchedulesCRUD.test_run_now | Pin a real side effect: assert a new job appears in `/api/jobs` list, OR pin schedule's `last_run_at` advanced. Currently passes with a no-op endpoint. |

### Dead/redundant

| Line | Test | Fix |
|---|---|---|
| 1263 | TestManualTriggerAPI.test_manual_trigger_force_regenerate | Duplicates test at line 1200 (`test_manual_trigger_with_force_regenerate_propagates`). Delete the older/simpler one or merge. |
