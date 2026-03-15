"""
Playwright E2E test configuration and fixtures.

This module provides fixtures for running end-to-end tests with Playwright.
"""

import os
import socket
import subprocess
import sys
import time
from typing import Generator

import pytest


def wait_for_port(port: int, timeout: float = 10.0) -> bool:
    """Wait for a port to become available."""
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


@pytest.fixture(scope="session")
def app_url(tmp_path_factory) -> Generator[str, None, None]:
    """Start an isolated application server for E2E testing."""
    config_dir = tmp_path_factory.mktemp("config")
    app_port = get_free_port()

    env = {
        **os.environ,
        "WEB_PORT": str(app_port),
        "CONFIG_DIR": str(config_dir),
        "WEB_AUTH_TOKEN": "e2e-test-token",
    }

    # Start the Flask app using run_server directly (bypasses CLI config validation).
    # Use the same interpreter as pytest so venv/path differences do not break startup.
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            f"from plex_generate_previews.web.app import run_server; run_server(host='0.0.0.0', port={app_port})",
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    if not wait_for_port(app_port, timeout=20):
        stdout, stderr = proc.communicate(timeout=5)
        proc.kill()
        raise RuntimeError(
            f"App server failed to start on port {app_port}.\n"
            f"stdout: {stdout.decode()}\nstderr: {stderr.decode()}"
        )

    yield f"http://localhost:{app_port}"

    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.fixture
def auth_token() -> str:
    """Return the E2E test auth token."""
    return "e2e-test-token"


# Browser configuration
@pytest.fixture(scope="session")
def browser_context_args(browser_context_args):
    """Configure browser context for tests."""
    return {
        **browser_context_args,
        "viewport": {"width": 1280, "height": 720},
        "ignore_https_errors": True,
    }
