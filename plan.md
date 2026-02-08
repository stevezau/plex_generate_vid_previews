# Plan: Comprehensive Code Review & Hardening

This plan addresses all 36 findings across security, code quality, error handling, configuration, dependencies, testing, Docker, web app security, concurrency, and logging. Since this is Docker-only and breaking changes are acceptable, we'll bump the minimum Python to 3.9+, tighten defaults aggressively, and restructure where needed. The work is grouped into 6 phases, ordered by impact — security-critical fixes first, then structural improvements, then polish.

---

## Status Tracker

> **Last updated**: Session 4 — Phase D complete

| Status | Count | Items |
|--------|-------|-------|
| **DONE** | 35 | 1-17, 19-27, 28-36 |
| **NOT DONE** | 1 | 18 (dead code already removed — no further action needed) |

### Session 1 (completed)
- Applied Phases 1-2 fully (Items 1-12)
- Applied Phase 3 partially (Items 14-16, 18, 20)
- Applied Phase 4 partially (Items 22-25)
- Hit context window limit

### Session 2 — Phase A Quick Fixes (completed)
Items completed:
- **Item 1 (finish)**: Removed stale `WEB_HIDE_TOKEN` from docker-compose.example.yml (2 occurrences), docs/configuration.md, docs/web-interface.md. Updated CORS default docs from `*` to `localhost`.
- **Item 18 (fix)**: The previous session removed the `http.client` and `xml.etree.ElementTree` imports but left them referenced in the `except` clause at media_processing.py L801 — a latent **NameError bug**. Fixed by collapsing the redundant `except (Exception, http.client.BadStatusLine, xml.etree.ElementTree.ParseError)` into a single `except Exception` (the removed types were already subclasses or handled by retry_plex_call).
- **Item 19 (finish)**: Added `threading.Lock` to `get_schedule_manager()` (scheduler.py) and `get_job_manager()` (jobs.py). settings_manager.py was already done in Session 1.
- **Item 25 (fix)**: Replaced static `os.environ.get('HTTP_X_FORWARDED_PROTO')` check with Werkzeug's `ProxyFix` middleware — correctly translates `X-Forwarded-Proto` header at request time.
- **Item 28 (critical)**: Added `flask-wtf>=1.2.0` to pyproject.toml — was missing despite code importing it since Session 1 CSRF changes.
- **Item 29**: Removed unused `psutil` from dependencies — confirmed no imports anywhere in codebase.
- **Item 30**: Moved `load_dotenv()` from module level into `load_config()` in config.py.

### Known Issues / Regressions
- **None identified** — all changes are additive or removal of dead code/unused deps.
- **Risk**: Removing `psutil` is safe only if no plugin or user script depends on it. The core codebase does not import it.
- **Risk**: `load_dotenv()` move means any code that reads env vars before calling `load_config()` won't see `.env` values. This is intentional — only `load_config()` should gate config loading.

### Session 3 — Phase B (completed)
Items completed:
- **Item 13**: Extracted shared processing loop into `_process_items_loop()` private method in worker.py. Both `process_items()` (Rich UI) and `process_items_headless()` (web/background) are now thin wrappers using callbacks (`on_task_complete`, `on_poll`, `on_finish`).
- **Item 21**: Added SRI integrity attributes and `crossorigin="anonymous"` to all 6 CDN references across base.html (4 tags) and login.html (2 tags). Used sha384 hashes for Bootstrap 5.3.2 (well-known) and sha256 hashes from jsdelivr data API for bootstrap-icons@1.11.1 and socket.io-client@4.7.2.

### Session 3 — Phase C: Docker (completed)
Items completed:
- **Item 26**: Converted Dockerfile to multi-stage build. Stage 1 (builder) installs gcc/musl-dev and builds pre-compiled wheels with `pip3 wheel`. Stage 2 (runtime) installs only runtime deps (GPU drivers, python3, mediainfo, etc.) and copies wheels from builder. Removed `software-properties-common` (unused). This eliminates ~100MB+ of compiler toolchain from image layer history.
- **Item 27**: Replaced hardcoded `curl -f http://localhost:8080/api/health` healthcheck with `python3 -c` using stdlib `urllib.request`. Now respects `WEB_PORT` env var via `os.environ.get('WEB_PORT', '8080')`. Removes dependency on curl binary.

