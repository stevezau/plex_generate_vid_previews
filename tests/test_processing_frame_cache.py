"""Tests for the multi-server frame cache."""

from __future__ import annotations

import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from plex_generate_previews.processing.frame_cache import (
    FrameCache,
    get_frame_cache,
    reset_frame_cache,
)
from plex_generate_previews.processing.multi_server import (
    MultiServerStatus,
    process_canonical_path,
)
from plex_generate_previews.servers import ServerRegistry


@pytest.fixture(autouse=True)
def _reset_singleton_cache():
    reset_frame_cache()
    yield
    reset_frame_cache()


def _populate_real_jpgs(directory: Path, count: int) -> None:
    """Write real Pillow-encoded JPGs (Jellyfin adapter requires decodable input)."""
    from PIL import Image

    directory.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (320, 180), (10, 20, 30))
    for i in range(count):
        img.save(directory / f"{i:05d}.jpg", "JPEG", quality=70)


def _seed_canonical_file(path: Path) -> None:
    """Create a fake media file so os.path.isfile passes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"placeholder")


class TestFrameCacheBasics:
    def test_get_returns_none_when_empty(self, tmp_path):
        cache = FrameCache(tmp_path / "cache")
        assert cache.get("/some/file.mkv") is None
        assert len(cache) == 0

    def test_put_and_get_roundtrip(self, tmp_path):
        cache = FrameCache(tmp_path / "cache")
        media = tmp_path / "media.mkv"
        media.write_bytes(b"x")

        # Pre-populate the cache slot with real frames the way the
        # dispatcher will (write directly into frame_dir_for).
        slot = cache.frame_dir_for(str(media))
        _populate_real_jpgs(slot, count=5)
        cache.put(str(media), frame_dir=slot, frame_count=5)

        entry = cache.get(str(media))
        assert entry is not None
        assert entry.frame_count == 5
        assert entry.frame_dir == slot

    def test_frame_dir_for_is_deterministic(self, tmp_path):
        cache = FrameCache(tmp_path / "cache")
        a = cache.frame_dir_for("/m/foo.mkv")
        b = cache.frame_dir_for("/m/foo.mkv")
        assert a == b

    def test_invalidate_removes_entry(self, tmp_path):
        cache = FrameCache(tmp_path / "cache")
        media = tmp_path / "m.mkv"
        media.write_bytes(b"x")
        slot = cache.frame_dir_for(str(media))
        _populate_real_jpgs(slot, count=2)
        cache.put(str(media), frame_dir=slot, frame_count=2)

        cache.invalidate(str(media))

        assert cache.get(str(media)) is None
        # On-disk dir is gone too.
        assert not slot.exists()

    def test_clear_drops_everything(self, tmp_path):
        cache = FrameCache(tmp_path / "cache")
        for i in range(3):
            media = tmp_path / f"m{i}.mkv"
            media.write_bytes(b"x")
            slot = cache.frame_dir_for(str(media))
            _populate_real_jpgs(slot, count=1)
            cache.put(str(media), frame_dir=slot, frame_count=1)
        assert len(cache) == 3
        cache.clear()
        assert len(cache) == 0


class TestCacheValidity:
    def test_mtime_change_invalidates(self, tmp_path):
        cache = FrameCache(tmp_path / "cache")
        media = tmp_path / "m.mkv"
        media.write_bytes(b"original")
        slot = cache.frame_dir_for(str(media))
        _populate_real_jpgs(slot, count=2)
        cache.put(str(media), frame_dir=slot, frame_count=2)

        # Bump mtime by writing new content.
        time.sleep(1.1)  # ensure mtime granularity catches the change
        media.write_bytes(b"newer-bigger-content")
        os.utime(media, None)

        assert cache.get(str(media)) is None

    def test_ttl_expiry(self, tmp_path):
        cache = FrameCache(tmp_path / "cache", ttl_seconds=0)
        media = tmp_path / "m.mkv"
        media.write_bytes(b"x")
        slot = cache.frame_dir_for(str(media))
        _populate_real_jpgs(slot, count=1)
        cache.put(str(media), frame_dir=slot, frame_count=1)

        # ttl=0 so any subsequent get is past the deadline.
        time.sleep(0.01)
        assert cache.get(str(media)) is None

    def test_missing_frame_dir_invalidates(self, tmp_path):
        cache = FrameCache(tmp_path / "cache")
        media = tmp_path / "m.mkv"
        media.write_bytes(b"x")
        slot = cache.frame_dir_for(str(media))
        _populate_real_jpgs(slot, count=1)
        cache.put(str(media), frame_dir=slot, frame_count=1)

        # Out-of-band rmtree (e.g. someone cleaned working_tmp).
        import shutil

        shutil.rmtree(slot)

        assert cache.get(str(media)) is None
        assert len(cache) == 0  # entry was evicted on lookup

    def test_missing_source_file_invalidates(self, tmp_path):
        cache = FrameCache(tmp_path / "cache")
        media = tmp_path / "m.mkv"
        media.write_bytes(b"x")
        slot = cache.frame_dir_for(str(media))
        _populate_real_jpgs(slot, count=1)
        cache.put(str(media), frame_dir=slot, frame_count=1)

        media.unlink()
        assert cache.get(str(media)) is None


class TestLruEviction:
    def test_oldest_entry_evicted_when_full(self, tmp_path):
        cache = FrameCache(tmp_path / "cache", max_entries=2)
        for path in ("a.mkv", "b.mkv", "c.mkv"):
            media = tmp_path / path
            media.write_bytes(b"x")
            slot = cache.frame_dir_for(str(media))
            _populate_real_jpgs(slot, count=1)
            cache.put(str(media), frame_dir=slot, frame_count=1)
        assert len(cache) == 2
        # 'a' was the first put → evicted; b and c remain.
        assert cache.get(str(tmp_path / "a.mkv")) is None
        assert cache.get(str(tmp_path / "b.mkv")) is not None

    def test_get_promotes_entry_to_most_recently_used(self, tmp_path):
        cache = FrameCache(tmp_path / "cache", max_entries=2)
        for path in ["a.mkv", "b.mkv"]:
            media = tmp_path / path
            media.write_bytes(b"x")
            slot = cache.frame_dir_for(str(media))
            _populate_real_jpgs(slot, count=1)
            cache.put(str(media), frame_dir=slot, frame_count=1)

        # Access 'a' so it's most recently used.
        cache.get(str(tmp_path / "a.mkv"))

        # Add 'c' — 'b' should now be the eviction victim.
        media_c = tmp_path / "c.mkv"
        media_c.write_bytes(b"x")
        slot_c = cache.frame_dir_for(str(media_c))
        _populate_real_jpgs(slot_c, count=1)
        cache.put(str(media_c), frame_dir=slot_c, frame_count=1)

        assert cache.get(str(tmp_path / "b.mkv")) is None
        assert cache.get(str(tmp_path / "a.mkv")) is not None


class TestSingletonAccessor:
    def test_returns_same_instance(self, tmp_path):
        a = get_frame_cache(base_dir=tmp_path / "cache")
        b = get_frame_cache(base_dir=tmp_path / "different")
        assert a is b  # second call ignores args

    def test_reset_clears_singleton(self, tmp_path):
        a = get_frame_cache(base_dir=tmp_path / "cache")
        reset_frame_cache()
        b = get_frame_cache(base_dir=tmp_path / "cache2")
        assert a is not b


# ---------------------------------------------------------------------------
# Dispatcher integration: cache hit skips FFmpeg
# ---------------------------------------------------------------------------


class TestDispatcherIntegration:
    def test_second_call_hits_cache_and_skips_ffmpeg(self, mock_config, tmp_path):
        mock_config.working_tmp_folder = str(tmp_path / "tmp")
        media = tmp_path / "data" / "movies" / "Test (2024)" / "Test (2024).mkv"
        _seed_canonical_file(media)

        registry = ServerRegistry.from_settings(
            [
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
                            "remote_paths": [str(tmp_path / "data" / "movies")],
                            "enabled": True,
                        }
                    ],
                    "output": {"adapter": "emby_sidecar", "width": 320, "frame_interval": 10},
                }
            ],
        )

        def fake_generate_images(video_file, output_folder, *args, **kwargs):
            _populate_real_jpgs(Path(output_folder), count=5)
            return (True, 5, "h264", 1.0, 30.0, None)

        with patch(
            "plex_generate_previews.processing.multi_server.generate_images",
            side_effect=fake_generate_images,
        ) as gen:
            # First call: cache miss → FFmpeg runs once.
            r1 = process_canonical_path(
                canonical_path=str(media),
                registry=registry,
                config=mock_config,
            )
            # Wipe published BIF so the second call's skip-if-exists doesn't
            # kick in (we want the cache to skip FFmpeg, not the publisher
            # to skip the publish).
            sidecar = media.parent / "Test (2024)-320-10.bif"
            sidecar.unlink()

            # Second call: cache hit → FFmpeg should NOT run again.
            r2 = process_canonical_path(
                canonical_path=str(media),
                registry=registry,
                config=mock_config,
            )

        assert r1.status is MultiServerStatus.PUBLISHED
        assert r2.status is MultiServerStatus.PUBLISHED
        # The cornerstone assertion: one FFmpeg invocation across both calls.
        assert gen.call_count == 1
        # Both calls produced a real BIF.
        assert sidecar.exists()

    def test_regenerate_bypasses_cache(self, mock_config, tmp_path):
        mock_config.working_tmp_folder = str(tmp_path / "tmp")
        media = tmp_path / "movies" / "Test.mkv"
        _seed_canonical_file(media)

        registry = ServerRegistry.from_settings(
            [
                {
                    "id": "emby-1",
                    "type": "emby",
                    "name": "Emby",
                    "enabled": True,
                    "url": "http://emby:8096",
                    "auth": {},
                    "libraries": [
                        {
                            "id": "1",
                            "name": "Movies",
                            "remote_paths": [str(tmp_path / "movies")],
                            "enabled": True,
                        }
                    ],
                    "output": {"adapter": "emby_sidecar", "width": 320, "frame_interval": 10},
                }
            ],
        )

        def fake_generate_images(video_file, output_folder, *args, **kwargs):
            _populate_real_jpgs(Path(output_folder), count=3)
            return (True, 3, "h264", 1.0, 30.0, None)

        with patch(
            "plex_generate_previews.processing.multi_server.generate_images",
            side_effect=fake_generate_images,
        ) as gen:
            process_canonical_path(
                canonical_path=str(media),
                registry=registry,
                config=mock_config,
            )
            process_canonical_path(
                canonical_path=str(media),
                registry=registry,
                config=mock_config,
                regenerate=True,
            )

        # regenerate=True forces a fresh extraction, no cache shortcut.
        assert gen.call_count == 2

    def test_use_frame_cache_false_uses_adhoc_tmp(self, mock_config, tmp_path):
        mock_config.working_tmp_folder = str(tmp_path / "tmp")
        media = tmp_path / "movies" / "Test.mkv"
        _seed_canonical_file(media)

        registry = ServerRegistry.from_settings(
            [
                {
                    "id": "emby-1",
                    "type": "emby",
                    "name": "Emby",
                    "enabled": True,
                    "url": "http://emby:8096",
                    "auth": {},
                    "libraries": [
                        {
                            "id": "1",
                            "name": "Movies",
                            "remote_paths": [str(tmp_path / "movies")],
                            "enabled": True,
                        }
                    ],
                    "output": {"adapter": "emby_sidecar", "width": 320, "frame_interval": 10},
                }
            ],
        )

        def fake_generate_images(video_file, output_folder, *args, **kwargs):
            _populate_real_jpgs(Path(output_folder), count=3)
            return (True, 3, "h264", 1.0, 30.0, None)

        with patch(
            "plex_generate_previews.processing.multi_server.generate_images",
            side_effect=fake_generate_images,
        ):
            process_canonical_path(
                canonical_path=str(media),
                registry=registry,
                config=mock_config,
                use_frame_cache=False,
            )

        # Cache directory was never populated.
        from plex_generate_previews.processing.frame_cache import get_frame_cache as _gfc

        cache = _gfc()
        assert len(cache) == 0
        # The single publisher succeeded.
        assert (tmp_path / "movies" / "Test-320-10.bif").exists()
