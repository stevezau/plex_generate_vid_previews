# Audit: tests/test_output_emby_sidecar.py — 9 tests, 4 classes

## TestNeedsServerMetadata

| Line | Test | Verdict |
|---|---|---|
| 26 | `test_returns_false` | **Strong** — pins `is False` (callers branch on this; if it flipped to True the dispatcher would needlessly fetch item metadata). |
| 32 | `test_name` | **Strong** — pins exact adapter name string `"emby_sidecar"` (used as a key in journal/metadata). |

## TestComputeOutputPaths

| Line | Test | Verdict |
|---|---|---|
| 37 | `test_default_naming` | **Strong** — strict equality on full sidecar path including `-320-10` suffix (Emby's expected naming). |
| 46 | `test_respects_width_and_interval` | **Strong** — strict equality with custom `-480-5` suffix; pins parameterization. |
| 55 | `test_handles_episode_paths_with_dashes` | **Strong** — strict equality on tricky episode path with internal dashes (`Show - S01E01 - Pilot`). Catches a regex/split regression that would mangle dashed names. |

## TestPublish

| Line | Test | Verdict |
|---|---|---|
| 66 | `test_writes_bif_with_emby_filename` | **Strong** — pins exact filename AND BIF magic bytes (full 8-byte signature). Real on-disk publish, real BIF. |
| 100 | `test_creates_missing_parent_dir` | **Strong** — pins parent-dir-creation behaviour by `out_path.exists()` after publish to a not-yet-existent subdir. |
| 123 | `test_empty_output_paths_raises` | **Strong** — pins `ValueError` on empty paths list (callers rely on this guard). |

## TestStaticHelpers

| Line | Test | Verdict |
|---|---|---|
| 131 | `test_sidecar_path` | **Strong** — strict equality on the static helper's output. Mirrors `test_default_naming` but for the public helper used by viewer/diagnostics; pins agreement between the helper and the adapter. |

## Summary

- **9 tests** — 9 Strong, 0 Weak / Bug-blind / Tautological
- Strict `==` everywhere on path outputs; magic-byte assertion on the actual BIF write.
- Coverage matrix: default + custom params + dashed episode paths + missing parent + empty list.

**File verdict: STRONG.** No changes needed.
