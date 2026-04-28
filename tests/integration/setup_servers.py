"""API-driven configuration of the integration test stack.

Runs after ``docker compose up -d`` to:

1. Walk Jellyfin's first-run wizard via ``/Startup/*`` so the server
   reaches the post-setup state without UI clicks.
2. Create an admin user and obtain an ``AccessToken`` on Emby.
3. Discover Plex's admin token (Plex bootstrap requires the user's
   one-time ``PLEX_CLAIM`` token; once claimed, the server identifies
   itself via ``/identity`` and we read the persisted token from the
   container's preferences).

The script writes a ``servers.env`` file alongside itself with the
resulting credentials, ready to be sourced by integration tests.

This is the **scaffold** — Phase 2/3 fill in the per-vendor steps as
those clients land. Phase 1 ships the structure and logging so the
docker-compose stack can be brought up and torn down with a single
command, but the per-server setup helpers raise NotImplementedError
until they're wired up.
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
SERVERS_ENV = HERE / "servers.env"

# Default endpoints from docker-compose.test.yml.
EMBY_URL = "http://127.0.0.1:8096"
JELLYFIN_URL = "http://127.0.0.1:8097"
PLEX_URL = "http://127.0.0.1:32401"


@dataclass
class ServerCredentials:
    """Captured auth + identity for one configured test server."""

    server_id: str
    server_name: str
    auth_token: str | None = None
    api_key: str | None = None
    user_id: str | None = None


def _wait_for_http(url: str, *, timeout: int = 120) -> None:
    """Poll ``url`` until it returns 2xx or ``timeout`` elapses.

    Used to gate setup steps until each server's HTTP listener is up.
    """
    deadline = time.time() + timeout
    last_exc: Exception | None = None
    while time.time() < deadline:
        try:
            response = requests.get(url, timeout=3)
            if 200 <= response.status_code < 300:
                return
        except requests.RequestException as exc:
            last_exc = exc
        time.sleep(2)
    raise TimeoutError(f"server at {url} not ready after {timeout}s; last error: {last_exc}")


def setup_emby(*, base_url: str = EMBY_URL) -> ServerCredentials:
    """Create an admin user on Emby and capture the AccessToken.

    TODO(phase2): implement via ``POST /Users/New`` +
    ``POST /Users/AuthenticateByName``. The Emby client lands in Phase 2
    of the multi-media-server refactor.
    """
    raise NotImplementedError("Emby setup arrives in Phase 2")


def setup_jellyfin(*, base_url: str = JELLYFIN_URL) -> ServerCredentials:
    """Walk Jellyfin's first-run wizard and capture credentials.

    TODO(phase3): POST through ``/Startup/Configuration`` /
    ``/Startup/User`` / ``/Startup/Complete`` to bypass the UI wizard,
    then ``POST /Users/AuthenticateByName`` for the AccessToken. The
    Jellyfin client lands in Phase 3.
    """
    raise NotImplementedError("Jellyfin setup arrives in Phase 3")


def setup_plex(*, base_url: str = PLEX_URL) -> ServerCredentials:
    """Capture Plex's admin token after a claim-token bootstrap.

    The user supplies ``PLEX_CLAIM`` to docker-compose; on first start
    Plex registers with their account and persists an admin token in
    ``/config/Library/Application Support/Plex Media Server/Preferences.xml``.
    We read that token via ``docker compose exec`` (or by mounting the
    volume) and surface it here.

    TODO(phase1-tail): implement reliably across distros. The simplest
    approach is to call the user's plex.tv account with the claim token
    they used, list resources via ``/api/v2/resources``, and extract
    ``accessToken`` for the matching ``machineIdentifier``.
    """
    raise NotImplementedError("Plex setup wired up alongside the orchestrator refactor")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--server",
        choices=("all", "emby", "jellyfin", "plex"),
        default="all",
        help="Which server(s) to configure (default: all).",
    )
    parser.add_argument(
        "--ready-timeout",
        type=int,
        default=120,
        help="Seconds to wait for each server's HTTP listener to come up.",
    )
    args = parser.parse_args()

    targets = ("emby", "jellyfin", "plex") if args.server == "all" else (args.server,)
    captured: list[ServerCredentials] = []

    for target in targets:
        if target == "emby":
            url = EMBY_URL
            setup_fn = setup_emby
        elif target == "jellyfin":
            url = JELLYFIN_URL
            setup_fn = setup_jellyfin
        else:
            url = PLEX_URL
            setup_fn = setup_plex

        print(f"[setup] waiting for {target} at {url} ...", flush=True)
        try:
            _wait_for_http(url, timeout=args.ready_timeout)
        except TimeoutError as exc:
            print(f"[setup] {target} did not become ready: {exc}", file=sys.stderr)
            return 1

        print(f"[setup] configuring {target} ...", flush=True)
        try:
            captured.append(setup_fn())
        except NotImplementedError as exc:
            print(f"[setup] skipping {target}: {exc}")

    if captured:
        with SERVERS_ENV.open("w", encoding="utf-8") as f:
            for c in captured:
                key_prefix = c.server_name.upper().replace(" ", "_")
                if c.auth_token:
                    f.write(f"{key_prefix}_TOKEN={c.auth_token}\n")
                if c.api_key:
                    f.write(f"{key_prefix}_API_KEY={c.api_key}\n")
                if c.user_id:
                    f.write(f"{key_prefix}_USER_ID={c.user_id}\n")
        print(f"[setup] credentials written to {SERVERS_ENV}", flush=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
