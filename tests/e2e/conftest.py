"""Playwright E2E test configuration and fixtures.

Two app subprocesses are exposed:

* ``app_url`` — session-scoped, used by every test that needs the app
  in its "setup complete" state (dashboard, settings, servers, etc.).
  An autouse fixture for those tests POSTs ``/api/setup/complete`` once.
  Wizard tests do NOT use this fixture.
* ``app_url_wizard`` — session-scoped, separate CONFIG_DIR, never
  marked complete. Used exclusively by ``tests/e2e/test_wizard_*``.
  Stays at first-run state because every writeable endpoint the
  wizard hits is mocked client-side via ``page.route()`` — the
  Python-side ``setup_complete`` flag never flips.

Auth bypass: both subprocesses run with ``WEB_AUTH_TOKEN=e2e-test-token``;
each session captures the Flask session cookie via a real form POST and
replays it into Playwright contexts via the ``session_cookie`` /
``session_cookie_wizard`` fixtures.
"""

from __future__ import annotations

import http.cookiejar
import os
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from collections.abc import Generator
from urllib.parse import urlparse

import pytest
from playwright.sync_api import BrowserContext, Page

# ---------------------------------------------------------------------------
# Subprocess plumbing
# ---------------------------------------------------------------------------


def wait_for_port(port: int, timeout: float = 10.0) -> bool:
    """Wait for a TCP port to become connectable."""
    start = time.time()
    while time.time() - start < timeout:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("localhost", port)) == 0:
                return True
        time.sleep(0.1)
    return False


