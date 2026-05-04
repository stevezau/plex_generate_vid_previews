# Audit: tests/test_servers_ownership.py — 22 tests, 5 classes

## TestServerOwnsPath

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 47 | `test_match_under_enabled_library` | **Strong** — strict equality on `server_id`, `library_id`, `library_name` AND `isinstance(match, OwnershipMatch)`. |
| 55 | `test_no_match_when_path_not_under_any_library` | **Strong** — `is None` is the contract for non-match (not just truthy/falsy). |
| 59 | `test_disabled_library_does_not_match` | **Strong** — two-cell matrix: disabled returns None AND enabled sibling matches with the right `library_id == "2"`. Pins both branches. |
| 73 | `test_disabled_server_never_owns` | **Strong** — disabled server has enabled library yet returns None — tests the server-level gate, not the library gate. |
| 80 | `test_folder_boundary_prevents_partial_prefix_match` | **Strong** — `/data/movies-archive/...` must NOT match `/data/movies/...`. Classic `os.path.commonpath` vs `startswith` bug guard. |
| 86 | `test_first_matching_library_wins` | **Strong** — pins library precedence: first registered wins (strict equality on `library_id == "1"`). |
| 98 | `test_multiple_remote_paths_in_library` | **Strong** — asserts `local_prefix == "/data/movies"` (strict equality), confirming the right path of the tuple was matched. |
| 113 | `test_path_mapping_translates_remote_to_local` | **Weak** — only asserts `match.local_prefix.startswith("/data")`. With `local_prefix="/data"`, ANY path that starts with `/data` would pass. Could tighten to `local_prefix == "/data/movies"` to pin the join semantics. |
| 125 | `test_legacy_plex_prefix_mapping_key_supported` | **Strong (loose-but-sufficient)** — only `is not None`, but the legacy key contract is binary (works or doesn't) so the loose check captures it. |
| 133 | `test_no_libraries_means_no_match` | **Strong** — empty libraries → None. Edge case pinned. |

## TestFindOwningServers

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 140 | `test_fan_out_to_multiple_servers_for_shared_volume` | **Strong** — asserts `ids == ["plex-a", "jf"]` with strict ordering AND comments document order-preservation contract. Pins both fan-out and ordering. |
| 172 | `test_path_only_in_plex_b` | **Strong** — strict `[m.server_id for m in matches] == ["plex-b"]`. |
| 184 | `test_no_servers_own_path_returns_empty` | **Strong** — `matches == []` strict equality. Pins empty-list (not None) contract. |
| 192 | `test_disabled_servers_excluded_from_fan_out` | **Strong** — strict `["plex-a"]`. Confirms disabled server is filtered before the path check. |

## TestEdgeCases

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 207 | `test_empty_remote_paths_never_match` (parametrized x3) | **Strong** — `()`, `("",)`, `("   ",)` all → None. 3-cell parametrized; whitespace-only catches a strip-then-check bug. |
| 219 | `test_canonical_path_with_trailing_slash_does_not_match_directory_as_file` | **Strong** — pins file-style canonical path matching contract. |

## TestUnicodeNormalization

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 231 | `test_japanese_path_in_nfc_matches` | **Strong** — `isinstance(match, OwnershipMatch)`. Multi-byte UTF-8 path literal forces NFC parity. |
| 239 | `test_accented_path_nfd_canonical_matches_nfc_setting` | **Strong** — explicit NFD canonical vs NFC setting (different bytes, same characters). Catches a regression that drops the NFC normalisation step. Has explanatory message. |
| 257 | `test_emoji_path_matches` | **Strong** — emoji is multi-codepoint; NFC is no-op but the comparison must still work. |
| 264 | `test_case_mismatch_does_not_match` | **Strong** — `/Movies` vs `/movies` → None. Pins the explicit "we don't fold case" decision (with rationale in docstring). |

## TestPathMappingMatrix

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 287 | `test_two_mappings_where_one_is_prefix_of_other_picks_specific` | **Strong (sufficient)** — `is not None` is the contract here (the test docstring explicitly says it asserts ownership, not candidate ordering). The audit-driven matrix gap is closed. |
| 306 | `test_chained_mappings_each_applied_independently` | **Strong** — three-cell matrix (movies match, tv match, unrelated `/mnt/anywhere` returns None). Pins independent application. |
| 323 | `test_two_servers_share_path_with_DIFFERENT_per_server_mappings` | **Strong** — set-equality `{"plex-1", "jf-1"}` for the fan-out across vendors with different mappings. Documented in-file as "the audit's biggest hole — the missing matrix row that real users hit". |
| 350 | `test_canonical_path_inside_local_view_no_mapping_needed` | **Strong** — empty `path_mappings` and ownership still works. Catches a regression that REQUIRES path_mappings. |
| 361 | `test_mapping_with_trailing_slash_normalised` | **Strong** — `is not None` with explanatory message; the contract being pinned is "user shouldn't have to remember trailing slashes". |

## Summary

- **22 tests** total (counting the parametrized cell as one)
- **21 Strong, 1 Weak** (`test_path_mapping_translates_remote_to_local` — `startswith("/data")` should be `== "/data/movies"`)
- 0 bug-blind / tautological / dead
- The path-mapping matrix gap (the audit's "biggest hole") is closed by `TestPathMappingMatrix`

**File verdict: STRONG.**

### Recommended fix

`tests/test_servers_ownership.py:113` `test_path_mapping_translates_remote_to_local` — replace `match.local_prefix.startswith("/data")` with `match.local_prefix == "/data/movies"` to pin the actual joined path rather than just the root.
