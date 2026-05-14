"""Reusable frame extraction cache.

When the same canonical path triggers multiple webhooks in quick
succession (e.g. Sonarr fires on download, then Plex fires on its own
``library.new`` for the same file a few seconds later), the second hit
should not pay for another FFmpeg pass. The :class:`FrameCache` keeps
the extracted JPGs from the first call in a dedicated cache directory,
keyed by canonical path + file mtime, with TTL- and size-based
eviction.

The dispatcher (:func:`processing.multi_server.process_canonical_path`)
consults the cache before calling :func:`generate_images`. On a hit it
reuses the existing frames; on a miss it generates and *publishes* the
result back into the cache.

Thread-safe — all mutating methods acquire an internal lock so the
cache is safe to share across the worker pool.

Cache validity rules:

- Entry is valid only when the source file's ``mtime`` matches the
  recorded mtime. A file that's been re-encoded / replaced returns a
  miss and the cache entry is evicted.
- Entries expire after ``ttl_seconds`` regardless of mtime — protects
  against cache file corruption or partial writes from a previous run
  by bounding the trust window.
- LRU eviction keeps the cache to ``max_entries`` directories.

The cache directory is set up under
``{working_tmp_folder}/frame_cache``; entries are subdirectories named
``frames-<sha256[:16]>`` so they can coexist with the ad-hoc tmp dirs
from the legacy single-Plex orchestrator.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

_DEFAULT_TTL_SECONDS = 3600  # 1 hour — covers cross-vendor webhook arrivals (e.g. Plex
# fires immediately, Jellyfin fires 15-30 min later for the same file once the user has
# both servers configured). Tunable via the ``frame_reuse`` block in settings.json.
_DEFAULT_MAX_ENTRIES = 1024  # generous; the disk cap below is the real backstop
_DEFAULT_MAX_DISK_MB = 2048  # 2 GB ceiling on the on-disk cache


@dataclass(frozen=True)
class CacheEntry:
    """One cached frame extraction."""

    canonical_path: str
    frame_dir: Path
    frame_count: int
    source_mtime: float
    cached_at: float


class FrameCache:
    """In-memory + on-disk LRU cache of extracted JPG frame directories.

    Args:
        base_dir: Directory under which cache entries (``frames-<hash>/``)
            live. Created if missing.
        max_entries: Maximum number of cached entries; oldest is evicted
            when the cache is full.
        ttl_seconds: Maximum age of a cache entry. Entries older than
            this miss on lookup and are evicted lazily.
    """

    def __init__(
        self,
        base_dir: str | Path,
        *,
        max_entries: int = _DEFAULT_MAX_ENTRIES,
        ttl_seconds: int = _DEFAULT_TTL_SECONDS,
        max_disk_mb: int = _DEFAULT_MAX_DISK_MB,
    ) -> None:
        self._base_dir = Path(base_dir)
        self._base_dir.mkdir(parents=True, exist_ok=True)
        self._max_entries = int(max_entries)
        self._ttl_seconds = int(ttl_seconds)
        self._max_disk_bytes = int(max_disk_mb) * 1024 * 1024
        self._lock = threading.RLock()
        # ordered insertion: oldest first; values are CacheEntry.
        self._entries: dict[str, CacheEntry] = {}
        # Per-key generation locks. Concurrent dispatchers for the
        # same canonical path serialise here — the first one through
        # generates frames, the rest wait and hit the populated
        # cache on second look. Without this, simultaneous webhook
        # fires race on FFmpeg's rename loop in the shared tmp dir.
        self._generation_locks: dict[str, threading.Lock] = {}
        self._generation_locks_lock = threading.Lock()

    # ---------------------------------------------------------- key helpers
    def _key(self, canonical_path: str) -> str:
        return hashlib.sha256(canonical_path.encode("utf-8")).hexdigest()[:16]

    def frame_dir_for(self, canonical_path: str) -> Path:
        """Return the path the cache *would* use for ``canonical_path``.

        Returned path may or may not exist — :meth:`get` is the only
        accessor that asserts the entry is currently valid. Useful for
        callers that want to write directly into the cache slot
        (the dispatcher does this so :func:`generate_images` writes
        straight into a cache-stable location instead of an ad-hoc tmp).
        """
        return self._base_dir / f"frames-{self._key(canonical_path)}"

    def generation_lock(self, canonical_path: str) -> threading.Lock:
        """Return the per-path lock used to coalesce concurrent generation.

        The dispatcher acquires this around its cache-miss → generate →
        cache-put sequence so simultaneous webhook fires for the same
        file don't race on the shared frame directory. Lazily created
        and never evicted (the dict grows with the universe of files
        ever processed; that's bounded by the user's library size).
        """
        key = self._key(canonical_path)
        with self._generation_locks_lock:
            lock = self._generation_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._generation_locks[key] = lock
            return lock

    # ---------------------------------------------------------- accessors
    def get(self, canonical_path: str) -> CacheEntry | None:
        """Return a valid cache entry for ``canonical_path`` or ``None``.

        An entry is valid iff:
        - it exists in the in-memory map,
        - its frame directory still exists on disk,
        - the source file's mtime is unchanged since the entry was cached,
        - the entry is younger than ``ttl_seconds``.

        On any failure the entry is evicted (memory + disk) so a
        subsequent put can repopulate cleanly.
        """
        key = self._key(canonical_path)
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None

            now = time.time()
            if now - entry.cached_at > self._ttl_seconds:
                logger.info(
                    "Frame cache miss: TTL expired for {} (age {:.0f}s, ttl {}s) — will re-extract",
                    canonical_path,
                    now - entry.cached_at,
                    self._ttl_seconds,
                )
                self._evict(key)
                return None

            if not entry.frame_dir.is_dir():
                logger.info("Frame cache miss: cache dir gone for {}; will re-extract", canonical_path)
                self._evict(key)
                return None

            try:
                current_mtime = os.path.getmtime(canonical_path)
            except OSError:
                # Source file disappeared — invalidate the entry.
                logger.info("Frame cache miss: source file no longer at {}; evicting", canonical_path)
                self._evict(key)
                return None

            # Tolerate a sub-second mtime drift (some filesystems round).
            if abs(current_mtime - entry.source_mtime) > 1.0:
                logger.info(
                    "Frame cache miss: source changed for {} (mtime {} → {}); will re-extract",
                    canonical_path,
                    entry.source_mtime,
                    current_mtime,
                )
                self._evict(key)
                return None

            # Move to "most recently used" position by re-inserting.
            self._entries.pop(key)
            self._entries[key] = entry
            return entry

    def put(
        self,
        canonical_path: str,
        *,
        frame_dir: Path,
        frame_count: int,
        source_mtime: float | None = None,
    ) -> CacheEntry:
        """Record a freshly-generated frame directory in the cache.

        ``frame_dir`` must already exist and contain the JPG frames; we
        don't move or copy anything — the caller is expected to have
        used :meth:`frame_dir_for` to write directly into the cache
        slot. We just record the metadata.
        """
        if source_mtime is None:
            try:
                source_mtime = os.path.getmtime(canonical_path)
            except OSError:
                source_mtime = 0.0

        entry = CacheEntry(
            canonical_path=canonical_path,
            frame_dir=Path(frame_dir),
            frame_count=int(frame_count),
            source_mtime=float(source_mtime),
            cached_at=time.time(),
        )
        key = self._key(canonical_path)
        with self._lock:
            self._entries.pop(key, None)
            self._entries[key] = entry
            self._enforce_caps()
        return entry

    def invalidate(self, canonical_path: str) -> None:
        """Remove ``canonical_path`` from the cache + disk if present."""
        with self._lock:
            self._evict(self._key(canonical_path))

    def clear(self) -> None:
        """Remove all entries from memory and disk."""
        with self._lock:
            for key in list(self._entries.keys()):
                self._evict(key)

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    # ---------------------------------------------------------- internals
    def _evict(self, key: str) -> None:
        """Remove an entry from memory; remove its on-disk directory.

        Called under ``self._lock``. Disk-removal failures are logged
        but never raised — the cache is best-effort.
        """
        entry = self._entries.pop(key, None)
        if entry is None:
            return
        try:
            if entry.frame_dir.is_dir():
                shutil.rmtree(entry.frame_dir)
        except OSError as exc:
            logger.debug("Frame cache: failed to rmtree {}: {}", entry.frame_dir, exc)

    def _enforce_caps(self) -> None:
        """Trim oldest entries until under both caps. Caller holds the lock.

        Two caps:
        * ``max_entries`` — hard ceiling on number of in-memory entries.
        * ``max_disk_bytes`` — total bytes used by cached frame directories.
          The disk cap is the realistic backstop for long-TTL operation
          on a large library; without it the cache would grow unbounded
          when ``ttl_seconds`` is set to multiple hours.
        """
        # Entry-count cap.
        while len(self._entries) > self._max_entries:
            oldest_key = next(iter(self._entries))
            self._evict(oldest_key)

        # Disk-size cap. Walk MRU order (insertion-oldest first) and
        # drop until we're under the limit. Stat failures are skipped
        # — the entry stays, but the count may be slightly off until
        # the next put. Cheaper than locking on disk I/O for every
        # eviction decision.
        if self._max_disk_bytes <= 0 or not self._entries:
            return

        def _entry_size(entry: CacheEntry) -> int:
            try:
                total = 0
                for child in entry.frame_dir.iterdir():
                    try:
                        total += child.stat().st_size
                    except OSError:
                        continue
                return total
            except OSError:
                return 0

        # Iterate in insertion order (oldest first); evict until under cap.
        # Build the size list once to avoid re-statting after each eviction.
        # Always keep at least the most recently inserted entry — the user
        # just paid for that extraction; evicting it on the same put would
        # leave them with nothing and force a re-extract on the next get.
        sizes = {key: _entry_size(entry) for key, entry in self._entries.items()}
        total = sum(sizes.values())
        keys_in_order = list(self._entries.keys())
        for key in keys_in_order[:-1]:  # skip the most recent entry
            if total <= self._max_disk_bytes:
                break
            sz = sizes.get(key, 0)
            self._evict(key)
            total -= sz


# Singleton accessor so the dispatcher and the worker pool share one cache.
_singleton: FrameCache | None = None
_singleton_lock = threading.Lock()


def _read_frame_reuse_setting() -> tuple[int, int]:
    """Return ``(ttl_seconds, max_disk_mb)`` from the user's ``frame_reuse`` block.

    Defaults: 1 hour TTL, 2 GB disk cap. When ``enabled`` is False the TTL
    falls back to the legacy 600s value to preserve pre-v? behaviour for
    users who explicitly opt out of cross-server reuse. Settings access is
    best-effort — if the manager isn't reachable (e.g. early-boot test
    contexts), we return defaults rather than crashing the cache.
    """
    try:
        from ..web.settings_manager import get_settings_manager

        block = get_settings_manager().get("frame_reuse") or {}
    except Exception:
        return _DEFAULT_TTL_SECONDS, _DEFAULT_MAX_DISK_MB

    if not isinstance(block, dict):
        return _DEFAULT_TTL_SECONDS, _DEFAULT_MAX_DISK_MB

    enabled = bool(block.get("enabled", True))
    if not enabled:
        # Legacy short-window behaviour for users who opt out of
        # cross-vendor reuse. Same disk cap regardless so unbounded
        # disk consumption can't slip through.
        return 600, int(block.get("max_cache_disk_mb", _DEFAULT_MAX_DISK_MB) or _DEFAULT_MAX_DISK_MB)

    ttl_min = int(block.get("ttl_minutes", 60) or 60)
    ttl_min = max(1, ttl_min)  # clamp pathological 0
    max_disk = int(block.get("max_cache_disk_mb", _DEFAULT_MAX_DISK_MB) or _DEFAULT_MAX_DISK_MB)
    max_disk = max(64, max_disk)  # don't let users shoot themselves in the foot
    return ttl_min * 60, max_disk


def get_frame_cache(
    *,
    base_dir: str | Path | None = None,
    max_entries: int = _DEFAULT_MAX_ENTRIES,
    ttl_seconds: int | None = None,
    max_disk_mb: int | None = None,
) -> FrameCache:
    """Return the process-wide :class:`FrameCache` (lazily constructed).

    The TTL and disk-cap are re-read from the user's ``frame_reuse``
    settings block on *every* call so toggling Settings → Performance →
    Frame Reuse takes effect immediately, with no restart. Explicit
    ``ttl_seconds`` / ``max_disk_mb`` arguments override the setting
    (used by tests that need a known value).

    First call with a non-default ``base_dir`` decides that arg.
    Subsequent calls with a *different* non-default value raise so the
    caller notices instead of silently writing into a stale location.
    Tests reset via :func:`reset_frame_cache` to bypass this guard.
    """
    global _singleton
    with _singleton_lock:
        settings_ttl, settings_disk = _read_frame_reuse_setting()
        effective_ttl = settings_ttl if ttl_seconds is None else int(ttl_seconds)
        effective_disk = settings_disk if max_disk_mb is None else int(max_disk_mb)

        if _singleton is None:
            if base_dir is None:
                # Fallback: a tempdir under /tmp keeps tests isolated when no
                # caller supplied a working_tmp_folder.
                import tempfile

                base_dir = os.path.join(tempfile.gettempdir(), "plex-previews-frame-cache")
            _singleton = FrameCache(
                base_dir=base_dir,
                max_entries=max_entries,
                ttl_seconds=effective_ttl,
                max_disk_mb=effective_disk,
            )
            return _singleton

        # Singleton already exists — verify the caller isn't passing
        # conflicting non-default config; that would silently write to
        # the wrong place. ``base_dir=None`` means "use whatever's
        # there", so don't compare those.
        if base_dir is not None and str(base_dir) != str(_singleton._base_dir):
            raise RuntimeError(
                f"FrameCache singleton already initialised with base_dir={_singleton._base_dir!r}; "
                f"cannot reconfigure with base_dir={base_dir!r} — call reset_frame_cache() first"
            )

        # Live-update TTL + disk cap so user-visible settings changes take
        # effect on the next dispatch — no process restart required.
        with _singleton._lock:
            _singleton._ttl_seconds = effective_ttl
            _singleton._max_disk_bytes = effective_disk * 1024 * 1024
        return _singleton


def reset_frame_cache() -> None:
    """Drop the singleton; the next :func:`get_frame_cache` call rebuilds it.

    Used by tests so each test gets a fresh, isolated cache.
    """
    global _singleton
    with _singleton_lock:
        if _singleton is not None:
            _singleton.clear()
        _singleton = None
