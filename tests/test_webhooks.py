"""
Unit tests for Radarr/Sonarr/Custom webhook integration.

Covers: authentication, event routing, debouncing, history endpoints,
and the webhooks page route.
"""

import json
import os
from unittest.mock import MagicMock, patch

import pytest

from media_preview_generator.web.app import create_app
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
    from media_preview_generator.web.routes import clear_gpu_cache

    clear_gpu_cache()
    # Reset webhook module state
    import media_preview_generator.web.webhooks as wh

    wh._webhook_history.clear()
    with wh._pending_lock:
        for t in wh._pending_timers.values():
            t.cancel()
        wh._pending_timers.clear()
        wh._pending_batches.clear()
        wh._recent_dispatches.clear()
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
    clear_gpu_cache()
    wh._webhook_history.clear()
    with wh._pending_lock:
        for t in wh._pending_timers.values():
            t.cancel()
        wh._pending_timers.clear()
        wh._pending_batches.clear()
        wh._recent_dispatches.clear()


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


def test_clean_title_from_basename():
    """_clean_title_from_basename collapses raw filenames to a Sonarr-style title."""
    from media_preview_generator.web.webhooks import _clean_title_from_basename as f

    # Episode with year + dash separators (most common Sonarr layout)
    assert f("Margarita (2024) - S02E01 - TBA [AMZN WEBDL-1080p][EAC3 5.1][ES][h264].mkv") == "Margarita S02E01"
    assert f("The Show - S01E10 - Pilot.mkv") == "The Show S01E10"
    # Scene-style dot-separated names
    assert f("Some.Show.S04E07.WEBRip.x264-GROUP.mkv") == "Some.Show S04E07"
    # Movies (year present, no SxxEyy)
    assert f("The Matrix (1999) [imdb-tt0133093].mkv") == "The Matrix (1999)"
    assert f("Inception (2010).mkv") == "Inception (2010)"
    # Plain / unparseable filenames degrade gracefully
    assert f("plain_filename.mkv") == "plain_filename"
    assert f("NoExtension") == "NoExtension"
    assert f("") == ""


def test_format_sonarr_episode_title():
    """_format_sonarr_episode_title adds SxxExx from episodes to series title."""
    from media_preview_generator.web.webhooks import _format_sonarr_episode_title

    assert _format_sonarr_episode_title("Show Name", []) == "Show Name"
    assert _format_sonarr_episode_title("Show Name", None) == "Show Name"
    assert _format_sonarr_episode_title("Show", [{"seasonNumber": 1, "episodeNumber": 5}]) == "Show S01E05"
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


@patch("media_preview_generator.web.webhooks._schedule_webhook_job")
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
    mock_schedule.assert_called_once_with("radarr", "Inception", "/movies/Inception (2010)/Inception.mkv")


@patch("media_preview_generator.web.webhooks._schedule_webhook_job")
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
    mock_schedule.assert_called_once_with("sonarr", "Breaking Bad", "/tv/Breaking Bad/Season 01/S01E01.mkv")


@patch("media_preview_generator.web.webhooks._schedule_webhook_job")
def test_sonarr_webhook_download_with_episode_info_includes_season_episode_in_title(mock_schedule, client):
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
# Custom Webhook Tests
# ---------------------------------------------------------------------------


def test_custom_webhook_test_event(client):
    """POST Test event to custom endpoint → 200 with success message."""
    payload = {"eventType": "Test"}
    resp = client.post("/api/webhooks/custom", json=payload, headers=_auth_headers())
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["success"] is True
    assert "configured successfully" in data["message"]


@patch("media_preview_generator.web.webhooks._schedule_webhook_job")
def test_custom_webhook_single_file_path(mock_schedule, client):
    """POST with file_path string → 202 and schedules one job."""
    payload = {"file_path": "/media/movies/Movie (2024)/Movie.mkv"}
    resp = client.post("/api/webhooks/custom", json=payload, headers=_auth_headers())
    assert resp.status_code == 202
    data = resp.get_json()
    assert data["success"] is True
    assert "1 file" in data["message"]
    mock_schedule.assert_called_once_with(
        "custom", "Movie.mkv", os.path.normpath("/media/movies/Movie (2024)/Movie.mkv")
    )


