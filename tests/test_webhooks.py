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
        wh._pending_batches.clear()
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
        wh._pending_batches.clear()


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


def test_format_sonarr_episode_title():
    """_format_sonarr_episode_title adds SxxExx from episodes to series title."""
    from plex_generate_previews.web.webhooks import _format_sonarr_episode_title

    assert _format_sonarr_episode_title("Show Name", []) == "Show Name"
    assert _format_sonarr_episode_title("Show Name", None) == "Show Name"
    assert (
        _format_sonarr_episode_title("Show", [{"seasonNumber": 1, "episodeNumber": 5}])
        == "Show S01E05"
    )
    assert (
        _format_sonarr_episode_title(
            "Murder at the Post Office",
            [{"seasonNumber": 1, "episodeNumber": 5}],
        )
        == "Murder at the Post Office S01E05"
    )
    assert (
        _format_sonarr_episode_title(
            "Show",
            [
                {"seasonNumber": 2, "episodeNumber": 3},
                {"seasonNumber": 2, "episodeNumber": 4},
            ],
        )
        == "Show S02E03, S02E04"
    )


# ---------------------------------------------------------------------------
# Radarr Webhook Tests
# ---------------------------------------------------------------------------


@patch("plex_generate_previews.web.webhooks._schedule_webhook_job")
def test_radarr_webhook_download_event(mock_schedule, client):
    """POST valid Radarr Download payload → 202 and schedules job with file path."""
    payload = {
        "eventType": "Download",
        "movie": {"title": "Inception", "folderPath": "/movies/Inception (2010)"},
        "movieFile": {"path": "/movies/Inception (2010)/Inception.mkv"},
    }
    resp = client.post("/api/webhooks/radarr", json=payload, headers=_auth_headers())
    assert resp.status_code == 202
    data = resp.get_json()
    assert data["success"] is True
    assert "Inception" in data["message"]
    mock_schedule.assert_called_once_with(
        "radarr", "Inception", "/movies/Inception (2010)/Inception.mkv"
    )


@patch("plex_generate_previews.web.webhooks._schedule_webhook_job")
def test_sonarr_webhook_download_event(mock_schedule, client):
    """POST valid Sonarr Download payload → 202 and schedules job with file path."""
    payload = {
        "eventType": "Download",
        "series": {"title": "Breaking Bad"},
        "episodeFile": {"path": "/tv/Breaking Bad/Season 01/S01E01.mkv"},
    }
    resp = client.post("/api/webhooks/sonarr", json=payload, headers=_auth_headers())
    assert resp.status_code == 202
    data = resp.get_json()
    assert data["success"] is True
    assert "Breaking Bad" in data["message"]
    mock_schedule.assert_called_once_with(
        "sonarr", "Breaking Bad", "/tv/Breaking Bad/Season 01/S01E01.mkv"
    )


@patch("plex_generate_previews.web.webhooks._schedule_webhook_job")
def test_sonarr_webhook_download_with_episode_info_includes_season_episode_in_title(
    mock_schedule, client
):
    """Sonarr payload with episodes[] → title includes SxxExx in webhook and history."""
    payload = {
        "eventType": "Download",
        "series": {"title": "Murder at the Post Office"},
        "episodes": [
            {"seasonNumber": 1, "episodeNumber": 5, "title": "The Letter"},
        ],
        "episodeFile": {"path": "/tv/Murder at the Post Office/Season 01/S01E05.mkv"},
    }
    resp = client.post("/api/webhooks/sonarr", json=payload, headers=_auth_headers())
    assert resp.status_code == 202
    data = resp.get_json()
    assert data["success"] is True
    assert "S01E05" in data["message"]
    assert "Murder at the Post Office" in data["message"]
    mock_schedule.assert_called_once_with(
        "sonarr",
        "Murder at the Post Office S01E05",
        "/tv/Murder at the Post Office/Season 01/S01E05.mkv",
    )


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
    """Configure webhook_secret and authenticate with it (X-Auth-Token)."""
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


