# E2E Coverage Matrix

**Scope:** Playwright + Flask-subprocess E2E tests in `tests/e2e/`.
**Branch:** `dev`. **Generated:** 2026-05-05.

**Inventory:**
- **23 test files** (excluding `__init__.py`, `conftest.py`, `_mocks.py`)
- **~70 individual `test_*` methods** across them
- **2 app subprocesses**: `app_url` (setup-complete) + `app_url_wizard` (first-run)
- Auth bypass via `WEB_AUTH_TOKEN=e2e-test-token` + captured Flask session cookie
- All vendor APIs / FFmpeg / GPU / BIF endpoints are client-side mocked via `page.route()` from `_mocks.py`

The **fundamental architectural limitation** of the suite as it stands: **almost everything downstream of an HTTP boundary is mocked**. There are no e2e tests where the Flask backend actually runs a job, dispatches to a worker, or completes a BIF — only ones that assert the UI button POSTs to the right URL with the right payload. SocketIO live updates (`job_progress`, `worker_phase`, `job_complete`) are never driven from the server side in e2e. This is the dominant gap pattern.

---

## Section 1 — Existing coverage

| Journey | Test file | Test method(s) | Confidence |
|---|---|---|---|
| Login page renders + autofocus + concise copy | `test_login_page.py`, `test_webapp.py` | `test_token_input_is_autofocused`, `test_login_page_subtitle_is_concise`, `test_login_page_loads`, `test_login_page_has_title` | Strong (UI smoke) |
| Login with **invalid** token shows error alert | `test_login_page.py` | `test_invalid_token_shows_error_alert` | Strong |
| Login with **valid** token redirects | `test_webapp.py` | `test_valid_token_redirects_away_from_login` | Smoke-only |
| Authenticated user can reach protected pages | `test_webapp.py` | `test_authenticated_user_can_access_protected_pages` | Smoke-only |
| Setup wizard — Plex full happy path (all 5 steps) | `test_wizard_full_flows.py` | `test_plex_full_wizard_completes` | Happy-path-only |
| Setup wizard — Emby happy path (skips steps 2+3) | `test_wizard_full_flows.py` | `test_emby_skips_plex_specific_steps` | Happy-path-only |
| Setup wizard — Jellyfin Quick-Connect inline | `test_wizard_emby_jellyfin_inline.py` | `test_jellyfin_quick_connect_save_advances_to_step4` | Happy-path-only |
| Setup wizard — Emby password inline | `test_wizard_emby_jellyfin_inline.py` | `test_emby_password_save_advances_to_step4` | Happy-path-only |
| Wizard step 1 — vendor picker variants | `test_wizard_step1_vendor_picker.py` | 6 tests covering all 3 vendors + back-link + skip-setup | Strong |
| Wizard step 2 — library picker | `test_wizard_step2_libraries.py` | 4 tests (3 cards, tick/untick, zero-libs empty grid) | Strong |
| Wizard step 3 — paths + folder browse + path validation | `test_wizard_step3_paths.py` | 5 tests (valid/invalid Plex folder, browse, add row, local path red) | Strong |
| Wizard step 4 — per-GPU panel + steppers + rescan | `test_wizard_step4_processing.py` | 6 tests (cards render, stepper inc/dec/clamp, disable greys, rescan call, CPU stepper) | Strong |
| Wizard step 5 — token enforcement (blank/short/mismatch/same/valid) | `test_wizard_step5_security.py` | 5 token-enforcement + 1 env-controlled hide-form | Strong |
| Wizard step 5 — env-controlled token bypasses POST | `test_wizard_step5_security.py` | `test_env_controlled_hides_form_and_proceeds_without_token_post` | Strong |
| Setup page accessible after login (smoke) | `test_webapp.py` | `test_setup_page_accessible_after_login`, `test_setup_wizard_has_5_steps`, `test_step5_has_security_label`, `test_step5_has_new_token_inputs`, `test_step5_has_finish_button` | Smoke-only |
| Folder picker modal — open, type/Enter, drill in, up disabled at root, pick + populate | `test_folder_picker.py` | 5 tests | Strong |
| Dashboard — empty state when no servers | `test_dashboard.py` | `test_empty_state_banner_visible_when_no_servers`, `test_empty_state_cta_links_to_servers` | Strong |
| Dashboard — per-server status block renders | `test_dashboard.py` | `test_media_servers_status_renders_per_server` | Smoke-only (renders names, no live updates) |
| Dashboard — per-GPU worker config card renders | `test_dashboard.py` | `test_per_gpu_card_renders_from_status` | Smoke-only |
| Dashboard — CPU stepper +/- POSTs settings | `test_dashboard.py` | `test_cpu_stepper_plus_increments_badge` | Strong (asserts payload key) |
| Dashboard — update-available badge shown | `test_dashboard.py` | `test_update_available_badge_shown_when_newer` | Smoke-only |
| Dashboard — "Start New Job" modal opens + POSTs | `test_dashboard_modals.py` | `test_new_job_modal_opens`, `test_new_job_modal_submits_to_jobs_endpoint` | Happy-path-only |
| Dashboard — "Manual Trigger" modal opens | `test_dashboard_modals.py` | `test_manual_trigger_modal_opens` | Smoke-only |
| Dashboard — Jellyfin-only registry can run full scan (Phase D regression) | `test_dashboard_modals.py` | `test_jellyfin_full_scan_posts_to_jobs_endpoint` | Happy-path-only |
| Dashboard — server dropdown shows vendor badges | `test_dashboard_modals.py` | `test_new_job_dropdown_shows_vendor_in_option_text`, `test_manual_trigger_dropdown_shows_vendor_in_option_text` | Strong (regression) |
| Dashboard — active-jobs render defers when hovered | `test_ui_hover_defer.py` | `test_active_jobs_render_defers_when_container_is_hovered`, `test_active_jobs_render_DOES_rebuild_when_NOT_hovered` | Strong (paired) |
| Dashboard — workers panel in-place update preserves DOM identity | `test_ui_workers_panel.py` | `test_re_render_with_same_workers_preserves_card_node_identity`, `test_vanished_worker_card_is_removed_by_key` | Strong |
| Dashboard — worker phase rendering (pre-FFmpeg, FFmpeg started, fallback active) | `test_ui_workers_panel.py` | 3 tests covering 3 phase states | Strong (matrix coverage) |
| Servers page — loads, heading, list container | `test_servers_page.py` | 3 layout tests | Smoke-only |
| Servers page — webhook URL input rendered + populated | `test_servers_page.py` | 2 tests | Smoke-only |
| Servers page — Add Server modal: 3 vendor buttons, Emby/Jellyfin auth picker, Plex OAuth | `test_servers_page.py` | 4 tests | Strong (vendor matrix) |
| Servers page — API integration smoke (list, 404, validation) | `test_servers_page.py` | 3 tests hitting `/api/servers` directly | Smoke-only |
| Servers page — Plex add via manual token | `test_servers_page.py` | `test_plex_add_via_manual_token_creates_server` | Happy-path-only |
| Servers page — Emby add via password | `test_servers_page.py` | `test_emby_add_via_password_creates_server` | Happy-path-only |
| Servers page — refresh-libraries button POSTs | `test_servers_page.py` | `test_refresh_libraries_button_calls_endpoint` | Smoke-only |
| Servers page — health-check pill visible / hidden | `test_servers_jellyfin_trickplay.py` | 2 tests | Strong (paired states) |
| Settings — sidebar links | `test_settings_page.py` | `test_sidebar_links_present` | Smoke-only |
| Settings — per-GPU panel + disable greys | `test_settings_page.py` | 2 tests | Strong |
| Settings — steppers (CPU, thumbnail interval, log rotation) | `test_settings_page.py`, `test_settings_steppers.py` | 3 + 3 tests | Strong |
| Settings — set custom token (matching), regenerate token | `test_settings_page.py` | 2 tests | Happy-path-only |
| Settings — backups panel renders + restore POSTs newest filename | `test_settings_page.py` | 2 tests | Strong (asserts payload) |
| Schedules — save recently-added against Jellyfin (Phase E regression) | `test_schedules.py` | `test_save_recently_added_schedule_against_jellyfin_succeeds` | Happy-path-only |
| Schedules — save full-library against Emby | `test_schedules.py` | `test_save_full_library_schedule_against_emby_succeeds` | Happy-path-only |
| Schedules — server dropdown vendor badges | `test_schedules.py` | `test_schedule_server_dropdown_shows_vendor_in_option_text` | Smoke-only |
| Preview Inspector (`/bif-viewer`) — Plex appears in picker (regression) | `test_preview_inspector.py` | `test_plex_server_appears_in_picker` | Strong (regression) |
| Preview Inspector — multi-vendor servers all appear | `test_preview_inspector.py` | `test_multi_vendor_servers_all_appear` | Strong |
| Preview Inspector — search + path tabs render & switch | `test_preview_inspector.py` | 2 tests | Smoke-only |
| Logs page — loads, returns 200 | `test_logs_page.py` | 2 tests | Smoke-only |
| Automation page — copy regression + section anchors | `test_webhooks_automation.py` | 4 tests | Smoke-only |
| Theme toggle — flips `data-bs-theme` + persists | `test_theme_toggle.py` | 2 tests | Strong (paired) |
| Health/auth-status JSON endpoints | `test_webapp.py` | `test_health_check_endpoint`, `test_auth_status_endpoint` | Smoke-only |

