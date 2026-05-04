# Audit: tests/test_servers_registry.py — 16 tests, 5 classes

## TestServerConfigFromDict

| Line | Test | Verdict |
|---|---|---|
| 20 | `test_minimal_entry` | **Strong** — strict equality on `id`, `type is ServerType.PLEX` (singleton check), `libraries == []` default. |
| 26 | `test_libraries_normalised` | **Strong** — pins library count, type-narrowing (`isinstance(Library)`), AND that `enabled=False` from input is preserved. |
| 54 | `test_unknown_type_raises` | **Strong** — explicit `UnsupportedServerTypeError` raise on unknown vendor. |
| 58 | `test_malformed_library_skipped_not_raised` | **Strong** — pins the resilience contract: a malformed entry (`"not-a-dict"`) is dropped, the good library survives. Catches a regression that aborts loading on first bad row. |

## TestServerConfigRoundTrip

| Line | Test | Verdict |
|---|---|---|
| 76 | `test_to_dict_inverse_of_from_dict` | **Weak (could be tighter)** — only asserts 3 of the ~10 fields round-trip (`libraries[0]["kind"]`, `url`, `timeout`). Doesn't assert `id`, `type`, `name`, `enabled`, `auth`, `verify_ssl`, `path_mappings`, `output`. A regression that drops `auth` from `to_dict` would silently pass — and that would be catastrophic (operator saves settings → auth section disappears → next reload fails to connect). **Note for fixing**: assert the entire `round_tripped` dict equals `original_data` (or near-equal — handle defaults), or assert each field individually. |

## TestServerRegistryFromSettings

| Line | Test | Verdict |
|---|---|---|
| 107 | `test_loads_plex_server_with_legacy_config` | **Strong** — pins `len == 1`, `isinstance(PlexServer)`, `id == "plex-default"`, `name == "Home Plex"`. |
| 127 | `test_unknown_server_type_skipped_with_warning` | **Strong** — audit-fixed: original used `caplog` but never asserted on it (loguru doesn't bridge to stdlib). Now installs a loguru sink and asserts the warning mentions "kodi". Pins the operator-diagnostic contract. |
| 171 | `test_unknown_type_string_skipped` | **Strong** — strict `[]` for both servers and configs after dropping the only (unsupported) row. |
| 179 | `test_empty_input_yields_empty_registry` | **Strong** — strict `[]` for `servers()` AND `find_owning_servers()`. |
| 184 | `test_loads_plex_server_without_legacy_config` | **Strong (regression pin)** — explicitly pins the bug fix where Preview Inspector instantiated a registry with no `legacy_config`. Asserts every synthesised legacy-config field (`plex_url`, `plex_token`, `plex_verify_ssl`, `plex_timeout`, `plex_config_folder`, `plex_bif_frame_interval`, `plex_library_ids`). The library-id assertion (`["1"]` from 2 libs, only 1 enabled) pins the enabled-filter contract. |

## TestServerRegistryFromLegacyConfig

| Line | Test | Verdict |
|---|---|---|
| 229 | `test_synthesises_single_plex_server` | **Strong** — `len == 1` AND `isinstance(PlexServer)`. |
| 235 | `test_returns_empty_when_no_legacy_plex` | **Strong** — pins that empty plex_url/plex_token → no synthesised server (vs synthesising a broken one). |

## TestServerRegistryAccessors

| Line | Test | Verdict |
|---|---|---|
| 243 | `test_get_returns_live_client` | **Strong** — `isinstance(PlexServer)` confirms the live client (not config) is returned. |
| 248 | `test_get_unknown_returns_none` | **Strong** — None-return contract for unknown id. |
| 252 | `test_get_config_includes_disabled_servers` | **Strong** — pins that `get_config` returns disabled rows (so the UI can show them). Asserts `enabled is False` strict equality. |

## TestFindOwningServers

| Line | Test | Verdict |
|---|---|---|
| 272 | `test_dispatches_to_underlying_resolver` | **Strong** — strict `["plex-default"]` list-equality on the matched server ids AND `[] == no_match`. Pins both the positive (path under `/data/movies` → owned by plex-default) AND the negative (path elsewhere → no owners) cells. |

## Summary

- **15 tests** total — 14 Strong, 1 Weak (`test_to_dict_inverse_of_from_dict` at line 76)
- Audit-fixed `test_unknown_server_type_skipped_with_warning` now pins the loguru log line, not just the silent-skip behaviour
- The `test_loads_plex_server_without_legacy_config` regression pin is exemplary — every synthesised field asserted
- `find_owning_servers` covers both match and no-match cells

**File verdict: STRONG (one weak round-trip — could leak field drops).**

Recommended fix: tighten `test_to_dict_inverse_of_from_dict` to assert every field of `original_data` is present and equal in `round_tripped`. Currently a regression that drops `auth` or `path_mappings` from serialization would silently pass — and these are persistence-critical fields.