@patch("media_preview_generator.web.webhooks._schedule_webhook_job")
def test_custom_webhook_multiple_file_paths(mock_schedule, client):
    """POST with file_paths array → 202 and schedules each path."""
    payload = {
        "file_paths": [
            "/tv/Show/S01E01.mkv",
            "/tv/Show/S01E02.mkv",
        ]
    }
    resp = client.post("/api/webhooks/custom", json=payload, headers=_auth_headers())
    assert resp.status_code == 202
    data = resp.get_json()
    assert data["success"] is True
    assert "2 files" in data["message"]
    assert mock_schedule.call_count == 2


@patch("media_preview_generator.web.webhooks._schedule_webhook_job")
def test_custom_webhook_with_title(mock_schedule, client):
    """POST with optional title uses it as display label."""
    payload = {
        "file_path": "/tv/Show/S01E01.mkv",
        "title": "My Show S01E01",
    }
    resp = client.post("/api/webhooks/custom", json=payload, headers=_auth_headers())
    assert resp.status_code == 202
    mock_schedule.assert_called_once_with("custom", "My Show S01E01", os.path.normpath("/tv/Show/S01E01.mkv"))


@patch("media_preview_generator.web.webhooks._schedule_webhook_job")
def test_custom_webhook_deduplicates_paths(mock_schedule, client):
    """POST with duplicate paths in file_path + file_paths → schedules only unique paths."""
    payload = {
        "file_path": "/movies/A.mkv",
        "file_paths": ["/movies/A.mkv", "/movies/B.mkv"],
    }
    resp = client.post("/api/webhooks/custom", json=payload, headers=_auth_headers())
    assert resp.status_code == 202
    assert mock_schedule.call_count == 2


def test_custom_webhook_missing_paths_returns_400(client):
    """POST without file_path or file_paths → 400."""
    payload = {"title": "No paths here"}
    resp = client.post("/api/webhooks/custom", json=payload, headers=_auth_headers())
    assert resp.status_code == 400
    data = resp.get_json()
    assert data["success"] is False
    assert "file_path" in data["error"]


def test_custom_webhook_empty_body_returns_400(client):
    """POST with empty body → 400."""
    resp = client.post(
        "/api/webhooks/custom",
        data="",
        content_type="application/json",
        headers=_auth_headers(),
    )
    assert resp.status_code == 400


def test_custom_webhook_empty_file_paths_array_returns_400(client):
    """POST with empty file_paths array → 400."""
    payload = {"file_paths": []}
    resp = client.post("/api/webhooks/custom", json=payload, headers=_auth_headers())
    assert resp.status_code == 400


@patch("media_preview_generator.web.webhooks._schedule_webhook_job")
def test_custom_webhook_disabled(mock_schedule, client, app):
    """When webhook_enabled is False → 200 with disabled message."""
    from media_preview_generator.web.settings_manager import get_settings_manager

    with app.app_context():
        sm = get_settings_manager()
        sm.set("webhook_enabled", False)

    payload = {"file_path": "/movies/Test.mkv"}
    resp = client.post("/api/webhooks/custom", json=payload, headers=_auth_headers())
    assert resp.status_code == 200
    assert "disabled" in resp.get_json()["message"].lower()
    mock_schedule.assert_not_called()


def test_custom_webhook_no_auth(client):
    """POST without token → 401."""
    resp = client.post(
        "/api/webhooks/custom",
        json={"eventType": "Test"},
        content_type="application/json",
    )
    assert resp.status_code == 401


def test_custom_webhook_appears_in_history(authed_client):
    """Custom webhook events appear in history with source='custom'."""
    authed_client.post(
        "/api/webhooks/custom",
        json={"eventType": "Test"},
        headers=_auth_headers(),
    )
    resp = authed_client.get("/api/webhooks/history", headers=_auth_headers())
    assert resp.status_code == 200
    events = resp.get_json()["events"]
    custom_events = [e for e in events if e["source"] == "custom"]
    assert len(custom_events) >= 1


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
    from media_preview_generator.web.settings_manager import get_settings_manager

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
    from media_preview_generator.web.settings_manager import get_settings_manager

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


