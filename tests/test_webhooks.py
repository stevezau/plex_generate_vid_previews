"""
Unit tests for Radarr/Sonarr webhook integration.

Covers: authentication, event routing, debouncing, history endpoints,
and the webhooks page route.
"""

import json
import os
from unittest.mock import MagicMock, patch

import pytest

from plex_generate_previews.web.app import create_app
from plex_generate_previews.web.settings_manager import reset_settings_manager

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_singletons():
    """Reset web singletons between tests."""
    reset_settings_manager()
    import plex_generate_previews.web.jobs as jobs_mod

    with jobs_mod._job_lock:
        jobs_mod._job_manager = None
    import plex_generate_previews.web.scheduler as sched_mod

    with sched_mod._schedule_lock:
        sched_mod._schedule_manager = None
    from plex_generate_previews.web.routes import clear_gpu_cache

    clear_gpu_cache()
    # Reset webhook module state
    import plex_generate_previews.web.webhooks as wh

    wh._webhook_history.clear()
    with wh._pending_lock:
        for t in wh._pending_timers.values():
            t.cancel()
        wh._pending_timers.clear()
    yield
    reset_settings_manager()
    with jobs_mod._job_lock:
        jobs_mod._job_manager = None
    with sched_mod._schedule_lock:
        sched_mod._schedule_manager = None
    clear_gpu_cache()
    wh._webhook_history.clear()
    with wh._pending_lock:
        for t in wh._pending_timers.values():
            t.cancel()
        wh._pending_timers.clear()


@pytest.fixture()
def app(tmp_path):
    """Create a Flask test app with a temporary config directory."""
    config_dir = str(tmp_path / "config")
    os.makedirs(config_dir, exist_ok=True)

    auth_file = os.path.join(config_dir, "auth.json")
    with open(auth_file, "w") as f:
        json.dump({"token": "test-token-12345678"}, f)

    settings_file = os.path.join(config_dir, "settings.json")
    with open(settings_file, "w") as f:
        json.dump({"setup_complete": True, "webhook_enabled": True}, f)

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
def client(app):
    return app.test_client()


@pytest.fixture()
def authed_client(client):
    with client.session_transaction() as sess:
        sess["authenticated"] = True
    return client


def _auth_headers(token: str = "test-token-12345678") -> dict:
    return {"X-Auth-Token": token, "Content-Type": "application/json"}


# ---------------------------------------------------------------------------
# Radarr Webhook Tests
# ---------------------------------------------------------------------------


@patch("plex_generate_previews.web.webhooks._schedule_webhook_job")
def test_radarr_webhook_download_event(mock_schedule, client):
    """POST valid Radarr Download payload → 202."""
    payload = {
        "eventType": "Download",
        "movie": {"title": "Inception", "folderPath": "/movies/Inception (2010)"},
    }
    resp = client.post("/api/webhooks/radarr", json=payload, headers=_auth_headers())
    assert resp.status_code == 202
    data = resp.get_json()
    assert data["success"] is True
    assert "Inception" in data["message"]
    mock_schedule.assert_called_once()


@patch("plex_generate_previews.web.webhooks._schedule_webhook_job")
def test_sonarr_webhook_download_event(mock_schedule, client):
    """POST valid Sonarr Download payload → 202."""
    payload = {
        "eventType": "Download",
        "series": {"title": "Breaking Bad"},
        "episodeFile": {"relativePath": "Season 01/S01E01.mkv"},
    }
    resp = client.post("/api/webhooks/sonarr", json=payload, headers=_auth_headers())
    assert resp.status_code == 202
    data = resp.get_json()
    assert data["success"] is True
    assert "Breaking Bad" in data["message"]
    mock_schedule.assert_called_once()


def test_radarr_webhook_test_event(client):
    """POST Test event → 200 with success message."""
    payload = {"eventType": "Test"}
    resp = client.post("/api/webhooks/radarr", json=payload, headers=_auth_headers())
    assert resp.status_code == 200
    assert "configured successfully" in resp.get_json()["message"]


def test_radarr_webhook_grab_ignored(client):
    """POST Grab event → 200, ignored."""
    payload = {"eventType": "Grab", "movie": {"title": "Whatever"}}
    resp = client.post("/api/webhooks/radarr", json=payload, headers=_auth_headers())
    assert resp.status_code == 200
    assert "Ignored" in resp.get_json()["message"]


# ---------------------------------------------------------------------------
# Authentication Tests
# ---------------------------------------------------------------------------


