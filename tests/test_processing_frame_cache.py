"""Tests for the multi-server frame cache."""

from __future__ import annotations

import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from media_preview_generator.processing.frame_cache import (
    FrameCache,
    get_frame_cache,
    reset_frame_cache,
)
from media_preview_generator.processing.multi_server import (
    MultiServerStatus,
    process_canonical_path,
)
from media_preview_generator.servers import ServerRegistry


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

    def test_eviction_removes_on_disk_frames(self, tmp_path):
        """Evicting an entry must clean up the on-disk frame dir too —
        otherwise a long-running process leaks disk space at the rate
        of one frame-dir per evicted file."""
        cache = FrameCache(tmp_path / "cache", max_entries=1)

        media_a = tmp_path / "a.mkv"
        media_a.write_bytes(b"x")
        slot_a = cache.frame_dir_for(str(media_a))
        _populate_real_jpgs(slot_a, count=2)
        cache.put(str(media_a), frame_dir=slot_a, frame_count=2)
        assert slot_a.is_dir()

        # Adding 'b' evicts 'a' — slot_a should be removed from disk.
        media_b = tmp_path / "b.mkv"
        media_b.write_bytes(b"x")
        slot_b = cache.frame_dir_for(str(media_b))
        _populate_real_jpgs(slot_b, count=2)
        cache.put(str(media_b), frame_dir=slot_b, frame_count=2)

        assert not slot_a.exists(), "evicted entry's frame dir should be deleted from disk"
        assert slot_b.is_dir()

    def test_max_entries_default_is_generous(self, tmp_path):
        """The entry-count cap is generous now (1024); the disk-size cap is
        the real backstop. Defends against a regression that would balloon
        disk usage when the TTL is set to many hours."""
        cache = FrameCache(tmp_path / "cache")
        # Push more than the legacy 32 cap to verify entries aren't being
        # evicted purely by count under the new defaults.
        for i in range(50):
            media = tmp_path / f"f{i:03d}.mkv"
            media.write_bytes(b"x")
            slot = cache.frame_dir_for(str(media))
            _populate_real_jpgs(slot, count=1)
            cache.put(str(media), frame_dir=slot, frame_count=1)
        # All 50 should be retained; the disk-cap is what bounds growth now.
        assert len(cache) == 50

    def test_eviction_at_size_cap_does_not_strand_generation_locks(self, tmp_path):
        """``generation_locks`` are intentionally never evicted (per docstring),
        but we assert it: the LRU policy applies only to entries, not locks.
        A regression that started evicting locks could deadlock concurrent
        webhook fires for the same file."""
        cache = FrameCache(tmp_path / "cache", max_entries=2)
        # Touch the lock for "a" then fill the cache to evict "a".
        lock_a = cache.generation_lock("/a.mkv")
        for path in ("a.mkv", "b.mkv", "c.mkv"):
            media = tmp_path / path
            media.write_bytes(b"x")
            slot = cache.frame_dir_for(str(media))
            _populate_real_jpgs(slot, count=1)
            cache.put(str(media), frame_dir=slot, frame_count=1)
        # 'a' is evicted from entries…
        assert cache.get(str(tmp_path / "a.mkv")) is None
        # …but the lock for the same path is still the same object —
        # i.e. lock identity preserved across the eviction.
        assert cache.generation_lock("/a.mkv") is lock_a


class TestSingletonAccessor:
    def test_returns_same_instance_with_matching_args(self, tmp_path):
        a = get_frame_cache(base_dir=tmp_path / "cache")
        b = get_frame_cache(base_dir=tmp_path / "cache")
        assert a is b  # idempotent when args match

    def test_returns_same_instance_when_second_call_omits_args(self, tmp_path):
        a = get_frame_cache(base_dir=tmp_path / "cache")
        b = get_frame_cache()
        assert a is b  # ``base_dir=None`` means "use existing"

    def test_reconfigure_with_different_base_dir_raises(self, tmp_path):
        get_frame_cache(base_dir=tmp_path / "cache")
        with pytest.raises(RuntimeError, match="already initialised"):
            get_frame_cache(base_dir=tmp_path / "different")

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
            "media_preview_generator.processing.multi_server.generate_images",
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
            "media_preview_generator.processing.multi_server.generate_images",
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
            "media_preview_generator.processing.multi_server.generate_images",
            side_effect=fake_generate_images,
        ):
            process_canonical_path(
                canonical_path=str(media),
                registry=registry,
                config=mock_config,
                use_frame_cache=False,
            )

        # Cache directory was never populated.
        from media_preview_generator.processing.frame_cache import get_frame_cache as _gfc

        cache = _gfc()
        assert len(cache) == 0
        # The single publisher succeeded.
        assert (tmp_path / "movies" / "Test-320-10.bif").exists()


