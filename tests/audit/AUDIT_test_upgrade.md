# Audit: tests/test_upgrade.py — ~70 tests

Tests env-var migration + schema-version migrations (v2 → v11) + frame_reuse seeding + last_seen_version sentinel.

## TestRunMigrations

| Line | Test | Verdict |
|---|---|---|
| 32 | `test_calls_env_and_schema_migrations` | **Strong** — pins multiple side-effects (env_migrated, schema_version, plex_url written, media_servers synthesised, frame_reuse seeded). End-to-end check. |

## TestSeedLastSeenVersionForUpgraders

| Line | Test | Verdict |
|---|---|---|
| 72 | `test_upgrader_with_plex_url_gets_sentinel` | **Strong** — pins `"0.0.0"` exactly. |
| 85 | `test_upgrader_with_only_setup_complete_gets_sentinel` | **Strong** — alternate signal path covered. |
| 97 | `test_existing_last_seen_version_preserved` | **Strong** — pins `"3.7.4"` not clobbered. |
| 108 | `test_fresh_install_not_seeded` | **Strong** — pins `is None` for empty settings (different cell). |
| 122 | `test_idempotent_across_repeat_runs` | **Strong** — runs twice, pins value preserved through real user-dismiss flow. |
| 137 | `test_upgrader_with_media_servers_only_gets_sentinel` | **Strong** — third detection branch (media_servers only). Matrix complete. |

## TestEnvVarMigration

| Line | Test | Verdict |
|---|---|---|
| 156 | `test_migrates_plex_url` | **Strong** — strict equality. |
| 163 | `test_migrates_int_values` | **Strong** — pins type coercion (string → int). |
| 170 | `test_migrates_bool_values` | **Strong** — `is False` strict. |
| 177 | `test_skips_existing_keys` | **Strong** — pins existing-value preservation. |
| 185 | `test_runs_only_once` | **Strong** — pins idempotency through the `_env_migrated` guard. |
| 196 | `test_sets_env_migrated_flag` | **Strong** — `is True`. |
| 202 | `test_migrates_libraries` | **Strong** — pins comma-split into list (not str). |

## TestEnvVarMigrationExtended

| Line | Test | Verdict |
|---|---|---|
| 213 | `test_invalid_int_env_var_logged` | **Strong** — pins `is None` (not crash, not partial-write). |
| 222 | `test_gpu_config_migrated_from_env` | **Strong** — pins `len(config) == 1` AND `workers == 2`. |
| 238 | `test_path_mappings_migrated_from_env` | **Strong** — pins length + plex_prefix value. |
| 250 | `test_deprecated_env_var_does_not_crash` | **Strong** — pins migration ran AND keys NOT persisted (was bug-blind before per inline comment). |

## TestBuildGpuConfigFromEnv

| Line | Test | Verdict |
|---|---|---|
| 276 | `test_returns_none_when_no_env_vars` | **Strong** — `is None` strict. |
| 285 | `test_builds_config_with_gpu_threads` | **Strong** — pins `len == 2`, all enabled, total_workers == 2. |
| 304 | `test_gpu_selection_disables_unselected` | **Strong** — strict True/False per index. |
| 322 | `test_ffmpeg_threads_propagated` | **Strong** — strict equality on ffmpeg_threads. |
| 335 | `test_returns_empty_when_no_gpus_detected` | **Strong** — strict `[]`. |

## TestMigrateSchema

| Line | Test | Verdict |
|---|---|---|
| 350 | `test_noop_when_already_at_current_version` | **Strong** — pins `gpu_threads == 4` preserved (no migration ran). |
| 359 | `test_refuses_when_disk_schema_is_newer_than_binary` | **Strong** — pins exception type AND message ("Refusing to start" + ".bak"). |
| 378 | `test_builds_gpu_config_from_flat_gpu_threads` | **Strong** — pins per-GPU names, all enabled, total workers == 3, ffmpeg_threads, stale keys removed, schema bumped. |
| 407 | `test_removes_stale_keys_without_gpu_config` | **Strong** — pins all 3 invariants (no flat keys, empty config, schema bumped). |
| 425 | `test_preserves_existing_gpu_config` | **Strong** — pins existing-config preservation + flat-key cleanup. |
| 449 | `test_no_flat_keys_noop` | **Strong** — pins `[]` + bumped schema. |
| 458 | `test_idempotent` | **Strong** — runs twice, pins equality. |

