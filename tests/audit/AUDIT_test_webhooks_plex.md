# Audit: tests/test_webhooks_plex.py — 24 tests, module-level

This file uses module-level test functions (no test classes) plus a few helper grouping comments.

## Webhook route tests (multipart payload)

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 113 | `test_plex_webhook_extracts_paths_from_inline_media_part` | **Strong** | 4 strict-equality checks: `status_code == 202`, `mock_schedule.called`, `args[0] == "plex"`, `args[1] == "Inline Movie"`, `args[2] == "/data/movies/Inline Movie/Inline Movie.mkv"`. Pins source/title/path tuple. |
| 136 | `test_plex_webhook_falls_back_to_plex_api_lookup` | **Strong** | Documented anti-D31 pattern: mocks at plexapi boundary (`plex_server` + `fetchItem`), NOT at `_resolve_plex_paths_from_rating_key`. Asserts `fetchItem.assert_called_once_with(98765)` (typed int!) AND `args[2] == "/data/tv/Show/S01E01.mkv"`. Catches "route stops calling resolver" regression. |
| 187 | `test_plex_webhook_ignores_non_library_new_events` | **Strong** | `status_code == 200`, `success is True`, `"Ignored" in message`, `mock_schedule.assert_not_called()`. Multi-pin. |
| 199 | `test_plex_webhook_test_ping_records_history` | **Strong** | Asserts no scheduling AND that history records `source == "plex" and status == "test"`. Catches drop-history regressions. |
| 212 | `test_plex_webhook_missing_rating_key_returns_400` | **Strong** | 400 + `"ratingKey" in body["error"]` substring. |
| 221 | `test_plex_webhook_disabled_when_master_off` | **Strong** | Pins master switch contract (Phase I5 — single global toggle). 200 + "disabled" substring. |
| 245 | `test_plex_webhook_requires_auth` | **Strong** | 401 on missing header. |
| 256 | `test_plex_webhook_accepts_query_token` | **Strong** | Documented: `bare .called would let a regression that schedules a job with empty/wrong values pass`. Pins all 3 schedule args. |
| 284 | `test_plex_webhook_rejects_invalid_query_token` | **Strong** | 401 on wrong token. |
| 294 | `test_plex_webhook_invalid_json_returns_400` | **Strong** | 400 on garbage payload. |
| 306 | `test_plex_webhook_no_paths_resolved_returns_200_ignored` | **Weak** | OR clause `"No file paths" in body["message"] or body["success"] is True`. The `success is True` branch is a tautology — the route returns success on most non-failure paths. Should pin the specific message and remove the fallback. |

## Resolver unit tests

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 319 | `test_resolve_plex_paths_from_rating_key_handles_failure` | **Strong** | Strict tuple equality `== ([], None)` on exception. |
| 330 | `test_resolve_plex_paths_from_rating_key_walks_media_parts` | **Strong** | Strict list equality on 2 part files AND `display_title == "Movie A (2023)"`. Catches walker bugs and title formatter coupling. |
| 365 | `test_resolve_plex_paths_from_rating_key_walks_show_to_episodes` | **Strong** | Documented GitHub #227 regression test. Strict list equality on episode paths from Show entry. |
| 400 | `test_resolve_plex_paths_from_rating_key_walks_season_to_episodes` | **Strong** | Same GH #227 fix for Season. Strict list equality. |

## Title formatter unit tests

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 432 | `test_format_plex_title_from_metadata_episode_full` | **Strong** | Strict equality `== "Beyond Paradise - S04E03 - The Whispers"`. |
| 445 | `test_format_plex_title_from_metadata_episode_drops_tautological_title` | **Strong** | Strict equality drops "Episode 3" suffix. |
| 460 | `test_format_plex_title_from_metadata_episode_blank_title_drops_suffix` | **Strong** | Strict equality. |
| 473 | `test_format_plex_title_from_metadata_movie_with_year` | **Strong** | Strict equality `== "Dune: Part Two (2024)"`. |
| 480 | `test_format_plex_title_from_metadata_movie_without_year` | **Strong** | Strict equality `== "Unknown Movie"`. |
| 487 | `test_format_plex_title_from_metadata_missing_fields_returns_none` | **Strong** | Five distinct missing-field cases all pinned to `is None`. Matrix coverage. |
| 512 | `test_format_plex_title_from_item_uses_plexapi_attrs` | **Strong** | Strict equality on full formatted string. |
| 526 | `test_format_plex_title_from_item_returns_none_when_item_is_none` | **Strong** | Strict `is None` on None input. |

## Title-flows-into-schedule integration tests

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 538 | `test_plex_webhook_uses_formatted_episode_title` | **Strong** | Pins formatted episode title appears in schedule call args (vs raw Metadata.title). |
| 564 | `test_plex_webhook_drops_tautological_episode_suffix` | **Strong** | Strict equality on "Beyond Paradise - S04E03" — pins title-collapse contract end-to-end. |
| 587 | `test_plex_webhook_uses_formatted_movie_title` | **Strong** | Strict equality on "Dune: Part Two (2024)". |
| 608 | `test_plex_webhook_uses_ratingkey_title_when_metadata_is_sparse` | **Strong** | Strict equality on resolver-derived title — pins fallback chain. |
| 632 | `test_plex_webhook_falls_back_to_raw_title_when_nothing_else_works` | **Strong** | Strict equality on raw title — pins last-resort fallback. |

## Summary

- **27 tests** total — 26 Strong, 1 Weak

**File verdict: STRONG (one weak test to fix).**

### Weak test to fix:
- **L306** `test_plex_webhook_no_paths_resolved_returns_200_ignored` — the OR clause `"No file paths" in body["message"] or body["success"] is True` is bug-blind. The `success is True` branch is too permissive (most non-error responses set success=True). Should pin only `"No file paths" in body["message"]` (or whatever the actual production message is) AND status_code == 200.

### Notes:
- The file is otherwise exemplary — particularly L136 which explicitly calls out and avoids the D31 anti-pattern of mocking same-module helpers.
- L256 docstring documents the "bare .called would let regressions through" lesson — good audit hygiene.
- GitHub #227 regression tests (L365, L400) are correctly tied to the upstream issue.