class TestConfigurableFrameReuse:
    """The user-facing ``frame_reuse`` settings block drives TTL + disk cap."""

    def test_default_ttl_covers_one_hour(self, tmp_path):
        """Default settings (no frame_reuse block) → 1-hour TTL.

        Covers the user's "added a Jellyfin server 30 min later, fired a
        webhook, expected reuse" scenario. The legacy 10-min TTL would
        have missed; the new 1-hour default catches it.
        """
        cache = FrameCache(tmp_path / "cache")  # uses _DEFAULT_TTL_SECONDS
        media = tmp_path / "media.mkv"
        media.write_bytes(b"x")
        slot = cache.frame_dir_for(str(media))
        _populate_real_jpgs(slot, count=2)
        cache.put(str(media), frame_dir=slot, frame_count=2)

        # Simulate 45 min later: rewind cached_at by 45 min.
        key = list(cache._entries.keys())[0]
        old_entry = cache._entries[key]
        cache._entries[key] = type(old_entry)(
            canonical_path=old_entry.canonical_path,
            frame_dir=old_entry.frame_dir,
            frame_count=old_entry.frame_count,
            source_mtime=old_entry.source_mtime,
            cached_at=time.time() - (45 * 60),
        )

        # 45 min < 60 min default TTL → still a hit.
        assert cache.get(str(media)) is not None

    def test_legacy_ten_minute_ttl_when_disabled(self):
        """When the user disables frame_reuse, TTL falls back to legacy 600s."""
        from unittest.mock import patch as _patch

        from media_preview_generator.processing.frame_cache import _read_frame_reuse_setting

        with _patch("media_preview_generator.web.settings_manager.get_settings_manager") as mock_sm:
            mock_sm.return_value.get.return_value = {"enabled": False}
            ttl, _disk = _read_frame_reuse_setting()
        assert ttl == 600

    def test_settings_block_drives_ttl(self):
        """ttl_minutes from settings is honoured when enabled=True."""
        from unittest.mock import patch as _patch

        from media_preview_generator.processing.frame_cache import _read_frame_reuse_setting

        with _patch("media_preview_generator.web.settings_manager.get_settings_manager") as mock_sm:
            mock_sm.return_value.get.return_value = {
                "enabled": True,
                "ttl_minutes": 120,
                "max_cache_disk_mb": 4096,
            }
            ttl, disk = _read_frame_reuse_setting()
        assert ttl == 120 * 60
        assert disk == 4096

    def test_disk_cap_evicts_when_over(self, tmp_path):
        """Disk cap LRU-evicts oldest entries when total exceeds the limit.

        Each entry holds N decodable JPGs; we set a tiny disk cap so even
        one entry blows past it and triggers eviction on the next put.
        """
        # 1 MB cap; each entry will be a few KB so cap doesn't bite immediately.
        cache = FrameCache(tmp_path / "cache", max_disk_mb=1)

        # Each entry is ~3 KB (one tiny JPG); add 5 entries — under cap.
        for i in range(5):
            media = tmp_path / f"f{i:03d}.mkv"
            media.write_bytes(b"x")
            slot = cache.frame_dir_for(str(media))
            _populate_real_jpgs(slot, count=1)
            cache.put(str(media), frame_dir=slot, frame_count=1)
        assert len(cache) == 5

        # Now drop the cap to ~1 KB and add one more entry; the LRU
        # eviction should drop the oldest entries until we're under cap.
        cache._max_disk_bytes = 1024
        media = tmp_path / "newest.mkv"
        media.write_bytes(b"x")
        slot = cache.frame_dir_for(str(media))
        _populate_real_jpgs(slot, count=1)
        cache.put(str(media), frame_dir=slot, frame_count=1)
        # At least some old entries should be gone; the newest survives.
        assert len(cache) < 6
        assert cache.get(str(media)) is not None

    def test_get_frame_cache_reads_settings_on_first_construction(self, tmp_path):
        """The singleton's TTL is seeded from the frame_reuse settings block."""
        from unittest.mock import patch as _patch

        with _patch("media_preview_generator.web.settings_manager.get_settings_manager") as mock_sm:
            mock_sm.return_value.get.return_value = {
                "enabled": True,
                "ttl_minutes": 30,
                "max_cache_disk_mb": 512,
            }
            cache = get_frame_cache(base_dir=str(tmp_path / "cache"))
        assert cache._ttl_seconds == 30 * 60
        assert cache._max_disk_bytes == 512 * 1024 * 1024
