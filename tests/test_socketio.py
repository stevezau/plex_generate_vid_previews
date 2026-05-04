"""
SocketIO integration tests.

Tests event emission, auth rejection, channel subscription, and
real-time job update events using flask-socketio's test client.

Requires flask-socketio's built-in test client (no browser needed).
"""

import json
import os
from unittest.mock import patch

import pytest

from media_preview_generator.web.app import create_app, socketio
from media_preview_generator.web.settings_manager import reset_settings_manager

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_singletons():
    """Reset web singletons between tests."""
    reset_settings_manager()
    import media_preview_generator.web.jobs as jobs_mod

    with jobs_mod._job_lock:
        jobs_mod._job_manager = None
    import media_preview_generator.web.scheduler as sched_mod

    with sched_mod._schedule_lock:
        sched_mod._schedule_manager = None
    yield
    reset_settings_manager()
    with jobs_mod._job_lock:
        jobs_mod._job_manager = None
    with sched_mod._schedule_lock:
        if sched_mod._schedule_manager is not None:
            try:
                sched_mod._schedule_manager.stop()
            except Exception:
                pass
            sched_mod._schedule_manager = None


@pytest.fixture()
def app(tmp_path):
    """Create a Flask app for SocketIO testing."""
    config_dir = str(tmp_path / "config")
    os.makedirs(config_dir, exist_ok=True)

    auth_file = os.path.join(config_dir, "auth.json")
    with open(auth_file, "w") as f:
        json.dump({"token": "test-token-12345678"}, f)

    with patch.dict(
        os.environ,
        {
            "CONFIG_DIR": config_dir,
            "WEB_AUTH_TOKEN": "test-token-12345678",
            "WEB_PORT": "8099",
        },
    ):
        flask_app = create_app(config_dir=config_dir)
        flask_app.config["TESTING"] = True
        flask_app.config["WTF_CSRF_ENABLED"] = False
        yield flask_app


@pytest.fixture()
def authed_socketio_client(app):
    """Create a SocketIO test client with an authenticated session.

    Uses flask-socketio's built-in test_client which works synchronously
    in tests.
    """
    flask_test_client = app.test_client()
    # Authenticate via session
    with flask_test_client.session_transaction() as sess:
        sess["authenticated"] = True

    try:
        sio_client = socketio.test_client(
            app,
            namespace="/jobs",
            flask_test_client=flask_test_client,
        )
    except Exception:
        pytest.skip("flask-socketio test client not available")
        return

    yield sio_client
    if sio_client.is_connected(namespace="/jobs"):
        sio_client.disconnect(namespace="/jobs")


@pytest.fixture()
def unauthed_socketio_client(app):
    """Create a SocketIO test client WITHOUT authentication."""
    flask_test_client = app.test_client()
    # Do NOT set session["authenticated"]
    try:
        sio_client = socketio.test_client(
            app,
            namespace="/jobs",
            flask_test_client=flask_test_client,
        )
    except Exception:
        pytest.skip("flask-socketio test client not available")
        return

    yield sio_client
    try:
        if sio_client.is_connected(namespace="/jobs"):
            sio_client.disconnect(namespace="/jobs")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Connection Tests
# ---------------------------------------------------------------------------


class TestSocketIOConnection:
    """Test SocketIO connect/disconnect behavior."""

    def test_authenticated_client_connects(self, authed_socketio_client):
        assert authed_socketio_client.is_connected(namespace="/jobs")

    def test_unauthenticated_client_rejected(self, unauthed_socketio_client):
        """Unauthenticated clients should be disconnected by the connect handler."""
        # The server calls disconnect() on auth failure, so the client
        # should not remain connected.
        assert not unauthed_socketio_client.is_connected(namespace="/jobs")

    def test_disconnect_works(self, authed_socketio_client):
        authed_socketio_client.disconnect(namespace="/jobs")
        assert not authed_socketio_client.is_connected(namespace="/jobs")


# ---------------------------------------------------------------------------
# Subscribe / Unsubscribe
# ---------------------------------------------------------------------------


