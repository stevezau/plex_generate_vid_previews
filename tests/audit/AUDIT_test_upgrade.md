# Audit: tests/test_upgrade.py — ~73 tests (re-audit, batch 6)

Tests env-var migration + schema-version migrations (v2 → v11) + frame_reuse seeding + last_seen_version sentinel + media-server synthesis + multi-window quiet hours.

## TestRunMigrations

| Line | Test | Verdict | Note |
|---|---|---|---|
| 32 | `test_calls_env_and_schema_migrations` | Strong | Pins env_migrated, schema_version, plex_url written, media_servers synthesised AND frame_reuse seeded. End-to-end. |

## TestSeedLastSeenVersionForUpgraders

| Line | Test | Verdict | Note |
|---|---|---|---|
| 72 | `test_upgrader_with_plex_url_gets_sentinel` | Strong | Pins `last_seen_version == "0.0.0"` post-migration, asserts None pre-migration. |
| 85 | `test_upgrader_with_only_setup_complete_gets_sentinel` | Strong | Pins sentinel value. |
| 97 | `test_existing_last_seen_version_preserved` | Strong | Pins `"3.7.4"` preserved. |
| 108 | `test_fresh_install_not_seeded` | Strong | Pins `is None` (no spurious seeding). |
| 122 | `test_idempotent_across_repeat_runs` | Strong | Pins value persists across two run_migrations calls + advances respect user updates. |
| 137 | `test_upgrader_with_media_servers_only_gets_sentinel` | Strong | Pins sentinel via media_servers signal. |

## TestEnvVarMigration

| Line | Test | Verdict | Note |
|---|---|---|---|
| 156 | `test_migrates_plex_url` | Strong | Pins exact URL. |
| 163 | `test_migrates_int_values` | Strong | Pins int 4. |
| 170 | `test_migrates_bool_values` | Strong | Pins `is False`. |
| 177 | `test_skips_existing_keys` | Strong | Pins precedence (existing wins). |
| 185 | `test_runs_only_once` | Strong | Pins second-run noop. |
| 196 | `test_sets_env_migrated_flag` | Strong | Pins `is True`. |
| 202 | `test_migrates_libraries` | Strong | Pins exact `["Movies", "TV Shows"]` list. |

## TestEnvVarMigrationExtended

| Line | Test | Verdict | Note |
|---|---|---|---|
| 213 | `test_invalid_int_env_var_logged` | Strong | Pins None AND env_migrated flag set. |
| 222 | `test_gpu_config_migrated_from_env` | Strong | Pins config[0].workers == 2. |
| 238 | `test_path_mappings_migrated_from_env` | Strong | Pins plex_prefix value. |
| 250 | `test_deprecated_env_var_does_not_crash` | Strong | Pins deprecated keys NOT persisted (negative assertions). |

## TestBuildGpuConfigFromEnv

| Line | Test | Verdict | Note |
|---|---|---|---|
| 276 | `test_returns_none_when_no_env_vars` | Strong | Pins None. |
| 285 | `test_builds_config_with_gpu_threads` | Strong | Pins len, all enabled, total workers == 2. |
| 304 | `test_gpu_selection_disables_unselected` | Strong | Pins per-GPU enabled flags. |
| 322 | `test_ffmpeg_threads_propagated` | Strong | Pins int 4. |
| 335 | `test_returns_empty_when_no_gpus_detected` | Strong | Pins `== []`. |

## TestMigrateSchema

| Line | Test | Verdict | Note |
|---|---|---|---|
| 350 | `test_noop_when_already_at_current_version` | Strong | Pins `gpu_threads == 4` preserved. |
| 359 | `test_refuses_when_disk_schema_is_newer_than_binary` | Strong | Pins SchemaDowngradeError + message contains `"Refusing to start"` and `".bak"`. |
| 378 | `test_builds_gpu_config_from_flat_gpu_threads` | Strong | Pins per-GPU config (name, workers sum, ffmpeg_threads, stale keys removed, schema bumped). |
| 407 | `test_removes_stale_keys_without_gpu_config` | Strong | Pins None for both flat keys + empty gpu_config + schema bumped. |
| 425 | `test_preserves_existing_gpu_config` | Strong | Pins workers == 2 preserved. |
| 449 | `test_no_flat_keys_noop` | Strong | Pins `gpu_config == []` and schema bumped. |
| 458 | `test_idempotent` | Strong | Pins config equality across two migration runs. |

## TestMigrateSchemaEdgeCases

