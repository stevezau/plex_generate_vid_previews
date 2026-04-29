"""Shared Playwright route-mock helpers for the e2e suite.

Every helper takes a ``page`` and registers ``page.route()`` handlers
that intercept the relevant API endpoints and return canned responses.
This keeps the wizard / settings / dashboard tests self-contained: no
real Plex/Emby/Jellyfin needed, and writeable endpoints don't pollute
the session-scoped subprocess's settings.json/jobs.db.

Two flavours of helper:

* ``mock_*`` — fire-and-forget; just register the route.
* ``capture_*`` — register the route AND return a list that captures
  the JSON bodies sent to it. Tests can assert against the captured
  payload to verify the wizard/page sent the right shape.
"""

from __future__ import annotations

import json
from typing import Any

from playwright.sync_api import Page, Route


def _fulfill_json(route: Route, body: Any, status: int = 200) -> None:
    route.fulfill(
        status=status,
        content_type="application/json",
        body=json.dumps(body),
    )


# ---------------------------------------------------------------------------
# Plex (wizard step 2 + manual sign-in)
# ---------------------------------------------------------------------------


def mock_plex_libraries(page: Page, libs: list[dict] | None = None, status: int = 200) -> None:
    """GET /api/plex/libraries — default 3 sensible libraries (Movies, TV, Other)."""
    if libs is None:
        libs = [
            {"id": "1", "name": "Movies", "type": "movie", "agent": "tv.plex.agents.movie"},
            {"id": "2", "name": "TV Shows", "type": "show", "agent": "tv.plex.agents.series"},
            {"id": "3", "name": "Documentaries", "type": "movie", "agent": "tv.plex.agents.movie"},
        ]
    body = {"libraries": libs} if status == 200 else {"error": "Unauthorized", "libraries": []}

    def handler(route: Route) -> None:
        _fulfill_json(route, body, status=status)

    page.route("**/api/plex/libraries**", handler)


def mock_plex_servers(page: Page, servers: list[dict] | None = None) -> None:
    """GET /api/plex/servers — list of plex.tv-discovered servers."""
    if servers is None:
        servers = [
            {
                "name": "Home Plex",
                "uri": "http://192.168.1.10:32400",
                "access_token": "tok-home",
                "machine_identifier": "abc123",
            }
        ]

    def handler(route: Route) -> None:
        _fulfill_json(route, {"servers": servers})

    page.route("**/api/plex/servers**", handler)


def mock_plex_test(page: Page, ok: bool = True, message: str = "Connected") -> None:
    """POST /api/plex/test — quick connection probe."""

    def handler(route: Route) -> None:
        _fulfill_json(route, {"success": ok, "message": message})

    page.route("**/api/plex/test", handler)


# ---------------------------------------------------------------------------
# Servers (CRUD + auth + test-connection)
# ---------------------------------------------------------------------------


def mock_servers_list(page: Page, servers: list[dict] | None = None) -> None:
    """GET /api/servers — return a static list. Doesn't intercept POST."""
    servers = servers if servers is not None else []

    def handler(route: Route) -> None:
        if route.request.method == "GET":
            _fulfill_json(route, {"servers": servers})
        else:
            route.continue_()

    page.route("**/api/servers", handler)


def capture_servers_save(
    page: Page,
    vendor: str = "plex",
    *,
    server_id: str = "srv-test-1",
    extra_response: dict | None = None,
) -> list[dict]:
    """Mock POST /api/servers and return a list capturing the bodies sent.

    Tests assert against captured[0] to verify the wizard/page sent the
    right payload shape (URL, auth, output, etc.).
    """
    captured: list[dict] = []
    response = {
        "id": server_id,
        "type": vendor,
        "name": f"{vendor.title()} Test",
        "enabled": True,
        **(extra_response or {}),
    }

    def handler(route: Route) -> None:
        if route.request.method == "POST":
            try:
                captured.append(route.request.post_data_json or {})
            except Exception:
                captured.append({})
            _fulfill_json(route, response)
        elif route.request.method == "GET":
            _fulfill_json(route, {"servers": []})
        else:
            route.continue_()

    page.route("**/api/servers", handler)
    return captured


