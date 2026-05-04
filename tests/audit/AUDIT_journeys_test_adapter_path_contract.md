# Audit: tests/journeys/test_adapter_path_contract.py — 11 tests, 3 classes + 1 parametrized

## TestPlexBundleAdapterPathLayout

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 61 | `test_static_helper_produces_canonical_layout` | **Strong** — exact byte-string equality on full path; D38-style layout drift fails loudly |
| 76 | `test_compute_output_paths_uses_prefetched_hash_when_present` | **Strong** — exact full-path equality; pins hash split at index 1 |
| 104 | `test_compute_output_paths_raises_when_item_id_missing` | **Strong** — `pytest.raises(ValueError, match="item_id")` is precise |
| 114 | `test_compute_output_paths_raises_when_server_missing` | **Strong** — match="PlexServer" pins error context |
| 119 | `test_compute_output_paths_raises_when_server_wrong_type` | **Strong** — distinguishes TypeError vs ValueError |

## TestEmbySidecarAdapterPathLayout

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 141 | `test_basic_layout` | **Strong** — exact path equality with diagnostic message |
| 151 | `test_custom_width_and_interval_in_filename` | **Strong** — width+interval in filename pinned |
| 160 | `test_episode_path_with_subdirs` | **Strong** — sidecar lives next to source, exact path |
| 169 | `test_no_server_metadata_required` | **Strong** — `is False` strict check on protocol contract |

## TestJellyfinTrickplayAdapterPathLayout

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 199 | `test_trickplay_dir_uses_basename_dot_trickplay` | **Strong** — D38 fix pin: full path equality, dot before trickplay matters |
| 210 | `test_sheet_dir_uses_width_space_dash_space_tilesxtiles` | **Strong** — D38 spaces-around-dash pin, exact equality |
| 221 | `test_compute_output_paths_returns_sheet_zero_jpg` | **Strong** — sheet 0 path freshness proxy pinned exactly |
| 234 | `test_custom_width_propagates` | **Strong** — width affects sheet dir not trickplay dir |
| 243 | `test_compute_output_paths_raises_when_item_id_missing` | **Strong** — ValueError match="item_id" |

## Parametrized

| Line | Test | Verdict |
|---|---|---|
| 280 | `test_pure_adapter_path_matrix` | **Strong** — exact equality across emby + jellyfin variants; cross-adapter regression guard |

## Summary

- **15 tests** (5+4+5+1) all **Strong**
- Every assertion uses exact byte-string equality with diagnostic messages
- Per-vendor path layout pinned for the D38 incident class

**File verdict: STRONG.** No changes needed.
