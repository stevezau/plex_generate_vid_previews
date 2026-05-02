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
        """Progress updates should emit events."""
        from media_preview_generator.web.jobs import get_job_manager

        job_manager = get_job_manager()
        job = job_manager.create_job(library_name="Anime")
        job_manager.start_job(job.id)
        # Drain setup events
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
