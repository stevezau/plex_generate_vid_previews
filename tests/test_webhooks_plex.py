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
        data={"payload": (io.BytesIO(json.dumps(payload_dict).encode()), "payload.json")},
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
            "Media": [{"Part": [{"file": "/data/movies/Inline Movie/Inline Movie.mkv"}]}],
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
def test_plex_webhook_falls_back_to_plex_api_lookup(mock_schedule, mock_resolve, client):
    """When Media.Part.file is missing, the endpoint should look the item up by ratingKey."""
    mock_schedule.return_value = True
    mock_resolve.return_value = (["/data/tv/Show/S01E01.mkv"], None)
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

    assert any(e.get("source") == "plex" and e.get("status") == "test" for e in wh._webhook_history)


def test_plex_webhook_missing_rating_key_returns_400(client):
    """library.new without Metadata.ratingKey should return 400."""
    payload = {"event": "library.new", "Metadata": {"title": "Orphan"}}
    resp = _multipart_post(client, payload)
    assert resp.status_code == 400
    body = resp.get_json()
    assert "ratingKey" in body["error"]


def test_plex_webhook_disabled_when_master_off(client, app):
    """When the master webhook_enabled switch is False, library.new is ignored.

    Phase I5 dropped the per-source plex_webhook_enabled gate — Plex Direct is
    "enabled" implicitly by the per-server registration. Pausing all webhooks
    is now a single global toggle.
    """
    from plex_generate_previews.web.settings_manager import get_settings_manager

    sm = get_settings_manager()
    sm.set("webhook_enabled", False)
    try:
        payload = {
            "event": "library.new",
            "Metadata": {"ratingKey": "1", "title": "Should be ignored"},
        }
        resp = _multipart_post(client, payload)
        assert resp.status_code == 200
        body = resp.get_json()
        assert "disabled" in body["message"].lower()
    finally:
        sm.set("webhook_enabled", True)


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
    mock_resolve.return_value = ([], None)
    payload = {
        "event": "library.new",
        "Metadata": {"ratingKey": "555", "title": "No paths"},
    }
    resp = _multipart_post(client, payload)
    assert resp.status_code == 200
    body = resp.get_json()
    assert "No file paths" in body["message"] or body["success"] is True


def test_resolve_plex_paths_from_rating_key_handles_failure():
    """Plex client failures should return ([], None), not raise."""
    from plex_generate_previews.web.webhooks import _resolve_plex_paths_from_rating_key

    with patch(
        "plex_generate_previews.config.load_config",
        side_effect=Exception("config blew up"),
    ):
        assert _resolve_plex_paths_from_rating_key("1") == ([], None)


def test_resolve_plex_paths_from_rating_key_walks_media_parts():
    """When the Plex client returns an item, all part files and a formatted
    display title should be collected from the same fetched item."""
    from plex_generate_previews.web.webhooks import _resolve_plex_paths_from_rating_key

    fake_part_a = MagicMock(file="/data/movies/A/A.mkv")
    fake_part_b = MagicMock(file="/data/movies/A/A-extras.mkv")
    fake_media = MagicMock(parts=[fake_part_a, fake_part_b])
    fake_item = MagicMock(
        media=[fake_media],
        type="movie",
        title="Movie A",
        year=2023,
    )

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
        paths, display_title = _resolve_plex_paths_from_rating_key("777")

    assert paths == [
        "/data/movies/A/A.mkv",
        "/data/movies/A/A-extras.mkv",
    ]
    assert display_title == "Movie A (2023)"


# ---------------------------------------------------------------------------
# Title formatter unit tests
# ---------------------------------------------------------------------------


def test_format_plex_title_from_metadata_episode_full():
    from plex_generate_previews.web.webhooks import _format_plex_title_from_metadata

    metadata = {
        "type": "episode",
        "grandparentTitle": "Beyond Paradise",
        "parentIndex": 4,
        "index": 3,
        "title": "The Whispers",
    }
    assert _format_plex_title_from_metadata(metadata) == "Beyond Paradise - S04E03 - The Whispers"


def test_format_plex_title_from_metadata_episode_drops_tautological_title():
    """When Plex uses the placeholder 'Episode N' as the episode title,
    the formatter should drop the redundant trailing segment."""
    from plex_generate_previews.web.webhooks import _format_plex_title_from_metadata

    metadata = {
        "type": "episode",
        "grandparentTitle": "Beyond Paradise",
        "parentIndex": 4,
        "index": 3,
        "title": "Episode 3",
    }
    assert _format_plex_title_from_metadata(metadata) == "Beyond Paradise - S04E03"


def test_format_plex_title_from_metadata_episode_blank_title_drops_suffix():
    from plex_generate_previews.web.webhooks import _format_plex_title_from_metadata

    metadata = {
        "type": "episode",
        "grandparentTitle": "Show",
        "parentIndex": 1,
        "index": 1,
        "title": "",
    }
    assert _format_plex_title_from_metadata(metadata) == "Show - S01E01"


def test_format_plex_title_from_metadata_movie_with_year():
    from plex_generate_previews.web.webhooks import _format_plex_title_from_metadata

    metadata = {"type": "movie", "title": "Dune: Part Two", "year": 2024}
    assert _format_plex_title_from_metadata(metadata) == "Dune: Part Two (2024)"


def test_format_plex_title_from_metadata_movie_without_year():
    from plex_generate_previews.web.webhooks import _format_plex_title_from_metadata

    metadata = {"type": "movie", "title": "Unknown Movie"}
    assert _format_plex_title_from_metadata(metadata) == "Unknown Movie"


