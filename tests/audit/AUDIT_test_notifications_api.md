# Audit: tests/test_notifications_api.py — 21 tests, 5 classes

## TestNotificationsAPI

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 113 | `test_list_empty_when_vulkan_healthy` | **Strong** — strict `data == {"notifications": []}` AND `status_code == 200`. Pins the healthy-baseline shape. |
| 127 | `test_list_contains_vulkan_warning_when_software` | **Strong** — strict `len == 1`, then per-field equality on `id`, `severity`, `dismissable is True`, `source == "vulkan_probe"`. Substring on title (`"Dolby Vision Profile 5"`) and `body_html` non-empty truthy check are the only loose ones — but they cover content stability without locking exact wording. |
| 149 | `test_session_dismiss_hides_notification` | **Strong** — pre/post comparison: 1 notification before, dismiss returns specific dict `{"ok": True, "id": ..., "persisted": False}`, post returns `{"notifications": []}`. Strict equality on three observable boundaries. |
| 172 | `test_session_dismiss_does_not_touch_settings_file` | **Strong** — file-based assertion: `after == before` on the raw JSON. Catches a regression that accidentally persists session dismissals. |
| 190 | `test_permanent_dismiss_persists_to_settings` | **Strong** — `body["ok"] is True`, `body["persisted"] is True`, AND filesystem read of settings.json confirming `id in stored.get("dismissed_notifications", [])`. End-to-end persistence check. |
| 208 | `test_permanent_dismiss_is_idempotent` | **Strong** — POSTs twice, asserts `.count(id) == 1`. Pins de-dup contract. |
| 221 | `test_permanent_dismiss_filters_from_list` | **Strong** — strict `{"notifications": []}` after permanent dismiss. |
| 231 | `test_reset_dismissed_restores_notification` | **Strong** — full round-trip: dismiss-permanent → reset returns `{"ok": True}` (strict equality) → restored list has the right id. Three-step contract pinned. |

## TestBuildActiveNotifications

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 253 | `test_builder_returns_empty_list_when_healthy` | **Strong** — strict `notifications == []`. Pure-function path. |
| 261 | `test_builder_includes_vulkan_warning_when_software` | **Strong** — `VULKAN_SOFTWARE_FALLBACK_ID in ids`. Pins the builder content. |
| 270 | `test_builder_suppresses_permanently_dismissed` | **Strong** — passing `dismissed_permanent=[id]` → `notifications == []`. Pins the filter. |

## TestDeprecatedImageNotification

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 282 | `test_silent_when_env_unset` | **Strong** — `"deprecated_docker_image_name" not in ids`. |
| 291 | `test_silent_when_running_canonical_image` | **Strong** — canonical image name → not in ids. Two-cell matrix on the env-var states. |
| 300 | `test_fires_when_running_deprecated_image` | **Strong** — three substring assertions for old image name, new image name, AND sunset date `"2026-10-29"`. Plus `severity == "warning"` and `dismissable is True`. Catches drift in the migration messaging. |

## TestSettingsManagerDismissedNotifications

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 329 | `test_dismissed_notifications_defaults_to_empty_list` | **Strong** — `== []`. Default-value pin. |
| 333 | `test_dismissed_notifications_empty_when_garbage_stored` | **Strong** — `"not-a-list"` → `== []`. Catches a regression that returns the garbage value instead of the empty default. |
| 337 | `test_dismiss_notification_permanent_persists` | **Strong** — strict `== ["foo"]` on first manager AND on a freshly-loaded second manager. Round-trip-through-disk. |
| 345 | `test_dismiss_notification_permanent_is_idempotent` | **Strong** — `== ["foo"]` after two calls. |
| 351 | `test_undismiss_notification_removes_entry` | **Strong** — `== ["bar"]` (the surviving entry). Pins both removal AND that the other entry survives. |
| 356 | `test_reset_dismissed_clears_all` | **Strong** — `== []`. |

## TestSchemaMigrationNotification

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 365 | `test_card_appears_after_migration_runs` | **Strong** — sets `_pending_migration_notice` flag, asserts `"schema_migration_completed" in ids`. The try/finally around the cleanup is correct (singleton SettingsManager pollution risk explicitly called out in comment). |
| 391 | `test_dismissing_card_clears_pending_flag` | **Strong** — POST dismiss → `sm.get("_pending_migration_notice") is None`. Strict identity check on the cleared flag. Pins the "one-shot" promise. |

## Summary

- **21 tests** total — all **Strong**
- 0 weak / bug-blind / tautological / dead
- Persistence round-trips through real disk (not mocked at the SUT seam)
- Three-state matrix on `DOCKER_IMAGE_NAME` (unset / canonical / deprecated)
- Schema-migration card has both fire AND clear tests with proper singleton cleanup

**File verdict: STRONG.** No changes needed.
