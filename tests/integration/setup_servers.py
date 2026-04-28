"""API-driven configuration of the integration test stack.

Runs after ``docker compose up -d`` to authenticate against each server,
configure a media library pointing at the synthetic test fixtures, and
write a ``servers.env`` file alongside this script with the resulting
credentials and identities.

Two server types are fully automated:

* **Emby** (``emby/embyserver:latest``) ships with an unconfigured default
  admin "MyEmbyUser" with no password. We authenticate as that user,
  capture the ``AccessToken`` and ``ServerId``, then create a movies
  library pointing at ``/em-media``.
* **Jellyfin** is *not* automated here — its first-run ``/Startup/User``
  endpoint is broken on a fresh install across 10.9 / 10.10 / 10.11
  (``Sequence contains no elements`` because the user table is empty).
  Run the Jellyfin wizard once manually via the web UI on
  ``http://127.0.0.1:8097``, then re-run this script with
  ``--jellyfin-token=<token>`` to capture credentials. Or skip Jellyfin
  with ``--server emby``.

Plex needs a one-time ``PLEX_CLAIM`` token from <https://plex.tv/claim>;
its setup is not currently automated by this script.

The output ``servers.env`` is consumed by the integration tests (which
import it via :mod:`os.environ` to address the live containers).
"""

from __future__ import annotations

import argparse
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
SERVERS_ENV = HERE / "servers.env"

EMBY_URL = "http://127.0.0.1:8096"
JELLYFIN_URL = "http://127.0.0.1:8097"
PLEX_URL = "http://127.0.0.1:32401"

_AUTH_HEADER = (
    'MediaBrowser Client="PlexGeneratePreviewsIntegration", '
    'Device="PlexGeneratePreviewsIntegration", '
    f'DeviceId="{uuid.uuid3(uuid.NAMESPACE_DNS, "PlexGeneratePreviewsIntegration").hex}", '
    'Version="1.0"'
)


@dataclass
class ServerCredentials:
    """Captured auth + identity for one configured test server."""

    name: str
    server_id: str
    access_token: str
    user_id: str
    base_url: str
    library_remote_path: str


def _wait_for_http(url: str, *, timeout: int = 120) -> None:
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


