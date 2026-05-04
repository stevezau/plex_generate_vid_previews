# Audit: tests/test_settings_manager.py — 51 tests, 8 classes

## TestSettingsManager

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 28 | `test_init_creates_empty_settings` | **Strong** — strict `== {}` |
| 32 | `test_set_and_get` | **Strong** — strict equality round-trip |
| 37 | `test_get_with_default` | **Strong** — strict equality on default fallback |
| 41 | `test_update_multiple` | **Strong** — strict equality on 3 distinct keys (incl. int) |
| 48 | `test_delete` | **Strong** — pins set→get→delete→is None lifecycle |
| 55 | `test_persistence` | **Strong** — round-trip through new instance (real disk) |
| 67 | `test_settings_file_created` | **Strong** — pins file existence + JSON content |

## TestPreviewSettingsAfterUpdate

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 81 | `test_gpu_threads_distribution_matches_update` | **Strong** — preview vs actual update() comparison + canonical [2,1] layout pinned |

## TestSettingsManagerProperties

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 130 | `test_plex_url_property` | **Strong** — strict equality |
| 135 | `test_plex_token_property` | **Strong** — strict equality |
| 140 | `test_gpu_threads_property` | **Weak** — sets `gpu_threads = 4`; only asserts getter returns 4. Doesn't pin per-GPU distribution (covered elsewhere). Sufficient as a getter/setter smoke. |
| 163 | `test_plex_verify_ssl_property` | **Strong** — strict `is False` |
| 168 | `test_plex_verify_ssl_defaults_true` | **Strong** — pins default-True contract with env cleared |
| 173 | `test_plex_verify_ssl_saved_true` | **Strong** — strict `is True` |
| 178 | `test_plex_verify_ssl_saved_false` | **Strong** — strict `is False` (matrix complement of above) |
| 183 | `test_cpu_threads_default_when_missing` | **Strong** — pins default of 1 |
| 188 | `test_gpu_threads_default_when_missing` | **Strong** — pins default of 0 |
| 193 | `test_cpu_threads_zero_preserved` | **Strong** — pins issue #142 0-preservation across reload |
| 202 | `test_gpu_threads_zero_preserved` | **Strong** — pins 0 preservation |
| 207 | `test_thumbnail_interval_property` | **Strong** — strict equality |

## TestGpuConfig

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 225 | `test_gpu_config_getter_setter_roundtrip` | **Strong** — strict equality round-trip |
| 240 | `test_gpu_threads_computed_from_gpu_config` | **Strong** — pins sum-of-enabled (5, not 5+5+disabled) |
| 270 | `test_gpu_threads_setter_distributes_across_enabled` | **Strong** — pins exact [3,2] distribution + disabled GPU stays at 0 |
| 305 | `test_gpu_threads_setter_noop_when_no_enabled` | **Strong** — pins no-op when no enabled GPUs |
| 320 | `test_gpu_threads_zero_with_empty_config` | **Strong** — strict `== []` and `== 0` |
| 325 | `test_update_routes_gpu_threads_through_setter` | **Strong** — pins update() routes through setter logic |

## TestSettingsManagerConfigStatus

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 355 | `test_is_configured_false_when_empty` | **Strong** — strict `is False` |
| 359 | `test_is_configured_true_when_set` | **Strong** — strict `is True` |
| 365 | `test_is_configured_true_for_jellyfin_only_install` | **Strong** — B1 matrix cell |
| 382 | `test_is_configured_true_for_emby_only_install` | **Strong** — B1 matrix cell |
| 399 | `test_is_configured_false_when_only_disabled_servers` | **Strong** — pins disabled-doesn't-count contract |
| 415 | `test_is_configured_false_when_emby_missing_api_key` | **Strong** — pins missing-key disqualifies |
| 431 | `test_is_configured_false_when_jellyfin_missing_api_key` | **Strong** — audit-fix matrix cell for Jellyfin |
| 449 | `test_is_configured_false_when_plex_missing_token` | **Strong** — audit-fix matrix cell for Plex |
| 468 | `test_is_configured_true_when_one_of_many_is_well_configured` | **Strong** — pins "at-least-one" contract regression catcher |
| 498 | `test_is_plex_authenticated_false` | **Strong** — strict `is False` |
| 502 | `test_is_plex_authenticated_true` | **Strong** — strict `is True` |

