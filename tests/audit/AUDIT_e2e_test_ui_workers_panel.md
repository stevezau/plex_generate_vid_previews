# Audit: tests/e2e/test_ui_workers_panel.py — 5 tests, 2 classes

## TestWorkersPanelInPlaceUpdate

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 42 | `test_re_render_with_same_workers_preserves_card_node_identity` | **Strong** — sentinel-attribute survives re-render. `survivingCount == initialCount == 2`. Tests the in-place patch contract (commits e46e73c, 75c8da8). |
| 111 | `test_vanished_worker_card_is_removed_by_key` | **Strong** — beforeCount=3, afterCount=2, `hasB is False`. Three assertions covering the vanished-worker cleanup contract. |

## TestWorkerCardPhaseRendering

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 169 | `test_pre_ffmpeg_phase_renders_phase_text_not_zero_percent` | **Strong** — `percentText == "Resolving item id on Jellyfin…"` (exact equality), speedDisplay == "none". Closes 933a26d / 58829b2 (worker showed "0.0%" during pre-FFmpeg). |
| 209 | `test_ffmpeg_started_phase_shows_percent_and_speed_normally` | **Strong** — mirror test floor: `percentText == "42.5%"`, `speedText == "5.2x"`, `speedDisplay != "none"`. Catches always-show-phase regression. |
| 253 | `test_fallback_active_renders_cpu_fallback_badge` | **Strong** — badgeHidden is False, noteHidden is False, noteText contains "HEVC". Diagnostic substring used appropriately. |

## Summary

- **5 tests** all **Strong**
- Tests the contract floor (mirror tests) for every phase-rendering branch
- Real Playwright + JS evaluate driving production functions
- Strict-equality on rendered text where possible

**File verdict: STRONG.** No changes needed.
