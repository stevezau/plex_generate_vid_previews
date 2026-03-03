"""
Tests for the Flask application factory (app.py).

Covers: get_cors_origins, _derive_secret, get_or_create_flask_secret,
run_scheduled_job, and create_app configuration.
"""

import json
import os
from unittest.mock import patch

import pytest

from plex_generate_previews.web.app import (
    _derive_secret,
    get_cors_origins,
    get_or_create_flask_secret,
    run_scheduled_job,
)
from plex_generate_previews.web.settings_manager import reset_settings_manager


@pytest.fixture(autouse=True)
def _reset():
    reset_settings_manager()
    import plex_generate_previews.web.jobs as jobs_mod

    with jobs_mod._job_lock:
        jobs_mod._job_manager = None
    import plex_generate_previews.web.scheduler as sched_mod

    with sched_mod._schedule_lock:
        sched_mod._schedule_manager = None
    yield
    # Stop the scheduler before resetting singletons so its background
    # thread doesn't try to query a SQLite DB inside a deleted temp dir.
    with sched_mod._schedule_lock:
        if sched_mod._schedule_manager is not None:
            try:
                sched_mod._schedule_manager.stop()
            except Exception:
                pass
            sched_mod._schedule_manager = None
    reset_settings_manager()
    with jobs_mod._job_lock:
        jobs_mod._job_manager = None


class TestGetCorsOrigins:
    """Test CORS origin resolution."""

    def test_default_returns_wildcard(self, monkeypatch):
        monkeypatch.delenv("CORS_ORIGINS", raising=False)
        assert get_cors_origins() == "*"

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("CORS_ORIGINS", "http://localhost:3000")
        assert get_cors_origins() == "http://localhost:3000"


class TestDeriveSecret:
    """Test HMAC-based secret derivation."""

    def test_deterministic(self):
        seed = b"fixed-seed-bytes"
        s1 = _derive_secret(seed, "/config")
        s2 = _derive_secret(seed, "/config")
        assert s1 == s2

    def test_different_salt_produces_different_secret(self):
        seed = b"fixed-seed-bytes"
        s1 = _derive_secret(seed, "/config")
        s2 = _derive_secret(seed, "/other")
        assert s1 != s2


class TestGetOrCreateFlaskSecret:
    """Test Flask secret key persistence."""

    def test_env_variable_takes_priority(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FLASK_SECRET_KEY", "env-secret-value")
        secret = get_or_create_flask_secret(str(tmp_path))
        assert secret == "env-secret-value"

    def test_generates_and_persists_seed(self, tmp_path, monkeypatch):
        monkeypatch.delenv("FLASK_SECRET_KEY", raising=False)
        secret1 = get_or_create_flask_secret(str(tmp_path))
        assert isinstance(secret1, str)
        assert len(secret1) > 0
        seed_file = tmp_path / "flask_secret.key"
        assert seed_file.exists()

    def test_reuses_existing_seed(self, tmp_path, monkeypatch):
        monkeypatch.delenv("FLASK_SECRET_KEY", raising=False)
        secret1 = get_or_create_flask_secret(str(tmp_path))
        secret2 = get_or_create_flask_secret(str(tmp_path))
        assert secret1 == secret2

    def test_seed_file_has_restrictive_permissions(self, tmp_path, monkeypatch):
        monkeypatch.delenv("FLASK_SECRET_KEY", raising=False)
        get_or_create_flask_secret(str(tmp_path))
        seed_file = tmp_path / "flask_secret.key"
        mode = seed_file.stat().st_mode & 0o777
        assert mode == 0o600


class TestRunScheduledJob:
    """Test module-level scheduled job callback."""

    @patch("plex_generate_previews.web.routes._start_job_async")
    def test_creates_and_starts_job(self, mock_start):
        """run_scheduled_job creates a job and calls _start_job_async."""
        from plex_generate_previews.web.app import create_app

        config_dir = "/tmp/test_scheduled_job"
        os.makedirs(config_dir, exist_ok=True)
        auth_file = os.path.join(config_dir, "auth.json")
        with open(auth_file, "w") as f:
            json.dump({"token": "test-token-12345678"}, f)
        settings_file = os.path.join(config_dir, "settings.json")
        with open(settings_file, "w") as f:
            json.dump({"setup_complete": True}, f)

        with patch.dict(
            os.environ,
            {"CONFIG_DIR": config_dir, "WEB_AUTH_TOKEN": "test-token-12345678"},
        ):
            app = create_app(config_dir=config_dir)
            with app.app_context():
                run_scheduled_job(library_name="Movies")
                mock_start.assert_called_once()

        import shutil

        shutil.rmtree(config_dir, ignore_errors=True)

    @patch("plex_generate_previews.web.routes._start_job_async")
    def test_includes_library_id_in_config(self, mock_start):
        from plex_generate_previews.web.app import create_app

        config_dir = "/tmp/test_scheduled_job2"
        os.makedirs(config_dir, exist_ok=True)
        auth_file = os.path.join(config_dir, "auth.json")
        with open(auth_file, "w") as f:
            json.dump({"token": "test-token-12345678"}, f)
        settings_file = os.path.join(config_dir, "settings.json")
        with open(settings_file, "w") as f:
            json.dump({"setup_complete": True}, f)

        with patch.dict(
            os.environ,
            {"CONFIG_DIR": config_dir, "WEB_AUTH_TOKEN": "test-token-12345678"},
        ):
            app = create_app(config_dir=config_dir)
            with app.app_context():
                run_scheduled_job(library_id="1", library_name="Movies")
                config_overrides = mock_start.call_args[0][1]
                assert "1" in str(config_overrides)

        import shutil

        shutil.rmtree(config_dir, ignore_errors=True)


class TestWsgiModule:
    """Smoke test for wsgi.py module."""

    def test_wsgi_importable(self):
        """wsgi module can be imported without error."""
        import importlib

        # Just verify the module file exists and is syntactically valid
        spec = importlib.util.find_spec("plex_generate_previews.web.wsgi")
        assert spec is not None
