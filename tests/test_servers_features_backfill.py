"""TEST_AUDIT Phase 5 — backfill tests for newly-shipped server features.

41 features shipped in 3 days. Most have ZERO direct test coverage today.
This file targets the most user-facing of those:

  * Vendor extraction toggle (POST /api/servers/{id}/vendor-extraction)
    — commits a5070b6, 9e4ce73, 2c925db
  * Vendor extraction status (GET /api/servers/{id}/vendor-extraction/status)
  * Server health check (GET /api/servers/{id}/health-check) — commit be11807
  * Server health check apply (POST /api/servers/{id}/health-check/apply)
    — commit 4592852
  * One-click Jellyfin plugin install (POST /api/servers/{id}/install-plugin)
    — commit bfc9613

Why this matters: the audit's incident archaeology covered HISTORICAL
bugs. These features are NEW — the user has been hitting bugs in them
and reaching for fixes manually. A regression in any of these would
silently break a UI feature the user just shipped.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from media_preview_generator.web.settings_manager import (
    get_settings_manager,
    reset_settings_manager,
)


@pytest.fixture
def mock_auth_config(tmp_path, monkeypatch):
    auth_file = str(tmp_path / "auth.json")
    monkeypatch.setattr("media_preview_generator.web.auth.AUTH_FILE", auth_file)
    monkeypatch.setattr("media_preview_generator.web.auth.get_config_dir", lambda: str(tmp_path))
    reset_settings_manager()
    from media_preview_generator.web.routes import clear_gpu_cache

    clear_gpu_cache()
    return str(tmp_path)


@pytest.fixture
def flask_app(tmp_path, mock_auth_config):
    from media_preview_generator.web.app import create_app

    app = create_app(config_dir=str(tmp_path))
    app.config["TESTING"] = True
    return app


@pytest.fixture
def client(flask_app):
    return flask_app.test_client()


@pytest.fixture
def auth_headers():
    from media_preview_generator.web.auth import get_auth_token

    return {"X-Auth-Token": get_auth_token()}


def _seed_jellyfin_server(server_id: str = "jelly-1") -> None:
    sm = get_settings_manager()
    sm.set(
        "media_servers",
        [
            {
                "id": server_id,
                "type": "jellyfin",
                "name": "Test Jellyfin",
                "enabled": True,
                "url": "http://jelly:8096",
                "auth": {"method": "api_key", "api_key": "key"},
                "libraries": [
                    {"id": "1", "name": "Movies", "remote_paths": ["/data/movies"], "enabled": True},
                    {"id": "2", "name": "TV", "remote_paths": ["/data/tv"], "enabled": True},
                ],
            }
        ],
    )


def _seed_plex_server(server_id: str = "plex-1") -> None:
    sm = get_settings_manager()
    sm.set(
        "media_servers",
        [
            {
                "id": server_id,
                "type": "plex",
                "name": "Test Plex",
                "enabled": True,
                "url": "http://plex:32400",
                "auth": {"token": "tok"},
                "libraries": [
                    {"id": "1", "name": "Movies", "remote_paths": ["/data/movies"], "enabled": True},
                ],
            }
        ],
    )


# ---------------------------------------------------------------------------
# POST /api/servers/{id}/vendor-extraction (commits a5070b6, 9e4ce73, 2c925db)
# ---------------------------------------------------------------------------


class TestVendorExtractionToggle:
    """Toggle the vendor's own scan-time preview generation off/on.

    When this app is generating previews, the vendor's scanner-thumbnail
    step is wasted CPU. Each vendor's flag is different (Plex
    ``scannerThumbnailVideoFiles``, Emby ``Extract*ImagesDuringLibraryScan``,
    Jellyfin ``ExtractTrickplayImagesDuringLibraryScan``).
    """

    def test_disable_extraction_returns_per_library_results(self, client, auth_headers):
        _seed_jellyfin_server()
        from media_preview_generator.servers.jellyfin import JellyfinServer

        with patch.object(
            JellyfinServer,
            "set_vendor_extraction",
            return_value={"1": "ok", "2": "ok"},
        ) as mock_set:
            response = client.post(
                "/api/servers/jelly-1/vendor-extraction",
                json={"scan_extraction": False},
                headers=auth_headers,
            )

        assert response.status_code == 200
        body = response.get_json()
        assert body["ok"] is True
        assert body["scan_extraction"] is False
        assert body["results"] == {"1": "ok", "2": "ok"}
        assert body["ok_count"] == 2
        assert body["error_count"] == 0
        assert body["total"] == 2
        # The toggle MUST have been called with scan_extraction=False
        # (NOT True — would silently re-enable a feature the user just
        # explicitly disabled).
        mock_set.assert_called_once_with(scan_extraction=False)

    def test_enable_extraction_passes_true_to_backend(self, client, auth_headers):
        _seed_jellyfin_server()
        from media_preview_generator.servers.jellyfin import JellyfinServer

        with patch.object(
            JellyfinServer,
            "set_vendor_extraction",
            return_value={"1": "ok"},
        ) as mock_set:
            response = client.post(
                "/api/servers/jelly-1/vendor-extraction",
                json={"scan_extraction": True},
                headers=auth_headers,
            )

        assert response.status_code == 200
        assert response.get_json()["scan_extraction"] is True
        mock_set.assert_called_once_with(scan_extraction=True)

    def test_partial_failure_reports_ok_false_with_per_library_breakdown(self, client, auth_headers):
        """Mixed success/error per library — payload reflects per-library
        outcome so the user can see WHICH library failed (and try again
        for those specifically).
        """
        _seed_jellyfin_server()
        from media_preview_generator.servers.jellyfin import JellyfinServer

        with patch.object(
            JellyfinServer,
            "set_vendor_extraction",
            return_value={"1": "ok", "2": "error: 401 Unauthorized"},
        ):
            response = client.post(
                "/api/servers/jelly-1/vendor-extraction",
                json={"scan_extraction": False},
                headers=auth_headers,
            )

        body = response.get_json()
        assert body["ok"] is False
        assert body["ok_count"] == 1
        assert body["error_count"] == 1
        assert body["total"] == 2
        # Per-library breakdown is preserved
        assert body["results"]["1"] == "ok"
        assert "401" in body["results"]["2"]

    def test_invalid_body_returns_400(self, client, auth_headers):
        _seed_jellyfin_server()
        # Missing scan_extraction key
        r1 = client.post("/api/servers/jelly-1/vendor-extraction", json={}, headers=auth_headers)
        assert r1.status_code == 400
        # Wrong type
        r2 = client.post(
            "/api/servers/jelly-1/vendor-extraction", json={"scan_extraction": "yes"}, headers=auth_headers
        )
        assert r2.status_code == 400

    def test_unknown_server_id_returns_404(self, client, auth_headers):
        _seed_jellyfin_server()
        response = client.post(
            "/api/servers/does-not-exist/vendor-extraction",
            json={"scan_extraction": False},
            headers=auth_headers,
        )
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# GET /api/servers/{id}/vendor-extraction/status
# ---------------------------------------------------------------------------


class TestVendorExtractionStatus:
    """Per-library count of "extracting/stopped/skipped" so the Edit Server
    modal can render a single state-appropriate CTA (commit 2c925db).
    """

    def test_status_returns_counts(self, client, auth_headers):
        _seed_jellyfin_server()
        from media_preview_generator.servers.jellyfin import JellyfinServer

        with patch.object(
            JellyfinServer,
            "get_vendor_extraction_status",
            return_value={
                "extracting_count": 1,
                "stopped_count": 1,
                "skipped_count": 0,
                "total": 2,
            },
        ):
            response = client.get(
                "/api/servers/jelly-1/vendor-extraction/status",
                headers=auth_headers,
            )

        assert response.status_code == 200
        body = response.get_json()
        assert body["extracting_count"] == 1
        assert body["stopped_count"] == 1
        assert body["total"] == 2
        # Vendor identifier must appear so the UI knows which CTA copy to render.
        assert body["vendor"] == "jellyfin"

    def test_status_probe_failure_returns_502(self, client, auth_headers):
        _seed_jellyfin_server()
        from media_preview_generator.servers.jellyfin import JellyfinServer

        with patch.object(
            JellyfinServer,
            "get_vendor_extraction_status",
            side_effect=RuntimeError("Jellyfin unreachable"),
        ):
            response = client.get(
                "/api/servers/jelly-1/vendor-extraction/status",
                headers=auth_headers,
            )

        # 502 (bad gateway) — backend probe failed, not our fault.
        assert response.status_code == 502
        assert "unreachable" in response.get_json().get("error", "").lower()


# ---------------------------------------------------------------------------
# GET /api/servers/{id}/health-check (commit be11807)
# ---------------------------------------------------------------------------


class TestServerHealthCheck:
    """Per-server settings audit. Returns per-library issues that the user
    can one-click apply via /apply.
    """

    def test_returns_issues_in_documented_shape(self, client, auth_headers):
        _seed_jellyfin_server()
        from media_preview_generator.servers.base import HealthCheckIssue
        from media_preview_generator.servers.jellyfin import JellyfinServer

        fake_issues = [
            HealthCheckIssue(
                library_id="1",
                library_name="Movies",
                flag="EnableTrickplayImageExtraction",
                label="Trickplay extraction",
                rationale="Required for Jellyfin to display previews",
                current=False,
                recommended=True,
                severity="critical",
                fixable=True,
            ),
        ]
        with patch.object(JellyfinServer, "check_settings_health", return_value=fake_issues):
            response = client.get("/api/servers/jelly-1/health-check", headers=auth_headers)

        assert response.status_code == 200
        body = response.get_json()
        # Top-level keys per the docstring contract.
        assert body["vendor"] == "jellyfin"
        assert body["issue_count"] == 1
        assert body["fixable_count"] == 1
        # Per-issue shape: every documented field is present and correctly typed.
        issue = body["issues"][0]
        assert issue["library_id"] == "1"
        assert issue["library_name"] == "Movies"
        assert issue["flag"] == "EnableTrickplayImageExtraction"
        assert issue["label"] == "Trickplay extraction"
        assert "Jellyfin" in issue["rationale"]
        assert issue["current"] is False
        assert issue["recommended"] is True
        assert issue["severity"] == "critical"
        assert issue["fixable"] is True

    def test_no_issues_returns_empty_list(self, client, auth_headers):
        """Healthy server → empty issues list (NOT None or 404).

        UI's "all good — green checkmark" rendering depends on this exact
        shape.
        """
        _seed_jellyfin_server()
        from media_preview_generator.servers.jellyfin import JellyfinServer

        with patch.object(JellyfinServer, "check_settings_health", return_value=[]):
            response = client.get("/api/servers/jelly-1/health-check", headers=auth_headers)

        assert response.status_code == 200
        body = response.get_json()
        assert body["issues"] == []
        assert body["issue_count"] == 0
        assert body["fixable_count"] == 0

    def test_probe_failure_returns_502(self, client, auth_headers):
        _seed_jellyfin_server()
        from media_preview_generator.servers.jellyfin import JellyfinServer

        with patch.object(
            JellyfinServer,
            "check_settings_health",
            side_effect=RuntimeError("Jellyfin offline"),
        ):
            response = client.get("/api/servers/jelly-1/health-check", headers=auth_headers)

        assert response.status_code == 502
        assert "offline" in response.get_json().get("error", "").lower()

    def test_unknown_server_returns_404(self, client, auth_headers):
        _seed_jellyfin_server()
        response = client.get("/api/servers/does-not-exist/health-check", headers=auth_headers)
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/servers/{id}/health-check/apply (commit 4592852)
# ---------------------------------------------------------------------------


class TestServerHealthCheckApply:
    """One-click "fix all flagged issues" or fix-specific-flags."""

    def test_apply_no_body_fixes_all_flagged(self, client, auth_headers):
        """Empty body = "fix everything currently flagged"."""
        _seed_jellyfin_server()
        from media_preview_generator.servers.jellyfin import JellyfinServer

        with patch.object(
            JellyfinServer,
            "apply_recommended_settings",
            return_value={"1:EnableTrickplayImageExtraction": "ok"},
        ) as mock_apply:
            response = client.post(
                "/api/servers/jelly-1/health-check/apply",
                json={},
                headers=auth_headers,
            )

        assert response.status_code == 200
        body = response.get_json()
        assert body["ok"] is True
        assert body["results"] == {"1:EnableTrickplayImageExtraction": "ok"}
        mock_apply.assert_called_once_with(flags=None)

    def test_apply_with_specific_flags_passes_through(self, client, auth_headers):
        """Body ``{"flags": [...]}`` restricts the fix to specific flags."""
        _seed_jellyfin_server()
        from media_preview_generator.servers.jellyfin import JellyfinServer

        with patch.object(
            JellyfinServer,
            "apply_recommended_settings",
            return_value={"1:Foo": "ok"},
        ) as mock_apply:
            response = client.post(
                "/api/servers/jelly-1/health-check/apply",
                json={"flags": ["Foo", "Bar"]},
                headers=auth_headers,
            )

        assert response.status_code == 200
        mock_apply.assert_called_once_with(flags=["Foo", "Bar"])

    def test_apply_partial_failure_reports_ok_false(self, client, auth_headers):
        """Some succeed, some fail → ok=False with per-flag breakdown."""
        _seed_jellyfin_server()
        from media_preview_generator.servers.jellyfin import JellyfinServer

        with patch.object(
            JellyfinServer,
            "apply_recommended_settings",
            return_value={"1:Foo": "ok", "2:Bar": "error: 403 Forbidden"},
        ):
            response = client.post(
                "/api/servers/jelly-1/health-check/apply",
                json={},
                headers=auth_headers,
            )

        body = response.get_json()
        assert body["ok"] is False
        assert body["results"]["1:Foo"] == "ok"
        assert "403" in body["results"]["2:Bar"]

    def test_apply_empty_results_is_ok_true_not_failure(self, client, auth_headers):
        """``apply`` returning {} = nothing needed fixing → ok=True (NOT
        ok=False — the user shouldn't see "failed" when there was nothing
        to do).
        """
        _seed_jellyfin_server()
        from media_preview_generator.servers.jellyfin import JellyfinServer

        with patch.object(JellyfinServer, "apply_recommended_settings", return_value={}):
            response = client.post(
                "/api/servers/jelly-1/health-check/apply",
                json={},
                headers=auth_headers,
            )

        assert response.status_code == 200
        body = response.get_json()
        assert body["ok"] is True

    def test_invalid_flags_type_returns_400(self, client, auth_headers):
        _seed_jellyfin_server()
        response = client.post(
            "/api/servers/jelly-1/health-check/apply",
            json={"flags": "not-a-list"},
            headers=auth_headers,
        )
        assert response.status_code == 400
