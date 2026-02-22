"""
Tests for CodeQL security fixes.

Validates path sanitization, information exposure prevention,
XSS mitigation, and secret file permissions.
"""

import json
import os
import stat
import pytest
from unittest.mock import patch


@pytest.fixture
def mock_auth_config(tmp_path, monkeypatch):
    """Mock auth module to use temp directory."""
    auth_file = str(tmp_path / "auth.json")
    monkeypatch.setattr("plex_generate_previews.web.auth.AUTH_FILE", auth_file)
    monkeypatch.setattr(
        "plex_generate_previews.web.auth.get_config_dir", lambda: str(tmp_path)
    )

    from plex_generate_previews.web.settings_manager import reset_settings_manager

    reset_settings_manager()
    from plex_generate_previews.web.routes import clear_gpu_cache

    clear_gpu_cache()

    return str(tmp_path)


@pytest.fixture
def flask_app(tmp_path, mock_auth_config):
    """Create Flask app for testing."""
    from plex_generate_previews.web.app import create_app

    app = create_app(config_dir=str(tmp_path))
    app.config["TESTING"] = True
    return app


@pytest.fixture
def client(flask_app):
    """Create test client."""
    return flask_app.test_client()


@pytest.fixture
def auth_headers(flask_app):
    """Get auth headers for API calls."""
    from plex_generate_previews.web.auth import get_auth_token

    token = get_auth_token()
    return {"X-Auth-Token": token}


class TestIsWithinBase:
    """Tests for the _is_within_base helper function."""

    def test_exact_match(self):
        """Path equal to the base returns True."""
        from plex_generate_previews.web.routes import _is_within_base

        assert _is_within_base("/plex", "/plex") is True

    def test_child_path(self, tmp_path):
        """Path inside the base returns True."""
        from plex_generate_previews.web.routes import _is_within_base

        base = str(tmp_path)
        child = str(tmp_path / "subdir")
        assert _is_within_base(base, child) is True

    def test_outside_path(self, tmp_path):
        """Path outside the base returns False."""
        from plex_generate_previews.web.routes import _is_within_base

        base = str(tmp_path / "allowed")
        outside = str(tmp_path / "other")
        assert _is_within_base(base, outside) is False

    def test_prefix_collision(self, tmp_path):
        """Base /plex should not match /plex2 (no trailing sep trick)."""
        from plex_generate_previews.web.routes import _is_within_base

        base = str(tmp_path / "plex")
        candidate = str(tmp_path / "plex2")
        assert _is_within_base(base, candidate) is False


class TestPathTraversalPrevention:
    """Tests for path traversal prevention in validate_paths endpoint."""

    def test_validate_paths_with_null_byte(self, client, auth_headers):
        """Paths with null bytes are rejected."""
        response = client.post(
            "/api/setup/validate-paths",
            headers={**auth_headers, "Content-Type": "application/json"},
            data=json.dumps({"plex_config_folder": "/plex\x00/etc/passwd"}),
        )
        assert response.status_code == 200
        data = json.loads(response.data)
        assert data["valid"] is False
        assert any("Invalid" in e for e in data["errors"])

    def test_validate_paths_traversal_resolved(
        self, client, auth_headers, tmp_path, monkeypatch
    ):
        """Paths with .. components are resolved via realpath."""
        plex_dir = tmp_path / "plex"
        plex_dir.mkdir()
        media_dir = plex_dir / "Media"
        media_dir.mkdir()
        localhost_dir = media_dir / "localhost"
        localhost_dir.mkdir()

        # Set PLEX_DATA_ROOT to tmp_path so the path is allowed
        monkeypatch.setattr(
            "plex_generate_previews.web.routes.PLEX_DATA_ROOT", str(tmp_path)
        )

        # Path with traversal components should be resolved
        traversal_path = str(plex_dir) + "/../plex"

        response = client.post(
            "/api/setup/validate-paths",
            headers={**auth_headers, "Content-Type": "application/json"},
            data=json.dumps({"plex_config_folder": traversal_path}),
        )
        assert response.status_code == 200
        data = json.loads(response.data)
        # Should resolve to the real path without errors about traversal
        assert not any("Invalid" in e for e in data["errors"])

    def test_validate_paths_outside_root_rejected(
        self, client, auth_headers, tmp_path, monkeypatch
    ):
        """Paths outside the configured PLEX_DATA_ROOT are rejected."""
        allowed_root = tmp_path / "allowed"
        allowed_root.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()

        monkeypatch.setattr(
            "plex_generate_previews.web.routes.PLEX_DATA_ROOT", str(allowed_root)
        )

        response = client.post(
            "/api/setup/validate-paths",
            headers={**auth_headers, "Content-Type": "application/json"},
            data=json.dumps({"plex_config_folder": str(outside)}),
        )
        assert response.status_code == 200
        data = json.loads(response.data)
        assert data["valid"] is False
        assert any("must be within" in e for e in data["errors"])

    def test_validate_paths_traversal_escape_rejected(
        self, client, auth_headers, tmp_path, monkeypatch
    ):
        """Path using .. to escape the root is rejected."""
        allowed_root = tmp_path / "plex"
        allowed_root.mkdir()
        secret = tmp_path / "secret"
        secret.mkdir()

        monkeypatch.setattr(
            "plex_generate_previews.web.routes.PLEX_DATA_ROOT", str(allowed_root)
        )

        response = client.post(
            "/api/setup/validate-paths",
            headers={**auth_headers, "Content-Type": "application/json"},
            data=json.dumps({"plex_config_folder": str(allowed_root) + "/../secret"}),
        )
        assert response.status_code == 200
        data = json.loads(response.data)
        assert data["valid"] is False
        assert any("must be within" in e for e in data["errors"])

    def test_validate_paths_local_media_null_byte(self, client, auth_headers):
        """Local media path with null bytes is rejected."""
        response = client.post(
            "/api/setup/validate-paths",
            headers={**auth_headers, "Content-Type": "application/json"},
            data=json.dumps(
                {
                    "plex_config_folder": "/plex",
                    "plex_videos_path_mapping": "/media",
                    "plex_local_videos_path_mapping": "/media\x00/etc/shadow",
                }
            ),
        )
        assert response.status_code == 200
        data = json.loads(response.data)
        assert data["valid"] is False