def test_webhook_bearer_token_auth(client, app):
    """Webhook accepts Authorization: Bearer with webhook_secret."""
    from plex_generate_previews.web.settings_manager import get_settings_manager

    with app.app_context():
        sm = get_settings_manager()
        sm.set("webhook_secret", "bearer-secret-1234")

    resp = client.post(
        "/api/webhooks/radarr",
        json={"eventType": "Test"},
        headers={
            "Authorization": "Bearer bearer-secret-1234",
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 200
    assert resp.get_json()["success"] is True


def test_webhook_basic_auth_password_as_token(client):
    """Webhook accepts Basic auth with token in password (for Sonarr/Radarr Username/Password field)."""
    import base64

    creds = base64.b64encode(b":test-token-12345678").decode("ascii")
    resp = client.post(
        "/api/webhooks/radarr",
        json={"eventType": "Test"},
        headers={
            "Authorization": f"Basic {creds}",
            "Content-Type": "application/json",
        },
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

    payload = {
        "eventType": "Download",
        "movie": {"title": "Test"},
        "movieFile": {"path": "/movies/Test/Test.mkv"},
    }
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


@patch("plex_generate_previews.web.webhooks.logger.warning")
def test_webhook_malformed_payload_logs_warning(mock_warning, client):
    """Malformed webhook JSON should be rejected and logged."""
    resp = client.post(
        "/api/webhooks/sonarr",
        data="",
        content_type="application/json",
        headers=_auth_headers(),
    )
    assert resp.status_code == 400
    mock_warning.assert_called_once()


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
    """Two rapid webhooks for same source should cancel the first timer."""
    mock_timer = MagicMock()
    mock_timer.daemon = True
    mock_timer_cls.return_value = mock_timer

    payload = {
        "eventType": "Download",
        "movie": {"title": "Movie A"},
        "movieFile": {"path": "/movies/Movie A/Movie A.mkv"},
    }

    # First webhook
    client.post("/api/webhooks/radarr", json=payload, headers=_auth_headers())
    # Second webhook (same source — should debounce)
    payload["movie"]["title"] = "Movie B"
    payload["movieFile"]["path"] = "/movies/Movie B/Movie B.mkv"
    client.post("/api/webhooks/radarr", json=payload, headers=_auth_headers())

    # The first timer should have been cancelled
    assert mock_timer.cancel.called
    # Timer should have been created twice
    assert mock_timer_cls.call_count == 2


def test_radarr_download_missing_file_path_is_ignored(client):
    """Radarr Download payload without file path should not queue a job."""
    payload = {"eventType": "Download", "movie": {"title": "No Path Movie"}}
    resp = client.post("/api/webhooks/radarr", json=payload, headers=_auth_headers())
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["success"] is True
    assert "no file path" in data["message"].lower()


@patch("plex_generate_previews.web.webhooks.logger.warning")
def test_radarr_download_missing_file_path_logs_warning(mock_warning, client):
    """Missing Radarr file path should emit a warning log."""
    payload = {"eventType": "Download", "movie": {"title": "No Path Movie"}}
    resp = client.post("/api/webhooks/radarr", json=payload, headers=_auth_headers())
    assert resp.status_code == 200
    assert any("missing file path" in str(call) for call in mock_warning.call_args_list)


def test_sonarr_download_missing_file_path_is_ignored(client):
    """Sonarr Download payload without file path should not queue a job."""
    payload = {"eventType": "Download", "series": {"title": "No Path Show"}}
    resp = client.post("/api/webhooks/sonarr", json=payload, headers=_auth_headers())
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["success"] is True
    assert "no file path" in data["message"].lower()


def test_radarr_download_malformed_movie_file_payload_is_ignored(client):
    """Malformed movieFile payload should not crash and should be ignored."""
    payload = {
        "eventType": "Download",
        "movie": {"title": "Bad Payload", "folderPath": "/movies/Bad Payload"},
        "movieFile": ["not-a-dict"],
    }
    resp = client.post("/api/webhooks/radarr", json=payload, headers=_auth_headers())
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["success"] is True
    assert "no file path" in data["message"].lower()


def test_sonarr_download_malformed_episode_file_payload_is_ignored(client):
    """Malformed episodeFile payload should not crash and should be ignored."""
    payload = {
        "eventType": "Download",
        "series": {"title": "Bad Show", "path": "/tv/Bad Show"},
        "episodeFile": ["not-a-dict"],
    }
    resp = client.post("/api/webhooks/sonarr", json=payload, headers=_auth_headers())
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["success"] is True
    assert "no file path" in data["message"].lower()


@patch("plex_generate_previews.web.webhooks.get_job_manager")
@patch("plex_generate_previews.web.webhooks.threading.Timer")
@patch("plex_generate_previews.web.routes._start_job_async")
def test_execute_webhook_job_batches_paths(
    mock_start_job, mock_timer_cls, mock_job_mgr
):
    """Debounced execution should pass batched webhook paths in one job."""
    from plex_generate_previews.web import webhooks as wh

    mock_timer = MagicMock()
    mock_timer.daemon = True
    mock_timer_cls.return_value = mock_timer

    mock_job = MagicMock()
    mock_job.id = "test-job-id"
    mock_job_mgr.return_value.create_job.return_value = mock_job

    wh._schedule_webhook_job("radarr", "Movie A", "/movies/A.mkv")
    wh._schedule_webhook_job("radarr", "Movie B", "/movies/B.mkv")
    key = wh._debounce_key("radarr")

    wh._execute_webhook_job(key)

    mock_start_job.assert_called_once()
    config_overrides = mock_start_job.call_args[0][1]
    assert sorted(config_overrides["webhook_paths"]) == [
        "/movies/A.mkv",
        "/movies/B.mkv",
    ]


@patch("plex_generate_previews.web.webhooks.get_settings_manager")
@patch("plex_generate_previews.web.webhooks.get_job_manager")
@patch("plex_generate_previews.web.webhooks.threading.Timer")
@patch("plex_generate_previews.web.routes._start_job_async")
def test_execute_webhook_job_single_file_uses_title_for_library_display(
    mock_start_job, mock_timer_cls, mock_job_mgr, mock_settings_mgr
):
    """Single-file webhook job uses display title (e.g. Show S01E05) in library_name for dashboard."""
    from plex_generate_previews.web import webhooks as wh

    mock_timer = MagicMock()
    mock_timer.daemon = True
    mock_timer_cls.return_value = mock_timer

    mock_job = MagicMock()
    mock_job.id = "job-1"
    mock_job_mgr.return_value.create_job.return_value = mock_job

    mock_settings = MagicMock()
    mock_settings.get.side_effect = lambda key, default=None: (
        [] if key == "selected_libraries" else default
    )
    mock_settings_mgr.return_value = mock_settings

    wh._schedule_webhook_job(
        "sonarr",
        "Murder at the Post Office S01E05",
        "/tv/Murder at the Post Office/Season 01/S01E05.mkv",
    )
    wh._execute_webhook_job(wh._debounce_key("sonarr"))

    mock_job_mgr.return_value.create_job.assert_called_once()
    call_kw = mock_job_mgr.return_value.create_job.call_args[1]
    assert call_kw["library_name"] == "Sonarr: Murder at the Post Office S01E05"


@patch("plex_generate_previews.web.webhooks.get_settings_manager")
@patch("plex_generate_previews.web.webhooks.get_job_manager")
@patch("plex_generate_previews.web.webhooks.threading.Timer")
@patch("plex_generate_previews.web.routes._start_job_async")
def test_execute_webhook_job_uses_selected_libraries(
    mock_start_job,
    mock_timer_cls,
    mock_job_mgr,
    mock_settings_mgr,
):
    """Webhook jobs should pass selected library IDs from settings."""
    from plex_generate_previews.web import webhooks as wh

    mock_timer = MagicMock()
    mock_timer.daemon = True
    mock_timer_cls.return_value = mock_timer

    mock_job = MagicMock()
    mock_job.id = "test-job-id"
    mock_job_mgr.return_value.create_job.return_value = mock_job

    mock_settings = MagicMock()
    mock_settings.get.side_effect = lambda key, default=None: (
        ["1", "2"] if key == "selected_libraries" else default
    )
    mock_settings_mgr.return_value = mock_settings

    wh._schedule_webhook_job("radarr", "Movie A", "/movies/A.mkv")
    wh._execute_webhook_job(wh._debounce_key("radarr"))

    config_overrides = mock_start_job.call_args[0][1]
    assert config_overrides["selected_libraries"] == ["1", "2"]


@patch("plex_generate_previews.web.webhooks.get_settings_manager")
@patch("plex_generate_previews.web.webhooks.get_job_manager")
@patch("plex_generate_previews.web.webhooks.threading.Timer")
@patch("plex_generate_previews.web.routes._start_job_async")
def test_execute_webhook_job_includes_retry_settings(
    mock_start_job, mock_timer_cls, mock_job_mgr, mock_settings_mgr
):
    """Webhook job config_overrides include webhook_retry_count and webhook_retry_delay from settings."""
    from plex_generate_previews.web import webhooks as wh

    mock_timer = MagicMock()
    mock_timer.daemon = True
    mock_timer_cls.return_value = mock_timer

    mock_job = MagicMock()
    mock_job.id = "retry-test-id"
    mock_job_mgr.return_value.create_job.return_value = mock_job

    mock_settings = MagicMock()
    mock_settings.get.side_effect = lambda key, default=None: {
        "selected_libraries": [],
        "webhook_retry_count": 5,
        "webhook_retry_delay": 120,
    }.get(key, default)
    mock_settings_mgr.return_value = mock_settings

    wh._schedule_webhook_job("radarr", "Movie A", "/movies/A.mkv")
    wh._execute_webhook_job(wh._debounce_key("radarr"))

    config_overrides = mock_start_job.call_args[0][1]
    assert config_overrides["webhook_retry_count"] == 5
    assert config_overrides["webhook_retry_delay"] == 120


@patch("plex_generate_previews.web.webhooks.get_job_manager")
@patch("plex_generate_previews.web.webhooks.threading.Timer")
@patch("plex_generate_previews.web.routes._start_job_async")
def test_webhook_payload_path_in_job_config_for_mapping(
    mock_start_job, mock_timer_cls, mock_job_mgr, client
):
    """Path extracted from Radarr payload is passed in job config for mapping-aware resolution."""
    from plex_generate_previews.web import webhooks as wh

    # Timer: do not start so we can run _execute_webhook_job ourselves after POST
    mock_timer = MagicMock()
    mock_timer.daemon = True
    mock_timer_cls.return_value = mock_timer

    mock_job = MagicMock()
    mock_job.id = "webhook-job-id"
    mock_job_mgr.return_value.create_job.return_value = mock_job

    payload_path = "/data/Movies/Test Movie (2024)/Test Movie.mkv"
    payload = {
        "eventType": "Download",
        "movie": {
            "title": "Test Movie",
            "folderPath": "/data/Movies/Test Movie (2024)",
        },
        "movieFile": {"path": payload_path},
    }
    resp = client.post("/api/webhooks/radarr", json=payload, headers=_auth_headers())
    assert resp.status_code == 202

    wh._execute_webhook_job(wh._debounce_key("radarr"))

    mock_start_job.assert_called_once()
    config_overrides = mock_start_job.call_args[0][1]
    assert "webhook_paths" in config_overrides
    expected_path = os.path.normpath(payload_path)
    assert expected_path in config_overrides["webhook_paths"], (
        f"Payload path should be in job config for mapping; got webhook_paths={config_overrides['webhook_paths']}"
    )


@patch("plex_generate_previews.web.webhooks.get_settings_manager")
@patch("plex_generate_previews.web.webhooks.get_job_manager")
@patch("plex_generate_previews.web.webhooks.threading.Timer")
@patch("plex_generate_previews.web.routes._start_job_async")
def test_triggered_history_entry_includes_batch_metadata(
    mock_start_job, mock_timer_cls, mock_job_mgr, mock_settings_mgr, authed_client
):
    """Triggered webhook batch adds history entry with job_id, path_count, and files_preview."""
    from plex_generate_previews.web import webhooks as wh

    mock_timer = MagicMock()
    mock_timer.daemon = True
    mock_timer_cls.return_value = mock_timer

    mock_job = MagicMock()
    mock_job.id = "batch-job-123"
    mock_job_mgr.return_value.create_job.return_value = mock_job

    mock_settings = MagicMock()
    mock_settings.get.side_effect = lambda key, default=None: (
        [] if key == "selected_libraries" else default
    )
    mock_settings_mgr.return_value = mock_settings

    wh._schedule_webhook_job("sonarr", "Show", "/tv/Show/S01E01.mkv")
    wh._schedule_webhook_job("sonarr", "Show", "/tv/Show/S01E02.mkv")
    wh._execute_webhook_job(wh._debounce_key("sonarr"))

    resp = authed_client.get("/api/webhooks/history", headers=_auth_headers())
    assert resp.status_code == 200
    events = resp.get_json()["events"]
    triggered = [e for e in events if e.get("status") == "triggered"]
    assert len(triggered) >= 1
    evt = triggered[0]
    assert evt.get("job_id") == "batch-job-123"
    assert evt.get("title") == "Show"  # first batch title used for triggered entry
    assert evt.get("path_count") == 2
    assert evt.get("files_preview") == ["S01E01.mkv", "S01E02.mkv"]


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