### Session 4 — Phase D: Testing & Logging (completed)
Items completed:
- **Item 31**: Created `tests/test_routes.py` — 30+ tests covering login flow, auth API (Bearer, X-Auth-Token, session), settings CRUD, job management (create, cancel, delete, logs, stats, workers), setup wizard (status, state, complete, set-token, validate-paths), schedules API, system status, health check, and path validation (null-byte rejection).
- **Item 32**: Created `tests/test_worker_concurrency.py` — 20+ tests covering Worker lifecycle (available→busy→complete), WorkerPool init (GPU round-robin, CPU-only), headless processing with real threads (all-success, all-fail, mixed), CPU fallback queue (codec error re-queue), graceful shutdown, thread-safe progress updates under contention, and worker callback in headless mode.
- **Item 35**: Created `tests/test_socketio.py` — tests covering authenticated/unauthenticated SocketIO connections, auth rejection on connect, subscribe/unsubscribe room management, and job lifecycle event emission (job_created, job_updated, job_progress) via flask-socketio's test client.
- **Item 36**: Enhanced `logging_config.py` with `LOG_FORMAT=json` option. Added `_json_sink` that writes one JSON object per log line to stderr with fields: timestamp, level, message, logger, function, line, module, exception. Default remains Rich/coloured `"pretty"` format. Supports both `log_format` kwarg and `LOG_FORMAT` env var. Added 7 tests to `test_logging_config.py` covering JSON sink registration, env var fallback, valid JSON output, and exception serialisation.

### Recommendations
- **All 36 items addressed** — only Item 18 was a no-op (dead code already removed prior).
- **Pin upper bounds (Item 28 remainder)**: Adding `<4`, `<25`, `<3` caps to flask/gevent/sqlalchemy should be done carefully — verify compatibility first.

---

## Phase 1 — Critical & High Security Fixes (highest priority)

1. ✅ **Stop logging auth tokens** — In `plex_generate_previews/web/auth.py`, remove or mask the token in the `logger.info` calls at `generate_token()`, `regenerate_token()`, and `log_token_on_startup()`. Replace with `logger.info("Authentication token generated (hidden)")` or log only the last 4 chars. Remove the `WEB_HIDE_TOKEN` toggle — tokens should *never* be logged in full.

2. ✅ **Restrict file permissions on `auth.json` and `settings.json`** — In `plex_generate_previews/web/auth.py` `save_auth_config()` and `plex_generate_previews/web/settings_manager.py`, add `os.chmod(path, 0o600)` after writing, matching the pattern already used for `flask_secret.key` in `plex_generate_previews/web/app.py`.

3. ✅ **Stop exposing tokens in API responses** — In `plex_generate_previews/web/routes.py`, remove `auth_token` from the `check_plex_pin` response and `accessToken` from `get_plex_servers` response. Store the token server-side in the session or settings instead of returning it to the browser. Update `get_token_info()` to return a masked version.

4. ✅ **Add CSRF protection** — Install `flask-wtf`, add `CSRFProtect(app)` in `plex_generate_previews/web/app.py`. Add `{{ csrf_token() }}` to all form templates (`login.html`, `settings.html`, `setup.html`). For AJAX calls in `app.js`, include the CSRF token header from a meta tag.

5. ✅ **Lock down CORS defaults** — In `plex_generate_previews/web/app.py` `get_cors_origins()`, change the default from `'*'` to the app's own origin (e.g., `http://localhost:{port}`). Users who need broader access can set `CORS_ORIGINS` explicitly.

6. ✅ **Re-enable SSL verification for Plex connections** — In `plex_generate_previews/plex_client.py`, remove `urllib3.disable_warnings()` and `session.verify = False`. Add a `PLEX_VERIFY_SSL` config option (default `True`), allowing users to opt out for self-signed certs. Log a warning if SSL verification is disabled.

