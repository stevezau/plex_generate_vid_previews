# Audit: tests/test_output_plex_bundle.py — 14 tests, 4 classes

## TestNeedsServerMetadata

| Line | Test | Verdict |
|---|---|---|
| 67 | `test_returns_true` | **Strong (defensive)** — pins `is True` on the metadata-required flag. Catches a refactor that flips it (pipeline would skip the metadata step). |
| 71 | `test_name` | **Strong** — strict equality on `"plex_bundle"`. The dispatcher routes by name; a rename would break the routing. |

## TestComputeOutputPaths

| Line | Test | Verdict |
|---|---|---|
| 77 | `test_path_structure_matches_plex_bundle_layout` | **Strong** — strict `Path` equality on the full bundle layout (`/cfg/Media/localhost/a/bcdef.../Indexes/index-sd.bif`) AND asserts the URL captured was `/library/metadata/42/tree`. Two-anchor: filesystem layout + the URL string. |
| 93 | `test_picks_matching_part_by_basename` | **Strong** — multi-part item: must pick the SECOND hash (matching basename `disc2.mkv`), not the first. Catches an `items[0]`-pick regression. |
| 113 | `test_falls_back_to_first_hash_when_no_basename_match` | **Strong** — pins the fallback contract when no basename matches (`zzz...`). Asserts the bif filename + the bundle hash are both right. |
| 126 | `test_empty_metadata_raises_not_yet_indexed` | **Strong** — pins `LibraryNotYetIndexedError` on empty MediaPart list (load-bearing for the slow-backoff queue). |
| 139 | `test_invalid_hash_for_matching_part_raises_not_yet_indexed` | **Strong** — pins the same exception when hash is too short (length≥2 guard). Catches a regression that lowered the guard. |
| 154 | `test_missing_item_id_raises_value_error` | **Strong** — `pytest.raises(ValueError, match="item_id")` — pins both error class AND message substring. |
| 164 | `test_non_plex_server_raises_type_error` | **Strong** — `pytest.raises(TypeError, match="PlexServer")` — pins the type-guard message. |
| 173 | `test_does_not_double_prefix_url_when_item_id_is_full_path` | **Strong (D31 regression — flagship)** — asserts `captured_urls[0] == "/library/metadata/557676/tree"` AND `"//library/metadata" not in captured_urls[0]` AND a positive bundle-path equality. The docstring explicitly documents that this is the test that would have caught D31, the production webhook bug that hid for 3 days. End-to-end with only `plex.query` mocked. |
| 229 | `test_handles_bare_rating_key_input` | **Strong** — pairs with above: bare ratingKey input → SAME URL. Confirms both shapes converge on one URL. |

## TestPrefetchedBundleMetadata

| Line | Test | Verdict |
|---|---|---|
| 272 | `test_prefetched_metadata_skips_tree_call` | **Strong** — wires `plex.query.side_effect` to AssertionError, asserts the bundle path is computed without invoking it, AND asserts `plex.query.assert_not_called()`. Two-anchor: positive output + zero calls. Pins the 9981×-roundtrip optimization. |
| 293 | `test_falls_back_to_tree_when_no_prefetch` | **Strong** — mirror cell: empty prefetch → /tree IS called. Asserts URL captured AND bundle hash propagated. |
| 305 | `test_prefetched_picks_matching_part_by_basename` | **Strong** — multi-part on the prefetch path: still picks basename-matching hash. Mirrors line 93 on the prefetch branch. Asserts `"bbbbbbbbb.bundle" in str(paths[0])` with explicit failure message. |

## TestPublish

| Line | Test | Verdict |
|---|---|---|
| 329 | `test_creates_parent_dirs_and_writes_bif` | **Strong** — writes 3 fake JPGs, calls `publish()`, asserts the deeply-nested output path EXISTS, AND asserts the BIF magic bytes (8-byte hex) match the spec. Two-anchor: filesystem side-effect + file format validity. |
| 358 | `test_empty_output_paths_raises` | **Strong** — pins `ValueError` on `publish(bundle, [])`. |

## Summary

- **15 tests** — all **Strong**
- 0 weak / bug-blind / tautological / dead / bug-locking / needs-human
- D31 flagship regression test (line 173) explicitly mocks ONLY at `plex.query` boundary — the comment block names the production bug and the hiding mechanism
- Prefetch optimization vs /tree fallback matrix complete (3 cells: skip-tree, fallback-to-tree, multi-part-on-prefetch)
- Publish covers both happy path (BIF magic verified) and error path (empty paths)

**File verdict: STRONG.** No changes needed. The mocking philosophy comment (lines 10-15) and the D31 regression comment (lines 174-187) are gold-standard test documentation.
