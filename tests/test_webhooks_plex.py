"""Tests for the Plex native webhook endpoint (/api/webhooks/plex).

Verifies multipart payload parsing, library.new event filtering,
ratingKey -> file path resolution (both inline Media.Part.file and
fallback via Plex API), and the synthetic test.ping event.
"""

import io
import json
import os
from unittest.mock import MagicMock, patch

import pytest

from plex_generate_previews.web.app import create_app
from plex_generate_previews.web.settings_manager import reset_settings_manager


@pytest.fixture(autouse=True)
def _reset_singletons():
    """Mirror the reset fixture from test_webhooks.py."""
    reset_settings_manager()
    import plex_generate_previews.web.jobs as jobs_mod

    with jobs_mod._job_lock:
        jobs_mod._job_manager = None
    import plex_generate_previews.web.scheduler as sched_mod

    with sched_mod._schedule_lock:
        sched_mod._schedule_manager = None
    from plex_generate_previews.web.routes import clear_gpu_cache

    clear_gpu_cache()
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
    config_dir = str(tmp_path / "config")
    os.makedirs(config_dir, exist_ok=True)

    auth_file = os.path.join(config_dir, "auth.json")
    with open(auth_file, "w") as f:
        json.dump({"token": "test-token-12345678"}, f)

    settings_file = os.path.join(config_dir, "settings.json")
    with open(settings_file, "w") as f:
        json.dump(
            {
                "setup_complete": True,
                "webhook_enabled": True,
                "plex_webhook_enabled": True,
            },
            f,
        )

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


def _multipart_post(client, payload_dict):
    """POST a Plex-style multipart/form-data request to /api/webhooks/plex."""
    return client.post(
        "/api/webhooks/plex",
        data={
            "payload": (io.BytesIO(json.dumps(payload_dict).encode()), "payload.json")
        },
        content_type="multipart/form-data",
        headers={"X-Auth-Token": "test-token-12345678"},
    )


@patch("plex_generate_previews.web.webhooks._schedule_webhook_job")
def test_plex_webhook_extracts_paths_from_inline_media_part(mock_schedule, client):
    """library.new with inline Media.Part.file paths should not require Plex API lookup."""
    mock_schedule.return_value = True
    payload = {
        "event": "library.new",
        "Metadata": {
            "ratingKey": "12345",
            "title": "Inline Movie",
            "type": "movie",
            "Media": [
                {"Part": [{"file": "/data/movies/Inline Movie/Inline Movie.mkv"}]}
            ],
        },
    }

    resp = _multipart_post(client, payload)
    assert resp.status_code == 202
    assert mock_schedule.called
    args = mock_schedule.call_args[0]
    assert args[0] == "plex"
    assert args[1] == "Inline Movie"
    assert args[2] == "/data/movies/Inline Movie/Inline Movie.mkv"


@patch("plex_generate_previews.web.webhooks._resolve_plex_paths_from_rating_key")
@patch("plex_generate_previews.web.webhooks._schedule_webhook_job")
def test_plex_webhook_falls_back_to_plex_api_lookup(
    mock_schedule, mock_resolve, client
):
    """When Media.Part.file is missing, the endpoint should look the item up by ratingKey."""
    mock_schedule.return_value = True
    mock_resolve.return_value = ["/data/tv/Show/S01E01.mkv"]
    payload = {
        "event": "library.new",
        "Metadata": {
            "ratingKey": "98765",
            "title": "Show S01E01",
            "type": "episode",
        },
    }

    resp = _multipart_post(client, payload)
    assert resp.status_code == 202
    mock_resolve.assert_called_once_with("98765")
    assert mock_schedule.called
    assert mock_schedule.call_args[0][2] == "/data/tv/Show/S01E01.mkv"


@patch("plex_generate_previews.web.webhooks._schedule_webhook_job")
def test_plex_webhook_ignores_non_library_new_events(mock_schedule, client):
    """media.play and friends should be acknowledged but not processed."""
    payload = {"event": "media.play", "Metadata": {"ratingKey": "1"}}
    resp = _multipart_post(client, payload)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["success"] is True
    assert "Ignored" in body["message"]
    mock_schedule.assert_not_called()


@patch("plex_generate_previews.web.webhooks._schedule_webhook_job")
def test_plex_webhook_test_ping_records_history(mock_schedule, client):
    """The synthetic test.ping event should respond OK without scheduling work."""
    resp = _multipart_post(client, {"event": "test.ping"})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["success"] is True
    mock_schedule.assert_not_called()

    import plex_generate_previews.web.webhooks as wh

    assert any(
        e.get("source") == "plex" and e.get("status") == "test"
        for e in wh._webhook_history
    )


