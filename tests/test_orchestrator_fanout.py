"""Tests for the secondary-publisher fan-out hook in :mod:`processing.orchestrator`.

The legacy single-Plex ``process_item`` flow now fans the freshly
extracted frames out to every non-Plex configured server (Emby /
Jellyfin) so scheduled scans drive the multi-server pipeline too —
not just webhook hits. These tests mock the FFmpeg + BIF-pack stages
so they run fast and verify just the fan-out wiring.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from media_preview_generator.processing.frame_cache import (
    get_frame_cache,
    reset_frame_cache,
)
from media_preview_generator.processing.orchestrator import _fan_out_secondary_publishers


def _populate_real_jpgs(directory: Path, count: int) -> None:
    """Write decodable Pillow JPGs (Jellyfin tile-grid needs decodable input)."""
    from PIL import Image

    directory.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (320, 180), (10, 20, 30))
    for i in range(count):
        img.save(directory / f"{i:05d}.jpg", "JPEG", quality=70)


@pytest.fixture(autouse=True)
def _reset_singleton_cache():
    reset_frame_cache()
    yield
    reset_frame_cache()


@pytest.fixture
def fanout_setup(mock_config, tmp_path, monkeypatch):
    """Common scaffolding: Plex tmp dir with frames + Emby+Jellyfin in settings."""
    mock_config.working_tmp_folder = str(tmp_path / "tmp")
    Path(mock_config.working_tmp_folder).mkdir(parents=True, exist_ok=True)

    media_root = tmp_path / "data" / "movies"
    media_dir = media_root / "Test (2024)"
    media_dir.mkdir(parents=True)
    media_file = media_dir / "Test (2024).mkv"
    media_file.write_bytes(b"placeholder")

    # Plex tmp_path the way process_item builds it (working_tmp/{hash}).
    plex_tmp_path = tmp_path / "tmp" / "abcd1234"
    _populate_real_jpgs(plex_tmp_path, count=5)

    # Configure Emby + Jellyfin (no Plex; the originating Plex is implicit
    # in the legacy single-Plex flow and doesn't need a media_servers entry
    # for this test).
    media_servers = [
        {
            "id": "emby-1",
            "type": "emby",
            "name": "Emby",
            "enabled": True,
            "url": "http://emby:8096",
            "auth": {"method": "api_key", "api_key": "k"},
            "libraries": [
                {
                    "id": "1",
                    "name": "Movies",
                    "remote_paths": [str(media_root)],
                    "enabled": True,
                }
            ],
            "output": {"adapter": "emby_sidecar", "width": 320, "frame_interval": 10},
        },
        {
            "id": "jelly-1",
            "type": "jellyfin",
            "name": "Jellyfin",
            "enabled": True,
            "url": "http://jellyfin:8096",
            "auth": {"method": "api_key", "api_key": "k"},
            "libraries": [
                {
                    "id": "9",
                    "name": "Movies",
                    "remote_paths": [str(media_root)],
                    "enabled": True,
                }
            ],
            "output": {"adapter": "jellyfin_trickplay", "width": 320, "frame_interval": 10},
        },
    ]
    fake_settings = type(
        "FakeSettings",
        (),
        {
            "get": lambda self, key, default=None: media_servers if key == "media_servers" else default,
        },
    )()
    monkeypatch.setattr(
        "media_preview_generator.web.settings_manager.get_settings_manager",
        lambda: fake_settings,
    )

    return {
        "config": mock_config,
        "media_file": str(media_file),
        "plex_tmp_path": str(plex_tmp_path),
        "media_root": str(media_root),
    }


class TestFanOut:
    def test_emby_and_jellyfin_publishers_run_using_plex_extracted_frames(self, fanout_setup):
        ctx = fanout_setup

        # Jellyfin needs an item id (its manifest is keyed by it). Mock the
        # reverse-lookup so the secondary fan-out doesn't try to hit a live
        # Jellyfin server during the unit test.
        with (
            patch("media_preview_generator.processing.multi_server.generate_images") as gen,
            patch(
                "media_preview_generator.servers.jellyfin.JellyfinServer.resolve_remote_path_to_item_id",
                return_value="jf-item-42",
            ),
            patch(
                "media_preview_generator.servers.jellyfin.JellyfinServer.trigger_refresh",
                return_value=None,
            ),
            patch(
                "media_preview_generator.servers.emby.EmbyServer.trigger_refresh",
                return_value=None,
            ),
        ):
            _fan_out_secondary_publishers(
                canonical_path=ctx["media_file"],
                frame_dir=ctx["plex_tmp_path"],
                config=ctx["config"],
            )

        # FFmpeg never ran a second time.
        assert gen.call_count == 0

        # Emby sidecar landed.
        sidecar = Path(ctx["media_file"]).parent / "Test (2024)-320-10.bif"
        assert sidecar.exists()

        # Jellyfin tile-grid landed (manifest + sheets dir).
        trickplay = Path(ctx["media_file"]).parent / "trickplay"
        assert (trickplay / "Test (2024)-320.json").exists()
        assert (trickplay / "Test (2024)-320").is_dir()

    def test_frames_moved_into_cache(self, fanout_setup):
        ctx = fanout_setup

        with (
            patch("media_preview_generator.processing.multi_server.generate_images"),
            patch(
                "media_preview_generator.servers.jellyfin.JellyfinServer.resolve_remote_path_to_item_id",
                return_value="jf-item-42",
            ),
            patch(
                "media_preview_generator.servers.jellyfin.JellyfinServer.trigger_refresh",
                return_value=None,
            ),
            patch(
                "media_preview_generator.servers.emby.EmbyServer.trigger_refresh",
                return_value=None,
            ),
        ):
            _fan_out_secondary_publishers(
                canonical_path=ctx["media_file"],
                frame_dir=ctx["plex_tmp_path"],
                config=ctx["config"],
            )

        # Original Plex tmp_path is gone (moved into the cache).
        assert not os.path.exists(ctx["plex_tmp_path"])

        # Cache has an entry for the canonical path.
        cache = get_frame_cache()
        entry = cache.get(ctx["media_file"])
        assert entry is not None
        assert entry.frame_count == 5

    def test_no_op_when_no_secondary_servers_configured(self, mock_config, tmp_path, monkeypatch):
        mock_config.working_tmp_folder = str(tmp_path / "tmp")

        # No media_servers at all → fan-out is a no-op.
        monkeypatch.setattr(
            "media_preview_generator.web.settings_manager.get_settings_manager",
            lambda: type("S", (), {"get": lambda self, k, d=None: d})(),
        )

        plex_tmp = tmp_path / "tmp" / "xyz"
        _populate_real_jpgs(plex_tmp, count=2)

        # Should return without touching anything (no cache populated, frames still present).
        _fan_out_secondary_publishers(
            canonical_path=str(tmp_path / "Foo.mkv"),
            frame_dir=str(plex_tmp),
            config=mock_config,
        )

        # Plex tmp dir untouched (no fan-out happened).
        assert plex_tmp.exists()
        assert get_frame_cache().get(str(tmp_path / "Foo.mkv")) is None

    def test_no_op_when_only_plex_servers_configured(self, mock_config, tmp_path, monkeypatch):
        mock_config.working_tmp_folder = str(tmp_path / "tmp")

        media_servers = [
            {
                "id": "plex-default",
                "type": "plex",
                "name": "Plex",
                "enabled": True,
                "url": "http://plex:32400",
                "auth": {"token": "t"},
            }
        ]
        monkeypatch.setattr(
            "media_preview_generator.web.settings_manager.get_settings_manager",
            lambda: type(
                "S",
                (),
                {"get": lambda self, k, d=None: media_servers if k == "media_servers" else d},
            )(),
        )

        plex_tmp = tmp_path / "tmp" / "abc"
        _populate_real_jpgs(plex_tmp, count=2)

        _fan_out_secondary_publishers(
            canonical_path=str(tmp_path / "Foo.mkv"),
            frame_dir=str(plex_tmp),
            config=mock_config,
        )

        # Plex-only registry: nothing to fan out to. Frames untouched.
        assert plex_tmp.exists()

    def test_swallows_settings_errors(self, mock_config, tmp_path, monkeypatch):
        """If settings can't be loaded, fan-out logs and skips — never crashes the scan."""
        mock_config.working_tmp_folder = str(tmp_path / "tmp")
        plex_tmp = tmp_path / "tmp" / "abc"
        _populate_real_jpgs(plex_tmp, count=1)

        def boom():
            raise RuntimeError("settings unavailable")

        monkeypatch.setattr(
            "media_preview_generator.web.settings_manager.get_settings_manager",
            boom,
        )

        # Must not raise.
        _fan_out_secondary_publishers(
            canonical_path=str(tmp_path / "Foo.mkv"),
            frame_dir=str(plex_tmp),
            config=mock_config,
        )

    def test_disabled_secondary_servers_skipped(self, mock_config, tmp_path, monkeypatch):
        """A disabled non-Plex server doesn't trigger fan-out work."""
        mock_config.working_tmp_folder = str(tmp_path / "tmp")
        media_servers = [
            {
                "id": "emby-1",
                "type": "emby",
                "name": "Emby",
                "enabled": False,  # disabled — skip
                "url": "http://emby:8096",
                "auth": {},
            }
        ]
        monkeypatch.setattr(
            "media_preview_generator.web.settings_manager.get_settings_manager",
            lambda: type(
                "S",
                (),
                {"get": lambda self, k, d=None: media_servers if k == "media_servers" else d},
            )(),
        )

        plex_tmp = tmp_path / "tmp" / "abc"
        _populate_real_jpgs(plex_tmp, count=1)

        _fan_out_secondary_publishers(
            canonical_path=str(tmp_path / "Foo.mkv"),
            frame_dir=str(plex_tmp),
            config=mock_config,
        )

        # Disabled server: no fan-out, frames untouched.
        assert plex_tmp.exists()

    def test_frame_dir_none_dispatches_extraction_to_canonical_pipeline(self, fanout_setup):
        """When Plex skipped extraction (BIF already exists), pass frame_dir=None.

        The fan-out should still dispatch through process_canonical_path so
        Emby/Jellyfin servers added later get backfilled — even though
        there are no Plex-extracted frames to seed the cache with.
        """
        ctx = fanout_setup

        # The helper does a deferred import; patch at source.
        from media_preview_generator.processing.multi_server import (
            MultiServerResult,
            MultiServerStatus,
        )

        with patch(
            "media_preview_generator.processing.multi_server.process_canonical_path",
            return_value=MultiServerResult(
                canonical_path=ctx["media_file"],
                status=MultiServerStatus.SKIPPED,
                publishers=[],
            ),
        ) as proc_orch:
            _fan_out_secondary_publishers(
                canonical_path=ctx["media_file"],
                frame_dir=None,
                config=ctx["config"],
            )

        # Dispatched once; no frame-cache seeding happened.
        proc_orch.assert_called_once()
        # Plex tmp dir is untouched because frame_dir was None.
        assert os.path.exists(ctx["plex_tmp_path"])