| Line | Test | Verdict | Note |
|---|---|---|---|
| 480 | `test_invalid_gpu_threads_value_handled` | Strong | Pins None and schema bumped. |
| 490 | `test_invalid_ffmpeg_threads_value_handled` | Strong | Pins None and schema bumped. |
| 500 | `test_gpu_config_empty_list_not_overwritten` | Strong | Pins `[]` preserved AND `gpu_threads` cleared. |
| 510 | `test_gpu_threads_zero_skips_config_build` | Strong | Pins `[]` AND `gpu_threads` cleared. |

## TestBuildGpuConfigFromEnvEdgeCases

| Line | Test | Verdict | Note |
|---|---|---|---|
| 523 | `test_invalid_gpu_threads_env_uses_default` | Strong | Pins workers == 1. |
| 536 | `test_invalid_ffmpeg_threads_env_uses_default` | Strong | Pins ffmpeg_threads == 2. |
| 550 | `test_gpu_selection_out_of_range_indices` | Strong | Pins `enabled is True` (existing index wins despite out-of-range). |
| 565 | `test_gpu_detection_exception_returns_empty` | Strong | Pins `== []`. |
| 577 | `test_gpu_threads_zero_disables_all` | Strong | Pins enabled=False AND workers=0. |
| 591 | `test_gpu_selection_non_numeric_falls_back_to_all` | Strong | Pins all enabled. |
| 608 | `test_gpu_selection_no_matching_indices_falls_back` | Strong | Pins enabled True AND workers == 2. |
| 623 | `test_remainder_distributed_across_gpus` | Strong | Pins exact `[3, 2]` distribution. |
| 641 | `test_only_ffmpeg_threads_set_triggers_migration` | Strong | Pins ffmpeg_threads + workers fallback. |

## TestBuildPathMappingsFromEnv

| Line | Test | Verdict | Note |
|---|---|---|---|
| 662 | `test_returns_none_when_no_env_vars` | Strong | Pins None. |
| 670 | `test_builds_mappings` | Strong | Pins all 3 fields exactly. |
| 682 | `test_returns_none_when_only_one_var_set` | Strong | Pins None. |
| 690 | `test_returns_none_when_get_path_mapping_pairs_raises` | Strong | Pins None on RuntimeError. |
| 703 | `test_returns_none_when_pairs_empty` | Strong | Pins None on empty list. |

## TestMigrateToV2Extended

| Line | Test | Verdict | Note |
|---|---|---|---|
| 720 | `test_gpu_detection_exception_still_removes_stale_keys` | Strong | Pins None for both keys + notes contain `"removed stale keys"`. |
| 735 | `test_worker_remainder_distribution` | Strong | Pins exact `[2, 2, 1]`. |
| 756 | `test_gpu_name_fallback` | Strong | Pins exact name `"vaapi GPU"`. |

## TestMigrateToV4

| Line | Test | Verdict | Note |
|---|---|---|---|
| 805 | `test_no_op_when_legacy_keys_absent` | Strong | Pins notes == [] AND no schedules created. |
| 815 | `test_converts_enabled_scanner_to_schedule` | Strong | Pins schedule fields (name, trigger_type, trigger_value, library_id, config.job_type, config.lookback_hours) + legacy keys removed + note phrasing. |
| 851 | `test_no_schedule_when_scanner_was_disabled` | Strong | Pins no schedules + legacy keys removed. |
| 872 | `test_creates_one_schedule_per_library_override` | Strong | Pins len == 2 + per-schedule fields + library_ids set. |

## TestMigrateToV6

| Line | Test | Verdict | Note |
|---|---|---|---|
| 902 | `test_no_op_when_gpu_config_missing` | Strong | Pins `notes == []`. |
| 907 | `test_no_op_when_no_stale_cuda_entry` | Strong | Pins notes == [] + len == 2 preserved. |
| 922 | `test_strips_legacy_cuda_entry` | Strong | Pins note count + remaining device == "/dev/dri/renderD128". |
| 941 | `test_leaves_indexed_cuda_entries_untouched` | Strong | Pins device list `["cuda:0", "cuda:1"]`. |

## TestMigrateToV7