class TestSubscription:
    """Test room subscription via subscribe/unsubscribe events."""

    def test_subscribe_to_job(self, authed_socketio_client):
        """Subscribe handler must accept the event silently and keep the connection live.

        ``isinstance(received, list)`` was tautological — get_received() ALWAYS
        returns a list. We assert the meaningful invariants instead: the
        handler is silent (no echo back), the client stays connected (didn't
        get bounced by an auth check inside the handler), and an empty/missing
        job_id is tolerated (the early-return branch).
        """
        authed_socketio_client.emit("subscribe", {"job_id": "test-job-123"}, namespace="/jobs")
        received = authed_socketio_client.get_received(namespace="/jobs")
        assert received == [], f"subscribe should be silent, got {received!r}"
        assert authed_socketio_client.is_connected(namespace="/jobs")

        # Empty payload should hit the `if job_id:` early return without raising.
        authed_socketio_client.emit("subscribe", {}, namespace="/jobs")
        assert authed_socketio_client.get_received(namespace="/jobs") == []
        assert authed_socketio_client.is_connected(namespace="/jobs")

    def test_unsubscribe_from_job(self, app, authed_socketio_client):
        """Unsubscribe handler must accept the event silently and keep the connection live.

        ``isinstance(received, list)`` was tautological. Note: the production
        ``_emit_event`` in ``JobManager`` broadcasts to the namespace (not to
        a specific room), so we cannot assert "no events arrive after
        leave_room" — that would test behaviour the code doesn't implement.
        Instead we verify the handler's real contract: it returns silently,
        doesn't disconnect the client, and is idempotent.
        """
        authed_socketio_client.emit("subscribe", {"job_id": "test-job-456"}, namespace="/jobs")
        # Drain whatever the subscribe handler may have emitted (currently nothing).
        authed_socketio_client.get_received(namespace="/jobs")

        authed_socketio_client.emit("unsubscribe", {"job_id": "test-job-456"}, namespace="/jobs")
        received = authed_socketio_client.get_received(namespace="/jobs")
        assert received == [], f"unsubscribe should be silent, got {received!r}"
        assert authed_socketio_client.is_connected(namespace="/jobs")

        # Idempotent: leaving an already-left room must not crash or emit.
        authed_socketio_client.emit("unsubscribe", {"job_id": "test-job-456"}, namespace="/jobs")
        assert authed_socketio_client.get_received(namespace="/jobs") == []
        assert authed_socketio_client.is_connected(namespace="/jobs")


# ---------------------------------------------------------------------------
# Event Emission from JobManager
# ---------------------------------------------------------------------------


def _wait_for_event(sio_client, event_name: str, timeout: float = 2.0) -> list:
    """Drain receive buffer until *event_name* arrives or *timeout* elapses.

    ``JobManager._emit_event`` spawns a daemon thread per emit (see
    ``media_preview_generator/web/jobs.py:265-272``) so the SocketIO event
    arrives asynchronously and can lose a race against ``get_received``
    — especially under pytest-xdist + coverage where the emit thread gets
    scheduled unpredictably. Poll instead of read-once.
    """
    import time as _time

    deadline = _time.monotonic() + timeout
    collected: list = []
    while _time.monotonic() < deadline:
        batch = sio_client.get_received(namespace="/jobs")
        if batch:
            collected.extend(batch)
            if any(r["name"] == event_name for r in collected):
                return collected
        _time.sleep(0.01)
    return collected


