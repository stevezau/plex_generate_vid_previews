"""
Tests for the Flask application factory (app.py).

Covers: get_cors_origins, _derive_secret, get_or_create_flask_secret,
run_scheduled_job, and create_app configuration.
"""

import json
import os
from unittest.mock import patch

import pytest

from media_preview_generator.web.app import (
    _derive_secret,
    _requeue_interrupted_on_startup,
    get_cors_origins,
    get_or_create_flask_secret,
    run_scheduled_job,
)
from media_preview_generator.web.settings_manager import reset_settings_manager


@pytest.fixture(autouse=True)
def _reset():
    reset_settings_manager()
    import media_preview_generator.web.jobs as jobs_mod

    with jobs_mod._job_lock:
        jobs_mod._job_manager = None
    import media_preview_generator.web.scheduler as sched_mod

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
        origins, is_default = get_cors_origins()
        assert origins == "*"
        assert is_default is True

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("CORS_ORIGINS", "http://localhost:3000")
        origins, is_default = get_cors_origins()
        assert origins == "http://localhost:3000"
        assert is_default is False


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

    @patch("media_preview_generator.web.routes._start_job_async")
    def test_creates_and_starts_job(self, mock_start, tmp_path):
        """run_scheduled_job creates a job and calls _start_job_async.

        Uses tmp_path (not a hard-coded /tmp/test_scheduled_job) so xdist
        workers don't race on the same dir and clobber each other's
        auth.json / settings.json mid-test.
        """
        from media_preview_generator.web.app import create_app

        config_dir = str(tmp_path / "scheduled_job")
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
            from media_preview_generator.web.jobs import get_job_manager

            app = create_app(config_dir=config_dir)
            with app.app_context():
                run_scheduled_job(library_name="Movies")
                # Audit fix — bug-blind D34 paradigm. Original test only
                # asserted ``mock_start.assert_called_once()`` which would
                # pass even if run_scheduled_job created a job with no
                # library_name (or the wrong one) and forwarded a bogus
                # job_id. Pin the kwargs the SUT controls: the positional
                # job_id passed to _start_job_async must resolve to a real
                # Job in the manager that carries the seeded library_name.
                mock_start.assert_called_once()
                job_id = mock_start.call_args.args[0]
                assert isinstance(job_id, str) and job_id, (
                    f"_start_job_async must receive a non-empty job_id; got {job_id!r}"
                )
                job = get_job_manager().get_job(job_id)
                assert job is not None, (
                    f"Job {job_id!r} created by run_scheduled_job is missing from JobManager — "
                    f"the schedule callback returned a job_id that doesn't exist."
                )
                assert job.library_name == "Movies", (
                    f"Schedule with library_name='Movies' must produce a Job carrying "
                    f"library_name='Movies'; got library_name={job.library_name!r}."
                )

    @patch("media_preview_generator.web.routes._start_job_async")
    def test_includes_library_id_in_config(self, mock_start, tmp_path):
        from media_preview_generator.web.app import create_app

        config_dir = str(tmp_path / "scheduled_job2")
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
                # Audit fix — original asserted ``"1" in str(config_overrides)``
                # which is a substring match: passes if ANY field in the dict
                # contains "1" (e.g. cpu_threads=1). Now assert the specific
                # field the SUT controls.
                # ``run_scheduled_job`` propagates the library_id ("1") as
                # the legacy ``selected_libraries`` entry (history: scheduler
                # interface predates the selected_library_ids/name split).
                # Whichever field carries it, the dispatcher sees ["1"].
                assert config_overrides.get("selected_library_ids") == ["1"] or config_overrides.get(
                    "selected_libraries"
                ) == ["1"], (
                    f"library_id '1' must propagate as selected_library_ids OR selected_libraries; "
                    f"got {config_overrides!r}"
                )

    @patch("media_preview_generator.web.routes._start_job_async")
    def test_scheduled_job_infers_server_id_from_library_id(self, mock_start, tmp_path):
        """TEST_AUDIT P0.2 — closes incident 933a26d (server-pin gap).

        When a schedule passes ``library_id`` but no ``server_id``, the
        scheduler callback must infer the server from the library.
        Without this inference, scheduled "TV Shows" runs fan out to
        every configured publisher (Emby/Jellyfin) and burn ~5s/item in
        not-in-library lookups for files those servers don't have.

        Pre-fix: the manual /api/jobs POST path inferred but the
        scheduler path did not. Symptom in production: scheduled "TV
        Daily" took 20 min for 202 items, only 1 ran FFmpeg, the rest
        were redundant cross-server lookups.

        Production wiring at app.py:72-76:
            if not server_id and library_id:
                server_id, server_name, server_type = _infer_server_from_library_id(library_id)
        """
        config_dir = str(tmp_path / "scheduled_job_infer")
        os.makedirs(config_dir, exist_ok=True)
        with open(os.path.join(config_dir, "auth.json"), "w") as f:
            json.dump({"token": "test-token-12345678"}, f)
        # Pre-seed media_servers so the inference can find the owning server.
        with open(os.path.join(config_dir, "settings.json"), "w") as f:
            json.dump(
                {
                    "setup_complete": True,
                    "media_servers": [
                        {
                            "id": "plex-tv",
                            "type": "plex",
                            "name": "Plex TV",
                            "enabled": True,
                            "url": "http://plex:32400",
                            "auth": {"token": "tok"},
                            "libraries": [{"id": "42", "name": "TV Shows", "enabled": True}],
                        },
                        # An OTHER server with a DIFFERENT library — must NOT
                        # be picked. Without this in the test data, the
                        # inference has only one option and would pass even
                        # if the library-matching logic were broken.
                        {
                            "id": "emby-other",
                            "type": "emby",
                            "name": "Emby",
                            "enabled": True,
                            "url": "http://emby:8096",
                            "auth": {"api_key": "key"},
                            "libraries": [{"id": "99", "name": "Movies", "enabled": True}],
                        },
                    ],
                },
                f,
            )

        with patch.dict(
            os.environ,
            {"CONFIG_DIR": config_dir, "WEB_AUTH_TOKEN": "test-token-12345678"},
        ):
            from media_preview_generator.web.app import create_app
            from media_preview_generator.web.jobs import get_job_manager

            app = create_app(config_dir=config_dir)
            with app.app_context():
                # Ensure Job manager re-reads settings from this config dir.
                run_scheduled_job(library_id="42", library_name="TV Shows")
                mock_start.assert_called_once()

                # The Job that was created must carry server_id="plex-tv"
                # (inferred from library_id=42 → owned by plex-tv).
                job_id = mock_start.call_args.args[0]
                job = get_job_manager().get_job(job_id)
                assert job is not None, f"Job {job_id} missing from manager"
                assert job.server_id == "plex-tv", (
                    f"Schedule with library_id=42 (owned by plex-tv) must produce a Job "
                    f"with server_id='plex-tv'; got server_id={job.server_id!r}. "
                    f"Pre-fix: server_id stayed empty → orchestrator fanned out to all "
                    f"configured servers including the unrelated Emby (incident 933a26d)."
                )

    @patch("media_preview_generator.web.routes._start_job_async")
    def test_scheduled_job_explicit_server_id_overrides_inference(self, mock_start, tmp_path):
        """When the schedule explicitly passes ``server_id``, no inference
        runs. Pin so a refactor that always-infers (overriding the explicit
        pin) is caught loudly.
        """
        config_dir = str(tmp_path / "scheduled_job_explicit")
        os.makedirs(config_dir, exist_ok=True)
        with open(os.path.join(config_dir, "auth.json"), "w") as f:
            json.dump({"token": "test-token-12345678"}, f)
        with open(os.path.join(config_dir, "settings.json"), "w") as f:
            json.dump(
                {
                    "setup_complete": True,
                    "media_servers": [
                        {
                            "id": "plex-tv",
                            "type": "plex",
                            "name": "Plex TV",
                            "enabled": True,
                            "url": "http://plex:32400",
                            "auth": {"token": "tok"},
                            "libraries": [{"id": "42", "name": "TV Shows", "enabled": True}],
                        },
                        {
                            "id": "emby-explicit",
                            "type": "emby",
                            "name": "Emby Pinned",
                            "enabled": True,
                            "url": "http://emby:8096",
                            "auth": {"api_key": "key"},
                            "libraries": [{"id": "42", "name": "TV Shows", "enabled": True}],
                        },
                    ],
                },
                f,
            )

        with patch.dict(
            os.environ,
            {"CONFIG_DIR": config_dir, "WEB_AUTH_TOKEN": "test-token-12345678"},
        ):
            from media_preview_generator.web.app import create_app
            from media_preview_generator.web.jobs import get_job_manager

            app = create_app(config_dir=config_dir)
            with app.app_context():
                # library_id=42 is owned by BOTH plex-tv and emby-explicit;
                # passing explicit server_id="emby-explicit" must win.
                run_scheduled_job(
                    library_id="42",
                    library_name="TV Shows",
                    server_id="emby-explicit",
                )
                mock_start.assert_called_once()

                job_id = mock_start.call_args.args[0]
                job = get_job_manager().get_job(job_id)
                assert job.server_id == "emby-explicit", (
                    f"Explicit server_id must override inference; got server_id={job.server_id!r} "
                    f"(inference would have picked one of plex-tv / emby-explicit, but explicit wins)"
                )