@patch("media_preview_generator.web.webhooks._schedule_webhook_job")
def test_webhook_disabled(mock_schedule, client, app):
    """When webhook_enabled is False → 200 with disabled message."""
    from media_preview_generator.web.settings_manager import get_settings_manager

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


@patch("media_preview_generator.web.webhooks.logger.warning")
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


@patch("media_preview_generator.web.webhooks.threading.Timer")
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


@patch("media_preview_generator.web.webhooks.logger.warning")
def test_radarr_download_missing_file_path_logs_warning(mock_warning, client):
    """Missing Radarr file path should emit a warning log."""
    payload = {"eventType": "Download", "movie": {"title": "No Path Movie"}}
    resp = client.post("/api/webhooks/radarr", json=payload, headers=_auth_headers())
    assert resp.status_code == 200
    assert any(
        "missing file path" in str(call) or "didn't carry a file path" in str(call)
        for call in mock_warning.call_args_list
    )


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


@patch("media_preview_generator.web.webhooks.get_job_manager")
@patch("media_preview_generator.web.webhooks.threading.Timer")
@patch("media_preview_generator.web.routes._start_job_async")
def test_execute_webhook_job_batches_paths(mock_start_job, mock_timer_cls, mock_job_mgr):
    """Debounced execution should pass batched webhook paths in one job."""
    from media_preview_generator.web import webhooks as wh

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


@patch("media_preview_generator.web.webhooks.get_settings_manager")
@patch("media_preview_generator.web.webhooks.get_job_manager")
@patch("media_preview_generator.web.webhooks.threading.Timer")
@patch("media_preview_generator.web.routes._start_job_async")
def test_execute_webhook_job_single_file_uses_title_for_library_display(
    mock_start_job, mock_timer_cls, mock_job_mgr, mock_settings_mgr
):
    """Single-file webhook job uses display title (e.g. Show S01E05) in library_name for dashboard."""
    from media_preview_generator.web import webhooks as wh

    mock_timer = MagicMock()
    mock_timer.daemon = True
    mock_timer_cls.return_value = mock_timer

    mock_job = MagicMock()
    mock_job.id = "job-1"
    mock_job_mgr.return_value.create_job.return_value = mock_job

    mock_settings = MagicMock()
    mock_settings.get.side_effect = lambda key, default=None: [] if key == "selected_libraries" else default
    mock_settings_mgr.return_value = mock_settings

    wh._schedule_webhook_job(
        "sonarr",
        "Murder at the Post Office S01E05",
        "/tv/Murder at the Post Office/Season 01/S01E05.mkv",
    )
    wh._execute_webhook_job(wh._debounce_key("sonarr"))

    mock_job_mgr.return_value.create_job.assert_called_once()
    call_kw = mock_job_mgr.return_value.create_job.call_args[1]
    # The "Sonarr: " / "Radarr: " prefix was removed once the Jobs row
    # picked up a source chip carrying the trigger label (D2 follow-up).
    # Library name is now just the title.
    assert call_kw["library_name"] == "Murder at the Post Office S01E05"


@patch("media_preview_generator.web.webhooks.get_settings_manager")
@patch("media_preview_generator.web.webhooks.get_job_manager")
@patch("media_preview_generator.web.webhooks.threading.Timer")
@patch("media_preview_generator.web.routes._start_job_async")
def test_execute_webhook_job_uses_selected_libraries(
    mock_start_job,
    mock_timer_cls,
    mock_job_mgr,
    mock_settings_mgr,
):
    """Webhook jobs should pass selected library IDs from settings."""
    from media_preview_generator.web import webhooks as wh

    mock_timer = MagicMock()
    mock_timer.daemon = True
    mock_timer_cls.return_value = mock_timer

    mock_job = MagicMock()
    mock_job.id = "test-job-id"
    mock_job_mgr.return_value.create_job.return_value = mock_job

    mock_settings = MagicMock()
    mock_settings.get.side_effect = lambda key, default=None: ["1", "2"] if key == "selected_libraries" else default
    mock_settings_mgr.return_value = mock_settings

    wh._schedule_webhook_job("radarr", "Movie A", "/movies/A.mkv")
    wh._execute_webhook_job(wh._debounce_key("radarr"))

    config_overrides = mock_start_job.call_args[0][1]
    assert config_overrides["selected_libraries"] == ["1", "2"]