def mock_servers_test_connection(
    page: Page,
    ok: bool = True,
    *,
    server_name: str = "Test Server",
    version: str | None = "1.40.0",
    warnings: list[dict] | None = None,
    message: str = "Connected",
) -> None:
    """POST /api/servers/test-connection — connection probe used by Add Server modal."""
    body = {
        "ok": ok,
        "server_name": server_name,
        "version": version,
        "message": message,
        "warnings": warnings or [],
    }

    def handler(route: Route) -> None:
        _fulfill_json(route, body)

    page.route("**/api/servers/test-connection", handler)


def mock_emby_password_auth(page: Page, ok: bool = True, *, token: str = "emby-tok") -> None:
    """POST /api/servers/auth/emby/password."""
    body = (
        {"ok": True, "access_token": token, "user_id": "emby-user-1"}
        if ok
        else {"ok": False, "message": "Invalid credentials"}
    )

    def handler(route: Route) -> None:
        _fulfill_json(route, body)

    page.route("**/api/servers/auth/emby/password", handler)


def mock_jellyfin_password_auth(page: Page, ok: bool = True, *, token: str = "jf-tok") -> None:
    """POST /api/servers/auth/jellyfin/password."""
    body = (
        {"ok": True, "access_token": token, "user_id": "jf-user-1"}
        if ok
        else {"ok": False, "message": "Invalid credentials"}
    )

    def handler(route: Route) -> None:
        _fulfill_json(route, body)

    page.route("**/api/servers/auth/jellyfin/password", handler)


def mock_jellyfin_quick_connect(
    page: Page,
    *,
    code: str = "ABC123",
    secret: str = "qc-secret",
    poll_attempts_until_authenticated: int = 1,
    final_token: str = "jf-qc-tok",
) -> None:
    """Mock the three Jellyfin Quick Connect endpoints to walk through the device-code flow.

    initiate → returns the code+secret.
    poll → returns authenticated=False the first ``poll_attempts_until_authenticated``
            times then authenticated=True.
    exchange → returns the final access_token + user_id.
    """
    poll_count = {"n": 0}

    def initiate(route: Route) -> None:
        _fulfill_json(route, {"ok": True, "code": code, "secret": secret})

    def poll(route: Route) -> None:
        poll_count["n"] += 1
        authenticated = poll_count["n"] >= poll_attempts_until_authenticated
        _fulfill_json(route, {"ok": True, "authenticated": authenticated})

    def exchange(route: Route) -> None:
        _fulfill_json(
            route,
            {
                "ok": True,
                "access_token": final_token,
                "user_id": "jf-qc-user",
                "server_name": "Jellyfin",
            },
        )

    page.route("**/api/servers/auth/jellyfin/quick-connect/initiate", initiate)
    page.route("**/api/servers/auth/jellyfin/quick-connect/poll", poll)
    page.route("**/api/servers/auth/jellyfin/quick-connect/exchange", exchange)


def mock_jellyfin_trickplay_fix(page: Page, ok: bool = True) -> list[bool]:
    """POST /api/servers/<id>/jellyfin/fix-trickplay. Returns a list capturing each call."""
    body = {"ok": ok, "fixed": ["lib1", "lib2"]} if ok else {"ok": False, "message": "Failed"}
    called: list[bool] = []

    def handler(route: Route) -> None:
        called.append(True)
        _fulfill_json(route, body)

    page.route("**/api/servers/*/jellyfin/fix-trickplay", handler)
    return called


def mock_servers_refresh_libraries(page: Page, count: int = 2) -> list[bool]:
    """POST /api/servers/<id>/refresh-libraries. Returns a list capturing each call."""
    called: list[bool] = []

    def handler(route: Route) -> None:
        called.append(True)
        _fulfill_json(route, {"ok": True, "count": count, "libraries": []})

    page.route("**/api/servers/*/refresh-libraries", handler)
    return called


# ---------------------------------------------------------------------------
# Setup wizard endpoints
# ---------------------------------------------------------------------------


def mock_setup_token_info(page: Page, *, env_controlled: bool = False) -> None:
    """GET /api/setup/token-info."""
    body = {
        "env_controlled": env_controlled,
        "token": "****wxyz",
        "token_length": 32,
        "source": "environment" if env_controlled else "config",
        "auth_method": "internal",
    }

    def handler(route: Route) -> None:
        _fulfill_json(route, body)

    page.route("**/api/setup/token-info", handler)