| Line | Test | Verdict | Note |
|---|---|---|---|
| 966 | `test_fresh_install_writes_empty_array` | Strong | Pins `media_servers == []` + note phrasing. |
| 975 | `test_synthesises_single_plex_entry_from_legacy_settings` | Strong | Pins all 12 fields of synthesised entry (id, type, enabled, url, verify_ssl, timeout, auth, output.adapter/folder/frame_interval, path_mappings, libraries). Exemplary multi-field pin. |
| 1014 | `test_prefers_plex_library_ids_over_titles` | Strong | Pins `ids == ["1", "2"]`. |
| 1032 | `test_no_op_when_media_servers_already_present` | Strong | Pins notes == [] + array unchanged. |
| 1044 | `test_legacy_plex_keys_remain_after_migration` | Strong | Pins legacy keys preserved (additive migration). |
| 1060 | `test_run_migrations_includes_v7` | Strong | End-to-end: pins schema_version >= 7 + media_servers populated. |

## TestMigrateToV8

| Line | Test | Verdict | Note |
|---|---|---|---|
| 1080 | `test_no_globals_no_op` | Strong | Pins `notes == []`. |
| 1088 | `test_empty_media_servers_keeps_globals_at_top_level` | Strong | Pins note substring + globals preserved. |
| 1104 | `test_multiple_servers_keeps_globals_with_warning` | Strong | Pins note substring + globals preserved + per-server lists empty. |
| 1127 | `test_single_plex_server_inherits_globals` | Strong | Pins server gains both lists, top-level keys deleted. |
| 1150 | `test_single_non_plex_server_also_inherits` | Strong | Same as above for Emby. |
| 1168 | `test_existing_per_server_rules_are_preserved_and_appended` | Weak | Only pins `len == 2` for each list — does not verify ORDER (existing-first vs new-first) or content. **Why downgraded:** a regression that overwrote instead of appending could still produce len == 2 if both lists already had 1 each, and a regression that swapped ordering would silently pass. |
| 1192 | `test_pre_v6_legacy_keys_cleaned_up` | Strong | Pins absence of both keys + note contains "pre-v6". |
| 1208 | `test_idempotent` | Strong | Pins len == 1 for both lists after two runs (no double-append). |

## TestMigrateToV9

| Line | Test | Verdict | Note |
|---|---|---|---|
| 1239 | `test_no_op_when_no_servers` | Strong | Pins `notes == []`. |
| 1245 | `test_dedupes_path_mappings_left_by_v7_v8_chain` | Strong | Pins exact rows (preserves order) + note count substring. |
| 1270 | `test_dedupe_treats_different_webhook_aliases_as_distinct` | Strong | Pins `len == 2` (both kept). |
| 1289 | `test_dedupes_exclude_paths` | Strong | Pins `len == 2`. |
| 1303 | `test_idempotent` | Strong | Pins second-run notes == [] + len preserved. |

## TestMigrateToV10

| Line | Test | Verdict | Note |
|---|---|---|---|
| 1323 | `test_no_op_when_no_servers` | Strong | Pins `notes == []`. |
| 1329 | `test_rewrites_legacy_plex_url_to_incoming` | Strong | Pins exact rewritten URL + note substring. |
| 1350 | `test_removes_per_server_webhook_secret` | Strong | Pins absence + note substring. |
| 1370 | `test_url_already_incoming_is_left_alone` | Strong | Pins `notes == []` + URL unchanged. |
| 1390 | `test_idempotent` | Strong | Pins second-run notes == [] + URL rewritten + secret removed. |

## TestLegacyPlexToMediaServer

| Line | Test | Verdict | Note |
|---|---|---|---|
| 1418 | `test_returns_none_when_no_plex_configured` | Strong | Pins None. |
| 1423 | `test_handles_token_only_install` | Strong | Pins token in entry + url == "". |
| 1433 | `test_falls_back_to_selected_libraries_key` | Strong | Pins `["Anime"]` derived from selected_libraries fallback. |

## TestMigrateToV11

| Line | Test | Verdict | Note |
|---|---|---|---|
| 1455 | `test_seeds_defaults_when_missing` | Strong | Pins exact dict shape with all 3 fields. |
| 1467 | `test_idempotent_when_block_already_present` | Strong | Pins user-customised values preserved + notes == []. |

**File verdict: STRONG (1 weak append-ordering test).** Holds up to adversarial re-review — schema-migration tests pin field-by-field, idempotency is checked everywhere, and edge cases (out-of-range, exception, empty, zero) are matrix-covered. Only weak spot is the v8 append-vs-overwrite test which doesn't pin ordering.

## Fix queue

- **L1168 `test_existing_per_server_rules_are_preserved_and_appended`** — pin the actual contents AND ordering of the merged lists. Suggest: assert `servers[0]["path_mappings"] == [{old_row}, {new_row}]` (or the documented append order) so a regression that overwrote (length stays 2 if both already had 1) or swapped ordering would fail loudly.