---

## Section 2 — Gaps

Below: every documented user journey from the request that is **not covered** (or where the existing coverage is so thin it does not exercise the journey end-to-end).

| Journey | Why it matters (bug class it would catch) | Suggested test name + outline |
|---|---|---|
| **Live job lifecycle from click → completion** (button → SocketIO `job_progress` events → completion → moved to history) | The largest blind spot. Every UI test stops at "button POSTed correctly". Bug class: SocketIO event-name regressions, job-state stuck in "queued"/"running" forever, history list never populating, "active jobs" never clearing the just-completed item. The user-facing failure is "I clicked Start and the UI shows nothing happening for hours" — currently uncaught. | `test_jobs_live_lifecycle.py::TestLiveJob::test_full_scan_emits_progress_then_completes_then_lands_in_history` — drive the real Flask backend; mock only FFmpeg + the Plex/registry layer; assert SocketIO emits progress and the active-job card flips to a history-row entry. |
| **Cancel / kill running job from active card** | `/api/jobs/<id>/cancel` is registered but no e2e test clicks the kill button or asserts that the worker terminates and the card disappears. Bug class: stuck "Cancelling…" state, ghost workers in panel after cancel, double-cancel exception. | `test_jobs_cancel.py::test_kill_button_cancels_running_job_and_removes_card` |
| **Pause / resume scan** (`/api/processing/pause`, `/api/processing/resume`, plus per-job `/jobs/<id>/pause`, `/resume`) | Not exercised at all in e2e. Bug class: pause toggle desync between UI and worker pool, resume not picking up where left off. | `test_processing_pause_resume.py::test_pause_button_freezes_workers_resume_button_thaws` |
| **Retry-ETA countdown** in dashboard | Mentioned in the request as a high-value journey. No test asserts the ETA timer ticks, hits zero, and triggers retry. Bug class: countdown frozen, ETA never reaching zero, multiple ETAs overlapping. | `test_dashboard_retry_eta.py::test_retry_eta_countdown_decrements_each_second_and_fires_at_zero` |
| **Edit existing server** (open card → edit modal → save → card re-renders new state) | `PUT /api/servers/<id>` exists but no test opens the edit flow. Add-server is tested but not edit. Bug class: form not pre-populated from current values, save not refreshing card, vendor switch corrupting record. | `test_servers_edit.py::test_editing_plex_url_persists_and_card_reflects` |
| **Delete server** (with `DELETE /api/servers/<id>`) | No test. Bug class: confirm-modal not appearing, downstream schedules/webhooks left orphaned, card not removed on success. | `test_servers_delete.py::test_delete_server_removes_card_and_orphans_clean` |
| **Add a 2nd server** (multi-server scenario from inside the running app) | Wizard tests cover *first* server only; servers-page add tests cover *single* save. No test asserts a second server appears alongside the first. Bug class: add-server modal pre-fills stale vendor state, dropdown ID collision. | `test_servers_add_second.py::test_adding_second_server_appends_card_without_clobbering_first` |
| **Schedule edit + delete + run-now** | Only *create* is tested. `PUT`, `DELETE`, `enable`, `disable`, `run` endpoints registered but no UI test clicks any of them. Bug class: edit-modal not pre-populated, run-now firing twice, disable-toggle race. | `test_schedules_lifecycle.py::test_edit_save_delete_runnow_full_round_trip` |
| **Quiet hours UI** (`/api/quiet-hours` GET/POST) | Endpoint exists, sidebar link in automation.html exists (`#section-schedules-quiet-hours`), no e2e test. Bug class: time pickers off-by-one timezone, save not persisting, quiet hours not actually blocking scheduled runs. | `test_schedules_quiet_hours.py::test_quiet_hours_save_and_reload` |
| **Webhook auto-fire** (POST `/webhooks/sonarr` → debounce timer → job materialises) | The most-shipped silent-failure surface. No test POSTs to a webhook endpoint and asserts a job appears in the active panel within the debounce window. Bug class: debounce timer never firing, batch keyed wrong, job created with wrong server scope. The captured webhook paths in `AUDIT_test_webhook_router.md` were the genesis of this audit family. | `test_webhooks_e2e.py::test_sonarr_webhook_creates_job_after_debounce_visible_in_ui` |
| **Webhook fire-now** (skip debounce via `/webhooks/pending/<key>/fire-now`) | UI must surface the pending batch + a "fire now" button — no e2e test exercises either. Bug class: pending-batch list never refreshing, fire-now button 404ing on wrong key. | `test_webhooks_fire_now.py::test_pending_batch_renders_and_fire_now_dispatches` |
| **Webhook history page** (`GET /webhooks/history`, `DELETE /webhooks/history`) | Endpoint exists, sidebar link `#section-webhooks-activity` exists, no e2e test. Bug class: history not paginating, clear-history not actually clearing. | `test_webhooks_history.py::test_history_renders_and_clear_button_empties_list` |
| **Plex webhook registration** (`/api/settings/plex_webhook/register`, `/unregister`, `/status`, `/test`) | UI panel lives in `plex_webhook_panel.js`. No e2e test clicks Register / Test. Bug class: register POST sending stale URL, status endpoint returning wrong "registered" boolean. | `test_plex_webhook_panel.py::test_register_unregister_test_round_trip` |
| **BIF preview viewer — actual frame load + scrub** | Existing tests stub `/api/bif/info` to return `{"frames": []}` so no frame is ever rendered. Bug class: scrubber drag not snapping to nearest frame, broken JPEG concat, off-by-one frame index, server-id-scoped frame URL wrong. | `test_preview_inspector_scrub.py::test_load_path_renders_first_frame_then_scrubber_drag_loads_others` |
| **BIF viewer — search tab actually queries + renders results** | Tests stub `{"results": []}` only. No assertion on search input → results card rendering. Bug class: search debounce broken, no-results state not shown, vendor-scoped search returning wrong server's media. | `test_preview_inspector_search.py::test_search_input_renders_result_rows_clicking_loads_preview` |
| **Servers — health-check apply fix** | Pill visibility is tested but no test clicks "Apply" on the pill, asserts the POST went, and asserts the pill disappears. | `test_server_health_apply.py::test_clicking_apply_fix_posts_flags_and_pill_clears` |
| **Servers — vendor extraction toggle** (`/servers/<id>/vendor-extraction`, `/status`) | Endpoints registered, no UI test toggles them. The whole point of the feature is to *stop Emby/Jellyfin generating their own previews*; if the toggle silently fails the user gets duplicate work. | `test_servers_vendor_extraction.py::test_disable_vendor_extraction_posts_and_status_reflects` |
| **Servers — server enable/disable toggle** (`PATCH /servers/<id>/enabled`) | No test. Bug class: disable toggle not removing server from job dispatch but card still says "enabled". | `test_servers_enabled_toggle.py::test_disable_server_card_reflects_and_status_grey` |
| **Servers — install plugin** (`POST /servers/<id>/install-plugin`) | No test. | `test_servers_install_plugin.py::test_install_plugin_button_posts_and_shows_success` |
| **Servers — output status panel** (`GET /servers/<id>/output-status`) | No test. Bug class: shows wrong path / counts / "no output yet" forever. | `test_servers_output_status.py::test_output_status_renders_path_and_counts` |
| **First-time setup token flow** (login during `setup_state.complete=False` with the temp token, distinct from the regular post-setup token) | Wizard tests cover the in-wizard token *change*; no test covers logging in with the *first-run* temp token then completing setup. Bug class: temp-token not honoured at /login, redirect loop. | `test_login_first_run.py::test_first_run_temp_token_logs_in_and_lands_on_wizard` |
| **Logout flow** | `/logout` route registered, no test clicks it / asserts session clears. | `test_logout.py::test_logout_clears_session_and_redirects_to_login` |
| **Logs page — live tail tick** | Test only loads the page with stubbed empty payload. No assertion that `/api/logs` polling renders new lines as they arrive. Bug class: tail frozen, log lines duplicated, filter-by-level dropping all rows. | `test_logs_live_tail.py::test_new_log_lines_appended_as_they_arrive_filter_by_level_works` |
| **Logs page — filter / log-level dropdown + history archive** | Endpoints `/api/logs/history`, `/api/settings/log-level` exist; no test exercises either. | `test_logs_history.py::test_filter_dropdown_narrows_visible_rows_log_level_persists` |
| **Notifications — toast appears, dismisses, queue ordering** | `/api/system/notifications` + dismiss endpoints registered + `notifications.js` module exists, no e2e test. Listed explicitly in request. | `test_notifications.py::test_toast_appears_dismisses_and_second_toast_queues_after` |
| **Notifications — dismiss-permanent + reset-dismissed** | Endpoints registered, no test. | `test_notifications_permanent.py::test_dismiss_permanent_persists_across_reload_and_reset_brings_back` |
| **Settings save + reload by category** (e.g. processing settings save → reload page → values persist) | Steppers test only the *click*. No test reloads the page and verifies the new value comes back. Bug class: settings written to wrong key, GET handler returning stale defaults. | `test_settings_persist.py::test_change_thumbnail_interval_save_reload_value_persists` |
| **Backup — create backup / list / download** | Restore is tested, but the create-backup trigger and download-backup are not. | `test_settings_backup_create.py::test_create_backup_appears_in_list_then_downloadable` |
| **Schema-migration upgrade** (older settings.json on disk → app boots → migrated successfully) | `upgrade.py` exists with unit tests, but no e2e test boots the app with a known-old settings file and asserts the UI reflects migrated values + the old keys are gone from disk. Bug class: migration silently dropping values, infinite migration loop, "settings corrupted" lockout. | `test_upgrade_e2e.py::test_old_settings_file_boots_into_migrated_dashboard_state` |
| **Folder picker — across path-mapping rows** (open picker on row 2 + 3, paths land in correct row) | One picker open is tested; no test confirms multiple picker invocations land their results in the *correct* row. Bug class: picker callback writing to row 1 always. | `test_folder_picker_multi_row.py::test_picker_on_third_row_writes_to_third_input` |
| **Mobile card-stack tables** (jobs, schedules, webhooks at < 640px) | Listed explicitly in request. No e2e test resizes viewport and asserts table → card layout. Bug class: columns overflow on iPhone width, stack hiding actions. | `test_mobile_card_stack.py::test_jobs_table_collapses_to_cards_below_640px` |
| **Brand orange / dark mode consistency on every page** | Theme toggle test covers `data-bs-theme` attr only. No test asserts the brand-orange CSS variable is applied identically across `/`, `/servers`, `/settings`, `/automation`, `/bif-viewer`, `/logs`. Bug class: one page hard-coding the old colour and slipping through review. | `test_theme_consistency.py::test_brand_orange_css_var_present_on_every_page` |
| **Automation — Plex webhook / Sonarr / Radarr / Custom config panel save** | The page renders but no test exercises *saving* a webhook config / secret / debounce window. | `test_automation_save.py::test_save_sonarr_webhook_secret_persists` |
| **Tdarr custom webhook hit** (`/webhooks/custom`) | Endpoint registered, no test. | `test_webhooks_custom.py::test_custom_payload_dispatches_job` |
| **Library refresh — assert UI reflects new library list, not just that endpoint was called** | Existing `test_refresh_libraries_button_calls_endpoint` is happy-path only — asserts the POST fired but not that the card re-renders with new library names. | `test_servers_refresh_libraries_render.py::test_refresh_libraries_re_renders_card_with_new_names` |
| **Plex OAuth PIN flow** (`/api/plex/auth/pin` POST + GET) | The wizard's Plex step has manual-token tested, but the OAuth PIN flow (open PIN window, poll, exchange) has no e2e coverage. Bug class: PIN poll never resolving, redirect after PIN missing token. | `test_plex_oauth_pin.py::test_oauth_pin_flow_authenticates_and_lands_in_step2` |
| **Schedules — server dropdown when no servers configured** | Vendor-badge test covers populated dropdown only. No test asserts the empty-state message + disabled save button. | `test_schedules_empty.py::test_no_servers_disables_save_with_helpful_message` |
| **Failed-files / job-files inspection** (`/api/jobs/<id>/files`, `/api/jobs/<id>/logs`) | No UI test opens a completed job and walks its file list / per-file logs. | `test_job_inspector.py::test_completed_job_opens_files_panel_with_per_file_status` |
| **Reprocess single job / clear all jobs** (`/api/jobs/<id>/reprocess`, `/api/jobs/clear`) | Endpoints registered, no e2e test. Clear-all is destructive — should be confirm-gated, no test asserting that gate. | `test_jobs_reprocess_clear.py::test_reprocess_re_queues_clear_all_requires_confirm` |
| **CORS / WSS connection** to SocketIO from a non-same-origin frontend | Listed in CLAUDE.md as supported (`CORS_ORIGINS`). No e2e test verifies cross-origin SocketIO actually works. | `test_socketio_cors.py::test_cross_origin_socketio_handshake_succeeds` |
| **GPU rescan** triggered from settings (`POST /api/system/rescan-gpus`) | Wizard step 4 tests the rescan button; settings page does not. | `test_settings_gpu_rescan.py::test_settings_rescan_button_calls_endpoint_and_re_renders` |
| **Vulkan debug** (`/api/system/vulkan`, `/vulkan/debug`) | Endpoints registered, no test. | `test_vulkan_debug.py::test_vulkan_debug_panel_renders_payload` |
| **Skip-setup landing state** | `test_skip_setup_link_posts_skip_and_redirects` covers the POST but no test asserts the dashboard then renders an "incomplete config" hint after skip. | `test_skip_setup_landing.py::test_after_skip_dashboard_shows_setup_incomplete_banner` |