7. ✅ **Avoid logging Plex token in error tracebacks** — In `plex_generate_previews/media_processing.py`, sanitize `e.request.headers` before logging by stripping `X-Plex-Token` from the output.

---

## Phase 2 — Error Handling & Robustness

8. ✅ **Replace bare `except:` with `except Exception:`** — In `plex_generate_previews/cli.py` at the two bare except clauses, narrow to `except Exception:` to avoid catching `SystemExit` and `KeyboardInterrupt`.

9. ✅ **Fix file handle leak in `_run_ffmpeg`** — In `plex_generate_previews/media_processing.py`, refactor to use a context manager (`with open(...) as f:`) for the stderr output file. Wrap the entire FFmpeg subprocess block in `try/finally` to ensure `os.remove(output_file)` runs on any exception.

10. ✅ **Expand `retry_plex_call` to handle transient network errors** — In `plex_generate_previews/plex_client.py`, add `ConnectionError`, `TimeoutError`, `requests.exceptions.ConnectionError`, and `requests.exceptions.Timeout` to the retry conditions. Keep the existing XML parse error retry.

11. ✅ **Log a warning when error log file creation fails** — In `plex_generate_previews/logging_config.py`, replace the silent `pass` in the `except (PermissionError, OSError)` block with `logger.warning("Could not create error log file: {e}")`.

12. ✅ **Add exception chaining (`from e`)** — Audit all `raise ...` inside `except` blocks in `config.py`, `media_processing.py`, and `plex_client.py`. Add `from e` to preserve traceback context.

---

## Phase 3 — Code Quality & Architecture

13. ✅ **Refactor `process_items` / `process_items_headless` duplication** — In `plex_generate_previews/worker.py`, extract shared logic (task assignment, exit-condition checking, worker management) into a private `_process_items_common()` method. Both public methods become thin wrappers that pass in a progress-reporting strategy (Rich console vs. headless logging).

14. ✅ **Consolidate `get_config_value` functions** — In `plex_generate_previews/config.py`, merge the module-level `get_config_value*` helpers and the local `get_value()` inside `load_config()` into a single unified function that accepts an optional `ui_settings` dict.

15. ✅ **Replace `list.pop(0)` with `collections.deque`** — In `plex_generate_previews/worker.py`, change `media_queue` from a `list` to a `deque` for O(1) popleft operations.

16. ✅ **Fix `_check_fallback_queue_empty` race condition** — In `plex_generate_previews/worker.py`, replace the get/put peek pattern with `queue.qsize() > 0` (acceptable for advisory checks) or use a dedicated flag protected by the existing lock.

17. ✅ **Add missing type hints** — Add return type annotations to `_run_ffmpeg` in `media_processing.py` and audit all public functions across the codebase for missing parameter/return type hints.

18. ✅ **Remove unused imports** — Remove `http.client` and `xml.etree.ElementTree` from the top of `media_processing.py` if they're not needed at module scope.

19. ✅ **Add thread safety to singleton initialization** — In `plex_generate_previews/web/settings_manager.py`, `scheduler.py`, and `jobs.py`, wrap the module-level singleton getters with `threading.Lock` guards.

20. ✅ **Bump `requires-python` to `>=3.9`** — In `pyproject.toml`, change the minimum to 3.9 (matches actual syntax usage and Docker base image). This is a breaking change — document in CHANGELOG.

---

## Phase 4 — Web Application Hardening

21. ✅ **Add Subresource Integrity (SRI) hashes to CDN assets** — In `plex_generate_previews/web/templates/base.html`, add `integrity="sha384-..."` and `crossorigin="anonymous"` attributes to all `<link>` and `<script>` tags loading from `cdn.jsdelivr.net`.

22. ✅ **Reduce session lifetime and add rotation** — In `plex_generate_previews/web/app.py`, reduce `PERMANENT_SESSION_LIFETIME` from 30 days to 7 days. Add session regeneration after successful login in the login route.