class TestInformationExposurePrevention:
    """Tests ensuring exception details are not leaked in API responses."""

    def test_get_jobs_error_no_leak(self, client, auth_headers):
        """Error in get_jobs does not expose exception details."""
        with patch(
            "plex_generate_previews.web.routes.get_job_manager",
            side_effect=RuntimeError("Database connection refused on port 5432"),
        ):
            response = client.get("/api/jobs", headers=auth_headers)
            assert response.status_code == 500
            data = json.loads(response.data)
            assert "5432" not in data.get("error", "")
            assert "Database" not in data.get("error", "")

    def test_get_worker_statuses_error_no_leak(self, client, auth_headers):
        """Error in get_worker_statuses does not expose exception details."""
        with patch(
            "plex_generate_previews.web.routes.get_job_manager",
            side_effect=RuntimeError("Internal memory error at 0xdeadbeef"),
        ):
            response = client.get("/api/jobs/workers", headers=auth_headers)
            assert response.status_code == 500
            data = json.loads(response.data)
            assert "0xdeadbeef" not in data.get("error", "")

    def test_get_job_stats_error_no_leak(self, client, auth_headers):
        """Error in get_job_stats does not expose exception details."""
        with patch(
            "plex_generate_previews.web.routes.get_job_manager",
            side_effect=RuntimeError("SQLAlchemy pool overflow"),
        ):
            response = client.get("/api/jobs/stats", headers=auth_headers)
            assert response.status_code == 500
            data = json.loads(response.data)
            assert "SQLAlchemy" not in data.get("error", "")

    def test_get_system_status_error_no_leak(self, client, auth_headers):
        """Error in get_system_status does not expose exception details."""
        with patch(
            "plex_generate_previews.web.routes.get_job_manager",
            side_effect=RuntimeError(
                "nvidia-smi binary not found at /usr/bin/nvidia-smi"
            ),
        ):
            response = client.get("/api/system/status", headers=auth_headers)
            assert response.status_code == 500
            data = json.loads(response.data)
            assert "nvidia-smi" not in data.get("error", "")


class TestFlaskSecretFilePermissions:
    """Tests for Flask secret key file permissions."""

    @pytest.mark.skipif(
        os.name == "nt", reason="POSIX file permissions not enforced on Windows"
    )
    def test_secret_file_has_restricted_permissions(self, tmp_path):
        """Generated Flask secret file has 0o600 permissions."""
        from plex_generate_previews.web.app import get_or_create_flask_secret

        secret = get_or_create_flask_secret(str(tmp_path))
        secret_file = tmp_path / "flask_secret.key"

        assert secret_file.exists()
        assert len(secret) > 0

        mode = stat.S_IMODE(secret_file.stat().st_mode)
        assert mode == 0o600, f"Expected 0o600, got {oct(mode)}"


class TestXSSPrevention:
    """Tests for XSS prevention in mock_plex_tv."""

    def test_auth_page_escapes_input(self):
        """Auth page HTML-escapes query parameters."""
        from tests.mocks.mock_plex_tv import app as mock_app

        with mock_app.test_client() as c:
            response = c.get(
                "/auth?pin=<script>alert(1)</script>&code=<img onerror=alert(1)>"
            )
            html = response.data.decode()
            assert "<script>" not in html
            assert "&lt;script&gt;" in html
            assert "<img onerror" not in html
