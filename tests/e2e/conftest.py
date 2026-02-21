"""
Playwright E2E test configuration and fixtures.

This module provides fixtures for running end-to-end tests with Playwright.
"""

import os
import pytest
import subprocess
import time
import socket
from typing import Generator


# Port configuration for test server
APP_PORT = 8081


def is_port_in_use(port: int) -> bool:
    """Check if a port is already in use."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("localhost", port)) == 0


def wait_for_port(port: int, timeout: float = 10.0) -> bool:
    """Wait for a port to become available."""
    start = time.time()
    while time.time() - start < timeout:
        if is_port_in_use(port):
            return True
        time.sleep(0.1)
    return False


@pytest.fixture(scope="session")
def app_url(tmp_path_factory) -> Generator[str, None, None]:
    """Start the application server for testing."""
    config_dir = tmp_path_factory.mktemp("config")

    if is_port_in_use(APP_PORT):
        yield f"http://localhost:{APP_PORT}"
        return

    env = {
        **os.environ,
        "WEB_PORT": str(APP_PORT),
        "CONFIG_DIR": str(config_dir),
        "WEB_AUTH_TOKEN": "e2e-test-token",
    }

    # Start the Flask app using run_server directly (bypasses CLI config validation).
    proc = subprocess.Popen(
        [
            "python",
            "-c",
            f"from plex_generate_previews.web.app import run_server; run_server(host='0.0.0.0', port={APP_PORT})",
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    if not wait_for_port(APP_PORT, timeout=20):
        stdout, stderr = proc.communicate(timeout=5)
        proc.kill()
        raise RuntimeError(
            f"App server failed to start on port {APP_PORT}.\n"
            f"stdout: {stdout.decode()}\nstderr: {stderr.decode()}"
        )

    yield f"http://localhost:{APP_PORT}"

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