---

## Section 3 — Brittle areas (single-test journeys where a 2nd or 3rd test would meaningfully reduce risk)

1. **Setup wizard happy paths** — `test_wizard_full_flows.py` has *one* Plex test and *one* Emby test. **No Jellyfin happy path** through all 5 steps. Add `test_jellyfin_full_wizard_completes`. Also no unhappy path: connection-test fails, GPU rescan returns zero devices, token rejection mid-step5. A single brittle "every endpoint must respond exactly right" walk; one upstream change breaks it silently.
2. **New-job modal** — only the open + happy-path POST is asserted. No test for: zero libraries selected (should block submit), invalid path entered, target-server unreachable mid-submit, double-click re-submits. The ONLY thing currently catching a regression in the modal's submit gate is one test that asserts a request fired.
3. **Add-server flows** — Plex via manual token + Emby via password are tested, but **Jellyfin add via password and via Quick-Connect from the servers page (not wizard)** are missing. The Add-Server modal is shared between wizard and servers-page, but the server-side persistence layer for "add via servers page after setup" has no Jellyfin coverage.
4. **Schedules** — only happy-path saves on Jellyfin/Emby. No edit / delete / run-now / disable / quiet-hours / overlap-with-pause coverage. One test breaks → no fallback signal.
5. **Folder picker** — exclusively driven from wizard step 3's Plex-config field. Same picker is reused across path-mapping rows + servers-page add modal but is only e2e-tested in one location.
6. **Theme toggle** — only flips the attribute and checks localStorage. Doesn't verify any actual *page element* repaints to dark mode (e.g. nav background). A regression that flipped the attribute but failed to repaint would pass.
7. **Backups** — restore is asserted (with payload), but list-population is asserted only by string presence. Add: ordering correctness (newest-first), file-name format validation, malformed-list defensive rendering.
8. **Health pill** — "visible when critical" + "hidden when zero issues" are tested. The middle case (warnings-only, no critical) is uncovered — the pill colour should be different.