## TestMigrateSchemaEdgeCases

| Line | Test | Verdict |
|---|---|---|
| 480 | `test_invalid_gpu_threads_value_handled` | **Strong** — pins None + schema bumped. |
| 490 | `test_invalid_ffmpeg_threads_value_handled` | **Strong** — same for ffmpeg_threads. |
| 500 | `test_gpu_config_empty_list_not_overwritten` | **Strong** — pins `[]` preserved (intentional clear). |
| 510 | `test_gpu_threads_zero_skips_config_build` | **Strong** — pins zero-special case. |

## TestBuildGpuConfigFromEnvEdgeCases

| Line | Test | Verdict |
|---|---|---|
| 523 | `test_invalid_gpu_threads_env_uses_default` | **Strong** — pins default workers == 1. |
| 536 | `test_invalid_ffmpeg_threads_env_uses_default` | **Strong** — pins default ffmpeg_threads == 2. |
| 550 | `test_gpu_selection_out_of_range_indices` | **Strong** — pins enabled True for valid index. |
| 565 | `test_gpu_detection_exception_returns_empty` | **Strong** — pins `[]`. |
| 577 | `test_gpu_threads_zero_disables_all` | **Strong** — pins enabled=False AND workers=0. |
| 591 | `test_gpu_selection_non_numeric_falls_back_to_all` | **Strong** — pins all enabled. |
| 608 | `test_gpu_selection_no_matching_indices_falls_back` | **Strong** — pins enabled True + workers == 2. |
| 623 | `test_remainder_distributed_across_gpus` | **Strong** — pins specific distribution `[3, 2]` AND total. |
| 641 | `test_only_ffmpeg_threads_set_triggers_migration` | **Strong** — pins ffmpeg_threads + workers default. |

## TestBuildPathMappingsFromEnv

| Line | Test | Verdict |
|---|---|---|
| 662 | `test_returns_none_when_no_env_vars` | **Strong** — `is None`. |
| 670 | `test_builds_mappings` | **Strong** — pins length + plex_prefix + local_prefix + webhook_prefixes==[]. |
| 682 | `test_returns_none_when_only_one_var_set` | **Strong** — pins None when partial input. |
| 690 | `test_returns_none_when_get_path_mapping_pairs_raises` | **Strong** — pins exception graceful handling. |
| 703 | `test_returns_none_when_pairs_empty` | **Strong** — pins None on empty pairs. |

## TestMigrateToV2Extended

| Line | Test | Verdict |
|---|---|---|
| 720 | `test_gpu_detection_exception_still_removes_stale_keys` | **Strong** — pins None + None + notes substring. |
| 735 | `test_worker_remainder_distribution` | **Strong** — pins exact `[2, 2, 1]` distribution. |
| 756 | `test_gpu_name_fallback` | **Strong** — pins fallback name format. |

## TestMigrateToV4

| Line | Test | Verdict |
|---|---|---|
| 805 | `test_no_op_when_legacy_keys_absent` | **Strong** — `notes == []` + empty schedules. |
| 815 | `test_converts_enabled_scanner_to_schedule` | **Strong** — 8+ assertions pinning every schedule field + legacy cleanup. |
| 851 | `test_no_schedule_when_scanner_was_disabled` | **Strong** — pins zero schedules + legacy cleanup. |
| 872 | `test_creates_one_schedule_per_library_override` | **Strong** — pins `len == 2`, library IDs set, config fields. |

## TestMigrateToV6

| Line | Test | Verdict |
|---|---|---|
| 902 | `test_no_op_when_gpu_config_missing` | **Strong** — `== []`. |
| 907 | `test_no_op_when_no_stale_cuda_entry` | **Strong** — pins `notes == []` AND `len == 2` preserved. |
| 922 | `test_strips_legacy_cuda_entry` | **Strong** — pins notes substring + len == 1 + remaining device. |
| 941 | `test_leaves_indexed_cuda_entries_untouched` | **Strong** — pins exact device list `["cuda:0", "cuda:1"]`. |

## TestMigrateToV7