def test_format_plex_title_from_metadata_missing_fields_returns_none():
    from plex_generate_previews.web.webhooks import _format_plex_title_from_metadata

    # Episode with no grandparentTitle
    assert _format_plex_title_from_metadata({"type": "episode", "parentIndex": 1, "index": 1, "title": "t"}) is None
    # Episode with non-integer season/episode indices
    assert (
        _format_plex_title_from_metadata(
            {
                "type": "episode",
                "grandparentTitle": "S",
                "parentIndex": "abc",
                "index": 1,
            }
        )
        is None
    )
    # Movie with empty title
    assert _format_plex_title_from_metadata({"type": "movie", "title": ""}) is None
    # Unknown type
    assert _format_plex_title_from_metadata({"type": "show", "title": "Foo"}) is None
    # Non-dict input
    assert _format_plex_title_from_metadata(None) is None


def test_format_plex_title_from_item_uses_plexapi_attrs():
    from plex_generate_previews.web.webhooks import _format_plex_title_from_item

    item = MagicMock(
        type="episode",
        grandparentTitle="Beyond Paradise",
        parentIndex=4,
        index=3,
        title="The Whispers",
        year=None,
    )
    assert _format_plex_title_from_item(item) == "Beyond Paradise - S04E03 - The Whispers"


def test_format_plex_title_from_item_returns_none_when_item_is_none():
    from plex_generate_previews.web.webhooks import _format_plex_title_from_item

    assert _format_plex_title_from_item(None) is None


# ---------------------------------------------------------------------------
# plex_webhook integration: title flows into _schedule_webhook_job
# ---------------------------------------------------------------------------


@patch("plex_generate_previews.web.webhooks._schedule_webhook_job")
def test_plex_webhook_uses_formatted_episode_title(mock_schedule, client):
    """Rich Metadata should produce a descriptive 'Show - SxxExx - Title' label,
    not the raw Metadata.title."""
    mock_schedule.return_value = True
    payload = {
        "event": "library.new",
        "Metadata": {
            "ratingKey": "12345",
            "type": "episode",
            "grandparentTitle": "Beyond Paradise",
            "parentIndex": 4,
            "index": 3,
            "title": "The Whispers",
            "Media": [{"Part": [{"file": ("/data/tv/Beyond Paradise/Season 04/Beyond Paradise S04E03.mkv")}]}],
        },
    }

    resp = _multipart_post(client, payload)
    assert resp.status_code == 202
    assert mock_schedule.called
    args = mock_schedule.call_args[0]
    assert args[0] == "plex"
    assert args[1] == "Beyond Paradise - S04E03 - The Whispers"


@patch("plex_generate_previews.web.webhooks._schedule_webhook_job")
def test_plex_webhook_drops_tautological_episode_suffix(mock_schedule, client):
    """When Plex sends title='Episode 3' (the placeholder it uses for shows
    with no canonical titles), the final label should collapse to 'Show - S04E03'."""
    mock_schedule.return_value = True
    payload = {
        "event": "library.new",
        "Metadata": {
            "ratingKey": "12345",
            "type": "episode",
            "grandparentTitle": "Beyond Paradise",
            "parentIndex": 4,
            "index": 3,
            "title": "Episode 3",
            "Media": [{"Part": [{"file": "/data/tv/BP/S04E03.mkv"}]}],
        },
    }

    resp = _multipart_post(client, payload)
    assert resp.status_code == 202
    assert mock_schedule.call_args[0][1] == "Beyond Paradise - S04E03"


@patch("plex_generate_previews.web.webhooks._schedule_webhook_job")
def test_plex_webhook_uses_formatted_movie_title(mock_schedule, client):
    """Movie metadata should produce 'Title (Year)'."""
    mock_schedule.return_value = True
    payload = {
        "event": "library.new",
        "Metadata": {
            "ratingKey": "98765",
            "type": "movie",
            "title": "Dune: Part Two",
            "year": 2024,
            "Media": [{"Part": [{"file": "/data/movies/Dune Part Two.mkv"}]}],
        },
    }

    resp = _multipart_post(client, payload)
    assert resp.status_code == 202
    assert mock_schedule.call_args[0][1] == "Dune: Part Two (2024)"


@patch("plex_generate_previews.web.webhooks._resolve_plex_paths_from_rating_key")
@patch("plex_generate_previews.web.webhooks._schedule_webhook_job")
def test_plex_webhook_uses_ratingkey_title_when_metadata_is_sparse(mock_schedule, mock_resolve, client):
    """When Metadata lacks the fields needed to format a title but the ratingKey
    lookup returns one, the resolver-derived title should be used."""
    mock_schedule.return_value = True
    mock_resolve.return_value = (
        ["/data/tv/Show/S01E01.mkv"],
        "Show - S01E01 - Pilot",
    )
    payload = {
        "event": "library.new",
        "Metadata": {
            "ratingKey": "98765",
            "title": "Some Raw Title",
            "type": "episode",
        },
    }

    resp = _multipart_post(client, payload)
    assert resp.status_code == 202
    assert mock_schedule.call_args[0][1] == "Show - S01E01 - Pilot"


@patch("plex_generate_previews.web.webhooks._resolve_plex_paths_from_rating_key")
@patch("plex_generate_previews.web.webhooks._schedule_webhook_job")
def test_plex_webhook_falls_back_to_raw_title_when_nothing_else_works(mock_schedule, mock_resolve, client):
    """If neither the metadata nor the ratingKey lookup produce a formatted
    title, the raw Metadata.title is used (prior behavior)."""
    mock_schedule.return_value = True
    mock_resolve.return_value = (["/data/tv/Show/unknown.mkv"], None)
    payload = {
        "event": "library.new",
        "Metadata": {
            "ratingKey": "98765",
            "title": "Some Raw Title",
        },
    }

    resp = _multipart_post(client, payload)
    assert resp.status_code == 202
    assert mock_schedule.call_args[0][1] == "Some Raw Title"