@patch("media_preview_generator.web.webhooks.get_settings_manager")
@patch("media_preview_generator.web.webhooks.get_job_manager")
@patch("media_preview_generator.web.webhooks.threading.Timer")
@patch("media_preview_generator.web.routes._start_job_async")
def test_execute_webhook_job_includes_retry_settings(mock_start_job, mock_timer_cls, mock_job_mgr, mock_settings_mgr):
    """Webhook job config_overrides include webhook_retry_count and webhook_retry_delay from settings."""
    from media_preview_generator.web import webhooks as wh

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


@patch("media_preview_generator.web.webhooks.get_job_manager")
@patch("media_preview_generator.web.webhooks.threading.Timer")
@patch("media_preview_generator.web.routes._start_job_async")
def test_webhook_payload_path_in_job_config_for_mapping(mock_start_job, mock_timer_cls, mock_job_mgr, client):
    """Path extracted from Radarr payload is passed in job config for mapping-aware resolution."""
    from media_preview_generator.web import webhooks as wh

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


@patch("media_preview_generator.web.webhooks.get_settings_manager")
@patch("media_preview_generator.web.webhooks.get_job_manager")
@patch("media_preview_generator.web.webhooks.threading.Timer")
@patch("media_preview_generator.web.routes._start_job_async")
def test_triggered_history_entry_includes_batch_metadata(
    mock_start_job, mock_timer_cls, mock_job_mgr, mock_settings_mgr, authed_client
):
    """Triggered webhook batch adds history entry with job_id, path_count, and files_preview."""
    from media_preview_generator.web import webhooks as wh

    mock_timer = MagicMock()
    mock_timer.daemon = True
    mock_timer_cls.return_value = mock_timer

    mock_job = MagicMock()
    mock_job.id = "batch-job-123"
    mock_job_mgr.return_value.create_job.return_value = mock_job

    mock_settings = MagicMock()
    mock_settings.get.side_effect = lambda key, default=None: [] if key == "selected_libraries" else default
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
# Dedup: drop duplicate webhook deliveries for the same (source, path)
# ---------------------------------------------------------------------------


@patch("media_preview_generator.web.webhooks.get_settings_manager")
@patch("media_preview_generator.web.webhooks.threading.Timer")
def test_schedule_webhook_job_dedupes_within_ttl(mock_timer_cls, mock_settings_mgr, app):
    """A second call with the same (source, path) during the TTL window
    should be dropped without starting a new timer, and should log a
    'deduped' history entry."""
    from datetime import datetime, timezone

    from media_preview_generator.web import webhooks as wh

    mock_timer_cls.return_value = MagicMock(daemon=True)
    mock_settings_mgr.return_value = MagicMock(get=lambda key, default=None: 60 if key == "webhook_delay" else default)

    # Prime the dedup cache with a recent dispatch for this exact
    # (source, server_id, path) — server_id is "" when the webhook isn't
    # scoped to one configured server.
    normalized_path = os.path.normpath("/tv/Show/S01E01.mkv").replace("\\", "/")
    now_ts = datetime.now(timezone.utc).timestamp()
    with wh._pending_lock:
        wh._recent_dispatches[("sonarr", "", normalized_path)] = now_ts

    result = wh._schedule_webhook_job("sonarr", "Show", "/tv/Show/S01E01.mkv")

    assert result is False
    # No timer should have been created
    mock_timer_cls.assert_not_called()
    # No batch should have been created either
    assert wh._pending_batches.get(wh._debounce_key("sonarr")) is None
    # A 'deduped' history entry should exist
    assert any(e.get("source") == "sonarr" and e.get("status") == "deduped" for e in wh._webhook_history)