def capture_setup_set_token(page: Page, *, ok: bool = True, error: str | None = None) -> list[dict]:
    """POST /api/setup/set-token — capture submitted tokens."""
    captured: list[dict] = []
    body = {"success": ok} if ok else {"success": False, "error": error or "Token rejected"}
    status = 200 if ok else 400

    def handler(route: Route) -> None:
        try:
            captured.append(route.request.post_data_json or {})
        except Exception:
            captured.append({})
        _fulfill_json(route, body, status=status)

    page.route("**/api/setup/set-token", handler)
    return captured


def mock_setup_complete(page: Page, redirect: str = "/") -> list[bool]:
    """POST /api/setup/complete — returns truthy redirect. Captures whether it was called."""
    called: list[bool] = []

    def handler(route: Route) -> None:
        called.append(True)
        _fulfill_json(route, {"success": True, "redirect": redirect})

    page.route("**/api/setup/complete", handler)
    return called


def mock_setup_status(page: Page, *, complete: bool = False, plex_authenticated: bool = False) -> None:
    """GET /api/setup/status — wizard polls this to gate next-button states."""

    def handler(route: Route) -> None:
        _fulfill_json(
            route,
            {
                "is_setup_complete": complete,
                "plex_authenticated": plex_authenticated,
                "configured": complete,
            },
        )

    page.route("**/api/setup/status", handler)


def mock_setup_state(page: Page, *, current_step: int = 1, state: dict | None = None) -> None:
    """GET + POST /api/setup/state — wizard persistence (resume capability).

    Without this mock, the wizard subprocess actually persists state to
    setup_state.json — so e.g. a previous test that walked to step 5
    leaves a state file that auto-resumes step 5 in the next test,
    hiding the vendor picker. Mock GET to always return step 1 + an
    empty state, and accept POSTs as no-ops.
    """

    def handler(route: Route) -> None:
        if route.request.method == "GET":
            _fulfill_json(route, {"current_step": current_step, "state": state or {}})
        else:
            _fulfill_json(route, {"success": True})

    page.route("**/api/setup/state", handler)


def mock_setup_skip(page: Page) -> list[bool]:
    """POST /api/setup/skip — used by step-1 'Skip setup' link."""
    called: list[bool] = []

    def handler(route: Route) -> None:
        called.append(True)
        _fulfill_json(route, {"success": True})

    page.route("**/api/setup/skip", handler)
    return called


def mock_setup_validate_paths(page: Page, *, valid: bool = True) -> None:
    """POST /api/setup/validate-paths — returns the legacy validation shape."""
    body = (
        {"valid": True, "errors": [], "warnings": [], "info": ["Plex Data Path: OK"]}
        if valid
        else {"valid": False, "errors": ["Plex Data Path not found"], "warnings": [], "info": []}
    )

    def handler(route: Route) -> None:
        _fulfill_json(route, body)

    page.route("**/api/setup/validate-paths", handler)


# ---------------------------------------------------------------------------
# Settings + system
# ---------------------------------------------------------------------------


def mock_settings_get(page: Page, settings: dict | None = None) -> None:
    """GET /api/settings — used by SettingsManager.get()."""
    body = settings or {
        "cpu_threads": 1,
        "thumbnail_interval": 2,
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
    }

    def handler(route: Route) -> None:
        if route.request.method == "GET":
            _fulfill_json(route, body)
        else:
            route.continue_()

    page.route("**/api/settings", handler)


def capture_settings_save(page: Page) -> list[dict]:
    """POST /api/settings — captures bodies. Combine with mock_settings_get for read-back."""
    captured: list[dict] = []

    def handler(route: Route) -> None:
        if route.request.method == "POST":
            try:
                captured.append(route.request.post_data_json or {})
            except Exception:
                captured.append({})
            _fulfill_json(route, {"success": True})
        else:
            route.continue_()

    page.route("**/api/settings", handler)
    return captured


