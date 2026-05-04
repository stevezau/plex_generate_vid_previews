# Audit: tests/test_api_servers.py — 36 tests, 9 classes

## TestListServers

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 52 | `test_empty_when_no_servers_configured` | **Strong** | Strict `==` on full response body shape and 200 code. |
| 57 | `test_returns_configured_servers` | **Strong** | Pins length, id, type, url. |
| 88 | `test_redacts_token` | **Strong** | Pins exact `***REDACTED***` string AND that method survives. Catches over- or under-redaction. |
| 107 | `test_redacts_emby_api_key_and_password` | **Strong** | Three credentials all pinned to `***REDACTED***`. Multi-secret matrix. |
| 132 | `test_skips_servers_with_unknown_type` | **Strong** | Strict `== ["plex"]` — pins both presence of plex AND absence of kodi. |
| 143 | `test_handles_legacy_settings_without_media_servers` | **Strong** | Strict `[]` and 200. |

## TestGetServer

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 151 | `test_returns_individual_server` | **Strong** | Pins id AND token redaction in same call. |
| 171 | `test_404_when_server_id_missing` | **Strong** | Strict 404. |

## TestPathOwners

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 178 | `test_diagnoses_ownership` | **Strong** | Pins `len(owners)==1`, server_id, AND library_name. |
| 213 | `test_returns_empty_when_no_owners` | **Strong** | Pins `owners == []` (path under no library). |
| 242 | `test_400_when_path_missing` | **Strong** | Strict 400 on missing query param. |

## TestRefreshLibraries

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 248 | `test_404_when_server_missing` | **Strong** | Strict 404. |
| 256 | `test_jellyfin_refresh_succeeds` | **Strong** | Pins `count==1`. (Could pin library name too — minor.) |
| 286 | `test_persists_libraries_and_preserves_enabled_toggle` | **Strong** | Pins enabled toggle preserved (False) AND new lib defaults to True AND remote_paths updated AND persisted ids set. Four contracts. |
| 344 | `test_502_when_server_unreachable` | **Strong** | Strict 502 on RuntimeError. |

## TestCreateServer

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 378 | `test_creates_emby_server_and_assigns_id` | **Strong** | Pins type, name, id-assigned, redacted-on-return, AND persisted to settings. |
| 401 | `test_400_when_type_missing` | **Strong** | Strict 400. |
| 409 | `test_400_when_unknown_type` | **Strong** | Strict 400 on unknown type ("kodi"). |
| 417 | `test_400_when_name_missing` | **Strong** | Strict 400. |
| 425 | `test_400_when_url_missing` | **Strong** | Strict 400. |
| 433 | `test_plex_multi_add_persists_server_identity_from_discovery` | **Strong** | Pins both identities present AND that both share single shared OAuth token. Catches the multi-server-identity bug. |
| 484 | `test_409_when_id_collides` | **Strong** | Audit-strengthened: pins 409 + body has error key + existing server NOT mutated (type/name/url unchanged). Was just status check. |

## TestUpdateServer

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 526 | `test_renames_server` | **Strong** | Pins name change AND that url + auth.api_key untouched. |
| 551 | `test_redacted_auth_does_not_clobber_secret` | **Strong** | Pins real secret survives the redacted-echo round-trip. Catches the credential-overwrite bug. |
| 578 | `test_id_field_immutable` | **Strong** | Pins id stays "s1" despite payload `"id": "hacked-id"`. Pins immutable contract. |
| 600 | `test_404_when_unknown_id` | **Strong** | Strict 404. |
| 608 | `test_exclude_paths_round_trip` | **Strong** | Three steps: PUT sets, GET returns, second PUT (without field) preserves. Pins all three with strict equality. |
| 656 | `test_400_when_exclude_paths_regex_invalid` | **Strong** | Pins 400 + `"regex"` substring in error msg. |
| 678 | `test_400_when_path_mapping_local_prefix_missing` | **Strong** | Pins 400 + `"does not exist"` in error. |
| 702 | `test_put_accepts_modern_remote_prefix_path_mapping` | **Strong** | Pins 200 on both name-only PUT (regression for re-validation bug) AND explicit remote_prefix payload. |
| 745 | `test_400_when_plex_config_folder_missing` | **Strong** | Pins 400 + "does not exist" message. |

## TestDeleteServer

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 770 | `test_removes_server` | **Strong** | Pins 200, deleted-id in response, AND remaining set is `{s2}`. |
| 797 | `test_404_when_unknown` | **Strong** | Strict 404. |

## TestTestConnection

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 803 | `test_emby_test_connection_succeeds` | **Strong** | Pins `ok is True`, server_id, server_name. |
| 833 | `test_jellyfin_test_connection_failure_reported` | **Strong** | Pins `ok is False` AND substring of error message. |
| 856 | `test_test_connection_does_not_persist` | **Strong** | Pins `media_servers in (None, [])`. Catches the "test accidentally creates server" bug. |
| 879 | `test_invalid_payload_returns_400` | **Strong** | Pins 400 + `body["ok"] is False`. |

## TestOutputStatus

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 906 | `test_emby_reports_missing_when_sidecar_absent` | **Strong** | Pins server_type, adapter, exists=False, AND that the expected sidecar path appears in both `paths` and `missing_paths` (catches drift in path computation). |
| 922 | `test_emby_reports_exists_when_sidecar_present` | **Strong** | Pins exists=True AND missing_paths==[]. |
| 941 | `test_plex_requires_item_id` | **Strong** | Pins `needs_item_id is True` AND `exists is False`. |
| 964 | `test_jellyfin_reports_missing_sheets_dir` | **Strong** | D38 contract: pins exists=False even when sheet dir exists, plus the `.trickplay` dir must appear in missing_paths. The "fresh signal needs at least one tile" contract. |
| 1002 | `test_404_when_server_missing` | **Strong** | Strict 404. |
| 1011 | `test_400_when_path_missing` | **Strong** | Strict 400. |

## Summary

- **36 tests** — all **Strong**
- 0 weak / bug-blind / tautological / bug-locking
- One audit-strengthened test: `test_409_when_id_collides` (line 484-523) was tightened from status-only to also pin error body + non-mutation of existing server
- Strong credential-handling matrix: token, api_key, password redacted on GET/POST, real secret preserved through redacted PUT, test-connection doesn't persist
- Minor improvement opportunity (not a bug): `test_jellyfin_refresh_succeeds` (line 256) only asserts `count==1` and doesn't check the library name "Movies" came through. Marginal — the count check would catch most regressions.

**File verdict: STRONG.** No changes needed.