@patch("media_preview_generator.web.webhooks.get_settings_manager")
@patch("media_preview_generator.web.webhooks.threading.Timer")
def test_schedule_webhook_job_allows_dispatch_after_ttl(mock_timer_cls, mock_settings_mgr, app):
    """Entries older than _RECENT_DISPATCH_TTL_SECONDS should be pruned
    and no longer block new dispatches."""
    from datetime import datetime, timezone

    from media_preview_generator.web import webhooks as wh

    mock_timer_cls.return_value = MagicMock(daemon=True)
    mock_settings_mgr.return_value = MagicMock(get=lambda key, default=None: 60 if key == "webhook_delay" else default)

    normalized_path = os.path.normpath("/tv/Show/S01E01.mkv").replace("\\", "/")
    stale_ts = datetime.now(timezone.utc).timestamp() - wh._RECENT_DISPATCH_TTL_SECONDS - 5
    with wh._pending_lock:
        wh._recent_dispatches[("sonarr", "", normalized_path)] = stale_ts

    result = wh._schedule_webhook_job("sonarr", "Show", "/tv/Show/S01E01.mkv")

    assert result is True
    mock_timer_cls.assert_called_once()
    # Stale entry should have been pruned from the cache
    assert ("sonarr", "", normalized_path) not in wh._recent_dispatches


@patch("media_preview_generator.web.webhooks.get_settings_manager")
@patch("media_preview_generator.web.webhooks.threading.Timer")
def test_schedule_webhook_job_dedup_is_per_source(mock_timer_cls, mock_settings_mgr, app):
    """A recent dispatch for ('plex', path) must not block ('sonarr', path)."""
    from datetime import datetime, timezone

    from media_preview_generator.web import webhooks as wh

    mock_timer_cls.return_value = MagicMock(daemon=True)
    mock_settings_mgr.return_value = MagicMock(get=lambda key, default=None: 60 if key == "webhook_delay" else default)

    normalized_path = os.path.normpath("/tv/Show/S01E01.mkv").replace("\\", "/")
    now_ts = datetime.now(timezone.utc).timestamp()
    with wh._pending_lock:
        wh._recent_dispatches[("plex", "", normalized_path)] = now_ts

    result = wh._schedule_webhook_job("sonarr", "Show", "/tv/Show/S01E01.mkv")

    assert result is True
    mock_timer_cls.assert_called_once()


@patch("media_preview_generator.web.webhooks.get_settings_manager")
@patch("media_preview_generator.web.webhooks.get_job_manager")
@patch("media_preview_generator.web.webhooks.threading.Timer")
@patch("media_preview_generator.web.routes._start_job_async")
def test_execute_webhook_job_records_dispatch_before_start(
    mock_start_job, mock_timer_cls, mock_job_mgr, mock_settings_mgr, app
):
    """After _execute_webhook_job runs, every path in the batch should be
    recorded in _recent_dispatches so subsequent duplicates are dropped."""
    from media_preview_generator.web import webhooks as wh

    mock_timer_cls.return_value = MagicMock(daemon=True)
    mock_job = MagicMock()
    mock_job.id = "job-dedup-123"
    mock_job_mgr.return_value.create_job.return_value = mock_job

    mock_settings = MagicMock()
    mock_settings.get.side_effect = lambda key, default=None: [] if key == "selected_libraries" else default
    mock_settings_mgr.return_value = mock_settings

    wh._schedule_webhook_job("sonarr", "Show", "/tv/Show/S01E01.mkv")
    wh._schedule_webhook_job("sonarr", "Show", "/tv/Show/S01E02.mkv")
    wh._execute_webhook_job(wh._debounce_key("sonarr"))

    normalized_e1 = os.path.normpath("/tv/Show/S01E01.mkv").replace("\\", "/")
    normalized_e2 = os.path.normpath("/tv/Show/S01E02.mkv").replace("\\", "/")
    assert ("sonarr", "", normalized_e1) in wh._recent_dispatches
    assert ("sonarr", "", normalized_e2) in wh._recent_dispatches
    mock_start_job.assert_called_once()