def mock_settings_backups(page: Page, files: list[dict] | None = None) -> None:
    """GET /api/settings/backups — multi-history shape."""
    if files is None:
        files = [
            {
                "name": "settings.json",
                "live_path": "/config/settings.json",
                "live_mtime": 2000.0,
                "backups": [
                    {
                        "filename": "settings.json.20260201-100000.bak",
                        "path": "/config/settings.json.20260201-100000.bak",
                        "timestamp": "20260201-100000",
                        "mtime": 1900.0,
                        "legacy": False,
                    },
                    {
                        "filename": "settings.json.20260101-100000.bak",
                        "path": "/config/settings.json.20260101-100000.bak",
                        "timestamp": "20260101-100000",
                        "mtime": 1500.0,
                        "legacy": False,
                    },
                    {
                        "filename": "settings.json.bak",
                        "path": "/config/settings.json.bak",
                        "timestamp": None,
                        "mtime": 500.0,
                        "legacy": True,
                    },
                ],
                "has_bak": True,
                "bak_newer": False,
            }
        ]

    def handler(route: Route) -> None:
        if route.request.method == "GET":
            _fulfill_json(route, {"files": files})
        else:
            route.continue_()

    page.route("**/api/settings/backups", handler)


def capture_settings_backups_restore(page: Page) -> list[dict]:
    """POST /api/settings/backups/restore — captures {file, backup} bodies."""
    captured: list[dict] = []

    def handler(route: Route) -> None:
        try:
            captured.append(route.request.post_data_json or {})
        except Exception:
            captured.append({})
        body = captured[-1] if captured else {}
        _fulfill_json(
            route,
            {"success": True, "file": body.get("file"), "backup": body.get("backup")},
        )

    page.route("**/api/settings/backups/restore", handler)
    return captured


def mock_validate_plex_config_folder(page: Page, *, valid: bool = True, error: str | None = None) -> None:
    """POST /api/settings/validate-plex-config-folder."""
    if valid:
        body = {
            "exists": True,
            "valid_plex_structure": True,
            "shard_count": 16,
            "writable": True,
            "detail": "Looks like a valid Plex install (16/16 shards)",
            "error": None,
        }
    else:
        body = {
            "exists": False,
            "valid_plex_structure": False,
            "shard_count": 0,
            "writable": False,
            "detail": "",
            "error": error or "Folder not found",
        }

    def handler(route: Route) -> None:
        _fulfill_json(route, body)

    page.route("**/api/settings/validate-plex-config-folder", handler)


def mock_validate_local_path(page: Page, *, exists: bool = True, error: str | None = None) -> None:
    """POST /api/settings/validate-local-path."""
    body = {"exists": exists, "readable": exists, "error": error}

    def handler(route: Route) -> None:
        _fulfill_json(route, body)

    page.route("**/api/settings/validate-local-path", handler)


def mock_system_status(page: Page, gpus: list[dict] | None = None) -> None:
    """GET /api/system/status — drives the per-GPU panel."""
    if gpus is None:
        gpus = [
            {"type": "nvidia", "device": "/dev/nvidia0", "name": "Test GPU 0", "status": "ok"},
            {"type": "nvidia", "device": "/dev/nvidia1", "name": "Test GPU 1", "status": "ok"},
        ]

    def handler(route: Route) -> None:
        _fulfill_json(
            route,
            {"gpus": gpus, "gpu_stats": [], "running_job": None, "pending_jobs": 0},
        )

    page.route("**/api/system/status", handler)


def mock_system_rescan_gpus(page: Page, gpus: list[dict] | None = None) -> list[bool]:
    """POST /api/system/rescan-gpus — captures call + returns gpus."""
    called: list[bool] = []
    if gpus is None:
        gpus = [{"type": "nvidia", "device": "/dev/nvidia0", "name": "Test GPU 0", "status": "ok"}]

    def handler(route: Route) -> None:
        called.append(True)
        _fulfill_json(route, {"gpus": gpus})

    page.route("**/api/system/rescan-gpus", handler)
    return called


def mock_browse_directories(page: Page, entries: list[dict] | None = None, path: str | None = None) -> None:
    """GET /api/system/browse — folder picker contents.

    If `path` is None, the mock echoes back the requested ``?path=`` query
    string so the picker can navigate. Pass an explicit `path` to pin the
    response (useful for "always return /plex" tests).
    """
    from urllib.parse import parse_qs, urlparse

    if entries is None:
        entries = [
            {"name": "data", "path": "/data"},
            {"name": "plex", "path": "/plex"},
            {"name": "config", "path": "/config"},
        ]

    def handler(route: Route) -> None:
        if path is not None:
            resolved = path
        else:
            qs = parse_qs(urlparse(route.request.url).query)
            resolved = (qs.get("path") or ["/"])[0] or "/"
        _fulfill_json(
            route,
            {
                "path": resolved,
                "parent": None if resolved == "/" else "/",
                "entries": entries,
                "error": None,
            },
        )

    page.route("**/api/system/browse**", handler)