def _authed_headers(token: str) -> dict:
    return {
        "Authorization": _AUTH_HEADER,
        "X-Emby-Token": token,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def setup_emby(*, base_url: str = EMBY_URL) -> ServerCredentials:
    """Authenticate as the default Emby admin and configure a movies library.

    Emby's docker image auto-creates an admin "MyEmbyUser" with no
    password on first start. We authenticate via
    ``/Users/AuthenticateByName`` to capture the ``AccessToken`` and
    ``ServerId``, then ensure a "Movies" virtual folder exists pointing
    at ``/em-media``.
    """
    _wait_for_http(f"{base_url}/System/Info/Public")

    # 1. Authenticate as the seeded default user.
    auth_response = requests.post(
        f"{base_url}/Users/AuthenticateByName",
        json={"Username": "MyEmbyUser", "Pw": ""},
        headers={"Authorization": _AUTH_HEADER, "Content-Type": "application/json"},
        timeout=30,
    )
    auth_response.raise_for_status()
    auth_data = auth_response.json()

    access_token = str(auth_data.get("AccessToken") or "")
    user_id = str((auth_data.get("User") or {}).get("Id") or "")
    server_id = str(auth_data.get("ServerId") or "")
    if not access_token or not server_id:
        raise RuntimeError(f"Emby auth response missing AccessToken/ServerId: {auth_data}")

    # 2. Configure a Movies library pointing at /em-media if not already there.
    folders_response = requests.get(
        f"{base_url}/Library/VirtualFolders",
        headers=_authed_headers(access_token),
        timeout=30,
    )
    folders_response.raise_for_status()
    existing = folders_response.json()
    have_movies = any(isinstance(f, dict) and f.get("Name") == "Movies" for f in (existing or []))
    if not have_movies:
        # Emby's AddVirtualFolder takes query params, not a JSON body.
        add_response = requests.post(
            f"{base_url}/Library/VirtualFolders",
            params={
                "name": "Movies",
                "collectionType": "movies",
                "paths": "/em-media/Movies",
                "refreshLibrary": "true",
            },
            headers=_authed_headers(access_token),
            timeout=60,
        )
        if not add_response.ok:
            raise RuntimeError(
                f"Emby AddVirtualFolder failed: HTTP {add_response.status_code} {add_response.text[:300]}"
            )

    return ServerCredentials(
        name="emby",
        server_id=server_id,
        access_token=access_token,
        user_id=user_id,
        base_url=base_url,
        library_remote_path="/em-media/Movies",
    )


def setup_jellyfin_with_existing_token(
    *,
    base_url: str,
    access_token: str,
    user_id: str,
) -> ServerCredentials:
    """Capture identity for an already-set-up Jellyfin (manual wizard).

    Jellyfin 10.9-10.11 have a bug where ``POST /Startup/User`` throws on
    a fresh install (the controller calls ``Users.First()`` against an
    empty user table). Until that's fixed upstream we don't try to
    automate the wizard — instead the user runs it once via the web UI
    and passes the resulting access token here.
    """
    info = requests.get(
        f"{base_url}/System/Info",
        headers=_authed_headers(access_token),
        timeout=30,
    )
    info.raise_for_status()
    server_id = str(info.json().get("Id") or "")
    if not server_id:
        raise RuntimeError("Jellyfin /System/Info missing Id field")

    return ServerCredentials(
        name="jellyfin",
        server_id=server_id,
        access_token=access_token,
        user_id=user_id,
        base_url=base_url,
        library_remote_path="/jf-media/Movies",
    )


def _write_env_file(credentials: list[ServerCredentials]) -> None:
    """Persist credentials to ``servers.env`` for the test suite to source."""
    lines: list[str] = []
    for c in credentials:
        prefix = c.name.upper()
        lines.append(f"{prefix}_URL={c.base_url}")
        lines.append(f"{prefix}_SERVER_ID={c.server_id}")
        lines.append(f"{prefix}_ACCESS_TOKEN={c.access_token}")
        lines.append(f"{prefix}_USER_ID={c.user_id}")
        lines.append(f"{prefix}_LIBRARY_REMOTE_PATH={c.library_remote_path}")
    SERVERS_ENV.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--server",
        choices=("all", "emby", "jellyfin"),
        default="all",
        help="Which server(s) to configure (default: all). 'all' attempts both.",
    )
    parser.add_argument(
        "--jellyfin-token",
        default=None,
        help="Jellyfin AccessToken from a manually-completed wizard (Jellyfin's API "
        "first-run flow is broken; we don't automate it). Pair with --jellyfin-user-id.",
    )
    parser.add_argument(
        "--jellyfin-user-id",
        default=None,
        help="Jellyfin admin user id matching --jellyfin-token.",
    )
    args = parser.parse_args()

    captured: list[ServerCredentials] = []

    if args.server in ("all", "emby"):
        print("[setup] configuring emby ...", flush=True)
        try:
            captured.append(setup_emby())
        except Exception as exc:
            print(f"[setup] emby failed: {exc}", file=sys.stderr)
            if args.server == "emby":
                return 1

    if args.server in ("all", "jellyfin"):
        if args.jellyfin_token and args.jellyfin_user_id:
            print("[setup] capturing jellyfin identity ...", flush=True)
            try:
                captured.append(
                    setup_jellyfin_with_existing_token(
                        base_url=JELLYFIN_URL,
                        access_token=args.jellyfin_token,
                        user_id=args.jellyfin_user_id,
                    )
                )
            except Exception as exc:
                print(f"[setup] jellyfin failed: {exc}", file=sys.stderr)
        else:
            print(
                "[setup] skipping jellyfin: pass --jellyfin-token + --jellyfin-user-id "
                "after completing the manual wizard at http://127.0.0.1:8097",
                file=sys.stderr,
            )

    if not captured:
        print("[setup] no servers configured; nothing to write", file=sys.stderr)
        return 1

    _write_env_file(captured)
    print(f"[setup] credentials written to {SERVERS_ENV}", flush=True)
    for c in captured:
        print(f"  - {c.name}: id={c.server_id} url={c.base_url}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
