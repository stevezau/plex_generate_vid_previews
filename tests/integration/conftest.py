"""Pytest fixtures for the multi-server integration suite.

Loads ``servers.env`` (written by :mod:`setup_servers`) into module-level
fixtures so each test gets the live container's URL, server-id, and
admin token without re-parsing the file.

Why a separate ``tests/integration/`` directory
-----------------------------------------------

Files in this directory drive **live** Emby / Jellyfin / Plex containers
brought up by ``docker-compose.test.yml``. They are NOT mocked. They:

* Require ``./generate_test_media.sh`` to have produced ``./media/``.
* Require ``setup_servers.py`` to have written ``./servers.env`` with
  per-vendor admin tokens captured from a live boot.
* Take 5–60s each (real FFmpeg on real video, real HTTP to containers).

To keep the default ``pytest`` run fast (~5s, 1300+ tests, no Docker
dependency), every test file here is decorated with
``@pytest.mark.integration`` (file-level ``pytestmark`` or per-class).
The default ``pyproject.toml`` ``addopts`` includes
``-m "not gpu and not e2e and not integration"`` which deselects the
whole directory when nothing was asked for.

Explicit invocations:

* ``pytest -m integration --no-cov tests/integration/`` — full suite
  against the live containers (boot the stack first).
* ``pytest --no-cov tests/integration/`` — collects 94 tests but selects
  0 (default ``-m`` filter still applies).  Confirms no ImportError.

If you ever see ``no tests collected`` from an explicit
``-m integration`` invocation, the most likely cause is a missing
``servers.env`` triggering a session-scoped ``pytest.skip`` in this
file (see ``servers_env`` fixture below) — bring the docker stack up.

Note for tooling (mutmut, etc.): this directory should be excluded by
default; mutating live-container code paths is meaningless and slow.
The ``tool.pytest.ini_options.markers`` block in ``pyproject.toml``
documents the ``integration`` marker explicitly.
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


@pytest.fixture(scope="session")
def plex_credentials(servers_env: dict[str, str]) -> dict[str, str]:
    """Captured Plex credentials, or skip if Plex wasn't configured."""
    needed = ("PLEX_URL", "PLEX_SERVER_ID", "PLEX_ACCESS_TOKEN")
    missing = [k for k in needed if not servers_env.get(k)]
    if missing:
        pytest.skip(f"Plex credentials missing: {missing}")
    return {k: servers_env[k] for k in needed}


@pytest.fixture(scope="session")
def jellyfin_credentials(servers_env: dict[str, str]) -> dict[str, str]:
    """Captured Jellyfin credentials, or skip if Jellyfin wasn't configured."""
    needed = ("JELLYFIN_URL", "JELLYFIN_SERVER_ID", "JELLYFIN_ACCESS_TOKEN")
    missing = [k for k in needed if not servers_env.get(k)]
    if missing:
        pytest.skip(f"Jellyfin credentials missing: {missing}")
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
    from media_preview_generator.processing.frame_cache import reset_frame_cache

    reset_frame_cache()
    yield
    reset_frame_cache()
