"""Tests for the Recently Added scanner.

Exercises the stateless scan_recently_added function directly — the
scheduler dispatch path is covered in test_scheduler.py.
"""

import os
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from media_preview_generator.web import recent_added_scanner as scanner
from media_preview_generator.web.settings_manager import reset_settings_manager


@pytest.fixture(autouse=True)
def _reset_singletons():
    reset_settings_manager()
    import media_preview_generator.web.webhooks as wh

    wh._webhook_history.clear()
    yield
    reset_settings_manager()
    wh._webhook_history.clear()


def _make_item(title, paths, added_at, item_type="movie", grandparent=None, season_ep=None):
    item = MagicMock()
    item.title = title
    item.type = item_type
    item.addedAt = added_at
    if grandparent:
        item.grandparentTitle = grandparent
    if season_ep:
        item.seasonEpisode = season_ep
    media = MagicMock()
    media.parts = [MagicMock(file=p) for p in paths]
    item.media = [media]
    return item


def _make_section(title, type_, key, items):
    section = MagicMock()
    section.title = title
    section.type = type_
    section.key = key
    section.search.return_value = items
    return section


def _make_plex(sections):
    plex = MagicMock()
    plex.library = MagicMock()
    plex.library.sections.return_value = sections
    return plex


@patch("media_preview_generator.web.webhooks._schedule_webhook_job")
def test_scan_submits_in_window_items(mock_schedule, tmp_path, monkeypatch):
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    mock_schedule.return_value = True
    now = datetime.now(timezone.utc)
    in_window = _make_item("New Movie", ["/data/movies/New.mkv"], now - timedelta(minutes=30))
    out_of_window = _make_item("Old Movie", ["/data/movies/Old.mkv"], now - timedelta(hours=4))
    section = _make_section("Movies", "movie", "1", [in_window, out_of_window])
    plex = _make_plex([section])

    submitted = scanner.scan_recently_added(1, plex=plex)

    assert submitted == 1
    mock_schedule.assert_called_once()
    args = mock_schedule.call_args[0]
    assert args[0] == "recently_added"
    assert args[2] == "/data/movies/New.mkv"


@patch("media_preview_generator.web.webhooks._schedule_webhook_job")
def test_scan_handles_episode_titles(mock_schedule, tmp_path, monkeypatch):
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    mock_schedule.return_value = True
    now = datetime.now(timezone.utc)
    ep = _make_item(
        "Pilot",
        ["/data/tv/Show/S01E01.mkv"],
        now - timedelta(minutes=5),
        item_type="episode",
        grandparent="Show",
        season_ep="s01e01",
    )
    section = _make_section("TV", "show", "2", [ep])
    plex = _make_plex([section])

    scanner.scan_recently_added(1, plex=plex)

    args = mock_schedule.call_args[0]
    assert args[1] == "Show S01E01"


@patch("media_preview_generator.web.webhooks._schedule_webhook_job")
def test_scan_with_explicit_library_ids_scans_only_those_sections(mock_schedule, tmp_path, monkeypatch):
    """When library_ids=[...], only sections with matching keys are scanned."""
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    mock_schedule.return_value = True
    now = datetime.now(timezone.utc)
    movie_item = _make_item("Movie", ["/data/movies/M.mkv"], now)
    ep_item = _make_item(
        "Pilot",
        ["/data/tv/S01E01.mkv"],
        now,
        item_type="episode",
        grandparent="Show",
        season_ep="s01e01",
    )
    movies = _make_section("Movies", "movie", "1", [movie_item])
    tv = _make_section("TV", "show", "2", [ep_item])
    plex = _make_plex([movies, tv])

    # Only scan section key "2" (TV)
    scanner.scan_recently_added(1, library_ids=["2"], plex=plex)

    assert mock_schedule.call_count == 1
    args = mock_schedule.call_args[0]
    assert args[2] == "/data/tv/S01E01.mkv"


@patch("media_preview_generator.web.webhooks._schedule_webhook_job")
def test_scan_empty_library_ids_falls_back_to_global_selected_libraries(mock_schedule, tmp_path, monkeypatch):
    """An empty library_ids falls back to the global selected_libraries."""
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    mock_schedule.return_value = True
    from media_preview_generator.web.settings_manager import get_settings_manager

    sm = get_settings_manager()
    sm.set("selected_libraries", ["Only This One"])

    now = datetime.now(timezone.utc)
    item = _make_item("Movie", ["/data/movies/M.mkv"], now)
    skipped = _make_section("Skipped", "movie", "1", [item])
    matched = _make_section("Only This One", "movie", "2", [item])
    plex = _make_plex([skipped, matched])

    scanner.scan_recently_added(1, library_ids=None, plex=plex)

    assert mock_schedule.call_count == 1