23. ✅ **Strengthen SocketIO auth** — In `plex_generate_previews/web/routes.py`, ensure the SocketIO `connect` handler explicitly calls `disconnect()` on auth failure. Add auth checks to `subscribe`/`unsubscribe` handlers.

24. ✅ **Fix `before_request` setup bypass** — In `plex_generate_previews/web/app.py`, narrow the API endpoint bypass to only the specific setup-related routes (e.g., `api.get_token_info`, `api.save_setup`) rather than all `api.*` endpoints.

25. ✅ **Default `SESSION_COOKIE_SECURE` based on forwarded headers** — In `plex_generate_previews/web/app.py`, also check for `X-Forwarded-Proto: https` (common behind reverse proxies) in addition to the `HTTPS` env var.

---

## Phase 5 — Docker & Dependencies

26. ✅ **Switch to multi-stage Docker build** — Refactor `Dockerfile` to use a builder stage for `pip install` with `gcc`/`musl-dev`, then copy only the installed packages into the final stage. This reduces attack surface and image size.

27. ✅ **Fix healthcheck port** — In `Dockerfile`, change the `HEALTHCHECK` to use `${WEB_PORT:-8080}` or use `wget` (already in Alpine) instead of `curl` to reduce installed packages.

28. ✅ **Pin all dependency ranges** — In `pyproject.toml`, add upper bounds to currently unbounded dependencies: `flask>=3.0.0,<4`, `gevent>=23.0.0,<25`, `sqlalchemy>=2.0.0,<3`, etc. Add `flask-wtf` as a new dependency (for CSRF).

29. ✅ **Remove `psutil` if unused** — Verify no code path imports `psutil`; if confirmed unused, remove from `pyproject.toml` dependencies.

30. ✅ **Stop `load_dotenv()` at import time** — In `plex_generate_previews/config.py`, move `load_dotenv()` inside `load_config()` so it only runs when explicitly called, not on module import.

---

## Phase 6 — Testing & Logging

31. ✅ **Add web route unit tests** — Create `tests/test_routes.py` covering: login flow, settings CRUD, job management, setup wizard, token endpoints. Use Flask's test client.

32. ✅ **Add concurrency tests** — Create `tests/test_worker_concurrency.py` with tests for: worker pool startup/shutdown, task assignment under load, fallback queue behavior, graceful cancellation.

33. ✅ **Replace `MagicMock` config with real `Config` dataclass** — In `tests/conftest.py`, create `mock_config` from the actual `Config` dataclass with test defaults rather than `MagicMock`. This catches attribute typos at test time.

34. ✅ **Add missing test fixture (`sample.jpg`)** — Create a minimal JPEG file at `tests/fixtures/sample.jpg` for BIF generation tests.

35. ✅ **Add SocketIO integration tests** — In `tests/e2e/` or a new `tests/test_socketio.py`, test event emission, auth rejection, and channel subscription.

36. ✅ **Switch to structured (JSON) logging option** — In `plex_generate_previews/logging_config.py`, add a `LOG_FORMAT=json` option for production deployments. Keep the current Rich console format as default.

---

## Verification

- Run `ruff check . && ruff format --check .` to validate style compliance
- Run `pytest --cov=plex_generate_previews --cov-fail-under=70` to validate test coverage maintained
- Run `docker build -t plex-previews:test .` to validate Docker build
- Manually test the web UI login flow, settings save, and preview generation
- Run `bandit -r plex_generate_previews/` for a security-focused static analysis pass
- Verify no tokens appear in logs at any level with `grep -ri "token" /tmp/*.log`

---

## Decisions

- **Breaking change accepted**: Bumping Python to 3.9+, changing API response shapes, tightening CORS to non-`*` default, enabling SSL verification by default
- **Phased execution**: Phases 1-2 are the security-critical path and should be done first; Phases 3-6 can be parallelized
- **CSRF via `flask-wtf`**: Chosen over manual token implementation for maintainability and broad Flask ecosystem support
- **Docker-first perspective**: Docker hardening (Phase 5) takes priority over standalone install concerns; healthcheck and multi-stage build targeted