class TestJobEvents:
    """Test that job lifecycle events are emitted via SocketIO."""

    def test_job_created_event(self, app, authed_socketio_client):
        """Creating a job should emit a job_created event."""
        from media_preview_generator.web.jobs import get_job_manager

        job_manager = get_job_manager()
        job = job_manager.create_job(library_name="Movies")

        received = _wait_for_event(authed_socketio_client, "job_created")
        event_names = [r["name"] for r in received]
        assert "job_created" in event_names

        # Verify payload
        created_events = [r for r in received if r["name"] == "job_created"]
        payload = created_events[0]["args"][0]
        assert payload["id"] == job.id
        assert payload["library_name"] == "Movies"

    def test_job_started_event(self, app, authed_socketio_client):
        """Starting a job should emit a job_started event."""
        from media_preview_generator.web.jobs import get_job_manager

        job_manager = get_job_manager()
        job = job_manager.create_job(library_name="TV")
        # Drain the creation event
        _wait_for_event(authed_socketio_client, "job_created")

        job_manager.start_job(job.id)
        received = _wait_for_event(authed_socketio_client, "job_started")
        event_names = [r["name"] for r in received]
        assert "job_started" in event_names

    def test_progress_update_event(self, app, authed_socketio_client):
        """Progress updates should emit events with the right payload.

        Audit fix — original asserted only that the event fired ("event
        emitted, but does it carry the right data?"). A regression that
        emitted the event with stale or wrong payload values would have
        passed. Now also assert the payload reflects the values we
        passed into update_progress.
        """
        from media_preview_generator.web.jobs import get_job_manager

        job_manager = get_job_manager()
        job = job_manager.create_job(library_name="Anime")
        job_manager.start_job(job.id)
        _wait_for_event(authed_socketio_client, "job_started")

        job_manager.update_progress(
            job.id,
            percent=50.0,
            processed_items=5,
            total_items=10,
            current_item="Episode 5",
        )

        received = _wait_for_event(authed_socketio_client, "job_progress")
        event_names = [r["name"] for r in received]
        assert "job_progress" in event_names

        # Inspect the payload — must carry our values. The wire shape is
        # ``{"job_id": "...", "progress": {percent, processed_items, total_items, current_item, ...}}``.
        progress_events = [r for r in received if r["name"] == "job_progress"]
        payload = progress_events[-1]["args"][0]
        assert payload.get("job_id") == job.id, f"job_progress event for wrong job: {payload!r}"
        progress = payload.get("progress") or {}
        assert progress.get("percent") == 50.0, f"percent missing or wrong: {payload!r}"
        assert progress.get("processed_items") == 5, f"processed_items missing or wrong: {payload!r}"
        assert progress.get("total_items") == 10, f"total_items missing or wrong: {payload!r}"
        assert progress.get("current_item") == "Episode 5", f"current_item missing or wrong: {payload!r}"

    def test_job_completed_event(self, app, authed_socketio_client):
        """Completing a job should emit a job_completed event with completed status."""
        from media_preview_generator.web.jobs import get_job_manager

        job_manager = get_job_manager()
        job = job_manager.create_job(library_name="Music Videos")
        job_manager.start_job(job.id)
        _wait_for_event(authed_socketio_client, "job_started")

        job_manager.complete_job(job.id)
        received = _wait_for_event(authed_socketio_client, "job_completed")
        event_names = [r["name"] for r in received]
        assert "job_completed" in event_names

        updated = [r for r in received if r["name"] == "job_completed"]
        assert updated[0]["args"][0]["status"] == "completed"


# ---------------------------------------------------------------------------
# TEST_AUDIT P0.6 — SocketIO server config pin (incident: 1873a23)
# ---------------------------------------------------------------------------


class TestSocketIOTransportConfig:
    """Pin SocketIO transport config so the WebSocket-upgrade regression
    cannot ship silently again.

    Background (commit 1873a23): the project originally had
    ``allow_upgrades=False`` to keep SocketIO on HTTP long-polling. Once
    a client upgrades to WebSocket under ``async_mode="threading"`` it
    pins one gunicorn thread for its lifetime; refreshed pages leave
    CLOSE_WAIT sockets that exhaust the thread pool and freeze the UI
    with "Failed to fetch" on /api/pause and 500s on GET /api/jobs.

    The setting was LOST during the plex_generate_previews →
    media_preview_generator package rename. This test introspects the
    actual underlying engineio Server config so a future rename, refactor,
    or deps bump that drops the kwarg fails loudly here instead of in
    production after a 20-minute job.
    """

    def test_allow_upgrades_is_false_on_underlying_engineio_server(self, app):
        """The engineio.Server instance must report allow_upgrades=False.

        flask-socketio wraps a python-socketio Server, which wraps an
        engineio.Server (``socketio.server.eio``). Asserting against the
        engineio Server is the production source of truth — checking the
        kwarg in the Python source would only catch deletion, not a deps
        bump that silently changed the default.
        """
        # ``socketio`` is the module-level Flask-SocketIO instance imported
        # at the top of this file. After ``create_app(app)`` it has been
        # initialised against this app.
        eio_server = socketio.server.eio
        assert eio_server.allow_upgrades is False, (
            f"engineio.Server.allow_upgrades must be False to keep transport on HTTP "
            f"long-polling; got {eio_server.allow_upgrades!r}. A client that successfully "
            f"upgrades to WebSocket under async_mode='threading' will pin a gunicorn "
            f"thread indefinitely — the 1873a23 'Failed to fetch / frozen UI' bug class."
        )

    def test_async_mode_is_threading(self, app):
        """async_mode must stay 'threading' — eventlet/gevent monkey-patch
        breaks worker.py subprocess.* calls (GitHub #154).

        Pin together with allow_upgrades because the two are inseparable
        design constraints documented at app.py:437-448. A regression
        flipping async_mode to eventlet would re-trigger the FFmpeg
        subprocess hangs without breaking allow_upgrades=False.
        """
        assert socketio.async_mode == "threading", (
            f"async_mode must remain 'threading' (eventlet/gevent breaks "
            f"worker subprocess calls per #154); got {socketio.async_mode!r}"
        )
