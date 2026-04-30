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


def _start_app(config_dir: str, port: int) -> subprocess.Popen:
    env = {
        **os.environ,
        "WEB_PORT": str(port),
        "CONFIG_DIR": config_dir,
        "WEB_AUTH_TOKEN": "e2e-test-token",
    }
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
    if not wait_for_port(port, timeout=20):
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


@pytest.fixture
def app_url_wizard(tmp_path_factory) -> Generator[str, None, None]:
    """Per-test app instance for wizard tests.

    Function-scoped so each wizard test gets a pristine first-run
    state — even though the wizard tests mock every writeable endpoint
    client-side, real /api/setup/state writes from previous walks would
    otherwise leak between tests via the persisted setup_state.json
    and auto-resume mid-wizard. Boot cost is ~2s; acceptable for the
    handful of wizard tests.
    """
    config_dir = tmp_path_factory.mktemp("config_wizard")
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


@pytest.fixture
def session_cookie_wizard(app_url_wizard: str) -> dict:
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
# Browser config
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args):
    return {
        **browser_context_args,
        "viewport": {"width": 1280, "height": 720},
        "ignore_https_errors": True,
    }