def test_webhook_no_auth(client):
    """POST without token → 401."""
    resp = client.post(
        "/api/webhooks/radarr",
        json={"eventType": "Test"},
        content_type="application/json",
    )
    assert resp.status_code == 401


def test_webhook_invalid_token(client):
    """POST with bad token → 401."""
    resp = client.post(
        "/api/webhooks/radarr",
        json={"eventType": "Test"},
        headers=_auth_headers("wrong-token"),
    )
    assert resp.status_code == 401


def test_webhook_secret_auth(client, app):
    """Configure webhook_secret and authenticate with it."""
    from plex_generate_previews.web.settings_manager import get_settings_manager

    with app.app_context():
        sm = get_settings_manager()
        sm.set("webhook_secret", "my-webhook-secret-1234")

    payload = {"eventType": "Test"}
    resp = client.post(
        "/api/webhooks/radarr",
        json=payload,
        headers=_auth_headers("my-webhook-secret-1234"),
    )
    assert resp.status_code == 200
    assert resp.get_json()["success"] is True


# ---------------------------------------------------------------------------
# Disabled / Malformed Tests
# ---------------------------------------------------------------------------


@patch("plex_generate_previews.web.webhooks._schedule_webhook_job")
def test_webhook_disabled(mock_schedule, client, app):
    """When webhook_enabled is False → 200 with disabled message."""
    from plex_generate_previews.web.settings_manager import get_settings_manager

    with app.app_context():
        sm = get_settings_manager()
        sm.set("webhook_enabled", False)

    payload = {"eventType": "Download", "movie": {"title": "Test"}}
    resp = client.post("/api/webhooks/radarr", json=payload, headers=_auth_headers())
    assert resp.status_code == 200
    assert "disabled" in resp.get_json()["message"].lower()
    mock_schedule.assert_not_called()


def test_webhook_malformed_payload(client):
    """POST with empty body → 400."""
    resp = client.post(
        "/api/webhooks/radarr",
        data="",
        content_type="application/json",
        headers=_auth_headers(),
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# History Tests
# ---------------------------------------------------------------------------


def test_webhook_history_endpoint(authed_client):
    """GET /api/webhooks/history returns events list."""
    # Trigger a test event first to populate history
    authed_client.post(
        "/api/webhooks/radarr",
        json={"eventType": "Test"},
        headers=_auth_headers(),
    )

    resp = authed_client.get("/api/webhooks/history", headers=_auth_headers())
    assert resp.status_code == 200
    data = resp.get_json()
    assert "events" in data
    assert len(data["events"]) >= 1
    assert data["events"][0]["source"] == "radarr"


def test_webhook_clear_history(authed_client):
    """DELETE /api/webhooks/history clears events."""
    authed_client.post(
        "/api/webhooks/radarr",
        json={"eventType": "Test"},
        headers=_auth_headers(),
    )

    resp = authed_client.delete("/api/webhooks/history", headers=_auth_headers())
    assert resp.status_code == 200
    assert resp.get_json()["success"] is True

    resp = authed_client.get("/api/webhooks/history", headers=_auth_headers())
    assert len(resp.get_json()["events"]) == 0


# ---------------------------------------------------------------------------
# Debounce Test
# ---------------------------------------------------------------------------


@patch("plex_generate_previews.web.webhooks.threading.Timer")
def test_webhook_debounce(mock_timer_cls, client):
    """Two rapid webhooks for same library should cancel the first timer."""
    mock_timer = MagicMock()
    mock_timer.daemon = True
    mock_timer_cls.return_value = mock_timer

    payload = {
        "eventType": "Download",
        "movie": {"title": "Movie A"},
    }

    # First webhook
    client.post("/api/webhooks/radarr", json=payload, headers=_auth_headers())
    # Second webhook (same library — should debounce)
    payload["movie"]["title"] = "Movie B"
    client.post("/api/webhooks/radarr", json=payload, headers=_auth_headers())

    # The first timer should have been cancelled
    assert mock_timer.cancel.called
    # Timer should have been created twice
    assert mock_timer_cls.call_count == 2


# ---------------------------------------------------------------------------
# Page Route Test
# ---------------------------------------------------------------------------


def test_webhooks_page_requires_login(client):
    """GET /webhooks without session → redirect to login."""
    resp = client.get("/webhooks")
    assert resp.status_code == 302
    assert "/login" in resp.headers.get("Location", "")


def test_webhooks_page_authenticated(authed_client):
    """GET /webhooks with session → 200."""
    resp = authed_client.get("/webhooks")
    assert resp.status_code == 200
    assert b"Webhooks" in resp.data
