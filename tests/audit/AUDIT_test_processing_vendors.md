# Audit: tests/test_processing_vendors.py — 8-method contract × 3 vendors + 7 standalone = 31 tests, 6 classes

## _ProcessorContractTests (mixin — instantiated 3× via TestEmbyProcessor / TestJellyfinProcessor / TestPlexProcessor)

Each row applies to all three concrete subclasses (Emby, Jellyfin, Plex) — that is the matrix-coverage shape recommended by `.claude/rules/testing.md`.

| Line | Test | Verdict |
|---|---|---|
| 67 | `test_registry_has_this_vendor` | **Strong** — `isinstance(get_processor_for(self.server_type), self.vendor)` pins the registry → concrete-class wiring per vendor. |
| 70 | `test_list_libraries_passes_through` | **Strong** — equality on returned list (incl. disabled-library passthrough). Mocks the MediaServer client, not the SUT. |
| 75 | `test_list_libraries_empty_on_failure` | **Strong** — pins the swallow-and-return-`[]` contract on RuntimeError (failure-mode contract). |
| 80 | `test_list_canonical_paths_yields_per_item_with_path_mapping` | **Strong** — five strict-equality assertions: count, `canonical_path`, `server_id`, `item_id_by_server` dict, `library_id`, AND a second-item path. Catches mapping arithmetic + ID propagation. |
| 97 | `test_list_canonical_paths_skips_disabled_libraries` | **Strong** — `assert == []` AND `mock_client.list_items.assert_not_called()` — pins both the output and the side-effect (no walk). Two-anchor check. |
| 105 | `test_list_canonical_paths_filters_by_library_ids` | **Strong** — captures `mock_client.list_items.call_args_list` and asserts `["lib-1"]` exactly (not `assert_called`). Catches a regression that walked all libs and only filtered the output. |
| 115 | `test_list_canonical_paths_honours_cancel_check` | **Strong (audit-fixed)** — asserts callback was consulted (`called["count"] >= 1`) AND walk produced `< len(_ITEMS)`. Comment block notes the audit history (was previously `<= 2`, meaningless). |
| 144 | `test_resolve_canonical_path_applies_mappings` | **Strong** — strict equality on the mapped path. |
| 153 | `test_resolve_canonical_path_returns_none_when_not_indexed` | **Strong** — pins the None-return for the not-yet-indexed contract. |

(That's 9 contract methods × 3 vendors = 27 tests, all Strong.)

## TestRegistryHasAllVendors

| Line | Test | Verdict |
|---|---|---|
| 178 | `test_three_vendors_registered_at_import` | **Strong** — sorted-list equality on registered ServerType values. Catches accidental drop OR addition of a vendor at module import. |

## TestEmbyishRecentlyAdded

| Line | Test | Verdict |
|---|---|---|
| 186 | `test_within_lookback_iso_with_z_suffix` | **Strong** — recent → True AND old → False. Two cells of the truth table. |
| 198 | `test_within_lookback_strips_subnano_precision` | **Strong (regression)** — pins .NET 7-digit fractional-second tolerance (Jellyfin/Emby quirk). A regression that re-introduces strict ISO parsing would fail. |
| 208 | `test_within_lookback_rejects_garbage` | **Strong** — both `"not a date"` and `""` → False. Catches the silent "always-true on parse error" failure mode. |
| 217 | `test_format_title_episode_and_movie` | **Strong** — strict equality on both formats (`"Show - S01E01 - Pilot"` and `"Cool Movie"`). Pins the user-facing title shape. |
| 230 | `test_scan_recently_added_filters_window_and_path_maps` | **Strong** — three assertions: `len == 1`, `canonical_path == "/l/recent.mkv"` (window filter + mapping), `title`, `item_id_by_server`. Comprehensively pins the scan contract. |

## TestPlexProcessorRecentlyAdded

| Line | Test | Verdict |
|---|---|---|
| 270 | `test_item_id_is_bare_ratingkey_not_url` | **Strong (D31 regression)** — explicitly asserts `item_id_by_server == {"srv-plex": "54321"}` — i.e., bare ratingKey, NOT the URL-form `/library/metadata/54321`. The docstring documents the doubled-prefix → 404 → silent-skip bug this prevents. Also includes a defensive `time.time()`-based addedAt to avoid host-tz drift. Excellent regression test. |

## Summary

- **31 tests** total (27 contract × 3 vendors + 1 registry + 5 embyish + 1 Plex regression) — all **Strong**
- 0 weak / bug-blind / tautological / dead / bug-locking / needs-human
- Matrix coverage: every contract method × every vendor
- D31 (URL-form item_id) regression explicitly pinned with comment trail
- Cancellation-callback test fixed in a previous audit pass

**File verdict: STRONG.** No changes needed. This is one of the better-engineered test files — the `_ProcessorContractTests` mixin pattern is exactly the matrix-coverage shape called out in `.claude/rules/testing.md`.