@patch("media_preview_generator.web.webhooks._schedule_webhook_job")
def test_scan_fractional_lookback_hours(mock_schedule, tmp_path, monkeypatch):
    """lookback_hours=0.25 should translate to a 15-minute window."""
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    mock_schedule.return_value = True
    now = datetime.now(timezone.utc)
    in_window = _make_item("Just added", ["/data/movies/New.mkv"], now - timedelta(minutes=5))
    out_of_window = _make_item("20 min ago", ["/data/movies/Old.mkv"], now - timedelta(minutes=20))
    section = _make_section("Movies", "movie", "1", [in_window, out_of_window])
    plex = _make_plex([section])

    submitted = scanner.scan_recently_added(0.25, plex=plex)

    assert submitted == 1
    args = mock_schedule.call_args[0]
    assert args[2] == "/data/movies/New.mkv"


@patch("media_preview_generator.web.webhooks._schedule_webhook_job")
def test_scan_handles_unsupported_section_type(mock_schedule, tmp_path, monkeypatch):
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    section = _make_section("Music", "artist", "1", [])
    plex = _make_plex([section])
    submitted = scanner.scan_recently_added(1, plex=plex)
    assert submitted == 0
    mock_schedule.assert_not_called()


@patch("media_preview_generator.web.webhooks._schedule_webhook_job")
def test_scan_handles_search_filter_unsupported(mock_schedule, tmp_path, monkeypatch):
    """If section.search raises with the addedAt filter, fall back to client-side filter."""
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    mock_schedule.return_value = True
    now = datetime.now(timezone.utc)
    item = _make_item("Movie", ["/data/movies/A.mkv"], now - timedelta(minutes=30))

    section = MagicMock()
    section.title = "Movies"
    section.type = "movie"
    section.key = "1"

    def search_side_effect(libtype=None, filters=None, sort=None, **kwargs):
        if filters is not None:
            raise Exception("addedAt filter unsupported")
        return [item]

    section.search.side_effect = search_side_effect
    plex = _make_plex([section])

    submitted = scanner.scan_recently_added(1, plex=plex)
    assert submitted == 1


@patch("media_preview_generator.web.webhooks._schedule_webhook_job")
def test_scan_skips_items_with_existing_bifs(mock_schedule, tmp_path, monkeypatch):
    """Items that already have a BIF file on disk are filtered out before dispatch."""
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    mock_schedule.return_value = True
    from media_preview_generator.web.settings_manager import get_settings_manager

    plex_config = tmp_path / "plex"
    plex_config.mkdir()
    sm = get_settings_manager()
    sm.set("plex_config_folder", str(plex_config))

    now = datetime.now(timezone.utc)
    item = _make_item("Already Processed", ["/data/movies/A.mkv"], now)
    item.key = "/library/metadata/42"
    section = _make_section("Movies", "movie", "1", [item])

    # Pre-create the BIF file for this item's bundle hash so the filter
    # finds it.  Scanner will call plex.query(item.key + '/tree') and
    # see our mocked MediaPart.hash attribute.
    bundle_hash = "abcdef1234567890"
    bif_dir = (
        plex_config / "Media" / "localhost" / bundle_hash[0] / f"{bundle_hash[1:]}.bundle" / "Contents" / "Indexes"
    )
    bif_dir.mkdir(parents=True)
    (bif_dir / "index-sd.bif").write_bytes(b"fake bif")

    import xml.etree.ElementTree as ET

    tree_xml = ET.fromstring(
        f'<MediaContainer><MediaPart hash="{bundle_hash}" file="/data/movies/A.mkv"/></MediaContainer>'
    )
    plex = _make_plex([section])
    plex.query.return_value = tree_xml

    submitted = scanner.scan_recently_added(1, plex=plex)

    assert submitted == 0
    mock_schedule.assert_not_called()


@patch("media_preview_generator.web.webhooks._schedule_webhook_job")
def test_scan_submits_items_missing_bif(mock_schedule, tmp_path, monkeypatch):
    """Items without a BIF on disk are forwarded through the pipeline."""
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    mock_schedule.return_value = True
    from media_preview_generator.web.settings_manager import get_settings_manager

    plex_config = tmp_path / "plex"
    plex_config.mkdir()
    sm = get_settings_manager()
    sm.set("plex_config_folder", str(plex_config))

    now = datetime.now(timezone.utc)
    item = _make_item("New Movie", ["/data/movies/N.mkv"], now)
    item.key = "/library/metadata/99"
    section = _make_section("Movies", "movie", "1", [item])

    # Plex returns a tree with a hash but we do NOT create the BIF file —
    # scanner should forward the item to _schedule_webhook_job.
    import xml.etree.ElementTree as ET

    tree_xml = ET.fromstring(
        '<MediaContainer><MediaPart hash="bb1234567890" file="/data/movies/N.mkv"/></MediaContainer>'
    )
    plex = _make_plex([section])
    plex.query.return_value = tree_xml

    submitted = scanner.scan_recently_added(1, plex=plex)

    assert submitted == 1
    mock_schedule.assert_called_once()


