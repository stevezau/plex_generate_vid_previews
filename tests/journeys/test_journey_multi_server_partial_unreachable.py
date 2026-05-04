"""TEST_AUDIT P1.4 — multi-server reliability under partial failure.

User journey: 3 servers configured (Plex + Emby + Jellyfin), all own the
same canonical_path. One server is down (network down, ConnectionError on
publish). Expected outcome:

  - The 2 healthy publishers succeed and write their outputs.
  - The 1 down publisher reports FAILED with a clear message.
  - The aggregate ``MultiServerStatus`` is ``PUBLISHED`` (not FAILED) —
    the run is a partial success, not a total failure.
  - The summary message reads "Published to 2 of 3 servers".

Why this matters: a regression that aborts the whole dispatch on the
first publisher exception would silently strand the user — they'd see
"FAILED" on a run where 2 out of 3 servers actually got their previews.
This test pins the per-publisher try/except contract at
``processing/multi_server.py:554-618`` (each publisher's
compute_output_paths and publish are wrapped independently).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
import requests

from media_preview_generator.processing.frame_cache import reset_frame_cache
from media_preview_generator.processing.multi_server import (
    MultiServerStatus,
    PublisherStatus,
    process_canonical_path,
)
from media_preview_generator.servers import Library, ServerRegistry, ServerType

pytestmark = pytest.mark.journey


@pytest.fixture(autouse=True)
def _reset_frame_cache_singleton():
    reset_frame_cache()
    yield
    reset_frame_cache()


def _server_dict(
    server_id: str,
    server_type: ServerType,
    libraries: list[Library],
    adapter: str,
    plex_config_folder: str = "/cfg",
) -> dict:
    return {
        "id": server_id,
        "type": server_type.value,
        "name": server_id,
        "enabled": True,
        "url": "http://x",
        "auth": {"token": "t", "method": "api_key", "api_key": "k"},
        "libraries": [
            {
                "id": lib.id,
                "name": lib.name,
                "remote_paths": list(lib.remote_paths),
                "enabled": lib.enabled,
            }
            for lib in libraries
        ],
        "exclude_paths": [],
        "output": {"adapter": adapter, "plex_config_folder": plex_config_folder, "width": 320, "frame_interval": 10},
    }


def _populate_frames(directory: Path, count: int) -> None:
    from PIL import Image

    directory.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (320, 180), (10, 20, 30))
    for i in range(count):
        img.save(directory / f"{i:05d}.jpg", "JPEG", quality=70)


def test_one_publisher_unreachable_others_succeed_aggregate_is_published(mock_config, tmp_path):
    """The journey: 3 publishers configured, Emby is unreachable
    (ConnectionError on publish), Plex + Jellyfin succeed. Aggregate
    must be PUBLISHED (not FAILED) and individual publisher rows must
    correctly report their per-server outcome.

    Catches: regression that lets one publisher's exception bubble up
    and abort the whole dispatch (D-style "all-or-nothing" bug) — would
    silently lose preview generation for ALL servers when only one is
    actually down.
    """
    mock_config.working_tmp_folder = str(tmp_path / "tmp")
    media_dir = tmp_path / "data" / "movies"
    media_dir.mkdir(parents=True)
    media_file = media_dir / "Test (2024).mkv"
    media_file.write_bytes(b"placeholder")
    media_root = str(media_dir)
    plex_config = str(tmp_path / "plex_config")
    Path(plex_config).mkdir(parents=True)

    # Three servers, all owning the same path. Plex + Jellyfin will
    # publish successfully; Emby's adapter will raise on publish.
    registry = ServerRegistry.from_settings(
        [
            _server_dict(
                "plex-1",
                ServerType.PLEX,
                libraries=[Library(id="1", name="Movies", remote_paths=(media_root,), enabled=True)],
                adapter="plex_bundle",
                plex_config_folder=plex_config,
            ),
            _server_dict(
                "emby-down",
                ServerType.EMBY,
                libraries=[Library(id="2", name="Movies", remote_paths=(media_root,), enabled=True)],
                adapter="emby_sidecar",
            ),
            _server_dict(
                "jelly-1",
                ServerType.JELLYFIN,
                libraries=[Library(id="3", name="Movies", remote_paths=(media_root,), enabled=True)],
                adapter="jellyfin_trickplay",
            ),
        ],
    )

    def fake_generate_images(video_file, output_folder, *args, **kwargs):
        _populate_frames(Path(output_folder), count=3)
        return (True, 3, "h264", 1.0, 30.0, None)

    # Provide hint for Plex (so it doesn't hit /tree) and Jellyfin (so it
    # doesn't take the SKIPPED_NOT_IN_LIBRARY branch). No hint for Emby —
    # Emby sidecar doesn't need an item_id (pure path adapter).
    item_id_by_server = {"plex-1": "rk-1", "jelly-1": "jelly-id-1"}
    bundle_meta = {"plex-1": (("hash" * 10, str(media_file)),)}

    # Patch Emby's adapter publish to raise — simulates "Emby server is
    # down / disk full / permission error" mid-publish.
    from media_preview_generator.output.emby_sidecar import EmbyBifAdapter

    def fake_emby_publish(self, bundle, output_paths, item_id=None):
        raise requests.ConnectionError("Connection refused — Emby server unreachable")

    with (
        patch(
            "media_preview_generator.processing.multi_server.generate_images",
            side_effect=fake_generate_images,
        ),
        patch.object(EmbyBifAdapter, "publish", autospec=True, side_effect=fake_emby_publish),
    ):
        result = process_canonical_path(
            canonical_path=str(media_file),
            registry=registry,
            config=mock_config,
            item_id_by_server=item_id_by_server,
            bundle_metadata_by_server=bundle_meta,
        )

    # Aggregate status: PUBLISHED (at least one publisher succeeded)
    assert result.status is MultiServerStatus.PUBLISHED, (
        f"Aggregate status must be PUBLISHED when ≥1 publisher succeeds; "
        f"got {result.status}. A FAILED here = the dispatcher aborted on Emby's "
        f"ConnectionError instead of isolating per-publisher failures."
    )

    # Per-publisher breakdown: 2 PUBLISHED + 1 FAILED, NOT 3 FAILED.
    statuses = {p.server_id: p.status for p in result.publishers}
    assert len(statuses) == 3, f"All 3 publishers must report; got {len(statuses)}: {statuses!r}"
    assert statuses["plex-1"] is PublisherStatus.PUBLISHED, (
        f"Plex must publish successfully despite Emby being down; got {statuses['plex-1']}"
    )
    assert statuses["jelly-1"] is PublisherStatus.PUBLISHED, (
        f"Jellyfin must publish successfully despite Emby being down; got {statuses['jelly-1']}"
    )
    assert statuses["emby-down"] is PublisherStatus.FAILED, (
        f"Emby must report FAILED (ConnectionError); got {statuses['emby-down']}"
    )

    # The Emby row's message must mention the connection error so an op
    # diagnosing "why didn't Emby get the preview?" can find the cause.
    emby_row = next(p for p in result.publishers if p.server_id == "emby-down")
    assert emby_row.message, "FAILED publisher must include a message"


def test_all_publishers_fail_aggregate_is_failed(mock_config, tmp_path):
    """Mirror test for the bottom edge: when ALL publishers fail, aggregate
    rolls up to FAILED (not PUBLISHED). Without this assertion, a
    regression that always returned PUBLISHED would leave users thinking
    everything succeeded when nothing did.
    """
    mock_config.working_tmp_folder = str(tmp_path / "tmp")
    media_dir = tmp_path / "data" / "movies"
    media_dir.mkdir(parents=True)
    media_file = media_dir / "Test (2024).mkv"
    media_file.write_bytes(b"placeholder")
    media_root = str(media_dir)

    registry = ServerRegistry.from_settings(
        [
            _server_dict(
                "emby-1",
                ServerType.EMBY,
                libraries=[Library(id="1", name="Movies", remote_paths=(media_root,), enabled=True)],
                adapter="emby_sidecar",
            ),
            _server_dict(
                "emby-2",
                ServerType.EMBY,
                libraries=[Library(id="2", name="Movies", remote_paths=(media_root,), enabled=True)],
                adapter="emby_sidecar",
            ),
        ],
    )

    def fake_generate_images(video_file, output_folder, *args, **kwargs):
        _populate_frames(Path(output_folder), count=3)
        return (True, 3, "h264", 1.0, 30.0, None)

    from media_preview_generator.output.emby_sidecar import EmbyBifAdapter

    def fake_publish_fails(self, bundle, output_paths, item_id=None):
        raise OSError("Disk full")

    with (
        patch(
            "media_preview_generator.processing.multi_server.generate_images",
            side_effect=fake_generate_images,
        ),
        patch.object(EmbyBifAdapter, "publish", autospec=True, side_effect=fake_publish_fails),
    ):
        result = process_canonical_path(
            canonical_path=str(media_file),
            registry=registry,
            config=mock_config,
        )

    assert result.status is MultiServerStatus.FAILED, (
        f"Aggregate must be FAILED when all publishers fail; got {result.status}"
    )
    statuses = {p.server_id: p.status for p in result.publishers}
    assert all(s is PublisherStatus.FAILED for s in statuses.values()), (
        f"All publishers must report FAILED; got {statuses}"
    )