| Line | Test | Verdict |
|---|---|---|
| 966 | `test_fresh_install_writes_empty_array` | **Strong** — pins `[]` + notes substring. |
| 975 | `test_synthesises_single_plex_entry_from_legacy_settings` | **Strong** — 12 strict-equality field assertions. Comprehensive. |
| 1014 | `test_prefers_plex_library_ids_over_titles` | **Strong** — pins exact ID list `["1", "2"]` not titles. |
| 1032 | `test_no_op_when_media_servers_already_present` | **Strong** — pins `notes == []` + array unchanged. |
| 1044 | `test_legacy_plex_keys_remain_after_migration` | **Strong** — pins additive (legacy keys preserved). |
| 1060 | `test_run_migrations_includes_v7` | **Strong** — pins schema_version + len(servers) + url. |

## TestMigrateToV8

| Line | Test | Verdict |
|---|---|---|
| 1080 | `test_no_globals_no_op` | **Strong** — `notes == []`. |
| 1088 | `test_empty_media_servers_keeps_globals_at_top_level` | **Strong** — pins warning note + globals preserved. |
| 1104 | `test_multiple_servers_keeps_globals_with_warning` | **Strong** — pins "2 servers configured" note + globals untouched + per-server lists empty. |
| 1127 | `test_single_plex_server_inherits_globals` | **Strong** — pins move + top-level keys deleted. |
| 1150 | `test_single_non_plex_server_also_inherits` | **Strong** — Emby variant. |
| 1168 | `test_existing_per_server_rules_are_preserved_and_appended` | **Strong** — pins length == 2 (no overwrite). |
| 1192 | `test_pre_v6_legacy_keys_cleaned_up` | **Strong** — pins keys absent + note substring. |
| 1208 | `test_idempotent` | **Strong** — runs twice, pins length == 1 (no double-append). |

## TestMigrateToV9

| Line | Test | Verdict |
|---|---|---|
| 1239 | `test_no_op_when_no_servers` | **Strong** — `notes == []`. |
| 1245 | `test_dedupes_path_mappings_left_by_v7_v8_chain` | **Strong** — pins exact rows survive + length 3 + notes substring. |
| 1270 | `test_dedupe_treats_different_webhook_aliases_as_distinct` | **Strong** — pins length 2 (different webhook_prefixes are distinct). |
| 1289 | `test_dedupes_exclude_paths` | **Strong** — pins length 2 (one dup removed). |
| 1303 | `test_idempotent` | **Strong** — runs twice, `notes == []` + length unchanged. |

## TestMigrateToV10

| Line | Test | Verdict |
|---|---|---|
| 1323 | `test_no_op_when_no_servers` | **Strong** — `notes == []`. |
| 1329 | `test_rewrites_legacy_plex_url_to_incoming` | **Strong** — strict equality on URL + notes substring. |
| 1350 | `test_removes_per_server_webhook_secret` | **Strong** — pins absence + notes. |
| 1370 | `test_url_already_incoming_is_left_alone` | **Strong** — `notes == []` + URL unchanged. |
| 1390 | `test_idempotent` | **Strong** — second run `notes == []` + final state pinned. |

## TestLegacyPlexToMediaServer

| Line | Test | Verdict |
|---|---|---|
| 1418 | `test_returns_none_when_no_plex_configured` | **Strong** — `is None`. |
| 1423 | `test_handles_token_only_install` | **Strong** — pins token preserved + `url == ""`. |
| 1433 | `test_falls_back_to_selected_libraries_key` | **Strong** — pins library names from selected_libraries. |

## TestMigrateToV11

| Line | Test | Verdict |
|---|---|---|
| 1455 | `test_seeds_defaults_when_missing` | **Strong** — pins exact dict (enabled=True, ttl=60, max=2048). |
| 1467 | `test_idempotent_when_block_already_present` | **Strong** — pins user-customised values preserved. |

## Summary

- **~70 tests** total
- **Strong**: ~70
- **Weak / Bug-blind / Tautological / Bug-locking**: 0
- **Needs human**: 0

**File verdict: STRONG.** No changes needed. This is an exemplary test file — comprehensive matrix coverage of the full migration chain (env, v2 → v11), idempotency for every migration, and edge cases (empty input, exceptions, deprecated keys, pre-v6 vestigial keys).