def mock_token_set(page: Page, *, ok: bool = True, error: str | None = None) -> list[dict]:
    """POST /api/token/set."""
    captured: list[dict] = []
    body = {"success": ok} if ok else {"success": False, "error": error or "Bad token"}
    status = 200 if ok else 400

    def handler(route: Route) -> None:
        try:
            captured.append(route.request.post_data_json or {})
        except Exception:
            captured.append({})
        _fulfill_json(route, body, status=status)

    page.route("**/api/token/set", handler)
    return captured


def mock_token_regenerate(page: Page) -> list[bool]:
    """POST /api/token/regenerate."""
    called: list[bool] = []

    def handler(route: Route) -> None:
        called.append(True)
        _fulfill_json(route, {"success": True})

    page.route("**/api/token/regenerate", handler)
    return called


# ---------------------------------------------------------------------------
# Dashboard / pages with many endpoints — sensible defaults
# ---------------------------------------------------------------------------


def mock_dashboard_defaults(page: Page, *, with_gpus: bool = True) -> None:
    """Register sensible defaults for every endpoint the dashboard polls.

    Tests that want to assert specific behaviour should override the
    relevant handler AFTER calling this (last-registered route wins
    in Playwright). Keeps test bodies focused on the assertion.
    """
    mock_setup_status(page, complete=True, plex_authenticated=True)
    mock_settings_get(page)

    if with_gpus:
        mock_system_status(
            page,
            gpus=[
                {"type": "nvidia", "device": "/dev/nvidia0", "name": "GPU 0", "status": "ok"},
            ],
        )
    else:
        mock_system_status(page, gpus=[])

    def empty_list(route: Route) -> None:
        _fulfill_json(route, [])

    def empty_dict(route: Route) -> None:
        _fulfill_json(route, {})

    page.route("**/api/system/config", lambda r: _fulfill_json(r, {"gpu_threads": 0, "cpu_threads": 1}))
    page.route("**/api/system/media-servers", lambda r: _fulfill_json(r, {"servers": []}))
    page.route("**/api/libraries", lambda r: _fulfill_json(r, {"libraries": []}))
    page.route("**/api/jobs?**", lambda r: _fulfill_json(r, {"jobs": [], "total": 0, "page": 1}))
    page.route("**/api/jobs/stats", lambda r: _fulfill_json(r, {"total": 0, "completed": 0, "failed": 0, "running": 0}))
    page.route("**/api/jobs/workers", lambda r: _fulfill_json(r, {"workers": []}))
    page.route("**/api/schedules", lambda r: _fulfill_json(r, {"schedules": []}))
    page.route("**/api/processing/state", lambda r: _fulfill_json(r, {"paused": False}))
    page.route("**/api/webhooks/pending", lambda r: _fulfill_json(r, {"pending": []}))
    page.route(
        "**/api/system/version",
        lambda r: _fulfill_json(
            r,
            {
                "current_version": "1.0.0",
                "latest_version": "1.0.0",
                "update_available": False,
                "install_type": "docker",
            },
        ),
    )
    page.route("**/api/system/notifications", lambda r: _fulfill_json(r, {"notifications": []}))


def mock_version_with_update(page: Page) -> None:
    """Override /api/system/version to report an available update."""
    page.route(
        "**/api/system/version",
        lambda r: _fulfill_json(
            r,
            {
                "current_version": "1.0.0",
                "latest_version": "2.0.0",
                "update_available": True,
                "install_type": "docker",
            },
        ),
    )


def mock_media_servers_status(page: Page, servers: list[dict] | None = None) -> None:
    """Override the dashboard's /api/system/media-servers status panel."""
    if servers is None:
        servers = [
            {
                "id": "plex-1",
                "name": "Home Plex",
                "type": "plex",
                "enabled": True,
                "status": "connected",
                "url": "http://plex.local:32400",
            }
        ]
    page.route("**/api/system/media-servers", lambda r: _fulfill_json(r, {"servers": servers}))
