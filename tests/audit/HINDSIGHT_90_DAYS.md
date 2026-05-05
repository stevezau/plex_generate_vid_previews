# Hindsight Audit — 90-Day Fix Commit Review (2026-02-04 → 2026-05-05)

**Window:** 90 days, `main` branch, commits matching `^fix:`.
**Total fix commits in window:** 70.
**Sampled in detail:** 32 (highest-impact: dispatcher, multi-server, webhooks, security, GPU, UI/JS).

> Production code: `media_preview_generator/`. (Note: many fixes still reference the legacy `plex_generate_previews/` path because they predate the package rename — interpret accordingly.)

---

## 1. Roll-up

### Category distribution (32 sampled)

| Category | Count | What it means |
|---|---|---|
| **A — Test existed but weak** (would have caught with stronger assertion) | 5 | The function had unit tests at fix time, but they didn't assert the buggy parameter / branch. |
| **B — Test existed but at wrong layer** (unit OK, wiring bug) | 6 | The unit-level helper was fine; the bug was in the *call site* / *integration*. |
| **C — Zero test coverage** | 8 | Fix added the first test (or area was a complete dead-zone). |
| **D — Test now exists** (added with the fix) | 9 | Healthy. Hindsight test is real and covers the regression. |
| **E — Bug class can't be unit-tested** (CSS / hardware / real subprocess / browser race) | 4 | Theme drift, NVIDIA Vulkan ICD probe, FFmpeg DV5 OOM, scroll/scrollspy. |

### Top 5 production files / areas with the most fix commits (90-day heatmap)