@patch("media_preview_generator.web.webhooks.get_settings_manager")
@patch("media_preview_generator.web.webhooks.threading.Timer")
def test_schedule_webhook_job_per_server_dedup_is_independent(mock_timer_cls, mock_settings_mgr, app):
    """A dispatch scoped to one server must not block the same path on another server."""
    from datetime import datetime, timezone

    from media_preview_generator.web import webhooks as wh

    mock_timer_cls.return_value = MagicMock(daemon=True)
    mock_settings_mgr.return_value = MagicMock(get=lambda key, default=None: 60 if key == "webhook_delay" else default)

    normalized_path = os.path.normpath("/tv/Show/S01E01.mkv").replace("\\", "/")
    now_ts = datetime.now(timezone.utc).timestamp()
    with wh._pending_lock:
        # Plex server "p1" already dispatched this path recently...
        wh._recent_dispatches[("sonarr", "p1", normalized_path)] = now_ts

    # ...the same path coming in for Emby server "e1" should NOT be deduped.
    result = wh._schedule_webhook_job("sonarr", "Show", "/tv/Show/S01E01.mkv", server_id="e1")

    assert result is True  # not deduped
    mock_timer_cls.assert_called_once()


@patch("media_preview_generator.web.webhooks.get_settings_manager")
@patch("media_preview_generator.web.webhooks.threading.Timer")
def test_schedule_webhook_job_per_server_keeps_separate_batches(mock_timer_cls, mock_settings_mgr, app):
    """Two server-scoped webhooks for the same source land in separate batches."""
    from media_preview_generator.web import webhooks as wh

    mock_timer_cls.return_value = MagicMock(daemon=True)
    mock_settings_mgr.return_value = MagicMock(get=lambda key, default=None: 60 if key == "webhook_delay" else default)

    wh._schedule_webhook_job("sonarr", "Show", "/tv/Show/S01E01.mkv", server_id="p1")
    wh._schedule_webhook_job("sonarr", "Show", "/tv/Show/S01E01.mkv", server_id="e1")

    p1_key = wh._debounce_key("sonarr", "p1")
    e1_key = wh._debounce_key("sonarr", "e1")
    assert p1_key in wh._pending_batches
    assert e1_key in wh._pending_batches
    assert p1_key != e1_key
    assert wh._pending_batches[p1_key]["server_id"] == "p1"
    assert wh._pending_batches[e1_key]["server_id"] == "e1"


@patch("media_preview_generator.web.webhooks.get_settings_manager")
@patch("media_preview_generator.web.webhooks.get_job_manager")
@patch("media_preview_generator.web.webhooks.threading.Timer")
@patch("media_preview_generator.web.routes._start_job_async")
def test_duplicate_after_dispatch_is_dropped_end_to_end(
    mock_start_job, mock_timer_cls, mock_job_mgr, mock_settings_mgr, app
):
    """End-to-end: fire a batch, then a second 'retry' webhook for the same
    path. The second call must be dropped (no new timer, no second job)."""
    from media_preview_generator.web import webhooks as wh

    mock_timer_cls.return_value = MagicMock(daemon=True)
    mock_job = MagicMock()
    mock_job.id = "job-e2e-1"
    mock_job_mgr.return_value.create_job.return_value = mock_job

    mock_settings = MagicMock()
    mock_settings.get.side_effect = lambda key, default=None: [] if key == "selected_libraries" else default
    mock_settings_mgr.return_value = mock_settings

    wh._schedule_webhook_job("plex", "Show S01E01", "/tv/Show/S01E01.mkv")
    wh._execute_webhook_job(wh._debounce_key("plex"))
    assert mock_start_job.call_count == 1

    mock_timer_cls.reset_mock()
    result = wh._schedule_webhook_job("plex", "Show S01E01", "/tv/Show/S01E01.mkv")
    assert result is False
    mock_timer_cls.assert_not_called()
    # No second job was dispatched
    assert mock_start_job.call_count == 1


# ---------------------------------------------------------------------------
# Page Route Test
# ---------------------------------------------------------------------------


def test_webhooks_page_requires_login(client):
    """GET /webhooks without session → redirect to login."""
    resp = client.get("/webhooks")
    assert resp.status_code == 302
    assert "/login" in resp.headers.get("Location", "")


def test_webhooks_page_redirects_to_automation(authed_client):
    """GET /webhooks with session → 302 redirect to /automation#webhooks."""
    resp = authed_client.get("/webhooks", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/automation#webhooks")