@patch("media_preview_generator.web.webhooks._schedule_webhook_job")
def test_scan_logs_history_when_items_submitted(mock_schedule, tmp_path, monkeypatch):
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    mock_schedule.return_value = True
    now = datetime.now(timezone.utc)
    item = _make_item("Movie", ["/data/movies/M.mkv"], now)
    section = _make_section("Movies", "movie", "1", [item])
    plex = _make_plex([section])

    scanner.scan_recently_added(1, plex=plex)

    import media_preview_generator.web.webhooks as wh

    assert any(e.get("source") == "recently_added" and e.get("status") == "queued" for e in wh._webhook_history)


@pytest.fixture
def _force_pdt_timezone():
    """Set worker libc TZ to America/Los_Angeles for the duration of the test.

    Restores both the env var AND calls ``time.tzset()`` AFTER the env
    var is restored, so the worker's libc tz state matches the env on
    cleanup. monkeypatch alone leaks: it restores the env var on
    fixture teardown but doesn't run our tzset(), so libc keeps the
    PDT setting and breaks every subsequent test that calls
    ``datetime.fromtimestamp()`` on a UTC-naive value.
    """
    import time as _time

    prev_tz = os.environ.get("TZ")
    os.environ["TZ"] = "America/Los_Angeles"
    if hasattr(_time, "tzset"):
        _time.tzset()
    try:
        yield
    finally:
        if prev_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = prev_tz
        if hasattr(_time, "tzset"):
            _time.tzset()


@patch("media_preview_generator.web.webhooks._schedule_webhook_job")
def test_scan_in_utc_minus_7_does_not_drop_in_window_items(mock_schedule, tmp_path, monkeypatch, _force_pdt_timezone):
    """GitHub #226 — on a UTC-7 (PDT) host, plexapi returns addedAt as a
    naive *local time*. The previous comparison (naive UTC cutoff vs
    naive local addedAt) silently dropped every item because the local
    timestamp was 7 hours behind the UTC cutoff. Pin the comparison via
    the to_utc_naive helper using a real PDT system timezone so
    ``datetime.fromtimestamp()`` actually returns the local-naive value
    plexapi would produce on that host."""
    import time as _time

    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    mock_schedule.return_value = True

    # plexapi computes addedAt via datetime.fromtimestamp(unix_ts) with
    # no tz arg → naive local time. Simulate "30 minutes ago" using
    # exactly that path so the test exercises the real bug surface, not
    # a synthetic stand-in.
    now_unix = _time.time()
    added_at = datetime.fromtimestamp(now_unix - 1800)  # naive local PDT
    item = _make_item("New Movie", ["/data/movies/New.mkv"], added_at)
    section = _make_section("Movies", "movie", "1", [item])
    plex = _make_plex([section])

    submitted = scanner.scan_recently_added(1, plex=plex)

    assert submitted == 1, "30-min-ago item on a PDT host must NOT be dropped"
    mock_schedule.assert_called_once()


def test_to_utc_naive_handles_naive_local_input():
    """to_utc_naive on a naive local datetime returns its UTC-naive
    equivalent. Spot-check using a known-fixed timestamp; matches
    plexapi's datetime.fromtimestamp(unix_ts) convention."""
    from media_preview_generator.utils import to_utc_naive

    # Pick a real unix ts and round-trip via local-naive; the result
    # must equal the equivalent UTC-naive datetime regardless of host tz.
    unix_ts = 1777352608  # 2026-04-28 05:03:28 UTC (the issue reporter's example)
    local_naive = datetime.fromtimestamp(unix_ts)
    expected_utc_naive = datetime.fromtimestamp(unix_ts, tz=timezone.utc).replace(tzinfo=None)
    assert to_utc_naive(local_naive) == expected_utc_naive


def test_to_utc_naive_passes_through_aware_input():
    """to_utc_naive on a tz-aware datetime returns its UTC-naive form,
    leaving the wall-clock value correct after offset adjustment."""
    from media_preview_generator.utils import to_utc_naive

    aware_pdt = datetime(2026, 4, 27, 22, 3, 28, tzinfo=timezone(timedelta(hours=-7)))
    assert to_utc_naive(aware_pdt) == datetime(2026, 4, 28, 5, 3, 28)