| File | Fixes | Notes |
|---|---|---|
| `media_processing.py` | **18** | DV5 / DV7-8 / HDR tonemap chain churn (#178, #212, #213, #216 family). Most visited file. |
| `gpu_detection.py` | **11** | Vulkan ICD selection + container DRI permissions + Win/Linux fallbacks. |
| `web/webhooks.py` | **8** | Auth, dedup, payload shape, Content-Type, dispatch wiring. |
| `web/static/js/app.js` | **8** | Schedules cron offset, theme classes, progress replay, settings scroll. |
| `web/routes/job_runner.py` | **5** | Failure-tracker scope, inflight guard, Plex rescan retry, pool reconciliation. |

### Top recurring bug-shapes (test bug-blind areas)

| Bug-shape | Recurrences | Why tests didn't catch it |
|---|---|---|
| **Dolby Vision tone-mapping filter chain** (`media_processing._build_dv*_chain`) | **9 fixes** in 90 days | Tests assert a specific filter string OR call count — they don't assert *frame correctness*, *RPU side-data preservation*, or *device interop*. The bug surface is "the FFmpeg command is well-formed but produces wrong pixels / OOMs on real hardware" → fundamentally not a unit-test problem (Category E). |
| **Vulkan / NVIDIA ICD probe** | **3 fixes** (`f7c55e1`, `7e8d0ca`, `9d84ac1`) | Each shipped with a test that mocked `subprocess.run` to return the *expected* string. Real failure mode (libGLX_nvidia → libEGL → GLVND vendor pick) was invisible to mocks. Mocking subprocess hides everything that matters about probe ordering. |
| **Webhook authentication / parsing wiring** | **5 fixes** (`51075c9`, `357c442`, `197df73`/`d481eb6`, `0346b1f`, `7a721a6`) | Helpers (`_extract_sonarr_file_path`, `validate_token`) had unit tests. The bugs were in the *Flask wiring*: header precedence, `force=True`, CSRF exemption list, log format string. Wrong layer. |
| **Job/worker pool dispatch wiring** | **4 fixes** (`9aee608`, `ab979a8`, `c8bb174`, `31cd4a0`) | `WorkerPool` and `JobDispatcher` had healthy unit tests. The bugs lived in the *callbacks* and *event ordering* between `_dispatch_items` and `_start_job_async`. |
| **Webhook history persistence + UI** | repeated (`197df73`, `d481eb6`, `0346b1f`) | Two consecutive identical commit titles ("improve webhook failure logging and persist history to disk") — strong signal that the first round shipped without a regression test that would have caught the loguru `%s` vs `{}` regression in commit 3. |

---

## 2. Per-fix table (32 sampled)

| Date | SHA | One-line | Files touched | Cat | Hindsight test? | Notes |
|---|---|---|---|---|---|---|
| 2026-04-18 | `70cba4e` | move fps before hwupload on DV5 libplacebo | `media_processing.py`, `test_media_processing.py` | E | partial — chain assertion updated | Test asserts filter string ordering. Real bug was VK_ERROR_OUT_OF_DEVICE_MEMORY on TITAN RTX. Test cannot reproduce. |
| 2026-04-18 | `9d84ac1` | prefer NVIDIA Vulkan over Intel on dual-GPU | `gpu_detection.py`, `test_gpu_detection_extended.py` | A | yes (mocked) | Mocked vulkaninfo output. Real ICD selection bug masked. |
| 2026-04-18 | `736d6c0` | populate `/dev/dri/by-path` for Intel under NVIDIA runtime | `gpu_detection.py` | C | none | Container init script behavior. No test. |
| 2026-04-16 | `09ad166` | DRM-based device derivation for DV5 VAAPI+Vulkan | `media_processing.py`, `test_media_processing.py` | D | yes | New tests added. |
| 2026-04-16 | `3f77a70` | enable VAAPI HW decode for DV5 Intel/AMD | `media_processing.py`, `test_media_processing.py` | D | yes | New tests. |
| 2026-04-15 | `36713ff` | drop `skip_frame` probe for DV8 keyframe path | `media_processing.py` | D | added `TestSkipFrameInitialDefaults` | Healthy. Probe deletion + 3 new test cases. |
| 2026-04-13 | `f30f67b` | move tz warning into bell notification center | `app.js`, `notifications.py`, templates | B | partial | `_build_..._notification` tested in `test_notifications_api.py`; the *dashboard banner removal* untested. |
| 2026-04-13 | `146e649` | stop ScrollSpy hijacking sidebar anchors | `settings.html`, `app.js` | E | none | Bootstrap behavior. No JS DOM tests for sidebar behavior. |
| 2026-04-13 | `19bdd58` | scroll-padding-top instead of JS scroll | `style.css`, `app.js` | E | none | Pure CSS layout. |
| 2026-04-13 | `be3f019` | measure navbar height at click time | `app.js` | E | none | Layout/measurement. |
| 2026-04-13 | `9a4790e` | Automation heading matches active tab | `automation.html` | C | none | Pure template; no Playwright e2e. |
| 2026-04-13 | `fbddcd5` | Settings scroll offset + dup Add Schedule btn | `settings.html`, `app.js` | C | none | Pure UI. |
| 2026-04-12 | `f167e5a` | DV5 slowness + green overlay (mega-fix) | 12 files | B | partial | Notification helpers tested; tone-map chain not pixel-tested. |
| 2026-04-11 | `852327c` | scope failure tracker per job | `media_processing.py`, `worker.py`, `job_runner.py`, `test_media_processing.py` | D | yes | 5 new `TestFailureScope` tests including concurrent-thread isolation. Best-in-class hindsight test. |
| 2026-04-11 | `9356649` | descriptive Plex webhook titles + dedup | `webhooks.py`, `test_webhooks.py`, `test_webhooks_plex.py` | D | yes | 14 new tests for title formatting + 5 dedup tests. |
| 2026-04-11 | `7e8d0ca` | NVIDIA Vulkan ICD via `__EGL_VENDOR_LIBRARY_FILENAMES` | `gpu_detection.py` | A | mocks subprocess | Test mocks `subprocess.run` for each Strategy 1/2/2b/3 — but a test where Strategy 1 returns the *wrong* ICD string would still pass because no test asserts *which env var actually got the NVIDIA driver to wake up*. |
| 2026-04-10 | `c5a050d` | Plex re-auth: pick server URL from list | `api_plex.py`, `settings.html` | C | none | Re-auth flow has zero unit tests for the URL picker branch. |
| 2026-04-10 | `a423705` | detect NVIDIA in containers without `/dev/dri` | `gpu_detection.py`, `test_gpu_detection_extended.py` | D | yes | New test. |
| 2026-04-10 | `bfa67e2` | only cap decoder threads when HW decode active | `media_processing.py`, `test_media_processing.py` | A→D | added | Was Cat A (existing thread-cap test didn't assert HW-vs-SW branch); fix added the missing matrix cell. |
| 2026-04-07 | `2d29fe9` | day-of-week offset in cron (APScheduler 0=Mon) | `app.js` | **C** | **none** | **Pure JS day arithmetic.** No `test_static_app_js.py` coverage. Schedules fired one day late for users for an unknown duration. |
| 2026-04-05 | `554da7f` | Windows backslashes in path mapping | `config.py`, `webhooks.py`, `test_config.py` | A→D | added | Existing path tests used POSIX paths only. Fix added Windows-shape inputs. |
| 2026-04-03 | `e31d051` | trigger Plex rescan for `skipped_file_not_found` | `job_runner.py` | **C** | **none** | The retry-on-stale-path branch has *no test*. `job_runner._start_job_async` is the largest function in the codebase and has near-zero direct coverage — only journey tests exercise it tangentially. |
| 2026-04-03 | `7a721a6` | Sportarr flat payload extraction | `webhooks.py` | **C** | **none** | `_extract_sonarr_file_path` has tests for nested shapes only. No matrix cell for `filePath`/`eventTitle` flat shape. |
| 2026-04-03 | `0346b1f` | loguru `%s`→`{}` format strings | `webhooks.py` | B | none | Loguru produces literal `%s` in log output. Logging format-string assertions don't exist anywhere. |
| 2026-04-03 | `51075c9` | CORS + harden webhook auth | `auth.py`, `webhooks.py`, `app.py` | B | partial | Token validation tested; *header precedence* (X-Auth-Token vs Authorization Bearer) not asserted. |
| 2026-04-02 | `197df73`/`d481eb6` | persist webhook history to disk | `webhooks.py` | C | none | Two identical commits = first one shipped without a regression test. History file load/save not unit-tested. |
| 2026-03-31 | `9aee608` | prevent duplicate job starts + reconcile pool | `processing.py`, `job_runner.py`, `test_job_dispatcher.py` | D | yes | Added `TestInflightJobGuard` + `TestPoolReconciliationOnDispatch`. Healthy. |
| 2026-03-31 | `ab979a8` | defer busy worker removal during reconcile | `worker.py`, `app.js`, `test_worker.py` | D | yes | Per-worker `_pending_removal` flag tested. |
| 2026-03-28 | `5939e0d` | resume interrupted jobs after restart | `app.py`, `jobs.py`, `api_jobs.py`, `test_app.py`, `test_jobs.py` | A→D | added | Three subtle bugs (`created_at` vs `started_at`, paused-flag survival, reprocess flag clear). Tests existed for `requeue_interrupted_jobs` but didn't assert the timestamp field used. |
| 2026-03-28 | `357c442` | parse webhook JSON regardless of Content-Type | `webhooks.py`, `app.py` | B | none | `request.get_json(force=True)` switch. No test sends a webhook with wrong Content-Type. |
| 2026-03-22 | `0a74c5b` | use `ratingKey` not `key` for episode lookup | `api_bif.py` | **C** | **none** | `bif_search` show-hub branch is an HTTP integration that mocks Plex JSON. The `key` value used in tests happened to look like `/library/metadata/N` (no `/children`) — masking the bug entirely. |
| 2026-03-22 | `eb26ea0` | propagate `cancel_check` to FFmpeg workers | `job_dispatcher.py`, `media_processing.py`, `worker.py`, 4 test files | D | yes | Threaded through whole call chain with new `CancellationError`. Healthy. |
| 2026-03-21 | `31cd4a0` | progress + worker status during first file | `job_dispatcher.py`, `processing.py`, `app.js`, `test_job_dispatcher.py` | D | yes (`_get_in_progress_fraction`) | Cross-referenced with `REGRESSION_VERIFIED.md` row 8 — verified. |

---

## 3. Recommendations

### Top 3 areas where adding tests would prevent the most bugs

1. **`media_preview_generator/web/routes/job_runner.py::_start_job_async`** — ~700-line function, currently exercised only via journey tests. Three of the 32 sampled fixes touched it (`9aee608`, `ab979a8`, `e31d051`, `5939e0d`). Direct unit tests should cover:
   - `unresolved_paths` *and* `not_found_on_disk` retry combinations (`e31d051`)
   - `_inflight_jobs` race when resume + auto-resume + revive land within ms (`9aee608`)
   - `_on_pool_available` callback when settings change between job creation and dispatch (`9aee608`)
   - `processing_paused` clearing during requeue (`5939e0d`)

2. **`media_preview_generator/web/static/js/app.js` — schedules cron arithmetic and progress replay.** The day-of-week offset bug (`2d29fe9`) shipped because there is no JS unit test file for `describeSchedule`, `showEditScheduleModal`, `saveSchedule`. Recommend a JSDOM/Vitest harness or, at minimum, a Playwright e2e in `tests/e2e/test_schedule_modal.py` that creates a schedule for "Monday only" and reads back the cron string. Same harness would have caught the `_pendingProgress` replay bug (`31cd4a0`).

3. **`media_preview_generator/web/webhooks.py` payload-shape matrix.** The Sportarr fix (`7a721a6`), Plex title fix (`9356649`), and Content-Type fix (`357c442`) all share root cause: `_extract_*_file_path` and `_handle_sonarr_compatible_webhook` are tested per-vendor with a single canonical payload shape. Add a parametrized matrix in `test_webhooks.py` covering: nested-path, root-path, `filePath`-flat, missing-Content-Type, `Authorization Bearer` shadow, blank-title fallback. One table-driven test class per webhook source.

### Top 3 existing tests that should be strengthened

1. **`tests/test_webhooks.py` auth helpers** — `_check_token_headers` precedence is not asserted. Add a test that sends *both* a valid `X-Auth-Token` and an invalid `Authorization: Bearer xxx` and asserts the request is accepted (current code prefers X-Auth-Token; a regression that swapped them would not fail any test). Reference: `media_preview_generator/web/auth.py:_check_token_headers` (currently lines ~211-228).

2. **`tests/test_gpu_detection_extended.py` Vulkan probe strategies** — every strategy is currently tested by mocking `subprocess.run` with a known return string. The bugs (`f7c55e1`, `7e8d0ca`) were about *which env var triggers a real driver to wake up*. The mock-based tests pass for any well-formed call ordering. Strengthen by asserting the *exact env-var keys passed to subprocess.run*: `__EGL_VENDOR_LIBRARY_FILENAMES`, `VK_DRIVER_FILES`, `VK_LOADER_DEBUG`. A test that swaps which strategy sets which env var would still pass today; it shouldn't.

3. **`tests/test_app.py::test_requeue_interrupted_jobs_*`** — the existing tests assert that requeue happens. They didn't assert *which timestamp field was checked* against `max_age_minutes`. The fix changed `created_at` → `started_at` (`5939e0d`); a regression that swapped it back would not fail any test. Add an assertion: a job created 10 hours ago but `started_at` 1 minute ago must be requeued (and vice versa). This is the exact "covers the matrix, not one cell" pattern from `.claude/rules/testing.md`.

### Top 3 bug classes that need a different testing strategy

1. **DV / HDR tone-mapping correctness (Category E, 9 recurring fixes)** — unit tests that assert filter strings cannot catch VK_ERROR_OUT_OF_DEVICE_MEMORY (`70cba4e`), pure-green overlays (`a06ed98`), or RPU side-data drops. **Strategy:** add a `@pytest.mark.gpu` integration that runs FFmpeg against a small DV5 sample, extracts a frame, and asserts a PSNR window vs. a golden reference (the commit body of `70cba4e` already cites "PSNR 45 dB vs the old chain"). Without this, every DV5 fix carries unbounded regression risk.

2. **CSS / theme drift across templates (Category E, 4 fixes — `3866a0a`, `8953d4b`, `0b399f2`, `f30f67b`)** — `test_theme_toggle.py` tests the toggle but not per-page rendering. **Strategy:** add a Playwright visual-regression checkpoint per major page (`/`, `/settings`, `/webhooks`, `/schedules`, `/logs`, `/bif`) in both themes. The "dark island on light page" class of bug only shows up visually. The repository already has `tests/VISUAL_REGRESSION_CHECKLIST.md` — operationalize it.

3. **Frontend JS pure logic (Category C, schedule cron + progress replay + path normalization in modals)** — these are *pure functions* (`describeSchedule`, etc.) but live in a 5000+ line `app.js` with no JS test runner. **Strategy:** either factor pure helpers into a separate ESM module and add Vitest, or — lower-cost — add `tests/e2e/test_schedule_modal.py` that round-trips schedule creation through the UI and asserts the resulting cron string. Same Playwright harness would have caught `2d29fe9` and `31cd4a0`.
