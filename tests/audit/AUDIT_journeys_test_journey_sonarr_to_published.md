# Audit: tests/journeys/test_journey_sonarr_to_published.py — 3 tests, 2 classes

## TestSonarrWebhookToJobJourney

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 135 | `test_sonarr_download_creates_job_with_correct_overrides` | **Strong** — asserts: 202 status, exactly 1 capture call, `webhook_paths == ["/data/tv/The Show/S01E05.mkv"]` (strict equality), `library_name == "The Show S01E05"` (verbatim, comment explicitly addresses substring drift), `source == "sonarr"`. Closes D34 paradigm by checking the kwargs the SUT controls. |
| 211 | `test_two_quick_sonarr_webhooks_for_same_file_dedup` | **Strong** — asserts r1=202 + r2=200 + exactly 1 captured call (dedup contract); `webhook_paths` payload also pinned |

## TestWebhookDisabledShortCircuit

| Line | Test | Verdict | Rationale |
|---|---|---|---|
| 280 | `test_disabled_webhook_creates_no_job` | **Strong** — `captured_calls == []` (strict empty), 200 response (not 202) |

## Summary

- **3 tests** all **Strong**
- End-to-end journey from POST → webhook handler → debounce → orchestrator boundary
- Mocks only at `_start_job_async` (the orchestrator seam) — real Flask test client driving the chain
- Uses `assert call_event.wait()` instead of `time.sleep` for thread synchronisation

**File verdict: STRONG.** No changes needed.