---

## Section 4 — Top 10 prioritized gaps

Ranked by **(user-facing bug-class severity) × (likelihood the gap hides a real bug today)**. Effort: **1=trivial / 2=medium / 3=hard** (3 means it requires the real backend to actually run a job, not just mocks).

| # | Journey | Suggested file + test name | Effort | Expected bug class caught |
|---|---|---|---|---|
| 1 | **Live job lifecycle (start → progress → complete → history)** with real backend + mocked FFmpeg only | `tests/e2e/test_jobs_live_lifecycle.py::test_full_scan_emits_progress_completes_lands_in_history` | 3 | SocketIO event-name drift (the bug class that broke `process_canonical_path` would land here); jobs stuck "queued" forever; history list never populating. Highest user-visible severity. |
| 2 | **Webhook auto-fire end-to-end** (POST `/webhooks/sonarr` → debounce → job in active panel) | `tests/e2e/test_webhooks_e2e.py::test_sonarr_webhook_creates_job_after_debounce_visible_in_ui` | 3 | Debounce timer broken; batch keyed wrong server; webhook silently dropped. The vendor-webhook surface that drove the recent audit batch — currently zero e2e safety net. |
| 3 | **Cancel / kill running job** | `tests/e2e/test_jobs_cancel.py::test_kill_button_cancels_running_job_and_removes_card` | 3 | Stuck "Cancelling…" state; ghost workers; double-cancel exception. Listed in the request as high-value. |
| 4 | **Edit existing server** (modal pre-populates → save → card re-renders) | `tests/e2e/test_servers_edit.py::test_editing_plex_url_persists_and_card_reflects` | 2 | Form not pre-populated from PUT body; save merging incorrectly; vendor-switch corrupting record. Listed in the request as high-value. |
| 5 | **BIF viewer — load path → render frame → scrubber drag** | `tests/e2e/test_preview_inspector_scrub.py::test_load_path_renders_frame_then_scrubber_loads_others` | 2 | Scrubber broken, off-by-one frame index, server-scoped frame URL wrong. The inspector currently can't tell you whether it would render a single byte. |
| 6 | **Schedule edit + delete + run-now lifecycle** | `tests/e2e/test_schedules_lifecycle.py::test_edit_delete_runnow_round_trip` | 2 | Edit-modal not pre-populated; run-now firing twice; disable-toggle race. Currently one create-side smoke test only. |
| 7 | **Settings save + reload per-category persistence** | `tests/e2e/test_settings_persist.py::test_change_thumbnail_interval_save_reload_persists` | 1 | Settings written to wrong key; GET returning stale defaults; partial-save losing other fields. Easy win — current steppers tests don't reload. |
| 8 | **Notifications toast lifecycle** (appear → auto-dismiss → queue ordering → dismiss-permanent) | `tests/e2e/test_notifications.py::test_toast_appears_dismisses_and_second_queues` | 2 | Toast queue stuck; dismiss-permanent flag not persisting; reset-dismissed not bringing back. Listed in the request. |
| 9 | **Schema-migration upgrade** (boot with old settings.json → app migrates → UI reflects new state) | `tests/e2e/test_upgrade_e2e.py::test_old_settings_file_boots_into_migrated_dashboard_state` | 2 | Migration silently dropping fields; infinite migration loop; "settings corrupted" lockout on user upgrade. Listed in the request. |
| 10 | **Pause / resume scan** (global + per-job) | `tests/e2e/test_processing_pause_resume.py::test_pause_freezes_workers_resume_thaws` | 3 | Pause toggle desync; resume not picking up where left off; ghost "paused" indicator after resume. Listed in the request. |

