"""Multi-server processing entry point.

Path-centric orchestration that consumes the :class:`ServerRegistry` and
fans out a single FFmpeg pass to every server that owns the canonical
file. Lives alongside the legacy :func:`process_item` entry point in
:mod:`.orchestrator` so existing callers stay working unchanged; new
callers (the universal webhook router and the planned scheduled-scan
refactor) come in through here.

Conceptual flow per call:

1. :func:`ServerRegistry.find_owning_servers` resolves the canonical
   path to every configured server whose enabled libraries cover it.
2. For each owning server, look up the adapter that matches the
   server's ``output.adapter`` setting (Plex bundle, Emby sidecar,
   Jellyfin trickplay tile-grid).
3. Run :func:`generate_images` **once** to produce JPG frames in a
   shared tmp directory keyed by ``hash(canonical_path)``.
4. Hand the resulting :class:`BifBundle` to each adapter's
   :meth:`compute_output_paths` and :meth:`publish`.
5. Trigger each server's refresh endpoint best-effort.

Errors from any single publisher are caught and recorded in the
per-publisher :class:`PublisherResult`; the others continue. The
overall :class:`MultiServerResult` aggregates status so the caller
can decide how to surface partial failures.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

from ..output import BifBundle, EmbyBifAdapter, JellyfinTrickplayAdapter, PlexBundleAdapter
from ..output.base import OutputAdapter
from ..servers.base import LibraryNotYetIndexedError, MediaServer, ServerConfig, ServerType
from .frame_cache import get_frame_cache
from .orchestrator import (
    CodecNotSupportedError,
    _cleanup_temp_directory,
    generate_images,
)

if TYPE_CHECKING:
    from ..config import Config
    from ..servers.registry import ServerRegistry


class PublisherStatus(str, Enum):
    """Per-publisher outcome categories."""

    PUBLISHED = "published"
    SKIPPED_NOT_INDEXED = "skipped_not_indexed"
    SKIPPED_OUTPUT_EXISTS = "skipped_output_exists"
    FAILED = "failed"


class MultiServerStatus(str, Enum):
    """Aggregate outcome for a single canonical-path processing call."""

    PUBLISHED = "published"  # at least one publisher succeeded
    NO_OWNERS = "no_owners"  # no enabled library covers the path
    FAILED = "failed"  # generation or every publisher failed
    NO_FRAMES = "no_frames"  # FFmpeg produced 0 frames (unrecoverable)


@dataclass
class PublisherResult:
    """Outcome of a single (server, adapter) publish attempt."""

    server_id: str
    server_name: str
    adapter_name: str
    status: PublisherStatus
    output_paths: list[Path] = field(default_factory=list)
    message: str = ""


@dataclass
class MultiServerResult:
    """Aggregate outcome of :func:`process_canonical_path`."""

    canonical_path: str
    status: MultiServerStatus
    publishers: list[PublisherResult] = field(default_factory=list)
    frame_count: int = 0
    message: str = ""

    @property
    def published_count(self) -> int:
        return sum(1 for p in self.publishers if p.status is PublisherStatus.PUBLISHED)


def _adapter_for_server(server_config: ServerConfig) -> OutputAdapter | None:
    """Construct the right :class:`OutputAdapter` for a server's settings.

    The adapter type is taken from ``server_config.output.adapter``
    with sensible defaults per server type. Returns ``None`` when the
    requested adapter name is unknown so the caller can skip that
    publisher rather than crashing the whole batch.
    """
    output = server_config.output or {}
    adapter_name = str(output.get("adapter") or "").strip().lower()
    width = int(output.get("width") or 320)
    frame_interval = int(output.get("frame_interval") or 10)

    # Default per server type when adapter name is missing.
    if not adapter_name:
        adapter_name = {
            ServerType.PLEX: "plex_bundle",
            ServerType.EMBY: "emby_sidecar",
            ServerType.JELLYFIN: "jellyfin_trickplay",
        }.get(server_config.type, "")

    if adapter_name == "plex_bundle":
        plex_config_folder = str(output.get("plex_config_folder") or "")
        if not plex_config_folder:
            logger.warning(
                "Plex server {!r} has no plex_config_folder configured; cannot publish",
                server_config.name,
            )
            return None
        return PlexBundleAdapter(
            plex_config_folder=plex_config_folder,
            frame_interval=frame_interval,
        )
    if adapter_name == "emby_sidecar":
        return EmbyBifAdapter(width=width, frame_interval=frame_interval)
    if adapter_name == "jellyfin_trickplay":
        return JellyfinTrickplayAdapter(width=width, frame_interval=frame_interval)

    logger.warning(
        "Unknown output adapter {!r} for server {!r}; skipping",
        adapter_name,
        server_config.name,
    )
    return None


def _tmp_path_for(canonical_path: str, working_tmp_folder: str) -> str:
    """Return a deterministic per-file tmp directory.

    Hashing the canonical path keeps the directory short and unique
    even when path lengths exceed filesystem limits, and lets the
    frame cache (Phase 4) key off the same hash.
    """
    digest = hashlib.sha256(canonical_path.encode("utf-8")).hexdigest()[:16]
    return os.path.join(working_tmp_folder, f"frames-{digest}")


def _resolve_publishers(
    canonical_path: str,
    registry: ServerRegistry,
    *,
    item_id_by_server: dict[str, str] | None = None,
) -> list[tuple[MediaServer, OutputAdapter, str | None]]:
    """Walk owning servers and pair each with the right adapter.

    Returns ``[(server, adapter, item_id)]`` tuples. ``item_id`` is the
    caller-supplied hint when the dispatcher already knows it (e.g. the
    webhook router). Servers without an instantiable adapter are
    skipped with a warning.
    """
    publishers: list[tuple[MediaServer, OutputAdapter, str | None]] = []
    item_id_hints = item_id_by_server or {}

    for match in registry.find_owning_servers(canonical_path):
        server = registry.get(match.server_id)
        if server is None:
            logger.debug(
                "find_owning_servers reported {} but no live client exists; skipping",
                match.server_id,
            )
            continue

        cfg = registry.get_config(match.server_id)
        if cfg is None:
            continue

        adapter = _adapter_for_server(cfg)
        if adapter is None:
            continue

        publishers.append((server, adapter, item_id_hints.get(match.server_id)))

    return publishers


def _resolve_item_id_for(server: MediaServer, canonical_path: str, hint: str | None) -> str | None:
    """Get an item id for a server, preferring the dispatcher's hint.

    When the caller (webhook router, scan loop) already knows the
    server's item id we use it directly. Otherwise we walk the
    server's API once to recover one — Plex, Emby, and Jellyfin all
    support a path-based lookup but the implementations vary; the
    abstract :class:`MediaServer` doesn't expose it. For now, lacking
    a hint means ``None`` is passed to adapters; only Plex and
    Jellyfin's manifest path actually need the id, and both raise an
    intelligible error when it's missing.
    """
    if hint:
        return hint
    # Future: extend MediaServer with a resolve_path_to_item_id() method
    # so we can recover the id from a webhook that only carried the path.
    del server, canonical_path
    return None


def _publish_one(
    server: MediaServer,
    adapter: OutputAdapter,
    bundle: BifBundle,
    item_id: str | None,
    *,
    skip_if_exists: bool,
) -> PublisherResult:
    """Run one publisher; convert exceptions into a :class:`PublisherResult`."""
    try:
        output_paths = adapter.compute_output_paths(bundle, server, item_id)
    except LibraryNotYetIndexedError as exc:
        return PublisherResult(
            server_id=server.id,
            server_name=server.name,
            adapter_name=adapter.name,
            status=PublisherStatus.SKIPPED_NOT_INDEXED,
            message=str(exc),
        )
    except (TypeError, ValueError) as exc:
        return PublisherResult(
            server_id=server.id,
            server_name=server.name,
            adapter_name=adapter.name,
            status=PublisherStatus.FAILED,
            message=f"compute_output_paths: {exc}",
        )
    except Exception as exc:
        logger.exception("Unexpected error in compute_output_paths for {}", server.name)
        return PublisherResult(
            server_id=server.id,
            server_name=server.name,
            adapter_name=adapter.name,
            status=PublisherStatus.FAILED,
            message=f"compute_output_paths: {exc}",
        )

    # If every output path already exists and the user hasn't asked to
    # regenerate, skip the actual publish — but still let the caller know
    # the publisher *would* have published.
    if skip_if_exists and output_paths and all(p.exists() for p in output_paths):
        return PublisherResult(
            server_id=server.id,
            server_name=server.name,
            adapter_name=adapter.name,
            status=PublisherStatus.SKIPPED_OUTPUT_EXISTS,
            output_paths=output_paths,
            message="Output already exists",
        )

    try:
        adapter.publish(bundle, output_paths)
    except Exception as exc:
        logger.exception("Publish failed for {} via {}", server.name, adapter.name)
        return PublisherResult(
            server_id=server.id,
            server_name=server.name,
            adapter_name=adapter.name,
            status=PublisherStatus.FAILED,
            output_paths=output_paths,
            message=f"publish: {exc}",
        )

    # Best-effort refresh; failures are logged but don't fail the publisher.
    try:
        server.trigger_refresh(item_id=item_id, remote_path=bundle.canonical_path)
    except Exception as exc:
        logger.debug("trigger_refresh failed for {}: {}", server.name, exc)

    return PublisherResult(
        server_id=server.id,
        server_name=server.name,
        adapter_name=adapter.name,
        status=PublisherStatus.PUBLISHED,
        output_paths=output_paths,
        message="Published",
    )


def process_canonical_path(
    canonical_path: str,
    registry: ServerRegistry,
    config: Config,
    *,
    item_id_by_server: dict[str, str] | None = None,
    gpu: str | None = None,
    gpu_device_path: str | None = None,
    progress_callback=None,
    ffmpeg_threads_override: int | None = None,
    cancel_check=None,
    worker_name: str = "",
    regenerate: bool = False,
    use_frame_cache: bool = True,
) -> MultiServerResult:
    """Process ``canonical_path`` and publish to every owning server.

    This is the path-centric entry point that consumes
    :class:`ServerRegistry` and the OutputAdapter pipeline. Existing
    Plex-only callers still use :func:`orchestrator.process_item`;
    new path-based callers (webhook router, scheduled scans) come in
    through here.

    Args:
        canonical_path: Absolute local path of the source media file.
        registry: Server registry to resolve owning publishers from.
        config: Existing :class:`Config` for FFmpeg / GPU settings.
        item_id_by_server: Optional ``{server_id: item_id}`` hint —
            avoids a per-server lookup when the dispatcher already
            knows the id (typical for Plex / Emby / Jellyfin webhooks).
        regenerate: When True, publish even if all output paths already
            exist on disk.

    Returns:
        :class:`MultiServerResult` aggregating per-publisher outcomes.
    """
    publishers = _resolve_publishers(
        canonical_path,
        registry,
        item_id_by_server=item_id_by_server,
    )
    if not publishers:
        logger.info("No configured server owns {}; skipping", canonical_path)
        return MultiServerResult(
            canonical_path=canonical_path,
            status=MultiServerStatus.NO_OWNERS,
            message="No enabled library covers this path on any configured server",
        )

    if not os.path.isfile(canonical_path):
        return MultiServerResult(
            canonical_path=canonical_path,
            status=MultiServerStatus.FAILED,
            message=f"Source file not found: {canonical_path}",
        )

    # Frame cache: when enabled, the second+ webhook for the same file
    # within the cache TTL skips FFmpeg entirely. Disabled callers
    # (regenerate=True, or callers that explicitly opt out) write into
    # an ad-hoc tmp dir that's cleaned up at the end.
    cache = get_frame_cache(base_dir=os.path.join(config.working_tmp_folder, "frame_cache"))
    cache_hit: bool = False
    cleanup_path: str | None = None

    if use_frame_cache and not regenerate:
        cached = cache.get(canonical_path)
        if cached is not None:
            tmp_path = str(cached.frame_dir)
            frame_count = cached.frame_count
            cache_hit = True
            logger.debug(
                "Frame cache hit for {} ({} frames)",
                canonical_path,
                frame_count,
            )

    if not cache_hit:
        # Generate into the cache slot (if cache enabled) or an ad-hoc tmp.
        if use_frame_cache:
            tmp_path = str(cache.frame_dir_for(canonical_path))
        else:
            tmp_path = _tmp_path_for(canonical_path, config.working_tmp_folder)
            cleanup_path = tmp_path  # only ad-hoc tmps get auto-cleaned
        os.makedirs(tmp_path, exist_ok=True)

    try:
        if not cache_hit:
            try:
                gen_result = generate_images(
                    canonical_path,
                    tmp_path,
                    gpu,
                    gpu_device_path,
                    config,
                    progress_callback,
                    ffmpeg_threads_override=ffmpeg_threads_override,
                    cancel_check=cancel_check,
                )
            except CodecNotSupportedError:
                # Re-raised so callers can fall back to CPU; not a publisher failure.
                raise
            except Exception as exc:
                logger.exception("Frame generation failed for {}", canonical_path)
                return MultiServerResult(
                    canonical_path=canonical_path,
                    status=MultiServerStatus.FAILED,
                    message=f"Frame generation failed: {exc}",
                )

            frame_count = 0
            if isinstance(gen_result, tuple) and len(gen_result) >= 2:
                frame_count = int(gen_result[1])
            if frame_count <= 0:
                # Re-derive from disk in case the helper returned a non-tuple.
                try:
                    frame_count = sum(1 for f in os.listdir(tmp_path) if f.lower().endswith(".jpg"))
                except OSError:
                    frame_count = 0
        else:
            gen_result = None

        if frame_count == 0:
            return MultiServerResult(
                canonical_path=canonical_path,
                status=MultiServerStatus.NO_FRAMES,
                message="FFmpeg produced 0 frames",
            )

        # Store in cache only on a fresh generation; cache hits already
        # have an entry. Skip caching when use_frame_cache=False so
        # regenerate flows don't re-populate stale slots.
        if not cache_hit and use_frame_cache:
            cache.put(canonical_path, frame_dir=Path(tmp_path), frame_count=frame_count)

        bundle = BifBundle(
            canonical_path=canonical_path,
            frame_dir=Path(tmp_path),
            bif_path=None,
            frame_interval=int(getattr(config, "plex_bif_frame_interval", 10) or 10),
            width=int(gen_result[3]) if isinstance(gen_result, tuple) and len(gen_result) > 3 else 320,
            height=180,
            frame_count=frame_count,
        )

        results: list[PublisherResult] = []
        for server, adapter, item_id_hint in publishers:
            item_id = _resolve_item_id_for(server, canonical_path, item_id_hint)
            results.append(
                _publish_one(
                    server,
                    adapter,
                    bundle,
                    item_id,
                    skip_if_exists=not regenerate,
                )
            )

        any_published = any(r.status is PublisherStatus.PUBLISHED for r in results)
        all_failed = all(r.status is PublisherStatus.FAILED for r in results)

        if any_published:
            status = MultiServerStatus.PUBLISHED
        elif all_failed:
            status = MultiServerStatus.FAILED
        else:
            # Mixture of skipped (output exists / not yet indexed) — treat
            # as published-equivalent for the dispatcher; the per-publisher
            # statuses preserve the detail.
            status = MultiServerStatus.PUBLISHED

        del worker_name  # accepted for parity with process_item; not consumed yet
        return MultiServerResult(
            canonical_path=canonical_path,
            status=status,
            publishers=results,
            frame_count=frame_count,
            message=f"{sum(1 for r in results if r.status is PublisherStatus.PUBLISHED)} of {len(results)} publisher(s) succeeded",
        )
    finally:
        # Only clean up tmp dirs that are *not* in the cache. Cache
        # entries persist for TTL so subsequent webhooks hit them.
        if cleanup_path is not None:
            _cleanup_temp_directory(cleanup_path)