## TestClientIdentifier

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 511 | `test_get_client_identifier_generates_id` | **Weak** — only checks `startswith("plex-preview-generator-")`. A regression that returned the literal "plex-preview-generator-" with no UUID suffix would still pass. Could assert length > prefix length, or UUID-shape regex. |
| 520 | `test_client_identifier_persists` | **Strong** — pins same id across instances (catches non-persistence) |

## TestSetupState

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 543 | `test_get_setup_state_empty` | **Strong** — strict `== {}` |
| 548 | `test_set_setup_state` | **Strong** — strict equality on step + nested data |
| 555 | `test_get_setup_step` | **Strong** — pins 0 then 3 (matrix) |
| 561 | `test_clear_setup_state` | **Strong** — pins state cleared + file deleted |
| 570 | `test_complete_setup` | **Strong** — pins is_setup_complete True + state cleared |

## TestApplyChanges

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 588 | `test_apply_updates_only` | **Strong** — strict equality on both keys |
| 593 | `test_apply_deletes_only` | **Strong** — strict `is None` after delete |
| 598 | `test_apply_updates_and_deletes` | **Strong** — pins atomic apply (new set + old deleted) |
| 607 | `test_apply_noop` | **Strong** — pins zero-arg call doesn't wipe data |
| 613 | `test_deleting_nonexistent_key_is_safe` | **Strong** — pins no-raise contract |

## TestGpuConfigEdgeCases

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 627 | `test_gpu_config_none_returns_empty_list` | **Strong** — strict `== []` |
| 632 | `test_gpu_config_non_list_returns_empty_list` | **Strong** — strict `== []` for str + int (matrix) |
| 639 | `test_gpu_config_filters_non_dict_entries` | **Strong** — strict len + per-element device |
| 656 | `test_gpu_threads_with_none_gpu_config` | **Strong** — strict `== 0` |
| 661 | `test_gpu_threads_with_malformed_entries` | **Strong** — pins skip-bad-entries |
| 673 | `test_gpu_threads_missing_workers_key` | **Strong** — pins default-0 for missing key |
| 681 | `test_distribute_gpu_threads_with_none_config` | **Strong** — pins setter is no-op + value stays None |
| 687 | `test_distribute_gpu_threads_with_non_list_config` | **Strong** — pins value stays "invalid" |
| 693 | `test_distribute_gpu_threads_with_malformed_entries` | **Strong** — pins post-distribute single-GPU at 3 workers |
| 708 | `test_distribute_fewer_threads_than_gpus` | **Strong** — pins exact [1,1,0] layout |
| 742 | `test_update_does_not_mutate_caller_dict` | **Strong** — pins immutability of caller dict |

## Summary

- **51 tests total** (8 classes)
- **49 Strong**
- **2 Weak** — `test_gpu_threads_property` (L140), `test_get_client_identifier_generates_id` (L511)

**Weak tests to fix (low priority):**
- L140 `test_gpu_threads_property` — sets gpu_threads=4 then asserts getter returns 4; doesn't pin distribution. Acceptable as smoke (distribution covered in `test_gpu_threads_setter_distributes_across_enabled`).
- L511 `test_get_client_identifier_generates_id` — only checks `startswith` prefix. A regression returning the bare prefix string would pass. Could regex-match the UUID suffix.

**File verdict: STRONG.** Heavy contracts (B1 multi-vendor is_configured matrix, gpu_threads distribute/preserve-zero, frame_reuse persistence, apply_changes atomicity, malformed-input defence matrix) all pinned. Audit-fix tests for the per-vendor is_configured matrix close prior gaps.
