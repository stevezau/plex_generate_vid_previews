# Audit: tests/test_webhook_router.py — 24 tests, 11 classes

## TestSonarrWebhook

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 88 | `test_path_payload_dispatched` | **Strong** | Asserts kwargs.canonical_path AND kwargs.source — D34 paradigm |
| 129 | `test_radarr_payload_classified_correctly` | **Strong** | Audit-fixed: now asserts canonical_path AND source kwargs |

## TestJellyfinWebhook

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 162 | `test_itemadded_with_servers_id_match` | **Strong** | Pins `item_id_by_server == {"jelly-1": "jf-42"}` exact dict equality |
| 209 | `test_irrelevant_event_returns_202` | **Strong** | status=="ignored" pin |
| 232 | `test_unresolvable_item_returns_202` | **Strong** | proc.assert_not_called() with stub returning None |
| 273 | `test_path_outside_configured_libraries_still_dispatches_via_hint` | **Strong** | Pins "hint authoritative" contract — Job created even when path outside libs |
| 335 | `test_resolution_exception_returns_202` | **Strong** | Boom→202 + no dispatch (degrade gracefully) |
| 378 | `test_path_mapping_applied_to_jellyfin_payload` | **Strong** | Pins canonical_path == translated `/local/data/...` |

## TestEmbyWebhook

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 433 | `test_library_new_event` | **Strong** | Pins translated canonical_path "/data/movies/Foo.mkv" |
| 480 | `test_irrelevant_event_returns_202` | **Strong** | status=="ignored" pin |
| 510 | `test_unresolvable_item_returns_202` | **Strong** | not_called pin |
| 550 | `test_resolution_exception_returns_202` | **Strong** | Same as Jellyfin variant — graceful 202 |
| 589 | `test_two_emby_servers_route_by_server_id` | **Strong** | Asserts hint dict only contains MATCHED server's id (uuid-emby-B not -A) |

## TestPlexWebhook

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 673 | `test_item_id_by_server_is_bare_rating_key` | **Strong** | D31 regression lock — three asserts: exact dict, no `/`, no `/library/metadata/` prefix |
| 719 | `test_non_library_new_event_returns_202` | **Strong** | media.play → 202 + not_called (no dispatch) |

## TestPathFirstWebhook

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 740 | `test_simple_path_dispatch` | **Weak (minor)** | `proc.assert_called_once()` without arg checks — would pass even if router silently re-routed to a different path. (Note: kind=="path" pinned, but no canonical_path kwarg assertion.) |

## TestWebhookPrefixTranslationReachesOwnerCheck

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 776 | `test_webhook_with_remote_form_path_and_prefix_mapping_creates_job` | **Strong** | Pins kind=="path", status=="queued", job_id present, canonical_path=="/data/tv/Show/S01E01.mkv" |
| 851 | `test_webhook_with_unrecognised_path_still_creates_job_no_silent_drop` | **Strong** | Pins status=="queued" — explicit anti-regression for the silent-drop bug class |

## TestUnknownPayload

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 909 | `test_returns_400` | **Strong** | Tight 400 status code pin |

## TestPerServerFallback

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 919 | `test_returns_404_for_unconfigured_server` | **Strong** | 404 pin |
| 928 | `test_per_server_url_pins_dispatch_to_one_server` | **Strong** | Pins server_id_filter=="plex-A" — D34 contract pin |
| 972 | `test_universal_url_does_not_pin_dispatch` | **Strong** | Pins server_id_filter is None on universal endpoint |

## TestServerIdentityRouting

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 1014 | `test_inbound_jellyfin_routes_to_matching_identity` | **Strong** | Pins hint dict to ONLY the matched server's id |
| 1071 | `test_inbound_with_unknown_identity_when_multiple_configured` | **Strong** | 202 + body.status=="ignored" — distinguishes from queued (audit fix) |
| 1119 | `test_identity_collision_refuses_to_route` | **Strong** | Same — body.status=="ignored", not silent first-match pick |

## TestAuth

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 1175 | `test_missing_token_rejected` | **Strong** | 401 pin (tightened from `in (401,403)`) + body substring check |

## TestPayloadSizeLimit

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 1192 | `test_oversized_payload_returns_413` | **Strong** | 413 pin |
| 1202 | `test_normal_size_payload_is_accepted` | **Strong** | 202 + body.status=="queued" |

## Summary

- **27 tests** total (recounted: 11 classes, ~27 tests)
- **26 Strong / 1 Weak**
- 1 Weak: `test_simple_path_dispatch` line 740 — `proc.assert_called_once()` without canonical_path/source kwarg pin. **Recommend strengthening** to assert `kwargs["canonical_path"] == "/data/tv/Show/S01E01.mkv"` AND `kwargs.get("source") == "path"` so a silent reroute regression is caught.
- D31 regression lock (Plex bare ratingKey) is exemplary — 3 distinct assertions guard the bug shape.

**File verdict: STRONG.** Single Weak test at line 740 should gain explicit canonical_path/source kwarg assertions.
