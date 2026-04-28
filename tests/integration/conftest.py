"""Pytest fixtures for the multi-server integration suite.

Loads ``servers.env`` (written by :mod:`setup_servers`) into module-level
fixtures so each test gets the live container's URL, server-id, and
admin token without re-parsing the file.
"""

from __future__ import annotations

from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SERVERS_ENV = HERE / "servers.env"


def _parse_env(path: Path) -> dict[str, str]:
    """Read a tiny KEY=VALUE file (no quoting) into a dict."""
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip()
    return out


@pytest.fixture(scope="session")
def servers_env() -> dict[str, str]:
    """All keys from ``tests/integration/servers.env``."""
    if not SERVERS_ENV.exists():
        pytest.skip(f"{SERVERS_ENV} not found — run `python tests/integration/setup_servers.py --server emby` first")
    return _parse_env(SERVERS_ENV)


@pytest.fixture(scope="session")
def emby_credentials(servers_env: dict[str, str]) -> dict[str, str]:
    """Captured Emby credentials, or skip if Emby wasn't configured."""
    needed = ("EMBY_URL", "EMBY_SERVER_ID", "EMBY_ACCESS_TOKEN", "EMBY_USER_ID")
    missing = [k for k in needed if not servers_env.get(k)]
    if missing:
        pytest.skip(f"Emby credentials missing: {missing}")
    return {k: servers_env[k] for k in needed}


@pytest.fixture
def media_root() -> Path:
    """Local path to the synthetic test fixtures (mounted into containers)."""
    media_dir = HERE / "media"
    if not media_dir.exists():
        pytest.skip(f"{media_dir} missing — run ./tests/integration/generate_test_media.sh")
    return media_dir


@pytest.fixture(autouse=True)
def _reset_singletons():
    """Reset the frame-cache singleton between tests in this directory."""
    from plex_generate_previews.processing.frame_cache import reset_frame_cache

    reset_frame_cache()
    yield
    reset_frame_cache()