class TestWsgiModule:
    """Smoke test for wsgi.py module."""

    def test_wsgi_importable(self, tmp_path, monkeypatch):
        """wsgi module IMPORTS cleanly + exports the WSGI app object.

        Audit fix — original asserted ``find_spec is not None`` which only
        checks the file exists. A regression that broke the import path
        (e.g. circular import, missing dependency) would only show up at
        gunicorn startup, not in this test. Actually IMPORT the module
        and verify it exposes ``app`` (the WSGI callable gunicorn loads).

        Environment isolation: ``wsgi.py`` calls ``create_app()`` at
        module load, which writes ``flask_secret.key`` into ``CONFIG_DIR``
        (default ``/config``) AND triggers ``save_auth_config()`` which
        uses the module-level ``auth.AUTH_FILE`` captured at first import
        of ``auth.py``. By the time this test runs, other tests have
        already imported auth, so AUTH_FILE is locked to ``/config/auth.json``.
        On CI runners that path is unwritable → PermissionError.

        Fix: redirect both CONFIG_DIR (env) AND auth.AUTH_FILE (module
        attribute) at tmp_path, then drop any cached wsgi module so the
        re-import sees a writable target.
        """
        import importlib
        import sys

        config_dir = tmp_path / "config"
        config_dir.mkdir()
        monkeypatch.setenv("CONFIG_DIR", str(config_dir))

        # auth.py captures AUTH_FILE at import time from CONFIG_DIR. Since
        # it's almost certainly already imported, the env var alone won't
        # retarget it — we must monkeypatch the attribute directly.
        from media_preview_generator.web import auth as _auth_mod

        monkeypatch.setattr(_auth_mod, "CONFIG_DIR", str(config_dir))
        monkeypatch.setattr(_auth_mod, "AUTH_FILE", str(config_dir / "auth.json"))

        # Force a fresh import — if a previous test (or our own re-run)
        # left wsgi in sys.modules, ``import_module`` would return the
        # cached module and never re-run create_app() against the override.
        sys.modules.pop("media_preview_generator.web.wsgi", None)

        # Find spec is the cheap pre-check.
        spec = importlib.util.find_spec("media_preview_generator.web.wsgi")
        assert spec is not None

        # Actually import the module — catches circular-import / missing-dep
        # regressions that find_spec alone won't detect.
        wsgi = importlib.import_module("media_preview_generator.web.wsgi")
        assert hasattr(wsgi, "app"), (
            "wsgi module must export `app` — gunicorn loads "
            "`media_preview_generator.web.wsgi:app` per pyproject Dockerfile config"
        )
        # The exported app must be a Flask app (or compatible WSGI callable).
        assert callable(wsgi.app), "wsgi.app must be callable (WSGI contract)"


