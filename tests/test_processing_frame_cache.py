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
    @pytest.mark.slow
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

    def test_sub_second_mtime_drift_still_hits(self, tmp_path):
        """A 0.5s mtime drift counts as the SAME file — within the 1.0s
        tolerance window in :meth:`get`.

        Why this matters: NFS, SMB, and FAT filesystems all round mtime
        to whole seconds. Without the ``> 1.0`` slack at frame_cache.py
        line ~177, every NFS-backed library would see false invalidations
        whenever any non-rounded code path touched the file. A regression
        tightening the comparison to strict equality would silently
        thrash the cache for the whole NFS user base.
        """
        cache = FrameCache(tmp_path / "cache")
        media = tmp_path / "nfs_like.mkv"
        media.write_bytes(b"x")
        slot = cache.frame_dir_for(str(media))
        _populate_real_jpgs(slot, count=1)

        # Put with the actual mtime, then nudge the cached mtime by 0.5s
        # to simulate the cross-filesystem drift the tolerance protects.
        cache.put(str(media), frame_dir=slot, frame_count=1)
        key = list(cache._entries.keys())[0]
        original = cache._entries[key]
        cache._entries[key] = type(original)(
            canonical_path=original.canonical_path,
            frame_dir=original.frame_dir,
            frame_count=original.frame_count,
            source_mtime=original.source_mtime - 0.5,  # 0.5s off — within tolerance
            cached_at=original.cached_at,
        )

        assert cache.get(str(media)) is not None, (
            "0.5s mtime drift should be tolerated — NFS rounding would otherwise thrash the cache"
        )

        # And confirm the boundary: a 1.5s drift IS treated as a real change.
        cache._entries[key] = type(original)(
            canonical_path=original.canonical_path,
            frame_dir=original.frame_dir,
            frame_count=original.frame_count,
            source_mtime=original.source_mtime - 1.5,  # past tolerance
            cached_at=original.cached_at,
        )
        # Re-populate slot since the previous get() may have evicted it
        # (it shouldn't have, but be defensive — the boundary check is
        # the assertion that matters).
        _populate_real_jpgs(slot, count=1)
        assert cache.get(str(media)) is None, "1.5s drift should be treated as a real source change"


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

    def test_generation_lock_actually_serializes_same_path(self, tmp_path):
        """Two threads asking for ``generation_lock(P)`` get the SAME lock so
        only one FFmpeg pass runs even when webhooks for path P arrive
        simultaneously.

        Why this matters: without serialisation, a Plex webhook + a Sonarr
        webhook arriving in the same 50 ms window both miss the cache,
        both call ``generate_images``, and the user pays 2x. The
        per-path lock in :meth:`generation_lock` is the only thing
        preventing that race. The existing eviction test proves lock
        IDENTITY survives, but never exercises the actual mutual-exclusion
        behaviour. This test does — thread B must block while A holds
        the lock, then enter once A releases.
        """
        import threading

        cache = FrameCache(tmp_path / "cache")
        lock = cache.generation_lock("/data/movies/x.mkv")

        a_acquired = threading.Event()
        a_release = threading.Event()
        b_acquired = threading.Event()

        def thread_a():
            with lock:
                a_acquired.set()
                # Hold the lock until the test releases us.
                a_release.wait(timeout=5)

        def thread_b():
            # B asks for the SAME canonical path → SAME lock.
            with cache.generation_lock("/data/movies/x.mkv"):
                b_acquired.set()

        ta = threading.Thread(target=thread_a)
        tb = threading.Thread(target=thread_b)
        ta.start()
        a_acquired.wait(timeout=2), "thread A must acquire its lock"
        # Start B AFTER A has the lock.
        tb.start()
        # Give B a generous chance to acquire — it MUST NOT, because A
        # is still holding the same lock object.
        assert not b_acquired.wait(timeout=0.3), (
            "thread B acquired the per-path generation lock while A still held it — "
            "concurrent FFmpeg passes would race; the lock isn't serialising"
        )
        # Release A; B should immediately enter.
        a_release.set()
        assert b_acquired.wait(timeout=2), "thread B never acquired after A released — lock didn't hand off"
        ta.join(timeout=2)
        tb.join(timeout=2)

    def test_generation_lock_distinct_paths_do_not_serialize(self, tmp_path):
        """Different canonical paths get DIFFERENT locks so two unrelated
        webhooks (one for /movies/A.mkv, one for /movies/B.mkv) can both
        run FFmpeg in parallel. A regression that returned a global lock
        would serialise the whole worker pool to one extraction at a
        time — devastating for throughput.
        """
        import threading

        cache = FrameCache(tmp_path / "cache")
        lock_a = cache.generation_lock("/data/movies/a.mkv")

        b_acquired = threading.Event()

        def thread_b():
            with cache.generation_lock("/data/movies/b.mkv"):
                b_acquired.set()

        with lock_a:
            tb = threading.Thread(target=thread_b)
            tb.start()
            # B is on a DIFFERENT path → must NOT block on A's lock.
            assert b_acquired.wait(timeout=2), (
                "thread B blocked even though it asked for a different path — locks aren't per-path"
            )
            tb.join(timeout=2)


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

    def test_settings_treats_zero_ttl_as_missing_and_uses_default(self):
        """ttl_minutes=0 in settings is treated as "use default" (60 min),
        NOT as a literal 0 that would make every cache lookup an instant
        miss.

        The defence is implicit in ``int(block.get("ttl_minutes", 60) or 60)``
        — the ``or 60`` short-circuit eats any falsy value (0, None, "")
        BEFORE the ``max(1, ttl_min)`` clamp on the next line ever sees
        it. Net effect: 0 → 60 minutes (the default), never 0 → 1 minute
        (the clamp floor). The comment on the next line is misleading —
        the clamp is unreachable for 0.

        Why this matters: a user typing 0 in settings.json would otherwise
        turn the entire cache into a no-op, silently doubling FFmpeg load.
        Either defence (default fallback OR clamp) prevents that; this
        test pins which one production actually uses so a future
        refactor doesn't accidentally collapse 0 to 0.
        """
        from unittest.mock import patch as _patch

        from media_preview_generator.processing.frame_cache import (
            _DEFAULT_TTL_SECONDS,
            _read_frame_reuse_setting,
        )

        with _patch("media_preview_generator.web.settings_manager.get_settings_manager") as mock_sm:
            mock_sm.return_value.get.return_value = {"enabled": True, "ttl_minutes": 0}
            ttl, _ = _read_frame_reuse_setting()
        assert ttl == _DEFAULT_TTL_SECONDS, (
            f"ttl_minutes=0 must produce default TTL ({_DEFAULT_TTL_SECONDS}s), got {ttl}s — "
            f"a regression that returned 0s would silently disable the cache"
        )

        # Same contract for None and "" — anything falsy goes to default.
        for falsy in (None, ""):
            with _patch("media_preview_generator.web.settings_manager.get_settings_manager") as mock_sm:
                mock_sm.return_value.get.return_value = {"enabled": True, "ttl_minutes": falsy}
                ttl, _ = _read_frame_reuse_setting()
            assert ttl == _DEFAULT_TTL_SECONDS, f"ttl_minutes={falsy!r} should fall back to default; got {ttl}s"

    def test_settings_clamps_pathological_small_disk_cap(self):
        """max_cache_disk_mb < 64 is clamped to 64 MB — protects users
        from accidentally setting a sub-1-frame disk cap that would
        evict every entry on every put. Mirrors the TTL clamp; same
        rationale.
        """
        from unittest.mock import patch as _patch

        from media_preview_generator.processing.frame_cache import _read_frame_reuse_setting

        with _patch("media_preview_generator.web.settings_manager.get_settings_manager") as mock_sm:
            mock_sm.return_value.get.return_value = {
                "enabled": True,
                "ttl_minutes": 60,
                "max_cache_disk_mb": 1,
            }
            _, disk = _read_frame_reuse_setting()
        assert disk == 64, f"max_cache_disk_mb=1 must be clamped to floor of 64, got {disk}"

    def test_settings_returns_defaults_when_block_not_a_dict(self):
        """Garbage in settings (e.g. ``frame_reuse: "yes"``) falls back to
        defaults instead of crashing. Same defensive contract the docstring
        promises at frame_cache.py line ~330.
        """
        from unittest.mock import patch as _patch

        from media_preview_generator.processing.frame_cache import (
            _DEFAULT_MAX_DISK_MB,
            _DEFAULT_TTL_SECONDS,
            _read_frame_reuse_setting,
        )

        with _patch("media_preview_generator.web.settings_manager.get_settings_manager") as mock_sm:
            mock_sm.return_value.get.return_value = "not-a-dict"
            ttl, disk = _read_frame_reuse_setting()
        assert ttl == _DEFAULT_TTL_SECONDS
        assert disk == _DEFAULT_MAX_DISK_MB

    def test_settings_returns_defaults_when_manager_raises(self):
        """If the settings manager isn't reachable (early-boot, test
        contexts), defaults are returned silently. The cache must keep
        working even when the settings store is unavailable — otherwise
        early-init dispatchers would crash with confusing errors.
        """
        from unittest.mock import patch as _patch

        from media_preview_generator.processing.frame_cache import (
            _DEFAULT_MAX_DISK_MB,
            _DEFAULT_TTL_SECONDS,
            _read_frame_reuse_setting,
        )

        with _patch("media_preview_generator.web.settings_manager.get_settings_manager") as mock_sm:
            mock_sm.side_effect = RuntimeError("no settings yet")
            ttl, disk = _read_frame_reuse_setting()
        assert ttl == _DEFAULT_TTL_SECONDS
        assert disk == _DEFAULT_MAX_DISK_MB

    def test_disk_cap_evicts_when_over(self, tmp_path):
        """Disk cap LRU-evicts oldest entries (oldest-first) until total <= cap.

        Each entry holds one decodable JPG (~600 bytes after PIL's
        quality=70 encode). We seed five entries while the cap is large,
        then drop the cap so the *next* put forces eviction. Because the
        evictor walks oldest-first and stops the moment it's under cap,
        the result is deterministic — every old entry must be gone and
        only the newly-inserted entry remains.
        """
        cache = FrameCache(tmp_path / "cache", max_disk_mb=1)

        # Five entries while cap is generous.
        for i in range(5):
            media = tmp_path / f"f{i:03d}.mkv"
            media.write_bytes(b"x")
            slot = cache.frame_dir_for(str(media))
            _populate_real_jpgs(slot, count=1)
            cache.put(str(media), frame_dir=slot, frame_count=1)
        assert len(cache) == 5

        # Compute the per-entry size from disk so the assertion adapts
        # to PIL's exact JPEG output without going loose.
        entry_sizes = [
            sum(child.stat().st_size for child in entry.frame_dir.iterdir()) for entry in cache._entries.values()
        ]
        # Cap chosen so only ONE entry can survive after the next put:
        # smaller than 2 entries' worth, larger than 1 entry's worth.
        cap = max(entry_sizes) + 1
        assert cap < sum(sorted(entry_sizes, reverse=True)[:2]), (
            "test setup: cap must force eviction down to exactly the newest entry"
        )
        cache._max_disk_bytes = cap

        media = tmp_path / "newest.mkv"
        media.write_bytes(b"x")
        slot = cache.frame_dir_for(str(media))
        _populate_real_jpgs(slot, count=1)
        cache.put(str(media), frame_dir=slot, frame_count=1)

        # Exactly one entry remains, and it is the newest one (the
        # always-keep-newest invariant the evictor advertises).
        assert len(cache) == 1
        assert cache.get(str(media)) is not None
        # Every previously-inserted entry was actually evicted, not just
        # marked stale — none should resolve via .get().
        for i in range(5):
            assert cache.get(str(tmp_path / f"f{i:03d}.mkv")) is None

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

    def test_settings_changes_apply_without_restart(self, tmp_path):
        """Changing frame_reuse in Settings takes effect on the next dispatch.

        Regression: the singleton used to cache TTL+cap forever, so users
        toggling Settings → Performance → Frame Reuse saw zero change
        until gunicorn restarted. ``get_frame_cache()`` now re-reads on
        every call and live-updates the singleton's fields.
        """
        from unittest.mock import patch as _patch

        # First construction: 60-minute TTL.
        with _patch("media_preview_generator.web.settings_manager.get_settings_manager") as mock_sm:
            mock_sm.return_value.get.return_value = {
                "enabled": True,
                "ttl_minutes": 60,
                "max_cache_disk_mb": 2048,
            }
            cache = get_frame_cache(base_dir=str(tmp_path / "cache"))
        assert cache._ttl_seconds == 60 * 60
        assert cache._max_disk_bytes == 2048 * 1024 * 1024

        # User changes settings and triggers a new dispatch — same singleton
        # but TTL + cap should reflect the new values immediately.
        with _patch("media_preview_generator.web.settings_manager.get_settings_manager") as mock_sm:
            mock_sm.return_value.get.return_value = {
                "enabled": True,
                "ttl_minutes": 5,
                "max_cache_disk_mb": 256,
            }
            cache_again = get_frame_cache(base_dir=str(tmp_path / "cache"))
        assert cache_again is cache, "should be the same singleton"
        assert cache_again._ttl_seconds == 5 * 60
        assert cache_again._max_disk_bytes == 256 * 1024 * 1024
