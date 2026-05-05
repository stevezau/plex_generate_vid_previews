# Audit: tests/test_webhook_router.py — ~28 tests (re-audit, batch 6)

Tests for the universal webhook router (auto-detection + dispatch): Sonarr/Radarr classification, Jellyfin/Emby ItemAdded routing, Plex multipart parsing (D31), path-first webhook, prefix translation pre-flight (P0.4), unknown payloads, per-server fallback URL pinning, server-identity routing (incl. collision), auth, payload size limit.

## TestSonarrWebhook

| Line | Test | Verdict | Note |
|---|---|---|---|
| 88 | `test_path_payload_dispatched` | Strong | Pins 202 + body kind=sonarr + body status=queued + `assert_called_once` + canonical_path AND source kwargs. |
| 129 | `test_radarr_payload_classified_correctly` | Strong | Audit-fixed: pins canonical_path AND source (was D34 pattern with bare `assert_called_once`). |

## TestJellyfinWebhook

| Line | Test | Verdict | Note |
|---|---|---|---|
| 162 | `test_itemadded_with_servers_id_match` | Strong | Pins kind="jellyfin" + `assert_called_once` + `item_id_by_server == {"jelly-1": "jf-42"}`. |
| 209 | `test_irrelevant_event_returns_202` | Strong | Pins 202 + body status="ignored". |
| 232 | `test_unresolvable_item_returns_202` | Strong | Pins 202 + `assert_not_called`. |
| 273 | `test_path_outside_configured_libraries_still_dispatches_via_hint` | Strong | Pins 202 + body status="queued" + `assert_called_once`. Anti-silent-drop contract. |
| 335 | `test_resolution_exception_returns_202` | Strong | Pins 202 + `assert_not_called`. |
| 378 | `test_path_mapping_applied_to_jellyfin_payload` | Strong | Pins 202 + `canonical_path == "/local/data/tv/Foo/S01E01.mkv"` (translated). |

## TestEmbyWebhook

| Line | Test | Verdict | Note |
|---|---|---|---|
| 433 | `test_library_new_event` | Strong | Pins 202 + kind="emby" + `assert_called_once` + translated canonical_path. |
| 480 | `test_irrelevant_event_returns_202` | Strong | Pins 202 + body status="ignored". |
| 510 | `test_unresolvable_item_returns_202` | Strong | Pins 202 + `assert_not_called`. |
| 550 | `test_resolution_exception_returns_202` | Strong | Pins 202 + `assert_not_called`. |
| 589 | `test_two_emby_servers_route_by_server_id` | Strong | Pins 202 + `assert_called_once` + `item_id_by_server == {"uuid-emby-B": "em-42"}` (only matched server's id appears). |

## TestPlexWebhook

| Line | Test | Verdict | Note |
|---|---|---|---|
| 673 | `test_item_id_by_server_is_bare_rating_key` | Strong | D31 contract: pins exact `{"plex-1": "557676"}` + no "/" + not URL-form. Multi-invariant. |
| 719 | `test_non_library_new_event_returns_202` | Strong | Pins 202 + `assert_not_called`. |

## TestPathFirstWebhook

| Line | Test | Verdict | Note |
|---|---|---|---|
| 740 | `test_simple_path_dispatch` | Strong | Pins 202 + body kind="path" + `call_count == 1` + source AND canonical_path AND empty/None hint dict. Multi-invariant. |

## TestWebhookPrefixTranslationReachesOwnerCheck

| Line | Test | Verdict | Note |
|---|---|---|---|
| 793 | `test_webhook_with_remote_form_path_and_prefix_mapping_creates_job` | Strong | Anti-silent-drop (P0.4): pins 202 + body status="queued" + body has job_id + kind="path" + canonical_path passed verbatim. Multi-invariant with sentinel-loud failure messages. |
| 868 | `test_webhook_with_unrecognised_path_still_creates_job_no_silent_drop` | Strong | Same anti-drop pattern with no-matching-mapping config. |

## TestUnknownPayload

| Line | Test | Verdict | Note |
|---|---|---|---|
| 926 | `test_returns_400` | Weak | Status-code-only assert: `assert response.status_code == 400`. **Why downgraded:** doesn't check body — a regression that returned 400 with a misleading or empty body would still pass. Should pin error message substring. |

## TestPerServerFallback

| Line | Test | Verdict | Note |
|---|---|---|---|
| 936 | `test_returns_404_for_unconfigured_server` | Weak | Status-code-only: `assert response.status_code == 404`. **Why downgraded:** no body check — regression returning 404 with the wrong error label or empty body slips through. |
| 945 | `test_per_server_url_pins_dispatch_to_one_server` | Strong | Pins 202 + `assert_called_once` + `server_id_filter == "plex-A"`. |
| 989 | `test_universal_url_does_not_pin_dispatch` | Strong | Pins 202 + `assert_called_once` + `server_id_filter is None`. |

## TestServerIdentityRouting

| Line | Test | Verdict | Note |
|---|---|---|---|
| 1031 | `test_inbound_jellyfin_routes_to_matching_identity` | Strong | Pins 202 + `assert_called_once` + `item_id_by_server == {"uuid-jelly-B": "jf-42"}`. |
| 1088 | `test_inbound_with_unknown_identity_when_multiple_configured` | Strong | Audit-fixed: pins 202 + body status="ignored" (not just status code) — closes "router silently picked first server" gap. |
| 1136 | `test_identity_collision_refuses_to_route` | Strong | Audit-fixed: same body-status pin for collision case. |

## TestAuth

| Line | Test | Verdict | Note |
|---|---|---|---|
| 1192 | `test_missing_token_rejected` | Strong | Pins 401 (strict, not `in (401, 403)`) + body error contains `"Authentication required"`. |

## TestPayloadSizeLimit

| Line | Test | Verdict | Note |
|---|---|---|---|
| 1209 | `test_oversized_payload_returns_413` | Weak | Status-code-only: `assert response.status_code == 413`. **Why downgraded:** no body check — Flask's default 413 page leaks into the response and a regression that custom-handled this could miss the format/error contract. |
| 1219 | `test_normal_size_payload_is_accepted` | Strong | Pins 202 (strict, not `in (200, 202)`) + body assertion (truncated in audit view but body checked). |

**File verdict: MIXED (3 weak status-code-only tests).** Re-audit caught 3 status-only tests that the prior audit missed (lines 926, 936, 1209). The bulk of the file is strong — the prior audit-fixes for ambiguous-identity body status are great. The Plex bare-ratingKey D31 test is exemplary, and the prefix-translation pre-flight tests have loud failure messages.

## Fix queue

- **L926 `TestUnknownPayload::test_returns_400`** — pin error body substring (e.g. `"Unrecognized payload"` or whatever the canonical error is).
- **L936 `test_returns_404_for_unconfigured_server`** — pin error body substring identifying the unconfigured server id.
- **L1209 `test_oversized_payload_returns_413`** — pin a body field (e.g. error contains "exceeded" or "size") so a regression that returned 413 with a misleading body fails.