def get_free_port() -> int:
    """Allocate an ephemeral localhost port for isolated E2E runs."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return int(s.getsockname()[1])


def _start_app(config_dir: str, port: int, extra_env: dict | None = None) -> subprocess.Popen:
    env = {
        **os.environ,
        "WEB_PORT": str(port),
        "CONFIG_DIR": config_dir,
        "WEB_AUTH_TOKEN": "e2e-test-token",
        # Activates the ``/api/__test/reset`` endpoint inside the Flask
        # subprocess. The endpoint is the load-bearing piece that lets
        # the session-scoped wizard fixture (below) reset all in-memory
        # + on-disk state between tests without spawning a fresh
        # Flask subprocess each time. Production builds never set this
        # env var, so the endpoint module is never imported.
        "MPG_TEST_RESET": "1",
    }
    if extra_env:
        env.update(extra_env)
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            f"from media_preview_generator.web.app import run_server; run_server(host='0.0.0.0', port={port})",
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    # 60s (was 20s): under high parallelism (xdist with many workers)
    # the OS scheduler can't give every concurrent Flask boot enough
    # CPU to finish in 20s. Flask boot involves GPU detection +
    # JobManager (reads jobs.db) + APScheduler (SQLite jobstore) +
    # SocketIO + module imports — ~2-3s wall on idle, but contention
    # serialises chunks of it. 60s is enough headroom for 32 workers
    # concurrently on a beefy local box (verified) while still
    # failing fast on real boot bugs (anything >60s is genuinely
    # stuck, not just slow).
    if not wait_for_port(port, timeout=60):
        stdout, stderr = proc.communicate(timeout=5)
        proc.kill()
        raise RuntimeError(f"App failed to start on port {port}.\nstdout: {stdout.decode()}\nstderr: {stderr.decode()}")
    return proc


@pytest.fixture(scope="session")
def app_url(tmp_path_factory) -> Generator[str, None, None]:
    """Main app (used by non-wizard tests)."""
    config_dir = tmp_path_factory.mktemp("config_main")
    port = get_free_port()
    proc = _start_app(str(config_dir), port)
    try:
        yield f"http://localhost:{port}"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.fixture(scope="session")
def app_url_wizard(tmp_path_factory, worker_id) -> Generator[str, None, None]:
    """Session-scoped Flask subprocess for wizard tests, per xdist worker.

    Standard pytest-xdist pattern: each worker gets ONE long-lived
    Flask subprocess that all the worker's wizard tests share.
    Without xdist, ``worker_id`` is "master"; under xdist it's
    "gw0", "gw1", ... so concurrent workers don't share state.

    State isolation between tests on the same worker comes from the
    ``_reset_wizard_state`` autouse fixture below, which POSTs to
    ``/api/__test/reset`` before each wizard test. That endpoint
    (registered only when ``MPG_TEST_RESET=1`` — see ``_start_app``)
    stops the scheduler, cancels timers, nukes all four global
    singletons, and deletes on-disk state files. Net effect: each
    test starts against a clean Flask, but with zero subprocess
    boot cost.

    Pre-fix: function-scoped — ~120 wizard tests × ~2s boot = 4min
    of pure overhead, plus subprocess churn that destabilised
    ``-n auto`` on beefy local boxes (73+ ERRORs from ``wait_for_port``
    contention).
    """
    config_dir = tmp_path_factory.mktemp(f"config_wizard_{worker_id}")
    port = get_free_port()
    proc = _start_app(str(config_dir), port)
    try:
        yield f"http://localhost:{port}"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.fixture(autouse=True)
def _reset_wizard_state(request) -> Generator[None, None, None]:
    """POST to ``/api/__test/reset`` before each wizard test.

    Calls the test-only endpoint registered when ``MPG_TEST_RESET=1``
    (see ``api_test.py``). The endpoint nukes all global singletons
    (JobManager, ScheduleManager, SettingsManager, JobGate) and
    deletes on-disk state files (jobs.db, settings.json,
    setup_state.json, etc.). Next request on Flask side re-initialises
    everything from defaults — equivalent to a fresh subprocess but
    without the 2s boot.

    No-op for tests that don't use ``app_url_wizard``. The autouse
    is fine because:
      * The fixturenames check makes this a zero-cost no-op for
        non-wizard tests.
      * For wizard tests it's a single ~50ms HTTP POST.
    """
    if "app_url_wizard" not in request.fixturenames:
        yield
        return
    url = request.getfixturevalue("app_url_wizard")
    try:
        req = urllib.request.Request(
            f"{url}/api/__test/reset",
            method="POST",
            headers={"X-Auth-Token": "e2e-test-token"},
        )
        # 10s timeout — the reset is usually <100ms but allow slack
        # under heavy parallelism. If it ever blocks longer than 10s
        # the next test will fail loudly rather than silently sharing
        # state.
        urllib.request.urlopen(req, timeout=10).close()  # noqa: S310
    except Exception:
        # Best-effort: a 401 (auth changed) or 403 (env var missing)
        # falls through to a normal test run which may flake. Tests
        # that depend on pristine state will fail visibly; tests
        # that don't won't notice.
        pass
    yield


# ---------------------------------------------------------------------------
# Auth bypass (real /login form POST → captured Flask session cookie)
# ---------------------------------------------------------------------------


@pytest.fixture
def auth_token() -> str:
    return "e2e-test-token"


def _capture_session_cookie(target_url: str) -> dict:
    """POST a real login + return the Flask session cookie as a Playwright dict."""
    cookie_jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookie_jar))
    data = urllib.parse.urlencode({"token": "e2e-test-token"}).encode()
    parsed = urlparse(target_url)
    req = urllib.request.Request(
        f"{target_url}/login",
        data=data,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    opener.open(req, timeout=10)  # noqa: S310 (test-only localhost)
    for c in cookie_jar:
        if c.name == "session":
            return {
                "name": "session",
                "value": c.value,
                "domain": parsed.hostname or "localhost",
                "path": "/",
                "httpOnly": True,
                "secure": False,
                "sameSite": "Lax",
            }
    raise RuntimeError("Login form returned no session cookie — did POST /login fail?")


@pytest.fixture(scope="session")
def session_cookie(app_url: str) -> dict:
    return _capture_session_cookie(app_url)


@pytest.fixture(scope="session")
def session_cookie_wizard(app_url_wizard: str) -> dict:
    """Session-scoped — matches ``app_url_wizard``'s scope.

    Function-scoped here would POST /login per test, and Flask-Limiter's
    rate limit on /login fires after ~5 logins in a short window
    (HTTP 429). The Flask session cookie is signed with the secret key
    (which doesn't change across resets) so a single capture remains
    valid for the whole session.
    """
    return _capture_session_cookie(app_url_wizard)


@pytest.fixture(scope="session")
def complete_setup(app_url: str) -> None:
    """Mark setup complete on the main app. Use in non-wizard tests via
    ``@pytest.fixture(autouse=True)`` at module level, or as a direct
    fixture dep.
    """
    req = urllib.request.Request(
        f"{app_url}/api/setup/complete",
        method="POST",
        headers={"X-Auth-Token": "e2e-test-token", "Content-Type": "application/json"},
        data=b"{}",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310 (test-only localhost)
        assert resp.status == 200


@pytest.fixture
def authed_page(page: Page, context: BrowserContext, app_url: str, session_cookie: dict) -> Page:
    """Page on the main app with the session cookie injected. Caller `page.goto(...)` to navigate."""
    context.add_cookies([session_cookie])
    return page


@pytest.fixture
def wizard_page(page: Page, context: BrowserContext, app_url_wizard: str, session_cookie_wizard: dict) -> Page:
    """Page on the wizard app with session cookie injected.

    Auto-mocks GET+POST /api/setup/state so a previous test's walk
    (which actually persists `current_step` to setup_state.json) doesn't
    auto-resume mid-wizard in the next test. Caller `page.goto(app_url_wizard + '/setup')`.
    """
    from ._mocks import mock_setup_state

    context.add_cookies([session_cookie_wizard])
    mock_setup_state(page, current_step=1)
    return page


# ---------------------------------------------------------------------------
# UI helpers
# ---------------------------------------------------------------------------


def accept_app_confirm(page: Page, timeout: int = 3000) -> None:
    """Click the in-app ``appConfirm`` Bootstrap modal's OK button.

    The app uses a custom modal (``#appConfirmModal``) instead of the
    native ``window.confirm()`` dialog, so ``page.on('dialog', ...)`` is
    a no-op for those flows. Tests that trigger a confirm-required
    action must call this helper after the click that opens the modal.
    """
    btn = page.locator("#appConfirmModalOkBtn")
    btn.wait_for(state="visible", timeout=timeout)
    btn.click()


# ---------------------------------------------------------------------------
# Browser config
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args):
    return {
        **browser_context_args,
        "viewport": {"width": 1280, "height": 720},
        "ignore_https_errors": True,
    }


# ---------------------------------------------------------------------------
# Backend-real fixtures
# ---------------------------------------------------------------------------
#
# Unlike ``app_url`` (which is paired with ``page.route()`` mocks at the
# HTTP boundary), these fixtures stand up a Flask subprocess where:
#
#   * GET /api/* responses come from the real Flask routes
#   * SocketIO events are emitted by the real JobManager
#   * Real APScheduler is running in-process
#   * Real WorkerPool runs jobs (FFmpeg shimmed via PATH override)
#
# Tests should NOT register ``page.route()`` mocks against API endpoints
# they care about — the whole point is to drive the real backend.


def _seed_settings_complete(config_dir: str, settings_overrides: dict | None = None) -> None:
    """Pre-write a settings.json that marks setup complete.
    Saves the subprocess from having to walk the wizard before each test.
    """
    import json
    from pathlib import Path

    settings = {
        "setup_complete": True,
        "cpu_threads": 0,
        "gpu_threads": 0,
        "thumbnail_interval": 5,
        "thumbnail_quality": 4,
        "tonemap_algorithm": "hable",
        "log_level": "INFO",
        "log_rotation_size": "10 MB",
        "log_retention_count": 5,
        "job_history_days": 30,
        "gpu_config": [],
        "path_mappings": [],
        "exclude_paths": [],
        "media_servers": [],
        "plex_verify_ssl": True,
        "auto_requeue_on_restart": False,
        "webhook_delay": 1,
    }
    if settings_overrides:
        settings.update(settings_overrides)
    Path(config_dir).mkdir(parents=True, exist_ok=True)
    (Path(config_dir) / "settings.json").write_text(json.dumps(settings))


def _build_fake_ffmpeg_path(tmp_dir: str) -> str:
    """Create a directory with a fake ``ffmpeg`` + ``ffprobe`` shim.

    The shim is a no-op shell script that prints minimal stderr so the
    FFmpeg progress parser doesn't choke. Tests that hit code paths
    actually invoking FFmpeg should override the script per-test.
    """
    from pathlib import Path

    bin_dir = Path(tmp_dir) / "fake_bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    for name in ("ffmpeg", "ffprobe"):
        script = bin_dir / name
        script.write_text("#!/bin/sh\nexit 0\n")
        script.chmod(0o755)
    return str(bin_dir)


@pytest.fixture
def backend_real_app(tmp_path_factory, request) -> Generator[tuple[str, str], None, None]:
    """Function-scoped Flask subprocess with NO ``page.route()`` defaults.

    Yields ``(app_url, config_dir)``. The subprocess runs against a
    pre-seeded settings.json so the dashboard loads in setup-complete
    mode without any wizard walk. PATH is rewritten to point at a fake
    FFmpeg/ffprobe shim — the real subprocess.run calls succeed
    instantly without doing any video work.

    Function-scoped so each test gets a clean job DB / settings file.
    Boot cost is ~3s; acceptable for backend-real tests where the whole
    point is exercising real wiring.

    Tests can pass ``request.param`` as a dict of settings overrides
    via parametrize; default seeds an empty media_servers list. A
    reserved ``_extra_env`` key (popped before seeding) is merged into
    the subprocess environment — used by tests that need to spoof env
    vars the app reads at boot (e.g. ``DOCKER_IMAGE_NAME``).
    """
    config_dir = tmp_path_factory.mktemp("backend_real_config")
    raw_overrides = dict(getattr(request, "param", None) or {})
    extra_env_overrides = raw_overrides.pop("_extra_env", {}) or {}
    _seed_settings_complete(str(config_dir), raw_overrides)

    fake_bin = _build_fake_ffmpeg_path(str(config_dir))
    extra_env = {
        # Put fake FFmpeg first on PATH so any subprocess.run("ffmpeg")
        # call hits the no-op shim instead of a real (or missing) binary.
        "PATH": fake_bin + os.pathsep + os.environ.get("PATH", ""),
        # Force CORS to a known value so SocketIO origin checks pass.
        "CORS_ORIGINS": "*",
        **extra_env_overrides,
    }
    port = get_free_port()
    proc = _start_app(str(config_dir), port, extra_env=extra_env)
    try:
        yield (f"http://localhost:{port}", str(config_dir))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.fixture
def backend_real_session_cookie(backend_real_app: tuple[str, str]) -> dict:
    """Captured Flask session cookie for the backend-real app subprocess."""
    return _capture_session_cookie(backend_real_app[0])


@pytest.fixture
def backend_real_page(
    page: Page,
    context: BrowserContext,
    backend_real_app: tuple[str, str],
    backend_real_session_cookie: dict,
) -> Page:
    """Authenticated page for the backend-real app, no API mocks installed."""
    context.add_cookies([backend_real_session_cookie])
    return page