class TestRequeueInterruptedOnStartup:
    """Startup helper only requeues jobs when the setting is enabled."""

    @patch("media_preview_generator.web.routes._start_job_async")
    @patch("media_preview_generator.web.app.get_job_manager")
    @patch("media_preview_generator.web.settings_manager.get_settings_manager")
    def test_string_false_disables_requeue(self, mock_get_settings_manager, mock_get_job_manager, mock_start_job):
        """String 'false' disables startup requeue the same as a bool false."""
        mock_get_settings_manager.return_value.get.side_effect = lambda key, default=None: {
            "auto_requeue_on_restart": "false",
            "requeue_max_age_minutes": 60,
        }.get(key, default)

        _requeue_interrupted_on_startup("/tmp/config")

        mock_get_job_manager.assert_not_called()
        mock_start_job.assert_not_called()

    @patch("media_preview_generator.web.routes._start_job_async")
    @patch("media_preview_generator.web.app.get_job_manager")
    @patch("media_preview_generator.web.settings_manager.get_settings_manager")
    def test_string_true_requeues_jobs(self, mock_get_settings_manager, mock_get_job_manager, mock_start_job):
        """String 'true' still enables startup requeue for persisted settings."""
        mock_get_settings_manager.return_value.get.side_effect = lambda key, default=None: {
            "auto_requeue_on_restart": "true",
            "requeue_max_age_minutes": "45",
        }.get(key, default)
        requeued_job = type("RequeuedJob", (), {"id": "job-123", "config": {"foo": "bar"}})()
        mock_get_job_manager.return_value.requeue_interrupted_jobs.return_value = [requeued_job]

        _requeue_interrupted_on_startup("/tmp/config")

        mock_get_job_manager.return_value.requeue_interrupted_jobs.assert_called_once_with(max_age_minutes=45)
        mock_start_job.assert_called_once_with("job-123", {"foo": "bar"})

    @patch("media_preview_generator.web.routes._start_job_async")
    @patch("media_preview_generator.web.app.get_job_manager")
    @patch("media_preview_generator.web.settings_manager.get_settings_manager")
    def test_processing_paused_cleared_on_startup(
        self, mock_get_settings_manager, mock_get_job_manager, mock_start_job
    ):
        """processing_paused is cleared on restart so requeued jobs can start."""
        sm = mock_get_settings_manager.return_value
        sm.get.side_effect = lambda key, default=None: {
            "auto_requeue_on_restart": True,
            "requeue_max_age_minutes": 720,
        }.get(key, default)
        sm.processing_paused = True

        requeued_job = type("RequeuedJob", (), {"id": "job-456", "config": {}})()
        mock_get_job_manager.return_value.requeue_interrupted_jobs.return_value = [requeued_job]

        _requeue_interrupted_on_startup("/tmp/config")

        assert sm.processing_paused is False
        mock_start_job.assert_called_once_with("job-456", {})


