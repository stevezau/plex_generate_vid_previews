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

_DEFAULT_TTL_SECONDS = 600  # 10 minutes — covers typical webhook-storm window
_DEFAULT_MAX_ENTRIES = 32


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
    ) -> None:
        self._base_dir = Path(base_dir)
        self._base_dir.mkdir(parents=True, exist_ok=True)
        self._max_entries = int(max_entries)
        self._ttl_seconds = int(ttl_seconds)
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
                logger.debug("Frame cache: TTL expired for {}", canonical_path)
                self._evict(key)
                return None

            if not entry.frame_dir.is_dir():
                logger.debug("Frame cache: dir missing for {}; evicting", canonical_path)
                self._evict(key)
                return None

            try:
                current_mtime = os.path.getmtime(canonical_path)
            except OSError:
                # Source file disappeared — invalidate the entry.
                logger.debug("Frame cache: source file missing for {}; evicting", canonical_path)
                self._evict(key)
                return None

            # Tolerate a sub-second mtime drift (some filesystems round).
            if abs(current_mtime - entry.source_mtime) > 1.0:
                logger.debug(
                    "Frame cache: mtime changed for {} (cached {} != current {}); evicting",
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
            self._enforce_max_entries()
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

    def _enforce_max_entries(self) -> None:
        """Trim oldest entries until len <= max_entries. Caller holds the lock."""
        while len(self._entries) > self._max_entries:
            # dict preserves insertion order (Python 3.7+) so the first
            # key is the oldest.
            oldest_key = next(iter(self._entries))
            self._evict(oldest_key)


# Singleton accessor so the dispatcher and the worker pool share one cache.
_singleton: FrameCache | None = None
_singleton_lock = threading.Lock()


def get_frame_cache(
    *,
    base_dir: str | Path | None = None,
    max_entries: int = _DEFAULT_MAX_ENTRIES,
    ttl_seconds: int = _DEFAULT_TTL_SECONDS,
) -> FrameCache:
    """Return the process-wide :class:`FrameCache` (lazily constructed).

    First call with a non-default arg decides that arg. Subsequent
    calls with a *different* non-default value raise so the caller
    notices instead of silently writing into a stale location.
    Tests reset via :func:`reset_frame_cache` to bypass this guard
    when they intentionally swap the cache out.
    """
    global _singleton
    with _singleton_lock:
        if _singleton is None:
            if base_dir is None:
                # Fallback: a tempdir under /tmp keeps tests isolated when no
                # caller supplied a working_tmp_folder.
                import tempfile

                base_dir = os.path.join(tempfile.gettempdir(), "plex-previews-frame-cache")
            _singleton = FrameCache(
                base_dir=base_dir,
                max_entries=max_entries,
                ttl_seconds=ttl_seconds,
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