def test_plex_webhook_missing_rating_key_returns_400(client):
    """library.new without Metadata.ratingKey should return 400."""
    payload = {"event": "library.new", "Metadata": {"title": "Orphan"}}
    resp = _multipart_post(client, payload)
    assert resp.status_code == 400
    body = resp.get_json()
    assert "ratingKey" in body["error"]


def test_plex_webhook_disabled_when_setting_off(client, app):
    """When plex_webhook_enabled is False, library.new should be ignored."""
    from plex_generate_previews.web.settings_manager import get_settings_manager

    sm = get_settings_manager()
    sm.set("plex_webhook_enabled", False)
    payload = {
        "event": "library.new",
        "Metadata": {"ratingKey": "1", "title": "Should be ignored"},
    }
    resp = _multipart_post(client, payload)
    assert resp.status_code == 200
    body = resp.get_json()
    assert "disabled" in body["message"].lower()


def test_plex_webhook_requires_auth(client):
    """Missing auth token returns 401."""
    resp = client.post(
        "/api/webhooks/plex",
        data={"payload": (io.BytesIO(b"{}"), "p.json")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 401


@patch("plex_generate_previews.web.webhooks._schedule_webhook_job")
def test_plex_webhook_accepts_query_token(mock_schedule, client):
    """Plex's webhook UI has no header field — auth must work via ?token= query param."""
    mock_schedule.return_value = True
    payload = {
        "event": "library.new",
        "Metadata": {
            "ratingKey": "1",
            "title": "Query token test",
            "Media": [{"Part": [{"file": "/data/movies/Q.mkv"}]}],
        },
    }
    resp = client.post(
        "/api/webhooks/plex?token=test-token-12345678",
        data={
            "payload": (io.BytesIO(json.dumps(payload).encode()), "payload.json"),
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code == 202
    assert mock_schedule.called


def test_plex_webhook_rejects_invalid_query_token(client):
    """A bad ?token= still returns 401."""
    resp = client.post(
        "/api/webhooks/plex?token=wrong",
        data={"payload": (io.BytesIO(b"{}"), "p.json")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 401


def test_plex_webhook_invalid_json_returns_400(client):
    """Garbage in the payload field returns 400."""
    resp = client.post(
        "/api/webhooks/plex",
        data={"payload": (io.BytesIO(b"not-json"), "p.json")},
        content_type="multipart/form-data",
        headers={"X-Auth-Token": "test-token-12345678"},
    )
    assert resp.status_code == 400


@patch("plex_generate_previews.web.webhooks._resolve_plex_paths_from_rating_key")
def test_plex_webhook_no_paths_resolved_returns_200_ignored(mock_resolve, client):
    """When neither inline paths nor Plex lookup yield anything, respond 200 (don't make Plex retry)."""
    mock_resolve.return_value = []
    payload = {
        "event": "library.new",
        "Metadata": {"ratingKey": "555", "title": "No paths"},
    }
    resp = _multipart_post(client, payload)
    assert resp.status_code == 200
    body = resp.get_json()
    assert "No file paths" in body["message"] or body["success"] is True


def test_resolve_plex_paths_from_rating_key_handles_failure():
    """Plex client failures should return an empty list, not raise."""
    from plex_generate_previews.web.webhooks import _resolve_plex_paths_from_rating_key

    with patch(
        "plex_generate_previews.config.load_config",
        side_effect=Exception("config blew up"),
    ):
        assert _resolve_plex_paths_from_rating_key("1") == []


def test_resolve_plex_paths_from_rating_key_walks_media_parts():
    """When the Plex client returns an item, all part files should be collected."""
    from plex_generate_previews.web.webhooks import _resolve_plex_paths_from_rating_key

    fake_part_a = MagicMock(file="/data/movies/A/A.mkv")
    fake_part_b = MagicMock(file="/data/movies/A/A-extras.mkv")
    fake_media = MagicMock(parts=[fake_part_a, fake_part_b])
    fake_item = MagicMock(media=[fake_media])

    fake_plex = MagicMock()
    fake_plex.fetchItem.return_value = fake_item

    with (
        patch("plex_generate_previews.config.load_config", return_value=MagicMock()),
        patch("plex_generate_previews.plex_client.plex_server", return_value=fake_plex),
        patch(
            "plex_generate_previews.plex_client.retry_plex_call",
            side_effect=lambda fn, *a, **kw: fn(*a, **kw),
        ),
    ):
        paths = _resolve_plex_paths_from_rating_key("777")

    assert paths == [
        "/data/movies/A/A.mkv",
        "/data/movies/A/A-extras.mkv",
    ]