class TestPrewarmCaches:
    """Test background cache pre-warming at startup.

    The previous ``test_prewarm_called_during_create_app`` mocked
    ``_prewarm_caches`` itself and asserted the mock was called — that's
    a tautology (we patch it, then check the patch fired) and tells us
    nothing about whether prewarm actually warms the caches. Replaced
    with a real-side-effect test that mocks the *boundaries*
    (vulkan probe, GPU detection, GitHub HTTP) and asserts those
    boundaries got hit when create_app runs.
    """

    @pytest.mark.real_prewarm
    def test_create_app_triggers_real_prewarm(self, tmp_path):
        """create_app must run prewarm such that the GPU + version helpers fire.

        Mocks at system boundaries (vulkan probe, GPU detection function,
        version helper) so we verify the actual prewarm code path executed,
        not just that we patched a function and called it.
        """
        import json
        import os
        import threading

        from media_preview_generator.web.app import create_app

        config_dir = str(tmp_path / "prewarm_real")
        os.makedirs(config_dir, exist_ok=True)
        auth_file = os.path.join(config_dir, "auth.json")
        with open(auth_file, "w") as f:
            json.dump({"token": "test-token-12345678"}, f)
        settings_file = os.path.join(config_dir, "settings.json")
        with open(settings_file, "w") as f:
            json.dump({"setup_complete": True}, f)

        with (
            patch.dict(
                os.environ,
                {"CONFIG_DIR": config_dir, "WEB_AUTH_TOKEN": "test-token-12345678"},
            ),
            patch(
                "media_preview_generator.gpu.vulkan_probe.get_vulkan_device_info",
                return_value=None,
            ) as mock_vulkan,
            patch(
                "media_preview_generator.web.routes.api_system._get_version_info",
                return_value={"current_version": "1.0.0"},
            ) as mock_version,
            patch(
                "media_preview_generator.web.routes._helpers._ensure_gpu_cache",
            ) as mock_gpu,
        ):
            create_app(config_dir=config_dir)
            # Wait for the daemon threads spawned by _prewarm_caches.
            for t in threading.enumerate():
                if t.name in ("prewarm-gpu", "prewarm-version"):
                    t.join(timeout=5)

            # Vulkan runs synchronously inline; GPU + version run in threads.
            mock_vulkan.assert_called_once()
            mock_gpu.assert_called_once()
            mock_version.assert_called_once()

    @pytest.mark.real_prewarm
    @patch(
        "media_preview_generator.gpu.vulkan_probe.get_vulkan_device_info",
        return_value=None,
    )
    @patch(
        "media_preview_generator.web.routes.api_system._get_version_info",
        return_value={"current_version": "1.0.0"},
    )
    @patch("media_preview_generator.web.routes._helpers._ensure_gpu_cache")
    def test_prewarm_calls_gpu_and_version(self, mock_gpu, mock_version, mock_vulkan):
        """_prewarm_caches starts threads for GPU and version caches."""
        import threading

        from media_preview_generator.web.app import _prewarm_caches

        _prewarm_caches()

        # Wait briefly for daemon threads to finish
        for t in threading.enumerate():
            if t.name in ("prewarm-gpu", "prewarm-version"):
                t.join(timeout=5)

        mock_gpu.assert_called_once()
        mock_version.assert_called_once()