---

## Cross-cutting suspect: tests that exist but only test the happy path

These tests exist in the suite but the agent suspects (from reading them) they would NOT catch realistic regressions:

- **`test_dashboard.py::test_media_servers_status_renders_per_server`** — only asserts that the server *names* appear. Would not catch a regression that rendered every server as "disconnected" or showed wrong vendor icons.
- **`test_dashboard_modals.py::test_new_job_modal_submits_to_jobs_endpoint`** — captures the POST body but the `assert captured` line only checks that *something* fired. Doesn't validate the payload's `server_id`, `library_ids`, or `scan_type` — exactly the fields the dispatcher kwargs audit (`AUDIT_test_dispatcher_kwargs_matrix.md`) said matter most.
- **`test_logs_page.py::test_logs_page_loads`** — stubs `/api/logs` to return `{"logs": [], "files": []}` then asserts a container element exists. This passes even if the entire log rendering pipeline is broken.
- **`test_preview_inspector.py`** — every test stubs `{"frames": []}` so no frame is ever rendered. The whole *value* of the inspector is rendering frames; no e2e test does it.
- **`test_servers_page.py::test_refresh_libraries_button_calls_endpoint`** — asserts the POST fired, not that the new library list re-renders into the card. A regression where the card never refreshed would pass.
- **`test_schedules.py::test_save_*_succeeds`** — POSTs and checks `captured` is non-empty, but doesn't validate that the table re-renders the new schedule row. UI-side persistence regression would slip.
- **`test_settings_steppers.py::test_log_retention_increments` / `test_job_history_days_increments`** — increment assertion only; no test reloads the page to confirm persistence.
- **`test_wizard_full_flows.py::test_plex_full_wizard_completes`** — every endpoint is mocked client-side; the test passes if the JS click handlers fire in the right order, regardless of whether the *real* `/api/setup/complete` would have rejected the payload. This is wizard-flow JS coverage, not wizard-completion safety.
- **`test_servers_jellyfin_trickplay.py::TestServerHealthPill`** — covers visible/hidden states but never clicks "Apply" on the pill, so the apply-fix flow is wholly untested.
- **`test_theme_toggle.py`** — flips the attribute but never asserts a page element actually repaints.

---

## Summary stats

- **Existing e2e test files**: 23 (plus `conftest.py` + `_mocks.py`)
- **Existing e2e test methods**: ~70
- **User journeys identified** (from templates + routes + docs): ~55
- **Journeys with strong e2e coverage**: ~14
- **Journeys with smoke-only coverage**: ~22
- **Journeys with happy-path-only coverage**: ~10
- **Journeys with NO e2e coverage**: ~30 (see Section 2 — 41 entries when sub-journeys are itemised)

**Dominant gap pattern**: every test stops at the HTTP boundary. The Flask backend, SocketIO event stream, worker pool, scheduler, debounce timer, and BIF generator are never exercised end-to-end from a click. Top 3 priorities (live job lifecycle, webhook auto-fire, cancel job) all require breaking that pattern.
