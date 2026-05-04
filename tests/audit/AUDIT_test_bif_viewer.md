# Audit: tests/test_bif_viewer.py — 32 tests, 11 classes

## TestReadBifMetadata

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 64 | `test_valid_bif` | **Strong** | 6 strict-equality pins on parsed metadata: version, frame_count, frame_interval_ms, frame_offsets length, frame_sizes length AND content (`all(s == 53 ...)`), file_size > 0. |
| 76 | `test_single_frame` | **Strong** | Strict count == 1 AND `frame_sizes == [4]`. |
| 83 | `test_file_not_found` | **Strong** | `pytest.raises(FileNotFoundError)`. |
| 87 | `test_bad_magic` | **Strong** | `pytest.raises(ValueError, match="bad magic")` — pins error message. |
| 93 | `test_truncated_header_raises` | **Strong** | Documented threat model: silently-returns-garbage regression would fool viewer. Accepts `(ValueError, struct.error)` — both are loud failures. |
| 110 | `test_missing_sentinel_in_index_table_raises` | **Strong** | `match="sentinel"` on the error message — pins the corrupt-BIF detection contract. |
| 138 | `test_reference_bif` | **Weak** | Only `frame_count > 0` and `frame_interval_ms > 0`. A regression returning the constant `1` would pass. **However** this is a fixture-validation smoke test (with skip-if-missing) — its purpose is "the parser doesn't blow up on the real file", which is acceptable. Marginal but defensible. |

## TestReadBifFrame

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 150 | `test_extract_each_frame` | **Strong** | Loops every frame and asserts byte equality. Strongest possible. |
| 156 | `test_without_preloaded_metadata` | **Strong** | Byte equality on first frame when metadata not preloaded. |
| 160 | `test_index_out_of_range` | **Strong** | `pytest.raises(IndexError)`. |
| 165 | `test_negative_index` | **Strong** | `pytest.raises(IndexError)` — pins distinct branch from out-of-range. |

## TestUnpackBifToJpegs

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 177 | `test_writes_one_jpeg_per_frame` | **Strong** | Strict count == 5, strict filename list with assertion message documenting the 1-indexed zero-padded contract that downstream FFmpeg consumer requires, AND byte equality round-trip on first + last frames. D34 contract pinned. |
| 196 | `test_returns_zero_when_bif_empty` | **Strong** | Strict count == 0 AND empty listdir. |

## TestBifViewerPage

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 292 | `test_requires_auth` | **Strong** | Pins `status_code == 302` (documented: catches a flip to 308) AND `/login` substring in Location header. |
| 300 | `test_renders_when_authenticated` | **Strong** | 200 + `b"Preview Inspector"` substring (page-content marker). |

## TestBifInfoEndpoint

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 312 | `test_requires_auth` | **Strong** | 401 without auth. |
| 316 | `test_returns_metadata` | **Strong** | 5 assertions on JSON payload: `frame_count == 5`, `frame_interval_ms == 2000`, `file_size > 0`, AND existence of `created_at` and `avg_frame_size` keys. |
| 326 | `test_invalid_path_rejected` | **Strong** | 400 on `/etc/passwd` — security boundary. |
| 330 | `test_missing_path` | **Strong** | 400 on missing query param. |
| 334 | `test_suspect_frames_detected` | **Strong** | Strict count `suspect_frame_count == 3`. |
| 346 | `test_allow_list_accepts_legacy_plex_prefix` | **Strong** | End-to-end test pinning legacy `plex_prefix`-shaped path_mappings still resolve through allow-list. Critical compat path documented. |

## TestBifFrameEndpoint

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 394 | `test_requires_auth` | **Strong** | 401. |
| 398 | `test_returns_jpeg` | **Strong** | 200 + content_type pin + byte equality on returned frame. |
| 404 | `test_second_frame` | **Strong** | Byte equality on second frame — distinct from first; rules out off-by-one. |
| 409 | `test_out_of_range` | **Strong** | 400 on out-of-range index. |
| 413 | `test_invalid_index` | **Strong** | 400 on non-numeric index. |

## TestParseSeasonEpisode

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 426 | `test_full_season_episode` | **Strong** | Strict equality on all 3 returns: base, season, ep. |
| 434 | `test_season_only` | **Strong** | Strict equality including `ep is None`. |
| 442 | `test_no_pattern` | **Strong** | Strict equality including both season+ep == None. |
| 450 | `test_case_insensitive` | **Strong** | Strict equality on lowercase `s02e10` parsing. |
| 458 | `test_single_digit` | **Strong** | Strict equality on `S1E3` form. |
| 466 | `test_pattern_only_returns_original_query` | **Strong** | Strict equality — base equals original query when nothing precedes the pattern. |

## TestBuildDisplayTitle

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 478 | `test_episode` | **Strong** | Strict equality on full formatted episode string. |
| 490 | `test_movie_with_year` | **Strong** | Strict equality `"Inception (2010)"`. |
| 496 | `test_movie_without_year` | **Strong** | Strict equality `"Memento"` — pins empty-year drop. |

## TestBifSearchEndpoint

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 504 | `test_requires_auth` | **Strong** | 401. |
| 508 | `test_short_query_rejected` | **Strong** | 400 on 1-char query. |
| 512 | `test_no_plex_configured` | **Strong** | 400 + substring match `"Plex not configured"`. |

## TestMultiServerBifSearch

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 522 | `test_unknown_server_returns_404` | **Strong** | 404 distinct from 400. |
| 526 | `test_short_query_rejected` | **Strong** | 400 on short query. |

## TestMultiServerTrickplayInfo

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 534 | `test_unknown_server_returns_404` | **Strong** | 404. |
| 541 | `test_invalid_path_returns_400` | **Strong** | 400 on path-traversal-shaped manifest path. |

## TestMultiServerTrickplayFrame

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 572 | `test_unknown_server_returns_404` | **Strong** | 404. |
| 579 | `test_returns_jpeg_slice_from_real_sheet` | **Strong** | Pixel-level end-to-end test: builds real 2x2 colored tile sheet, requests index=2, decodes returned JPEG, asserts blue dominates `b > r and b > g`. Catches off-by-one in tile slicing AND wrong-orientation regressions. Exemplary. |
| 631 | `test_path_traversal_rejected` | **Strong** | 403 on `/etc/` traversal — security boundary pin. |

## Summary

- **41 tests** total — 40 Strong, 1 Weak (defensible smoke test)

**File verdict: STRONG.**

### Weak test (defensible — keep with note):
- **L138** `test_reference_bif` — only `> 0` checks. This is an intentional smoke test against a checked-in reference fixture (with `skip` if missing). Its job is "the parser doesn't crash on a real-world BIF", not "we know exact frame count". Acceptable as-is, but if the fixture has a known frame_count/interval, it could be tightened to strict equality. Not blocking.

### Notes:
- L177 `test_writes_one_jpeg_per_frame` — exemplary D34 contract pin with documented filename shape requirement.
- L579 `test_returns_jpeg_slice_from_real_sheet` — pixel-level decode + dominant-color assertion is a model for end-to-end image tests.
- L292 `test_requires_auth` — explicitly pins 302 (not 308) with documented rationale.
