# Audit: tests/test_output_jellyfin_trickplay.py — 11 tests, 3 classes

## TestNeedsServerMetadata

| Line | Test | Verdict |
|---|---|---|
| 48 | `test_returns_true` | **Strong** — pins `is True` (mirror of Emby adapter; this one DOES need item_id from the API). |
| 51 | `test_name` | **Strong** — pins exact name `"jellyfin_trickplay"` (used as journal/dispatch key). |

## TestComputeOutputPaths

| Line | Test | Verdict |
|---|---|---|
| 56 | `test_sheet0_path_matches_jellyfin_pathmanager_formula` | **Strong** — strict equality on full sheet0 path matching Jellyfin's `PathManager.cs` formula `<media_dir>/<basename>.trickplay/<width> - <tileW>x<tileH>/0.jpg`. Inline comment cites the upstream source — contract pinned. |
| 73 | `test_respects_custom_width` | **Strong** — strict equality with `width=480` → `480 - 10x10/0.jpg`. Pins the parameterization. |
| 79 | `test_missing_item_id_raises` | **Strong** — `pytest.raises(ValueError, match="item_id")` pins both exception type AND message substring (which is meaningful here — surfaces the missing field). |
| 85 | `test_static_helpers_match_compute_output_paths` | **Strong** — strict equality on `trickplay_dir` and `sheet_dir` outputs. Critical: pins the helper-vs-adapter agreement that the BIF Viewer relies on. |

## TestPublish

| Line | Test | Verdict |
|---|---|---|
| 98 | `test_writes_one_sheet_for_under_100_frames` | **Strong** — pins (a) sheets dir exists, (b) exactly one file named `0.jpg`, (c) sheet image dimensions `(3200, 1800)` = 10×320 by 10×180 (10×10 grid even when only 15 frames available — Jellyfin contract), (d) NO manifest.json written (Jellyfin synthesises TrickplayInfo). All four are spec-mandated. |
| 130 | `test_writes_multiple_sheets_for_over_100_frames` | **Strong** — strict equality on filenames list `["0.jpg", "1.jpg", "2.jpg"]` for 250 frames (3 sheets, last partial). Pins the splitting math. |
| 150 | `test_creates_missing_trickplay_dir` | **Strong** — pre-asserts directory absent, post-asserts present (`is_dir()`). Pins parent creation. |
| 169 | `test_purges_stale_tiles_from_prior_run` | **Strong** — pre-creates stale `5.jpg`, then asserts only `["0.jpg"]` remains after publish. Pins the cleanup contract that the docstring says Jellyfin relies on (else `ThumbnailCount` includes stale). Catches a real-world bug class. |
| 196 | `test_empty_frame_dir_raises` | **Strong** — `pytest.raises(RuntimeError, match="No JPG frames")` — pins both type AND message. |
| 210 | `test_empty_output_paths_raises` | **Strong** — pins ValueError on empty list. |
| 216 | `test_resizes_frames_when_dimensions_differ` | **Strong** — pins that mixed-size frames still produce a uniform `(3200, 1800)` grid (tile size driven by first frame). Catches FFmpeg-quirk regression. |

## Summary

- **11 tests** — 11 Strong, 0 Weak / Bug-blind / Tautological
- Path computation pinned to upstream Jellyfin formula (with cited source ref).
- Publish behavior pinned at the *pixel-dimension level* — strongest possible assertion (catches both filename AND grid-math regressions).
- Stale-tile purge has a real test exercising the actual prior-run scenario.
- Helper-vs-adapter consistency pinned (Viewer integration contract).

**File verdict: STRONG.** No changes needed.
